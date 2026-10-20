#!/usr/bin/env python3
"""Prove the Rust port (rust/flashpod-core) matches the Python library.

    python3 scripts/rust_parity.py [--firmware-cache DIR] [--keep]

The Python library is the reference (emupod's docs/FLASHPOD_RUST.md, section
4). This builds the Rust harness binary (`fpcore`), makes a corpus of card
images with the Python library -- synthetic ones always, and one per cached
catalog firmware when the cache is there -- runs the same call through both
implementations, and compares:

  * results: every field the Rust side produces must equal the Python
    `to_dict()` field. Fields Rust doesn't produce yet are listed as PENDING,
    never counted as passing; a field Rust produces that Python doesn't is a
    failure;
  * errors: the same stable `code` and `details.reason` (messages may
    differ where Python quotes an OS error);
  * the id source: Python's flashpod.entropy.SplitMix64 against Rust's, raw
    outputs and the getrandbits / randrange / choice calls flashpod makes.

A negative check runs first: a Rust result with one field altered must be
reported as a mismatch, so a comparison that can't fail is caught. Exit 1 on
any mismatch. Synthetic inputs only (no real music, no David's cards).
"""
import argparse
import copy
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUST = os.path.join(ROOT, "rust")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, entropy  # noqa: E402

SEED, NOW = 20261020, 1760000000          # the fixed id seed and clock


def cargo():
    for c in (shutil.which("cargo"),
              os.path.expanduser("~/.cargo/bin/cargo")):
        if c and os.path.exists(c):
            return c
    sys.exit("rust_parity: cargo not found (install Rust: https://rustup.rs)")


def build():
    subprocess.run([cargo(), "build", "--release", "-q"], cwd=RUST,
                   check=True)
    return os.path.join(RUST, "target", "release", "fpcore")


