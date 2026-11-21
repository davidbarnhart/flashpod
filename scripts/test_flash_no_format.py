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
  * refused, image unchanged: a blank card with no partition table.

Keep means keep (2026-11-21): --no-format uses the card's partition table AS
IT IS and writes only the firmware partition. A layout that merely DIFFERS
from flashpod's own is kept; one that is WRONG is refused:
  * a grown image (data partition no longer where flashpod's formula would
    put it, which the old --no-format refused) is kept, table unchanged;
  * every layout in kept_layout_fixtures: accepted ones keep every byte
    outside the firmware partition and name what was kept; wrong ones (and a
    Mac/APM card, which this command names but doesn't handle) are refused
    with the image unchanged and the reason in the message;
  * CONTROL: the old table rewrite after the write fails that identity
    check on every accepted layout but flashpod's own;
  * the reported card at full size (sparse): disk 30,320,640 sectors, data
    65599..30318590, one sector short of flashpod's formula: kept.
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
import kept_layout_fixtures as kept  # noqa: E402
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


SAYS = {
    "no_table": "no partition table", "no_firmware_partition": "no firmware partition",
    "firmware_start": "starts at sector 2048", "overlap": "overlap",
    "past_end": "runs past the end", "fat_larger_than_partition": "says it is 40001 sectors",
    "partition_too_small": "larger than the drive's firmware partition",
    "apm": "Apple Partition Map",
}


def kept_layouts(tmp, fw):
    print("keep means keep: cards laid out by other tools")
    for i, (name, build, expect) in enumerate(kept.FIXTURES):
        if name.startswith("APM"):
            expect = "apm"          # named, not handled by flash --no-format
        # a number, not the name: the refusal echoes the path, and a reason
        # word in it would satisfy the "says why" check by itself
        img = os.path.join(tmp, "kept-%d.img" % i)
        build(img)
        if expect != "keep":
            before = sha(img)
            rc, out = flashpod("flash", img, "--firmware", fw, "--no-format", "--yes")
            check(rc != 0 and sha(img) == before and SAYS[expect] in out,
                  "%s: refused, unchanged, says %r (rc %d)" % (name, SAYS[expect], rc))
            continue
        outside = kept.outside_ranges(img)
        before = [sha(img, a, b - a) for a, b in outside]
        rc, out = flashpod("flash", img, "--firmware", fw, "--no-format", "--yes")
        check(rc == 0 and "partition table kept as it is" in out,
              "%s: kept (rc %d)%s" % (name, rc, "" if rc == 0 else ": " + out.strip().splitlines()[-1]))
        check([sha(img, a, b - a) for a, b in outside] == before,
              "%s: every byte outside the firmware partition is unchanged" % name)
        from flashpod.firmware import load_firmware
        want = load_firmware(fw)                # install-ready, as written
        with open(img, "rb") as f:
            f.seek(kept.firmware_partition(img)[0] * 512)
            check(f.read(len(want)) == want, "%s: the firmware is in its partition" % name)
        kept.old_table_rewrite(img)
        caught = [sha(img, a, b - a) for a, b in outside] != before
        check(caught == (name != "flashpod's own"),
              "%s: control, the old table rewrite %s the identity check"
              % (name, "fails" if caught else "passes"))


def formula_matches_flashpod():
    print("the fixtures' formula is flashpod's own")
    for total in (120000, 30320639, 30320640):
        hdr, start, n = layout.build_mbr_layout(total)
        check(kept.formula_entries(total)[1][1:] == (start, n),
              "%d sectors: data %d+%d" % (total, start, n))
    # the reported card: one sector fewer over the iPod's own disk path
    _h, start, n = layout.build_mbr_layout(30320639)
    check((start, start + n - 1) == (65599, 30318590),
          "flashpod's layout for 30,320,639 sectors is data 65599..30318590")


def reported_card(tmp, fw):
    print("the reported card at full size (sparse)")
    img = os.path.join(tmp, "reported.img")
    total, d0, d1 = 30320640, 65599, 30318590
    kept._image(img, total, [(0, 63, 65536), (0x0B, d0, d1 - d0 + 1)], [(d0, d1 - d0 + 1)])
    spots = [(0, 512), (d0 * 512, 512), ((total - 8) * 512, 4096)]
    before = [sha(img, a, n) for a, n in spots]
    rc, out = flashpod("flash", img, "--firmware", fw, "--no-format", "--yes")
    check(rc == 0 and "partition table kept as it is: MBR, firmware 63..65598, data 65599..30318590" in out,
          "kept, and named (rc %d)" % rc)
    check("unallocated" not in out, "no tail warning: it leaves 2049 sectors")
    check([sha(img, a, n) for a, n in spots] == before,
          "its MBR, FAT boot sector and tail are unchanged")
    os.unlink(img)


def main():
    tmp = synth.scratch_dir("fp-noformat-")
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
    check("partition table kept as it is: MBR, firmware 63..65598" in out,
          "the dry run checked the card's table and names what it keeps")
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

    print("a grown card is kept, not refused")
    with open(img, "r+b") as f:                 # grow the card by 64 MiB
        f.truncate(os.path.getsize(img) + (64 << 20))
    table_before = sha(img, 0, 512)
    data_before = sha(img, start, length)
    rc, out = flashpod("flash", img, "--firmware", fw, "--no-format", "--yes")
    check(rc == 0 and "partition table kept as it is" in out,
          "flash --no-format keeps it (rc %d)" % rc)
    check(sha(img, 0, 512) == table_before and sha(img, start, length) == data_before,
          "the partition table and the data partition are unchanged")
    check(len(card.tracks(img)["tracks"]) == n_tracks, "the card still lists its tracks")

    print("refused, image unchanged")
    blank = os.path.join(tmp, "blank.img")
    card.create_blank(blank, 96 << 20)
    before = sha(blank)
    rc, out = flashpod("flash", blank, "--firmware", fw2, "--no-format", "--yes")
    check(rc != 0 and "no partition table" in out and sha(blank) == before,
          "a blank card with no partition table (rc %d)" % rc)

    formula_matches_flashpod()
    kept_layouts(tmp, fw2)
    reported_card(tmp, fw2)

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
