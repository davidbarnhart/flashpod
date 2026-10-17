#!/usr/bin/env python3
"""`flashpod flash --no-format` says what it does, and keeps the music.

    PYTHONPATH=. python3 scripts/test_flash_no_format.py

Found 2026-10-16 (David, via the firmware session, bridge #1217): a
--no-format dry run printed "PLAN — this will ERASE /dev/sdc" and "would
wipe signatures, ... pure-Python FAT32 on data", though --no-format writes
only the partition table, the firmware and the last 34 sectors, and keeps
the data partition. It also never checked that the card's data partition
sits where the new partition table points, which would send the iPod to the
wrong sectors.

On synthetic images, through the CLI:
  * a --no-format plan and dry run never say ERASE or FAT32; they say the
    data partition is kept; the dry run leaves the image unchanged;
  * control: a plain flash's plan still says ERASE and FAT32 (so the scan
    above can fail);
  * a real --no-format flash with a different firmware: the new firmware is
    on the card, every byte of the data partition is unchanged, and the
    card still lists its tracks;
  * a dry run that can't read the card says so (a real run then checks);
  * refused, image unchanged: a card whose data partition differs from the
    planned one (the image was grown), and a blank card with no partition
    table.
Against the tree before the fix, the plan, dry-run and refusal checks fail.
"""
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, layout  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def flashpod(*args):
    p = subprocess.run([sys.executable, "-m", "flashpod"] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, cwd=ROOT,
                       env=dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1"))
    return p.returncode, p.stdout.decode("utf-8", "replace")


def sha(path, start=0, length=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        h.update(f.read() if length is None else f.read(length))
    return h.hexdigest()


def data_region(img):
    """(start byte, length) of the image's data partition, from its MBR."""
    import struct
    with open(img, "rb") as f:
        mbr = f.read(512)
    start, count = struct.unpack_from("<II", mbr, 462 + 8)
    return start * 512, count * 512


def main():
    tmp = tempfile.mkdtemp(prefix="fp-noformat-")
    fw = synth.write_firmware(os.path.join(tmp, "fw.bin"))
    fw2 = synth.write_firmware(os.path.join(tmp, "fw2.bin"), seed=2)
    music = synth.make_music(os.path.join(tmp, "music"))
    img = os.path.join(tmp, "card.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, fw)
    card.init(img)
    card.add(img, [os.path.join(music, "Alpha Band")])
    n_tracks = len(card.tracks(img)["tracks"])
    check(n_tracks > 0, "the card holds %d tracks" % n_tracks)

    print("the --no-format plan and dry run")
    before = sha(img)
    rc, out = flashpod("flash", img, "--firmware", fw2, "--no-format",
                       "--dry-run")
    check(rc == 0, "dry run exits 0 (rc %d)" % rc)
    check("ERASE" not in out, "never says ERASE")
    check("FAT32" not in out, "never says FAT32")
    check("data partition and its music are kept" in out,
          "the plan says the data partition is kept")
    check("is left as it is" in out, "the dry run says the data is left alone")
    check("data partition matches the planned layout" in out,
          "the dry run checked the card's data partition")
    check(sha(img) == before, "the dry run leaves the image unchanged")

    print("control: a plain flash still warns")
    rc, out = flashpod("flash", img, "--firmware", fw2, "--dry-run")
    check("ERASE" in out and "FAT32" in out,
          "a plain flash's plan says ERASE and FAT32")

    print("a real --no-format flash keeps the music")
    start, length = data_region(img)
    data_before = sha(img, start, length)
    rc, out = flashpod("flash", img, "--firmware", fw2, "--no-format", "--yes")
    check(rc == 0, "flash --no-format exits 0 (rc %d)" % rc)
    check(sha(img, start, length) == data_before,
          "every byte of the data partition is unchanged")
    check(data_region(img) == (start, length), "the partition table points at it")
    check(len(card.tracks(img)["tracks"]) == n_tracks,
          "the card still lists its %d tracks" % n_tracks)
    with open(img, "rb") as f, open(fw2, "rb") as g:
        f.seek(layout.FW_START * 512)
        new_fw = g.read()
        on_card = f.read(len(new_fw))
    with open(fw, "rb") as g:
        old_fw = g.read()
    check(on_card != old_fw[:len(on_card)], "the firmware region changed")
    fresh = os.path.join(tmp, "fresh.img")
    card.create_blank(fresh, 96 << 20)
    card.flash(fresh, fw2)
    check(sha(img, 0, layout.DATA_START * 512)
          == sha(fresh, 0, layout.DATA_START * 512),
          "the lead-in (MBR + firmware) matches a fresh flash of the same firmware")

    if os.geteuid() != 0:                       # root reads it regardless
        print("a dry run that can't read the card (David's case: no root)")
        os.chmod(img, 0)
        try:
            rc, out = flashpod("flash", img, "--firmware", fw2, "--no-format",
                               "--dry-run")
        finally:
            os.chmod(img, 0o600)
        check(rc == 0 and "a real run checks before writing" in out
              and "ERASE" not in out and "FAT32" not in out,
              "says it couldn't check, and still never says ERASE or FAT32")

    print("refused, image unchanged")
    with open(img, "r+b") as f:                 # grow the card by 64 MiB
        f.truncate(os.path.getsize(img) + (64 << 20))
    before = sha(img)
    rc, out = flashpod("flash", img, "--firmware", fw2, "--no-format", "--yes")
    check(rc != 0 and "wrong sectors" in out and sha(img) == before,
          "a data partition elsewhere than planned (rc %d: %s)"
          % (rc, out.strip().splitlines()[-1] if out.strip() else ""))
    rc, out = flashpod("flash", img, "--firmware", fw2, "--no-format",
                       "--dry-run")
    check(rc != 0 and "wrong sectors" in out and sha(img) == before,
          "the dry run refuses it too (rc %d)" % rc)
    blank = os.path.join(tmp, "blank.img")
    card.create_blank(blank, 96 << 20)
    before = sha(blank)
    rc, out = flashpod("flash", blank, "--firmware", fw2, "--no-format", "--yes")
    check(rc != 0 and "no partition table" in out and sha(blank) == before,
          "a blank card with no partition table (rc %d)" % rc)

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
