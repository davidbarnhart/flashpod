#!/usr/bin/env python3
"""Verify a flashed iPod card image, reading only the backing file.

    PYTHONPATH=. python3 scripts/verify_ipod_image.py CARD.img [--firmware FW]

Opens the image directly -- no mount, no attached device, no root -- using
flashpod's own pure-Python FAT driver. That means it can check a card image
on any machine, and it checks what actually landed on the media rather than
what some OS's mount layer reports.

Checks, in order:
  1. MBR signature and the two-partition layout (firmware + FAT32 data)
  2. the firmware region is really populated (and matches --firmware if given)
  3. the FAT32 filesystem mounts and carries the expected volume label
  4. the iPod directory structure exists (iPod_Control/{iTunes,Music})
  5. iTunesDB is present and non-trivial
  6. music files are actually in iPod_Control/Music/F**

Exit status is 0 only if every check passes.
"""
import argparse
import gzip
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flashpod import fatfs

SECTOR = 512
FW_START = 63                       # flashpod puts the firmware partition here

GRN, RED, YEL, RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    GRN = RED = YEL = RST = ""

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    mark = (GRN + "PASS" + RST) if ok else (RED + "FAIL" + RST)
    print("  [%s] %-42s %s" % (mark, name, detail))
    return ok


def note(msg):
    print("         %s%s%s" % (YEL, msg, RST))


# -- 1. partition table --------------------------------------------------------
def read_mbr(path):
    with open(path, "rb") as f:
        return f.read(SECTOR)


def parse_partitions(mbr):
    """The four primary MBR entries as dicts (type, lba_start, sectors)."""
    parts = []
    for i in range(4):
        off = 446 + i * 16
        e = mbr[off:off + 16]
        ptype = e[4]
        lba, count = struct.unpack("<II", e[8:16])
        parts.append({"index": i + 1, "type": ptype, "lba": lba, "sectors": count})
    return parts


def verify_partitions(path):
    mbr = read_mbr(path)
    check("MBR boot signature 0x55AA", mbr[510:512] == b"\x55\xaa",
          "got %s" % mbr[510:512].hex())
    parts = parse_partitions(mbr)
    used = [p for p in parts if p["type"] != 0 or p["sectors"]]
    check("exactly two partitions defined", len(used) == 2,
          "found %d" % len(used))
    if len(used) < 2:
        return None
    fw, data = used[0], used[1]
    check("firmware partition starts at sector %d" % FW_START,
          fw["lba"] == FW_START, "starts at %d" % fw["lba"])
    check("data partition is FAT32 (type 0x0B)", data["type"] == 0x0B,
          "type 0x%02X" % data["type"])
    check("data partition follows the firmware one",
          data["lba"] > fw["lba"] + fw["sectors"] - 1,
          "fw ends %d, data starts %d" % (fw["lba"] + fw["sectors"] - 1, data["lba"]))
    print("         firmware: sectors %d..%d (%.1f MiB)"
          % (fw["lba"], fw["lba"] + fw["sectors"] - 1, fw["sectors"] * SECTOR / 2**20))
    print("         data    : sectors %d..%d (%.1f GiB)"
          % (data["lba"], data["lba"] + data["sectors"] - 1,
             data["sectors"] * SECTOR / 2**30))
    return fw, data


# -- 2. firmware region --------------------------------------------------------
def load_expected(fw_path):
    with open(fw_path, "rb") as f:
        head = f.read(2)
        f.seek(0)
        return gzip.decompress(f.read()) if head == b"\x1f\x8b" else f.read()


def verify_firmware(path, fw_part, expected_path):
    off = fw_part["lba"] * SECTOR
    with open(path, "rb") as f:
        f.seek(off)
        head = f.read(64 * 1024)
    check("firmware region is not blank", any(head),
          "first 64 KiB all zero" if not any(head) else "")
    if not expected_path:
        note("no --firmware given; skipping byte-for-byte comparison")
        return
    want = load_expected(expected_path)
    with open(path, "rb") as f:
        f.seek(off)
        got = f.read(len(want))
    check("firmware matches %s byte-for-byte" % os.path.basename(expected_path),
          got == want,
          "%d bytes compared" % len(want) if got == want else "differs")


