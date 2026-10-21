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


def build_wasm(tmp):
    """The wasm build of flashpod_core::api (rust/wasm-check) and a wrapper
    that runs it under Node with fpcore run's arguments; None without node."""
    import shutil as _sh
    node = _sh.which("node")
    if not node:
        return None
    subprocess.run([cargo(), "build", "--release", "-q", "-p", "wasm-check",
                    "--target", "wasm32-unknown-unknown"], cwd=RUST, check=True)
    wasm = os.path.join(RUST, "target", "wasm32-unknown-unknown", "release",
                        "wasm_check.wasm")
    runner = os.path.join(tmp, "fpcore-wasm")
    with open(runner, "w") as f:
        f.write('#!/bin/sh\nshift\nexec "%s" "%s" "%s" "$@"\n'
                % (node, os.path.join(HERE, "wasm_check.mjs"), wasm))
    os.chmod(runner, 0o755)
    return runner, os.path.getsize(wasm)


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
        if not (isinstance(py, dict) and isinstance(rs, dict)):
            bad = [] if py == rs else ["Python %r, Rust %r" % (py, rs)]
            if record:
                self.checked += 0 if bad else 1
                self.fails.extend("%s: %s" % (what, b) for b in bad)
            return bad
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

    def call(self, what, py_fn, *rust_args, rename=None, ignore=(), norm=None):
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
        for k in ignore:                     # timings: never equal
            if py[0] == "ok" and isinstance(py[1], dict):
                py[1].pop(k, None)
        rs = ("ok", rs) if rc == 0 else ("err", rs)
        if norm and py[0] == rs[0] == "ok":
            py, rs = ("ok", norm(py[1])), ("ok", norm(rs[1]))
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


# -- M3: init, add, remove, rebuild, plan_add --------------------------------
SEED3, NOW3 = 777, 1760100000
# M3 ran on mutagen's tags handed over as JSON; since M4 the Rust side reads
# the files itself. --python-tags brings the old way back.
PYTHON_TAGS = False


def _audio_fields(audio, size):
    from flashpod.tracks import easy_tags, first
    tags = easy_tags(audio)
    info = audio.info
    return {"audio": {
        "title": first(tags, "title"), "artist": first(tags, "artist"),
        "album": first(tags, "album"), "genre": first(tags, "genre"),
        "composer": first(tags, "composer"),
        "tracknumber": first(tags, "tracknumber"), "date": first(tags, "date"),
        "length": info.length, "bitrate": getattr(info, "bitrate", 0) or 0,
        "sample_rate": getattr(info, "sample_rate", 0) or 0, "size": size}}


def source_tags(files):
    """What make_track reads from each file, for the Rust side."""
    from flashpod import tracks
    out = {}
    for p in tracks.expand(files, report=lambda m: None):
        try:
            audio = tracks.read_audio(p)
        except Exception as exc:                   # noqa: BLE001 (as make_track)
            out[p] = {"error": str(exc)}
            continue
        out[p] = {"unrecognized": True} if audio is None \
            else _audio_fields(audio, os.path.getsize(p))
    return out


def card_tags(img):
    """What a rebuild reads from each music file on the card."""
    from flashpod import tracks
    out = {}
    with card.open_target(img) as c:
        tgt = c.target()
        for relpath, _loc in tgt.iter_music_files():
            try:
                fh = tgt.fs.open_file(relpath)
                audio = tracks.read_audio_stream(fh, os.path.splitext(relpath)[1])
            except (OSError, ValueError) as exc:
                out[relpath] = {"error": str(exc)}
                continue
            out[relpath] = {"unrecognized": True} if audio is None \
                else _audio_fields(audio, fh.size)
    return out


class _Timeout(Exception):
    pass