class Parity(object):
    def __init__(self, fpcore):
        self.fpcore = fpcore
        self.fails = []
        self.pending = {}
        self.checked = 0

    def rust(self, *args):
        p = subprocess.run([self.fpcore] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
        if p.returncode not in (0, 1):
            raise RuntimeError("fpcore %s: rc %d: %s"
                               % (" ".join(args), p.returncode,
                                  p.stderr.decode()))
        return p.returncode, json.loads(p.stdout.decode())

    def compare(self, what, py, rs, record=True):
        """Mismatched field names (also recorded as failures)."""
        bad = []
        for k, v in rs.items():
            if k not in py:
                bad.append("%s: Rust has %r, Python doesn't" % (k, v))
            elif v != py[k]:
                bad.append("%s: Python %r, Rust %r" % (k, py[k], v))
        if record:
            for k in py:
                if k not in rs:
                    self.pending.setdefault(what.split(" ")[0], set()).add(k)
            self.checked += len(rs) - len(bad)
            for b in bad:
                self.fails.append("%s: %s" % (what, b))
        return bad

    def call(self, what, py_fn, *rust_args):
        """Run py_fn() and fpcore *rust_args; compare result or error."""
        try:
            py = ("ok", py_fn().to_dict())
        except card.FlashpodError as e:
            py = ("err", e.to_dict())
        rc, rs = self.rust(*rust_args)
        rs = ("ok", rs) if rc == 0 else ("err", rs)
        if py[0] != rs[0]:
            self.fails.append("%s: Python %s %r, Rust %s %r"
                              % (what, py[0], py[1], rs[0], rs[1]))
        elif py[0] == "err":
            for k in ("code",):
                if py[1][k] != rs[1][k]:
                    self.fails.append("%s: error %s Python %r, Rust %r"
                                      % (what, k, py[1][k], rs[1][k]))
            pr = py[1]["details"].get("reason")
            rr = rs[1]["details"].get("reason")
            if pr != rr:
                self.fails.append("%s: error reason Python %r, Rust %r"
                                  % (what, pr, rr))
            self.checked += 2
        else:
            self.compare(what, py[1], rs[1])
        return py, rs


class TracksResult(object):
    """card.tracks returns a plain dict; give it the to_dict() the
    comparison expects."""

    def __init__(self, d):
        self.d = d

    def to_dict(self):
        return self.d


def corpus(tmp, fw_cache):
    """(name, path) of card images, all made with the Python library in
    deterministic mode (same bytes every run)."""
    out = []
    fw = synth.write_firmware(os.path.join(tmp, "fw.bin"))
    music = synth.make_music(os.path.join(tmp, "music"))

    def img(name):
        return os.path.join(tmp, name)

    with entropy.deterministic(SEED, NOW):
        p = img("empty.img")
        open(p, "wb").close()
        out.append(("empty file", p))
        p = img("tiny.img")
        open(p, "wb").write(b"\0" * 100)
        out.append(("under a sector", p))
        p = img("blank.img")
        card.create_blank(p, 96 << 20)
        out.append(("blank", p))
        p = img("flashed.img")
        card.create_blank(p, 96 << 20)
        card.flash(p, fw)
        out.append(("flashed", p))
        p = img("music.img")
        shutil.copy(img("flashed.img"), p)
        card.init(p)
        card.add(p, [music])
        out.append(("init + music", p))
        p = img("short.img")                 # truncated: data runs past the end
        shutil.copy(img("flashed.img"), p)
        with open(p, "r+b") as f:
            f.truncate(80 << 20)
        out.append(("truncated", p))
        p = img("tail.img")                  # tail reserve eaten into
        shutil.copy(img("flashed.img"), p)
        with open(p, "r+b") as f:
            f.truncate((96 << 20) - 1024 * 512)
        out.append(("short tail reserve", p))
        p = img("foreign.img")               # someone else's layout
        mbr = bytearray(512)
        struct.pack_into("<BBBBBBBBII", mbr, 446, 0, 0, 0, 0, 0x0c, 0, 0, 0,
                         2048, 100000)
        mbr[510:512] = b"\x55\xaa"
        with open(p, "wb") as f:
            f.write(mbr)
            f.truncate(96 << 20)
        out.append(("foreign layout", p))
        p = img("big.img")                   # data partition past 128 GiB
        card.create_blank(p, 130 << 30)
        card.flash(p, fw, lba48=True)
        out.append(("130 GiB, --lba48", p))
        p = img("inited.img")                # an empty library
        shutil.copy(img("flashed.img"), p)
        card.init(p)
        out.append(("init, no music", p))
        p = img("unicode.img")               # non-ASCII tags and names
        shutil.copy(img("flashed.img"), p)
        card.init(p, name="Bibliothèque ß 音楽")
        card.add(p, [unicode_music(os.path.join(tmp, "umusic"))])
        out.append(("non-ASCII tags", p))
        p = img("corrupt.img")               # garbage after the DB's magic
        shutil.copy(img("music.img"), p)
        corrupt_db(p)
        out.append(("damaged iTunesDB", p))
        p = img("nodb.img")                  # iPod_Control, no iTunesDB
        shutil.copy(img("music.img"), p)
        with card.open_target(p, write=True) as c:
            c.target().fs.remove(card.DB_PATH)
        out.append(("no iTunesDB, music files", p))
        p = img("orphan.img")                # a file no track points at,
        shutil.copy(img("music.img"), p)     # a track whose file is gone
        loc = card.tracks(p)["tracks"][0]["location"]
        with card.open_target(p, write=True) as c:
            fs = c.target().fs
            fs.write_file("iPod_Control/Music/F00/stray.mp3", b"\xff" * 3000)
            fs.remove(loc.lstrip(":").replace(":", "/"))
        out.append(("orphan + missing", p))
        p = img("nofree.img")                # FSInfo without a free count
        shutil.copy(img("music.img"), p)
        no_free_count(p)
        out.append(("no FSInfo free count", p))
        for path in sorted(glob.glob(os.path.join(fw_cache, "*.bin.gz")))[:30]:
            name = os.path.basename(path)
            p = img("fw-" + name + ".img")
            card.create_blank(p, 96 << 20)
            try:
                card.flash(p, path)
            except card.FlashpodError:
                os.unlink(p)
                continue
            out.append(("firmware " + name, p))
            if name.startswith("flashpod_1.1.6"):
                q = img("fw16-music.img")    # the library-fix ceiling
                shutil.copy(p, q)
                card.init(q)
                card.add(q, [music])
                out.append(("flashpod 1.6 + music", q))
                q = img("fw16-armed.img")
                card.create_blank(q, 96 << 20)
                card.flash(q, path, arm=True)
                out.append(("flashpod 1.6, armed", q))
    return out


def unicode_music(root):
    """A few synthetic MP3s with non-ASCII tags (ffmpeg)."""
    os.makedirs(root)
    songs = [("Café Tacvba", "Ré", "Él"), ("Straße", "Großes Album", "Fuß"),
             ("坂本龍一", "音楽図鑑", "Tibetan Dance"), ("İstanbul", "Ölüm", "Şarkı")]
    for i, (artist, album, title) in enumerate(songs):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=%d:duration=1" % (300 + 50 * i),
                        "-ac", "1", "-ar", "22050", "-b:a", "32k",
                        "-metadata", "artist=" + artist,
                        "-metadata", "album=" + album,
                        "-metadata", "title=" + title,
                        os.path.join(root, "u%d.mp3" % i)], check=True)
    return root


