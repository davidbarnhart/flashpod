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
  * errors: the same `code`, `details` and message (except a message
    quoting an OS error, "[Errno ...]");
  * writes (create_blank, flash, flash_firmware, format_data): the same
    result or error, and the same bytes in the image -- every region either
    image holds data in (sparse-aware, for the 140 GiB card);
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

    def call(self, what, py_fn, *rust_args, rename=None):
        """Run py_fn() and fpcore *rust_args; compare result or error.
        ``rename=(rust_path, python_path)``: the two sides worked on their
        own copies, so the Rust copy's path is read as the Python one's."""
        try:
            py = ("ok", py_fn().to_dict())
        except card.FlashpodError as e:
            py = ("err", e.to_dict())
        rc, rs = self.rust(*rust_args)
        if rename:
            rs = json.loads(json.dumps(rs).replace(json.dumps(rename[0])[1:-1],
                                                   json.dumps(rename[1])[1:-1]))
        rs = ("ok", rs) if rc == 0 else ("err", rs)
        if py[0] != rs[0]:
            self.fails.append("%s: Python %s %r, Rust %s %r"
                              % (what, py[0], py[1], rs[0], rs[1]))
        elif py[0] == "err":
            # the whole error: code, message, details -- except where Python
            # quotes an OS error ("[Errno 2] ..."), which no port can match
            keys = ["code", "details"]
            if "[Errno" not in py[1]["message"]:
                keys.append("message")
            for k in keys:
                if py[1][k] != rs[1][k]:
                    self.fails.append("%s: error %s Python %r, Rust %r"
                                      % (what, k, py[1][k], rs[1][k]))
            self.checked += len(keys)
        else:
            self.compare(what, py[1], rs[1])
        return py, rs


# -- M2: the writers, compared byte for byte ------------------------------
SEEK_DATA, SEEK_HOLE = getattr(os, "SEEK_DATA", 3), getattr(os, "SEEK_HOLE", 4)


def _extents(path):
    """(start, end) of the regions holding data (sparse-aware)."""
    out, size = [], os.path.getsize(path)
    with open(path, "rb") as f:
        fd, off = f.fileno(), 0
        while off < size:
            try:
                start = os.lseek(fd, off, SEEK_DATA)
            except OSError:
                break
            end = os.lseek(fd, start, SEEK_HOLE)
            out.append((start, end))
            off = end
    return out


def same_image(a, b):
    """None if images a and b hold the same bytes, else where they differ."""
    if os.path.getsize(a) != os.path.getsize(b):
        return "sizes %d and %d" % (os.path.getsize(a), os.path.getsize(b))
    spans = sorted(_extents(a) + _extents(b))
    with open(a, "rb") as fa, open(b, "rb") as fb:
        for start, end in spans:
            off = start
            while off < end:
                n = min(1 << 22, end - off)
                fa.seek(off)
                fb.seek(off)
                x, y = fa.read(n), fb.read(n)
                if x != y:
                    i = next(k for k in range(len(x)) if x[k] != y[k])
                    return "first difference at byte %d" % (off + i)
                off += n
    return None


class Plain(object):
    def __init__(self, v):
        self.v = v

    def to_dict(self):
        return self.v