def _py_tags(read):
    """read() -> (audio, size) through mutagen, as make_track sees it: the
    per-file form, with a time limit (mutagen loops forever on one MP4
    shape)."""
    import signal

    def alarm(*_):
        raise _Timeout()
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(5)
    try:
        audio, size = read()
        return {"unrecognized": True} if audio is None \
            else _audio_fields(audio, size)
    except _Timeout:
        return {"timeout": True}
    except Exception as exc:                       # noqa: BLE001 (as make_track)
        return {"error": str(exc)}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _too_big(fields):
    a = fields.get("audio") or {}
    return any(isinstance(a.get(k), int) and abs(a[k]) >= 1 << 63
               for k in ("bitrate", "sample_rate"))


def compare_tags(par, what, labels, py, rs, named):
    """Per file: the same outcome (tags / unreadable / not audio), and for
    tags every field equal. An unreadable file's message is mutagen's own
    text on one side and the port's on the other: not compared (named)."""
    bad = 0
    for path, label in labels:
        a, b = py[path], rs[path]
        if "timeout" in a and "error" in b:
            named["mutagen never returns (Rust: unreadable)"] = \
                named.get("mutagen never returns (Rust: unreadable)", 0) + 1
            continue
        if _too_big(a) and "error" in b:
            named["a number past 2**63 (Python's DB write then fails)"] = \
                named.get("a number past 2**63 (Python's DB write then fails)", 0) + 1
            continue
        if label.startswith("Ogg") and "error" in b:
            named["an Ogg file (not read by the port)"] = \
                named.get("an Ogg file (not read by the port)", 0) + 1
            continue
        ka, kb = sorted(a), sorted(b)
        if ka != kb:
            par.fails.append("%s %s: Python %r, Rust %r" % (what, label, a, b))
            bad += 1
            continue
        if "audio" in a:
            if par.compare("%s %s" % (what, label), a["audio"], b["audio"]):
                bad += 1
        else:
            par.checked += 1
            if "error" in a:
                named["unreadable: message not compared"] = \
                    named.get("unreadable: message not compared", 0) + 1
    return bad


def tag_files(par, tmp, n_damaged):
    """M4: every corpus file and n damaged copies through mutagen (via
    tracks.read_audio) and through the Rust reader; the same files on a card,
    read lazily off the FAT by both (the rebuild path)."""
    import mutagen
    import tag_corpus
    from flashpod import tracks
    named = {}
    files = tag_corpus.build(os.path.join(tmp, "tags"))
    made = open(os.path.join(RUST, "flashpod-core", "src", "tags",
                             "tables.rs")).readline()
    print("tags: %d synthetic files, mutagen %s%s"
          % (len(files), mutagen.version_string,
             "" if "mutagen %s," % mutagen.version_string in made else
             " (the port follows the mutagen in tables.rs: %s)"
             % made.split("mutagen ")[1].split(",")[0]))

    def run(what, items):
        py = {p: _py_tags(lambda p=p: (tracks.read_audio(p), os.path.getsize(p)))
              for _, p in items}
        rs = {}
        for i in range(0, len(items), 400):
            _, part = par.rust("tags", *[p for _, p in items[i:i + 400]])
            rs.update(part)
        labels = [(p, lab) for lab, p in items]
        bad = compare_tags(par, what, labels, py, rs, named)
        kinds = {}
        for v in py.values():
            k = sorted(v)[0]
            kinds[k] = kinds.get(k, 0) + 1
        print("  %s %d files, %d differ (Python: %s)"
              % ("ok  " if not bad else "FAIL", len(items), bad,
                 ", ".join("%d %s" % (n, k) for k, n in sorted(kinds.items()))))
    run("tags", files)

    print("tags on a card (read off the FAT, as a rebuild does)")
    img = os.path.join(tmp, "tags.img")
    card.create_blank(img, 128 << 20)
    card.flash(img, synth.write_firmware(os.path.join(tmp, "tagfw.bin")))
    card.init(img)
    names = {}
    with card.open_target(img, write=True) as c:
        fs = c.target().fs
        for i, (label, p) in enumerate(files):
            rel = "iPod_Control/Music/F%02d/t%03d%s" % (
                i % 50, i, os.path.splitext(p)[1])
            fs.write_file(rel, open(p, "rb").read())
            names[rel] = label
    py = {}
    with card.open_target(img) as c:
        tgt = c.target()
        for rel, _loc in tgt.iter_music_files():
            fh = tgt.fs.open_file(rel)
            py[rel] = _py_tags(lambda fh=fh, rel=rel: (
                tracks.read_audio_stream(fh, os.path.splitext(rel)[1]), fh.size))
    _, rs = par.rust("card-tags", img)
    labels = [(rel, names[rel]) for rel in sorted(py)]
    if sorted(py) != sorted(rs):
        par.fails.append("card-tags: Python files %d, Rust files %d"
                         % (len(py), len(rs)))
    bad = compare_tags(par, "card-tags", labels, py, rs, named)
    print("  %s %d files, %d differ" % ("ok  " if not bad else "FAIL",
                                        len(labels), bad))
    os.unlink(img)

    print("tags: %d damaged copies (seeded)" % n_damaged)
    run("damaged", tag_corpus.damaged(files, os.path.join(tmp, "dmg"),
                                      n_damaged))
    for k, v in sorted(named.items()):
        print("  NAMED %s: %d" % (k, v))


