#!/usr/bin/env python3
"""Wiring test: open_raw_fat must give fatfs.BlockDev the PATH on Linux.

BlockDev's path mode is where O_DIRECT lives — the thing that makes the raw
driver unbuffered on Linux the way /dev/rdiskN does on macOS. Handing it a
buffered file object instead re-introduces the page cache, whose readahead/
writeback re-batching is exactly the large/queued I/O the gen-1 FireWire
bridge corrupts; the raw path then silently depends on pinned queue settings.
That regression actually shipped: PR #54 (2026-07-25) rewired open_raw_fat
through plat.open_raw()/fileobj= for Windows and dropped Linux's O_DIRECT;
a FireWire add on that build crashed the bridge the next day. This test pins
the wiring, per platform, with everything below open_raw_fat mocked out.

    PYTHONPATH=. python3 scripts/test_raw_open_wiring.py
"""
import sys
import types

sys.argv = ["flashpod"]
from flashpod import cli                    # noqa: E402


class FakePlat:
    def __init__(self, direct):
        self._direct = direct
        self.opened = None
    def raw_max_xfer(self):
        return 8
    def raw_read_node(self, dev):
        return dev
    def raw_open_direct(self):
        return self._direct
    def open_raw(self, node, mode):
        self.opened = (node, mode)
        return "FAKE-FILEOBJ"


class CapturingBlockDev:
    """Stands in for fatfs.BlockDev; records how it was constructed."""
    last = None
    def __init__(self, path=None, part_start=0, max_xfer=8, writable=False,
                 fileobj=None):
        CapturingBlockDev.last = {"path": path, "fileobj": fileobj,
                                  "writable": writable, "max_xfer": max_xfer}
        self.part_start = part_start
    def read(self, lba, count):
        # a valid-looking FAT boot sector so open_raw_fat skips the MBR walk
        boot = bytearray(512)
        boot[82:85] = b"FAT"
        boot[510:512] = b"\x55\xaa"
        return bytes(boot)


def check(name, ok, detail=""):
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))
    assert ok, detail


real_blockdev, real_fat32 = cli.fatfs.BlockDev, cli.fatfs.Fat32
cli.fatfs.BlockDev = CapturingBlockDev
cli.fatfs.Fat32 = lambda dev: dev
try:
    # Linux-style platform: BlockDev must get the PATH (O_DIRECT territory)
    # and open_raw must NOT be used.
    plat = FakePlat(direct=True)
    cli.platform.current = lambda: plat
    cli.open_raw_fat("/dev/sdb2", writable=True)
    got = CapturingBlockDev.last
    check("direct platform: BlockDev opens the path itself",
          got["path"] == "/dev/sdb2" and got["fileobj"] is None, repr(got))
    check("direct platform: writable flag carried through",
          got["writable"] is True, repr(got))
    check("direct platform: plat.open_raw never called",
          plat.opened is None, repr(plat.opened))

    # macOS/Windows-style platform: the platform's own wrapper file object
    # must be used (AlignedRawIO / WinHandleIO live behind open_raw).
    plat = FakePlat(direct=False)
    cli.platform.current = lambda: plat
    cli.open_raw_fat("/dev/rdisk2s2")
    got = CapturingBlockDev.last
    check("wrapped platform: BlockDev wraps open_raw's file object",
          got["fileobj"] == "FAKE-FILEOBJ" and got["path"] is None, repr(got))
    check("wrapped platform: opened read-only by default",
          plat.opened == ("/dev/rdisk2s2", "rb"), repr(plat.opened))
finally:
    cli.fatfs.BlockDev, cli.fatfs.Fat32 = real_blockdev, real_fat32

print("\nALL ASSERTIONS PASSED")