# -- 3-6. the filesystem -------------------------------------------------------
def verify_volume_label(path, data_part, want="IPOD"):
    """BS_VolLab lives at offset 71 of the FAT32 boot sector."""
    with open(path, "rb") as f:
        f.seek(data_part["lba"] * SECTOR)
        boot = f.read(SECTOR)
    check("FAT32 boot sector signature", boot[510:512] == b"\x55\xaa")
    label = boot[71:82].decode("ascii", "replace").strip()
    check("volume label is %r" % want, label == want, "got %r" % label)


def verify_filesystem(path, data_part, min_tracks):
    dev = fatfs.BlockDev(path=path, part_start=data_part["lba"], max_xfer=8)
    try:
        try:
            fs = fatfs.Fat32(dev)
        except Exception as exc:                       # noqa: BLE001
            check("FAT32 filesystem parses", False, str(exc))
            return
        check("FAT32 filesystem parses", True)

        for d in ("iPod_Control", "iPod_Control/iTunes", "iPod_Control/Music"):
            check("directory %s exists" % d, fs.exists(d))

        db = "iPod_Control/iTunes/iTunesDB"
        if check("iTunesDB present", fs.exists(db)):
            data = fs.read_file(db) or b""
            check("iTunesDB is non-trivial", len(data) > 512,
                  "%d bytes" % len(data))
            # the iTunesDB is a tree of 'mh..' chunks; the outermost is mhbd
            check("iTunesDB starts with the 'mhbd' header", data[:4] == b"mhbd",
                  "header %r" % data[:4])

        # music lives in iPod_Control/Music/F00..F49
        tracks = []
        music = fs.listdir("iPod_Control/Music") or []
        for entry in music:
            if not (entry.attr & fatfs.ATTR_DIRECTORY):
                continue
            sub = "iPod_Control/Music/%s" % entry.name
            for t in (fs.listdir(sub) or []):
                if not (t.attr & fatfs.ATTR_DIRECTORY):
                    tracks.append(("%s/%s" % (sub, t.name), t.size))
        check("music folders exist (F00..)", len(music) > 0,
              "%d folders" % len(music))
        check("at least %d music file(s) on the card" % min_tracks,
              len(tracks) >= min_tracks, "found %d" % len(tracks))
        for name, size in tracks[:10]:
            print("         track: %-52s %d bytes" % (name, size))
        if len(tracks) > 10:
            print("         ... and %d more" % (len(tracks) - 10))

        if tracks:
            first = fs.read_file(tracks[0][0]) or b""
            check("first track reads back non-empty", len(first) > 1024,
                  "%d bytes" % len(first))
            # MP3 (ID3 tag or a frame sync), WAV/AIFF container, or MPEG-4
            magic = (first[:3] == b"ID3"
                     or first[:2] in (b"\xff\xfb", b"\xff\xfa",
                                      b"\xff\xf3", b"\xff\xf2")
                     or first[:4] == b"RIFF"          # .wav
                     or first[:4] == b"FORM"          # .aif / .aiff
                     or first[4:8] == b"ftyp")        # .m4a / .aac
            check("first track looks like audio", magic, "starts %r" % first[:4])
    finally:
        dev.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="the backing file the card was flashed to")
    ap.add_argument("--firmware", help="firmware image to compare byte-for-byte "
                                       "(.bin or .bin.gz)")
    ap.add_argument("--min-tracks", type=int, default=1,
                    help="minimum music files expected (default 1; 0 to allow none)")
    opts = ap.parse_args()

    if not os.path.exists(opts.image):
        sys.exit("no such image: %s" % opts.image)
    size = os.path.getsize(opts.image)
    print("verifying %s (%.1f GiB apparent)" % (opts.image, size / 2**30))

    print("\npartition table")
    parts = verify_partitions(opts.image)
    if parts:
        fw_part, data_part = parts
        print("\nfirmware")
        verify_firmware(opts.image, fw_part, opts.firmware)
        print("\nfilesystem")
        verify_volume_label(opts.image, data_part)
        verify_filesystem(opts.image, data_part, opts.min_tracks)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("\n%d/%d checks passed" % (passed, total))
    if passed != total:
        print(RED + "IMAGE IS NOT A VALID FLASHED IPOD CARD" + RST)
        return 1
    print(GRN + "image looks like a correctly flashed iPod card" + RST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