def library_ops(par, tmp, images, fw_cache):
    from flashpod import library as library_mod
    print("library: init, add, plan_add, remove, rebuild")
    w = os.path.join(tmp, "m3")
    os.makedirs(w)
    n = [0]
    music = os.path.join(tmp, "music")
    umusic = os.path.join(tmp, "umusic")
    env = dict(FLASHPOD_SEED=str(SEED3), FLASHPOD_NOW=str(NOW3))

    def case(what, start, py_fn, rust_args, tags=None, rust_env=None,
             py_setup=None, py_teardown=None, norm=None):
        n[0] += 1
        a = os.path.join(w, "%d-py.img" % n[0])
        b = os.path.join(w, "%d-rs.img" % n[0])
        for d in (a, b):
            subprocess.run(["cp", "--sparse=always", start, d], check=True)
        args = [x.replace("@CARD@", b) if x != "@" else b for x in rust_args]
        if tags is not None and PYTHON_TAGS:
            tp = os.path.join(w, "%d-tags.json" % n[0])
            json.dump(tags, open(tp, "w"))
            args += ["--tags", tp]
        saved = dict(os.environ)
        os.environ.update(env)
        os.environ.update(rust_env or {})
        before = len(par.fails)
        try:
            if py_setup:
                py_setup()

            def run_py():
                with entropy.deterministic(SEED3, NOW3):
                    return wrap(py_fn(a))
            par.call(what, run_py, *args, rename=(b, a), ignore=("seconds",),
                     norm=norm)
        finally:
            if py_teardown:
                py_teardown()
            os.environ.clear()
            os.environ.update(saved)
        diff = same_image(a, b)
        if diff:
            par.fails.append("%s: images differ (%s)" % (what, diff))
        else:
            par.checked += 1
        print("  %s %s" % ("ok  " if len(par.fails) == before else "FAIL", what))
        os.unlink(a)
        os.unlink(b)

    def wrap(v):
        return v if hasattr(v, "to_dict") else Plain(v)

    flashed = dict(images)["flashed"]
    inited = dict(images)["init, no music"]
    mcard = dict(images)["init + music"]
    ucard = dict(images)["non-ASCII tags"]
    corrupt = dict(images)["damaged iTunesDB"]
    nodb = dict(images)["no iTunesDB, music files"]
    nofree = dict(images)["no FSInfo free count"]
    blank = dict(images)["blank"]

    # init
    case("init", flashed, lambda a: card.init(a), ["init", "@"])
    case("init with a non-ASCII name", flashed,
         lambda a: card.init(a, "Bibliothèque ß 音楽"),
         ["init", "@", "--name", "Bibliothèque ß 音楽"])
    case("init a card that has a DB", inited, lambda a: card.init(a),
         ["init", "@"])
    case("init a card with a damaged DB", corrupt, lambda a: card.init(a),
         ["init", "@"])
    case("init --overwrite over music", mcard,
         lambda a: card.init(a, overwrite=True), ["init", "@", "--overwrite"])
    case("init a blank card", blank, lambda a: card.init(a), ["init", "@"])

    # add / plan_add
    t_music = source_tags([music])
    t_umusic = source_tags([umusic])
    alpha = os.path.join(music, "Alpha Band")
    one = sorted(t_music)[0]
    case("plan_add a folder", inited, lambda a: card.plan_add(a, [music]),
         ["plan-add", "@", music], tags=t_music)
    case("plan_add, no assessment", mcard,
         lambda a: card.plan_add(a, [music], assess=False),
         ["plan-add", "@", music, "--no-assess"], tags=t_music)
    case("add a folder", inited, lambda a: card.add(a, [music]),
         ["add", "@", music], tags=t_music)
    case("add it again (all duplicates)", mcard, lambda a: card.add(a, [music]),
         ["add", "@", music], tags=t_music)
    case("add one file", inited, lambda a: card.add(a, [one]),
         ["add", "@", one], tags=t_music)
    case("add non-ASCII tags", mcard, lambda a: card.add(a, [umusic]),
         ["add", "@", umusic], tags=t_umusic)
    case("add without an FSInfo count", nofree, lambda a: card.add(a, [umusic]),
         ["add", "@", umusic], tags=t_umusic)
    case("add to a card without a DB", nodb, lambda a: card.add(a, [alpha]),
         ["add", "@", alpha], tags=t_music)
    case("add to a damaged DB", corrupt, lambda a: card.add(a, [alpha]),
         ["add", "@", alpha], tags=t_music)
    empty = os.path.join(w, "empty-dir")
    os.makedirs(empty)
    case("add a folder with no audio", inited, lambda a: card.add(a, [empty]),
         ["add", "@", empty], tags={})
    limit = str(len(library_mod.itunesdb.serialize(card.library(inited))) + 500)
    for pol in ("refuse", "fit", "force"):
        case("add past FLASHPOD_DB_LIMIT, ceiling %s" % pol, inited,
             lambda a, pol=pol: card.add(a, [music], policy={"ceiling": pol}),
             ["add", "@", music, "--ceiling", pol], tags=t_music,
             rust_env={"FLASHPOD_DB_LIMIT": limit},
             py_setup=lambda: os.environ.__setitem__("FLASHPOD_DB_LIMIT", limit))
    big = os.path.join(w, "big.wav")              # past a minimum card's space
    synth._wav(big, 30, 440, rate=44100)
    small = os.path.join(w, "small.img")
    card.create_blank(small, card.min_card_bytes())
    card.flash(small, synth.write_firmware(os.path.join(w, "fw.bin")))
    with entropy.deterministic(SEED3, NOW3):
        card.init(small)
    bigs = [big] + [os.path.join(w, "big%d.wav" % i) for i in range(2, 30)]
    for i, p in enumerate(bigs[1:], 2):
        synth._wav(p, 30, 440 + i, rate=44100)
    t_big = source_tags(bigs)
    for full in ("stop", "fit"):
        case("add past the free space, full %s" % full, small,
             lambda a, full=full: card.add(a, bigs, policy={"full": full}),
             ["add", "@"] + bigs + ["--full", full], tags=t_big)
    f16 = [x for x in glob.glob(os.path.join(fw_cache, "flashpod_1.1.6*.bin.gz"))]
    if f16:
        c16 = os.path.join(w, "c16.img")
        card.create_blank(c16, 96 << 20)
        card.flash(c16, f16[0])
        with entropy.deterministic(SEED3, NOW3):
            card.init(c16)
        stock = len(library_mod.itunesdb.serialize(card.library(c16))) + 500
        old = library_mod.IPOD_DB_SIZE_LIMIT, library_mod.IPOD_PATCHED_TRACK_LIMIT

        def shrink():
            library_mod.IPOD_DB_SIZE_LIMIT = stock

        def restore():
            library_mod.IPOD_DB_SIZE_LIMIT = old[0]
        case("add past the stock limit on flashpod 1.6 (track cap)", c16,
             lambda a: card.add(a, [music]), ["add", "@", music], tags=t_music,
             rust_env={"FLASHPOD_PARITY_STOCK_LIMIT": str(stock)},
             py_setup=shrink, py_teardown=restore)

    # a FLAC track through later saves (its marker and time added kept)
    flac = os.path.join(w, "song.flac")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=1", "-metadata",
                    "title=A FLAC song", flac], check=True,
                   stdin=subprocess.DEVNULL)
    fcard = os.path.join(w, "flac.img")
    subprocess.run(["cp", "--sparse=always", inited, fcard], check=True)
    with entropy.deterministic(5, 1600000000):
        card.add(fcard, [flac])
    case("add after a FLAC track", fcard, lambda a: card.add(a, [alpha]),
         ["add", "@", alpha], tags=t_music)
    fid = str(card.tracks(fcard)["tracks"][0]["id"])
    with entropy.deterministic(6, 1600000000):
        card.add(fcard, [alpha])
    keep = [str(t["id"]) for t in card.tracks(fcard)["tracks"] if str(t["id"]) != fid]
    case("remove beside a FLAC track", fcard,
         lambda a: card.remove(a, what=keep[:1]), ["remove", "@"] + keep[:1])

    # progress and cancel (M5): the same events, and a cancelled add saves
    # exactly the tracks already copied, on both sides
    def recorded(fn, cancel_after=None):
        def run(a):
            events, files = [], [0]

            def progress(stage, done, total, item):
                if stage in ("tags", "copy"):
                    files[0] += 1
                events.append([stage, done, total, item])

            def cancelled():
                return cancel_after is not None and files[0] >= cancel_after
            d = fn(a, progress, cancelled).to_dict()
            d["events"] = events
            return Plain(d)
        return run
    nmusic = len(source_tags([music]))
    ids_m = [str(t["id"]) for t in card.tracks(mcard)["tracks"]]
    case("add with progress", inited,
         recorded(lambda a, p, c: card.add(a, [music], progress=p, cancelled=c)),
         ["add", "@", music, "--events"])
    for k in (1, 3, nmusic + 1, nmusic + 3, 99):
        case("add cancelled after %d files announced" % k, inited,
             recorded(lambda a, p, c: card.add(a, [music], progress=p, cancelled=c), k),
             ["add", "@", music, "--events", "--cancel-after", str(k)])
    case("plan_add with progress", mcard,
         recorded(lambda a, p, c: card.plan_add(a, [music], progress=p, cancelled=c)),
         ["plan-add", "@", music, "--events"])
    case("plan_add cancelled", inited,
         recorded(lambda a, p, c: card.plan_add(a, [music], progress=p, cancelled=c), 2),
         ["plan-add", "@", music, "--events", "--cancel-after", "2"])
    case("rebuild with progress", mcard,
         recorded(lambda a, p, c: card.rebuild(a, progress=p)),
         ["rebuild", "@", "--events"])

    # the JSON command API (flashpod_core::api, what emupod's app calls),
    # natively through fpcore run and again in the wasm build under Node
    def api(**cmd):
        return json.dumps(dict(cmd, seed=SEED3, now=NOW3))
    # a browser host has no folders to walk: it passes the files themselves
    from flashpod import tracks as _tracks
    mfiles = _tracks.expand([music], report=lambda m: None)
    wasm = build_wasm(w)
    print("  wasm build: %s" % ("%d bytes" % wasm[1] if wasm else
                                "SKIPPED (no node)"))
    native_case = case

    def case(what, start, py_fn, rust_args, **kw):        # noqa: F811
        native_case(what, start, py_fn, rust_args, **kw)
        if wasm and rust_args[0] == "run":
            saved_fpcore = par.fpcore
            par.fpcore = wasm[0]
            try:
                native_case(what + " (wasm)", start, py_fn, rust_args, **kw)
            finally:
                par.fpcore = saved_fpcore
    apifw = synth.write_firmware(os.path.join(w, "apifw.bin"))
    case("api init", flashed, lambda a: card.init(a), ["run", "@", api(op="init")])
    case("api add", inited,
         recorded(lambda a, p, c: card.add(a, [music], progress=p, cancelled=c)),
         ["run", "@", api(op="add", files=mfiles), "--events"])
    case("api add, cancelled", inited,
         recorded(lambda a, p, c: card.add(a, [music], progress=p, cancelled=c), 8),
         ["run", "@", api(op="add", files=mfiles), "--events", "--cancel-after", "8"])
    case("api add past the free space, fit", small,
         lambda a: card.add(a, bigs, policy={"full": "fit"}),
         ["run", "@", api(op="add", files=bigs, full="fit")])
    case("api plan_add", mcard, lambda a: card.plan_add(a, [music]),
         ["run", "@", api(op="plan_add", files=mfiles)])
    case("api remove", mcard, lambda a: card.remove(a, what=ids_m[:1]),
         ["run", "@", api(op="remove", what=ids_m[:1])])
    case("api remove, no match", mcard, lambda a: card.remove(a, what=["artist", "Nobody"]),
         ["run", "@", api(op="remove", what=["artist", "Nobody"])])
    case("api rebuild", mcard, lambda a: card.rebuild(a), ["run", "@", api(op="rebuild")])
    case("api inspect", mcard, lambda a: card.inspect(a),
         ["run", "@", api(op="inspect", path="@CARD@")])
    case("api tracks", mcard, lambda a: TracksResult(card.tracks(a)),
         ["run", "@", api(op="tracks")])
    case("api flash", blank, lambda a: card.flash(a, apifw),
         ["run", "@", api(op="flash"), "--blob", apifw])
    case("api flash --no-format --arm (refused: synthetic aupd)", flashed,
         lambda a: card.flash(a, apifw, format=False, arm=True),
         ["run", "@", api(op="flash", format=False, arm=True), "--blob", apifw])
    case("api flash_firmware", flashed, lambda a: card.flash_firmware(a, apifw),
         ["run", "@", api(op="flash_firmware"), "--blob", apifw])
    case("api format_data", mcard, lambda a: card.format_data(a),
         ["run", "@", api(op="format_data")])
    notfw = one                                   # an MP3, not firmware
    case("api flash, not firmware", blank, lambda a: card.flash(a, notfw),
         ["run", "@", api(op="flash"), "--blob", notfw])
    case("api min_card_bytes", blank, lambda a: Plain(card.min_card_bytes()),
         ["run", "@", api(op="min_card_bytes")])

    case = native_case

    # remove
    ids = [str(t["id"]) for t in card.tracks(mcard)["tracks"]]
    artist = card.tracks(mcard)["tracks"][0]["artist"]
    album = next(t["album"] for t in reversed(card.tracks(mcard)["tracks"])
                 if t["album"])
    case("remove by id", mcard, lambda a: card.remove(a, what=ids[:2]),
         ["remove", "@"] + ids[:2])
    case("remove an artist (other case)", mcard,
         lambda a: card.remove(a, what=["artist", artist.upper()]),
         ["remove", "@", "artist", artist.upper()])
    case("remove an album", mcard,
         lambda a: card.remove(a, what=["album"] + album.split()),
         ["remove", "@", "album"] + album.split())
    case("remove a missing id", mcard, lambda a: card.remove(a, what=["9999"]),
         ["remove", "@", "9999"])
    case("remove non-ids", mcard, lambda a: card.remove(a, what=["x1"]),
         ["remove", "@", "x1"])
    case("remove an unknown artist", mcard,
         lambda a: card.remove(a, what=["artist", "Nobody"]),
         ["remove", "@", "artist", "Nobody"])
    case("remove artist with no name", mcard,
         lambda a: card.remove(a, what=["artist"]), ["remove", "@", "artist"])
    case("remove on a card without a DB", nodb,
         lambda a: card.remove(a, what=["52"]), ["remove", "@", "52"])
    case("remove on a damaged DB", corrupt,
         lambda a: card.remove(a, what=["52"]), ["remove", "@", "52"])

    # rebuild
    for name, start in (("a card with music", mcard), ("no DB", nodb),
                        ("non-ASCII tags", ucard), ("a damaged DB", corrupt)):
        case("rebuild %s" % name, start, lambda a: card.rebuild(a),
             ["rebuild", "@"], tags=card_tags(start))
    case("rebuild --no-save, named", mcard,
         lambda a: card.rebuild(a, name="Re", save=False),
         ["rebuild", "@", "--name", "Re", "--no-save"], tags=card_tags(mcard))

    # every format at once: the tag corpus files Python reads (Rust doesn't
    # read Ogg), added, then rebuilt
    import tag_corpus
    from flashpod import tracks
    readable = []
    for label, p in tag_corpus.build(os.path.join(w, "tagc")):
        r = _py_tags(lambda p=p: (tracks.read_audio(p), os.path.getsize(p)))
        if ("audio" in r or "unrecognized" in r) \
                and "Ogg" not in label and not _too_big(r):
            readable.append(p)
    big = os.path.join(w, "tags-card.img")
    card.create_blank(big, 256 << 20)
    card.flash(big, synth.write_firmware(os.path.join(w, "fw2.bin")))
    with entropy.deterministic(SEED3, NOW3):
        card.init(big)
    fresh_big = os.path.join(w, "tags-card-fresh.img")
    subprocess.run(["cp", "--sparse=always", big, fresh_big], check=True)
    case("add %d files of every format" % len(readable), big,
         lambda a: card.add(a, readable), ["add", "@"] + readable)
    with entropy.deterministic(SEED3, NOW3):
        card.add(big, readable)
    case("rebuild a card of every format", big, lambda a: card.rebuild(a),
         ["rebuild", "@"])
    if wasm:                                       # every format, in wasm too
        saved_fpcore = par.fpcore
        for runner, tag in ((saved_fpcore, ""), (wasm[0], " (wasm)")):
            par.fpcore = runner
            case("api add %d files of every format%s" % (len(readable), tag),
                 fresh_big, lambda a: card.add(a, readable),
                 ["run", "@", json.dumps({"op": "add", "files": readable,
                                          "seed": SEED3, "now": NOW3})])
        par.fpcore = saved_fpcore

    # a rebuild past files mutagen can't read: skipped and named (0.5.2);
    # the reason after "skipping PATH:" is mutagen's text on one side
    unreadable = [p for label, p in tag_corpus.build(os.path.join(w, "tagc2"))
                  if "error" in _py_tags(lambda p=p: (tracks.read_audio(p),
                                                       os.path.getsize(p)))][:12]
    with card.open_target(big, write=True) as c:
        for i, p in enumerate(unreadable):
            c.target().fs.write_file("iPod_Control/Music/F07/bad%02d%s" % (
                i, os.path.splitext(p)[1]), open(p, "rb").read())

    def reasons_cut(d):
        d = dict(d)
        d["skipped"] = [m.split(": ", 1)[0] for m in d.get("skipped", [])]
        return d
    case("rebuild past %d unreadable files" % len(unreadable), big,
         lambda a: card.rebuild(a), ["rebuild", "@"], norm=reasons_cut)


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
    ap.add_argument("--damaged", type=int, default=3000,
                    help="damaged audio files to compare (M4)")
    ap.add_argument("--python-tags", action="store_true",
                    help="give the Rust side mutagen's tags (M3's way)")
    ap.add_argument("--tags-only", action="store_true",
                    help="only the tag comparison (M4)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the corpus directory (it is kept anyway on a "
                         "mismatch)")
    opts = ap.parse_args()
    global PYTHON_TAGS
    PYTHON_TAGS = opts.python_tags
    par = Parity(build())
    tmp = tempfile.mkdtemp(prefix="fp-parity-")
    if opts.tags_only:
        tag_files(par, tmp, opts.damaged)
        return finish(par, tmp, opts)

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
    library_ops(par, tmp, images, opts.firmware_cache)
    tag_files(par, tmp, opts.damaged)
    return finish(par, tmp, opts)


def finish(par, tmp, opts):
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