def writers(par, tmp, images, fw_cache):
    import gzip
    import zipfile
    print("writers: min_card_bytes, create_blank, flash, flash_firmware, "
          "format_data")
    w = os.path.join(tmp, "w")
    os.makedirs(w)
    rc, rs = par.rust("min-card-bytes")
    if rs != card.min_card_bytes():
        par.fails.append("min_card_bytes: Python %d, Rust %r"
                         % (card.min_card_bytes(), rs))
    else:
        par.checked += 1
    n = [0]

    def pair(start):
        """Two copies of a starting image (or of nothing)."""
        n[0] += 1
        a = os.path.join(w, "%d-py.img" % n[0])
        b = os.path.join(w, "%d-rs.img" % n[0])
        if start is not None:
            for d in (a, b):
                subprocess.run(["cp", "--sparse=always", start, d], check=True)
        return a, b

    def case(what, start, py_fn, rust_args):
        a, b = pair(start)
        before = len(par.fails)
        par.call(what, lambda: py_fn(a), *[x if x != "@" else b
                                           for x in rust_args], rename=(b, a))
        if os.path.exists(a) or os.path.exists(b):
            if os.path.exists(a) != os.path.exists(b):
                par.fails.append("%s: the image exists on one side only" % what)
            else:
                diff = same_image(a, b)
                if diff:
                    par.fails.append("%s: images differ (%s)" % (what, diff))
                else:
                    par.checked += 1
        print("  %s %s" % ("ok  " if len(par.fails) == before else "FAIL", what))
        for p in (a, b):
            if os.path.exists(p):
                os.unlink(p)

    blank = dict(images)["blank"]
    music = dict(images)["init + music"]
    foreign = dict(images)["foreign layout"]
    mn = card.min_card_bytes()
    for size, what in ((96 << 20, "96 MiB"), ((96 << 20) + 100, "96 MiB + 100"),
                       (mn, "the minimum"), (mn - 512, "one sector short")):
        case("create_blank %s" % what, None,
             lambda a, size=size: card.create_blank(a, size),
             ["create-blank", "@", str(size)])
    case("create_blank on an existing file", blank,
         lambda a: card.create_blank(a, 96 << 20),
         ["create-blank", "@", str(96 << 20)])

    # firmware in every container, good and bad
    raw = synth.firmware_bytes()
    fws = {"raw": raw}
    p = os.path.join(w, "fw.bin.gz")
    with gzip.open(p, "wb") as f:
        f.write(raw)
    fws["gz"] = p
    p = os.path.join(w, "fw.ipsw")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Firmware-1.1.5", raw)
        z.writestr("manifest.plist", "<plist/>")
    fws["ipsw"] = p
    p = os.path.join(w, "nofw.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "no firmware here")
    fws["zip without Firmware"] = p
    fws["bad checksum"] = synth.firmware_bytes(bad_checksum=True)
    fws["not firmware"] = b"hello" * 1000
    fws["over 32 MiB"] = synth.firmware_bytes(
        payload=synth.osos_payload(length=33 << 20))
    for name in list(fws):
        if isinstance(fws[name], bytes):
            p = os.path.join(w, "fw-%s.bin" % name.replace(" ", "_"))
            open(p, "wb").write(fws[name])
            fws[name] = p
    real = sorted(glob.glob(os.path.join(fw_cache, "*.bin.gz")))
    picks = [x for x in real if any(k in x for k in (
        "flashpod_1.1.6", "iPod_1.1.5", "iPod_1.0.0", "iPod_2.2.3",
        "iPod_4.3.1.1", "iPod_5.1.2.1"))]
    for x in picks:
        fws[os.path.basename(x)] = x
    ipsw16 = os.path.expanduser(os.environ.get("FLASHPOD_PARITY_IPSW", ""))
    if ipsw16 and os.path.exists(ipsw16):
        fws["real .ipsw"] = ipsw16
    for name, path in fws.items():
        case("flash, firmware %s" % name, blank,
             lambda a, path=path: card.flash(a, path), ["flash", "@", path])

    fw = fws["raw"]
    case("flash --no-format", blank, lambda a: card.flash(a, fw, format=False),
         ["flash", "@", fw, "--no-format"])
    case("flash --max-data-gb 0.05", blank,
         lambda a: card.flash(a, fw, max_data_gb=0.05),
         ["flash", "@", fw, "--max-data-gb", "0.05"])
    case("flash over a card with music", music, lambda a: card.flash(a, fw),
         ["flash", "@", fw])
    small = os.path.join(w, "small.img")
    with open(small, "wb") as f:
        f.truncate(10 << 20)
    case("flash a 10 MiB image (too small)", small,
         lambda a: card.flash(a, fw), ["flash", "@", fw])
    tiny = os.path.join(w, "tiny-data.img")    # a data partition, no FAT32 room
    with open(tiny, "wb") as f:
        f.truncate(mn - 64 * 512)
    case("flash just under the minimum", tiny, lambda a: card.flash(a, fw),
         ["flash", "@", fw])
    case("flash --no-format just under the minimum", tiny,
         lambda a: card.flash(a, fw, format=False),
         ["flash", "@", fw, "--no-format"])
    big = os.path.join(w, "big.img")
    with open(big, "wb") as f:
        f.truncate(140 << 30)
    case("flash --lba48, 140 GiB", big, lambda a: card.flash(a, fw, lba48=True),
         ["flash", "@", fw, "--lba48"])
    case("flash, 140 GiB (LBA28 cap)", big, lambda a: card.flash(a, fw),
         ["flash", "@", fw])
    case("flash --arm, no disk-mode program", blank,
         lambda a: card.flash(a, fw, arm=True), ["flash", "@", fw, "--arm"])
    if "flashpod_1.1.6_2026_10_16.bin.gz" in fws:
        f16 = fws["flashpod_1.1.6_2026_10_16.bin.gz"]
        case("flash --arm, flashpod 1.6", blank,
             lambda a: card.flash(a, f16, arm=True), ["flash", "@", f16, "--arm"])
        case("flash_firmware 1.6 onto a card with music", music,
             lambda a: card.flash_firmware(a, f16), ["flash-firmware", "@", f16])
    case("flash_firmware onto a card with music", music,
         lambda a: card.flash_firmware(a, fws["gz"]),
         ["flash-firmware", "@", fws["gz"]])
    case("flash_firmware onto a foreign layout", foreign,
         lambda a: card.flash_firmware(a, fw), ["flash-firmware", "@", fw])
    case("flash_firmware onto a blank card", blank,
         lambda a: card.flash_firmware(a, fw), ["flash-firmware", "@", fw])
    case("flash_firmware, bad firmware", music,
         lambda a: card.flash_firmware(a, fws["bad checksum"]),
         ["flash-firmware", "@", fws["bad checksum"]])
    case("format_data on a card with music", music, lambda a: card.format_data(a),
         ["format-data", "@"])
    case("format_data on a foreign layout", foreign,
         lambda a: card.format_data(a), ["format-data", "@"])
    case("format_data on a blank card", blank, lambda a: card.format_data(a),
         ["format-data", "@"])
    narrow = os.path.join(w, "narrow.img")      # FAT partition too small
    mbr = bytearray(512)
    struct.pack_into("<BBBBBBBBII", mbr, 446 + 16, 0, 0, 0, 0, 0x0b, 0, 0, 0,
                     65599, 50000)
    mbr[510:512] = b"\x55\xaa"
    with open(narrow, "wb") as f:
        f.write(mbr)
        f.truncate(96 << 20)
    case("format_data, partition too small for FAT32", narrow,
         lambda a: card.format_data(a), ["format-data", "@"])


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

    writers(par, tmp, images, opts.firmware_cache)

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
