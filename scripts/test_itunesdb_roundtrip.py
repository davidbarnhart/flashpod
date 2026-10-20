#!/usr/bin/env python3
"""A track's type marker and time added survive a load and save.

    PYTHONPATH=. python3 scripts/test_itunesdb_roundtrip.py

Found 2026-10-20 while porting the library to Rust: the iTunesDB parser
read neither a track's type marker (mhit +24) nor its time added (+88), so
every save after a load -- each add, rm and rebuild -- rewrote FLAC tracks
with the "\\0MP3" marker and every track's time added as now. The parser
now reads both back. A time added of 0 (which a database from another
writer may hold there) is still taken as now, as before.

Checks, on synthetic data:
  * a FLAC and an MP3 track, serialized, parsed and serialized again: the
    track records come back byte for byte, marker and time added included;
  * a stored time added of 0 parses as now;
  * on a synthetic card: add a FLAC file, then an MP3 in a second add; the
    FLAC track still carries "\\0FLA" and its first time added.
Against the tree before the fix, the round trip and the card check fail.
"""
import os
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, entropy, itunesdb  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def track_records(db):
    """The mhit records of a serialized DB, in order."""
    out, i = [], db.find(b"mhit")
    while i >= 0:
        tlen = struct.unpack_from("<I", db, i + 8)[0]
        out.append(db[i:i + tlen])
        i = db.find(b"mhit", i + tlen)
    return out


def mhit_fields(rec):
    return rec[24:28], struct.unpack_from("<I", rec, 88)[0]


def main():
    print("serialize, parse, serialize")
    lib = itunesdb.Library("Round trip")
    for i, (marker, added, title) in enumerate((
            (itunesdb.MARKER_FLAC, 3000000000, "a flac"),
            (itunesdb.MARKER_MP3, 3100000000, "an mp3"))):
        t = itunesdb.Track()
        t.id, t.title, t.marker, t.time_added = 52 + i, title, marker, added
        t.location = ":iPod_Control:Music:F00:fp%06d" % i
        lib.tracks.append(t)
    first = itunesdb.serialize(lib)
    with entropy.deterministic(1, 1700000000):    # a different "now"
        again = itunesdb.serialize(itunesdb.parse_bytes(first))
    a, b = track_records(first), track_records(again)
    check(len(a) == 2 and a == b, "the track records come back byte for byte")
    check([mhit_fields(r) for r in b]
          == [(itunesdb.MARKER_FLAC, 3000000000), (itunesdb.MARKER_MP3, 3100000000)],
          "marker and time added kept: %r" % [mhit_fields(r) for r in b])

    print("a stored time added of 0")
    zero = bytearray(first)
    i = zero.find(b"mhit")
    struct.pack_into("<I", zero, i + 88, 0)
    with entropy.deterministic(1, 1700000000):
        t = itunesdb.parse_bytes(bytes(zero)).tracks[0]
    check(t.time_added == 1700000000 + itunesdb.MAC_EPOCH_OFFSET,
          "parses as now (%d)" % t.time_added)

    print("on a card: a FLAC track through a later add")
    tmp = synth.scratch_dir("fp-roundtrip-")
    flac = os.path.join(tmp, "song.flac")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=500:duration=1", "-metadata",
                    "title=A FLAC song", flac], check=True,
                   stdin=subprocess.DEVNULL)
    music = synth.make_music(os.path.join(tmp, "music"))
    mp3 = os.path.join(music, "Alpha Band", "First Album", "01 Opening.mp3")
    img = os.path.join(tmp, "card.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, synth.write_firmware(os.path.join(tmp, "fw.bin")))
    card.init(img)
    with entropy.deterministic(2, 1600000000):
        card.add(img, [flac])
    with entropy.deterministic(3, 1650000000):    # later
        card.add(img, [mp3])
    with card.open_target(img) as c:
        db = c.target().fs.read_file(card.DB_PATH)
    recs = {r[r.find(b"mhod"):].decode("utf-16-le", "ignore").find("A FLAC song") >= 0: r
            for r in track_records(db)}
    rec = recs.get(True)
    check(rec is not None and mhit_fields(rec)
          == (itunesdb.MARKER_FLAC, 1600000000 + itunesdb.MAC_EPOCH_OFFSET),
          "the FLAC track kept \\0FLA and its time added: %r"
          % (mhit_fields(rec) if rec else None,))

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
