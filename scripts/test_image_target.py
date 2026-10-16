#!/usr/bin/env python3
"""Logic test: a regular file is a card -- `flash`, `init` and `ls` on a disk image.

    PYTHONPATH=. python3 scripts/test_image_target.py

`flashpod flash ipod.img` and `--raw ipod.img` treat a regular file as a whole
card (flashpod/platform/image.py): the same layout, no root, and none of the
steps that only a device needs. This runs the real CLI on a sparse image file,
in-process, with two things checked:

  * the result is a card: MBR signature, the firmware partition at FW_START
    holding exactly the firmware bytes, a FAT32 that `init` writes and the raw
    reader then loads an (empty) library from;
  * nothing device-only ran: the OS backend's unmount, wipefs, re-read,
    buffer flush and eject are replaced by functions that FAIL the test, and
    the run must never reach them.

Control: a device node (/dev/null) and a missing path are NOT images, and leave
the OS backend in place -- so the check cannot pass by treating everything as a
file. Run as root, the "no root" half proves nothing; it says so.
"""
import os
import struct
import sys
import tempfile

from flashpod import cli, ipod_flash, platform
from flashpod.platform.image import ImagePlatform

FW = bytes(range(256)) * 512            # 128 KiB that no layout would produce by accident


def fresh_platform():
    platform._cached = None
    inner = platform.current()

    def forbidden(name):
        def f(*a, **k):
            raise AssertionError("device-only step %s ran on an image" % name)
        return f
    for name in ("unmount_all", "wipe_signatures", "reread_partition_table",
                 "flush_buffers", "eject", "invalidate_cached_partitions",
                 "validate_target", "prepare_raw_write"):
        setattr(inner, name, forbidden(name))
    return inner


def run(*argv):
    saved = sys.argv
    sys.argv = ["flashpod"] + list(argv)
    try:
        return cli.main()
    finally:
        sys.argv = saved


def main():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("note: running as root, so 'needs no root' is not tested here")
    ipod_flash.load_firmware = lambda path: FW

    # -- control: what is not a file stays a device ---------------------------
    for path in ("/dev/null", "/nonexistent/ipod.img"):
        platform._cached = None
        if os.path.exists(path) or path.startswith("/nonexistent"):
            assert not platform.use_image(path), "%s taken for an image" % path
            assert not isinstance(platform.current(), ImagePlatform), path
    print("control: a device node and a missing path are not images")

    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "ipod.img")
        with open(img, "wb") as f:
            f.truncate(256 << 20)
        inner = fresh_platform()

        rc = run("flash", img, "--firmware", "fw.bin", "--yes")
        assert rc == 0, "flash returned %r" % rc
        plat = platform.current()
        assert isinstance(plat, ImagePlatform) and plat._inner is inner, plat
        assert plat.is_admin()

        with open(img, "rb") as f:
            disk = f.read((ipod_flash.FW_START + len(FW) // 512 + 1) * 512)
        assert disk[510:512] == b"\x55\xaa", "no MBR signature"
        start = struct.unpack_from("<I", disk, 446 + 8)[0]
        assert start == ipod_flash.FW_START, "firmware partition at %d" % start
        at = ipod_flash.FW_START * 512
        assert disk[at:at + len(FW)] == FW, "firmware bytes differ"
        assert os.path.getsize(img) == 256 << 20, "the image changed size"
        print("flash: MBR, firmware partition and firmware bytes as on a card")

        rc = run("init", "--raw", img)
        assert rc == 0, "init returned %r" % rc
        target = cli.open_raw_target(img, writable=False)
        assert target is not None, "the raw reader could not open the image"
        lib = target.load_library()
        assert lib is not None and len(lib.tracks) == 0, lib
        print("init: the raw reader loads an empty library from the image's FAT32")

    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
