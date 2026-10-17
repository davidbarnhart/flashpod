#!/usr/bin/env python3
"""A broken iTunesDB must be refused, not read as an empty library.

    PYTHONPATH=. python3 scripts/test_itunesdb_strict.py

Found 2026-10-16 (the CLI-unchanged harness): an iTunesDB whose bytes after
the "mhbd" magic are garbage parsed as a library with ZERO tracks -- the walk
stopped quietly at the first unexpected bytes. The next `flashpod add` then
wrote a database holding only the new tracks: every existing entry dropped,
their files left orphaned on the card. The parser now raises ValueError,
which the CLI and flashpod.card report as a corrupt database (with a rebuild
offered) instead.

Checks, all on synthetic data:
  * valid databases still parse: our writer at 0, 1 and 40 tracks, and one
    with extra dataset types (as newer writers add) around the real ones;
  * refused: garbage after the magic, a truncated file, a track count larger
    than the records present, a dataset length past the end;
  * on a card image with 4 tracks and a garbage-body DB: card.inspect says
    corrupt, card.add refuses, and the CLI's `add --raw` refuses -- each
    leaving the image byte-for-byte unchanged.

Measured before the fix (af3a51b), same card: the parser read 0 tracks, the
add wrote a database of only the 5 new tracks, and the 4 earlier tracks'
files were left on the card as orphans. Run against that tree, this test
fails 7 checks.
"""
import hashlib
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, errors, itunesdb, layout  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def library(n, name="Strict"):
    lib = itunesdb.Library(name)
    for i in range(n):
        t = itunesdb.Track()
        t.id = 52 + i
        t.title = "Song %d" % i
        t.artist = "Artist %d" % (i % 3)
        t.album = "Album"
        t.location = ":iPod_Control:Music:F00:fp%06d.mp3" % i
        t.size = 1000 + i
        t.tracklen = 2000
        lib.tracks.append(t)
    return lib


def refused(buf):
    try:
        itunesdb.parse_bytes(buf)
    except ValueError:
        return True
    return False


def with_extra_datasets(db):
    """Insert dummy mhsd datasets of unknown types before and after the real
    ones, fixing up the mhbd child count and total length."""
    hlen = struct.unpack_from("<I", db, 4)[0]
    nchild = struct.unpack_from("<I", db, 20)[0]

    def dummy(t):
        return b"mhsd" + struct.pack("<III", 96, 96 + 8, t) + b"\0" * 80 + b"\0" * 8
    out = bytearray(db[:hlen]) + dummy(9) + db[hlen:] + dummy(10)
    struct.pack_into("<I", out, 8, len(out))
    struct.pack_into("<I", out, 20, nchild + 2)
    return bytes(out)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    print("valid databases still parse")
    for n in (0, 1, 40):
        lib = itunesdb.parse_bytes(itunesdb.serialize(library(n)))
        check(len(lib.tracks) == n and lib.name == "Strict"
              and (n == 0 or lib.tracks[-1].title == "Song %d" % (n - 1)),
              "our writer, %d tracks" % n)
    db = itunesdb.serialize(library(3))
    lib = itunesdb.parse_bytes(with_extra_datasets(db))
    check(len(lib.tracks) == 3, "extra dataset types around the real ones")

    print("broken databases are refused")
    body = bytearray(db)
    body[4:4 + 80] = b"\xff\xfe" * 40
    check(refused(bytes(body)), "garbage after the magic")
    check(refused(db[:len(db) // 2]), "truncated to half")
    off = db.index(b"mhlt")
    more = bytearray(db)
    struct.pack_into("<I", more, off + 8, 4)
    check(refused(bytes(more)), "track count 4 with 3 records present")
    off = db.index(b"mhsd")
    long_ = bytearray(db)
    struct.pack_into("<I", long_, off + 8, len(db) * 2)
    check(refused(bytes(long_)), "a dataset length past the end")

    print("on a card")
    tmp = tempfile.mkdtemp(prefix="fp-strict-")
    img = os.path.join(tmp, "card.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, synth.write_firmware(os.path.join(tmp, "fw.bin")))
    card.init(img)
    music = synth.make_music(os.path.join(tmp, "music"))
    card.add(img, [os.path.join(music, "Alpha Band"),
                   os.path.join(music, "Beta Group", "Only Album",
                                "03 Third.mp3")])
    check(card.inspect(img).itunesdb["tracks"] == 4, "4 tracks on the card")
    with open(img, "r+b") as f:             # garbage over every DB body
        data = f.read()
        i = data.find(b"mhbd", layout.DATA_START * 512)
        while i >= 0:
            f.seek(i + 4)
            f.write(b"\xff\xfe" * 40)
            i = data.find(b"mhbd", i + 4)
    before = sha(img)
    info = card.inspect(img)
    check(info.itunesdb["state"] == "corrupt", "inspect: corrupt (%s)"
          % info.itunesdb.get("error", info.itunesdb["state"]))
    try:
        card.add(img, [music])
        ok = False
    except errors.CorruptLibrary:
        ok = True
    check(ok and sha(img) == before, "card.add: CorruptLibrary, image unchanged")
    p = subprocess.run([sys.executable, "-m", "flashpod", "add", "--raw", img,
                        os.path.join(music, "Beta Group", "Only Album",
                                     "untagged.wav")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, cwd=ROOT,
                       env=dict(os.environ, PYTHONPATH=ROOT))
    out = p.stdout.decode()
    check(p.returncode == 1 and "corrupt" in out and sha(img) == before,
          "CLI add --raw: refused as corrupt, image unchanged (rc %d: %s)"
          % (p.returncode, out.strip().splitlines()[-1] if out.strip() else ""))

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