def corrupt_db(path):
    """Overwrite the bytes after every "mhbd" magic in the data area."""
    with open(path, "r+b") as f:
        data = f.read()
        i = data.find(b"mhbd", 65599 * 512)
        while i >= 0:
            f.seek(i + 4)
            f.write(b"\xff\xfe" * 40)
            i = data.find(b"mhbd", i + 4)


def no_free_count(path):
    """Set the FSInfo free-cluster count to 0xFFFFFFFF ("unknown")."""
    with open(path, "r+b") as f:
        mbr = f.read(512)
        start = struct.unpack_from("<I", mbr, 462 + 8)[0]
        f.seek(start * 512)
        boot = f.read(512)
        fsinfo = struct.unpack_from("<H", boot, 48)[0]
        f.seek((start + fsinfo) * 512 + 488)
        f.write(b"\xff\xff\xff\xff")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--firmware-cache", default=os.path.expanduser(
        "~/.cache/flashpod/firmware"))
    ap.add_argument("--keep", action="store_true",
                    help="keep the corpus directory (it is kept anyway on a "
                         "mismatch)")
    opts = ap.parse_args()
    par = Parity(build())
    tmp = tempfile.mkdtemp(prefix="fp-parity-")

    print("negative check: an altered field is reported")
    images = corpus(tmp, opts.firmware_cache)
    path = dict(images)["flashed"]
    py = card.inspect(path).to_dict()
    _, rs = par.rust("inspect", path)
    rs_bad = copy.deepcopy(rs)
    rs_bad["layout_ok"] = not rs_bad["layout_ok"]
    if not par.compare("negative", py, rs_bad, record=False):
        par.fails.append("negative check: an altered field was NOT reported")
    else:
        print("  ok")

    print("entropy: SplitMix64")
    for seed in (0, 1, SEED, (1 << 64) - 1):
        r = entropy.SplitMix64(seed)
        py = [str(r.next()) for _ in range(64)]
        _, rs = par.rust("splitmix", str(seed), "64")
        r = entropy.SplitMix64(seed)
        pyc = [[str(r.getrandbits(64)), str(r.randrange(10 ** 6)),
                str(r.choice(range(7)))] for _ in range(32)]
        _, rsc = par.rust("entropy", str(seed), "32")
        for what, a, b in (("outputs", py, rs), ("calls", pyc, rsc)):
            if a != b:
                par.fails.append("entropy seed %d %s differ" % (seed, what))
            else:
                par.checked += 1
    print("  %s" % ("ok" if not any(f.startswith("entropy") for f in par.fails)
                    else "FAIL"))

    print("inspect: %d cards, plus errors" % len(images))
    for name, path in images:
        before = len(par.fails)
        par.call("inspect %s" % name, lambda: card.inspect(path),
                 "inspect", path)
        print("  %s %s" % ("ok  " if len(par.fails) == before else "FAIL",
                           name))
    print("inspect with FLASHPOD_DB_LIMIT set")
    for value in ("0", "1000", "junk"):
        os.environ["FLASHPOD_DB_LIMIT"] = value
        for name, path in images:
            if "music" in name or "1.6" in name:
                before = len(par.fails)
                par.call("inspect %s (limit %s)" % (name, value),
                         lambda: card.inspect(path), "inspect", path)
                print("  %s %s, FLASHPOD_DB_LIMIT=%s"
                      % ("ok  " if len(par.fails) == before else "FAIL",
                         name, value))
    del os.environ["FLASHPOD_DB_LIMIT"]

    print("tracks")
    for name, path in images:
        if os.path.getsize(path) < 512 or name in ("foreign layout",):
            continue                         # Python raises a bare OSError
        before = len(par.fails)
        par.call("tracks %s" % name, lambda: TracksResult(card.tracks(path)),
                 "tracks", path)
        print("  %s %s" % ("ok  " if len(par.fails) == before else "FAIL",
                           name))

    for name, path in (("a missing path", os.path.join(tmp, "nope.img")),
                       ("a directory", tmp)):
        before = len(par.fails)
        par.call("inspect %s" % name, lambda: card.inspect(path),
                 "inspect", path)
        print("  %s %s (error)" % ("ok  " if len(par.fails) == before
                                   else "FAIL", name))

    print()
    for call, keys in sorted(par.pending.items()):
        print("PENDING %s: %s" % (call, ", ".join(sorted(keys))))
    if opts.keep or par.fails:
        print("corpus kept in %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    if par.fails:
        print("\n%d MISMATCHES" % len(par.fails))
        for f in par.fails:
            print("  " + f)
        return 1
    print("PARITY: %d values identical, nothing differs" % par.checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
