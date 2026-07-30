#!/usr/bin/env python3
"""Wiring test: raw writes to a partition partmgr claims but never mounts.

The hard Windows case (real hardware, 2026-07-28): an iPod FAT slice that
Get-Partition/Get-Volume don't surface (type Unknown) yet partmgr still claims
from the on-disk MBR. No drive letter, no volume GUID -> nothing to lock; and
Set-Disk -IsOffline is policy-blocked. Interior writes get WinError 5. flash
already beats this by clearing the MBR so partmgr drops the claim; add/rm/init
must do the same but RESTORE the MBR afterwards. This test pins that flow with
the low-level handle ops mocked out. Import-only on any OS.

    PYTHONPATH=. python3 scripts/test_windows_raw_write_claim.py
"""
import io
import sys

from flashpod.platform import windows as w      # noqa: E402

DEV = "\\\\.\\PhysicalDrive2"
PART_LBA = 65599


def check(name, ok, detail=""):
    print("  [%s] %-60s %s" % ("PASS" if ok else "FAIL", name, detail))
    assert ok, detail


def fake_mbr(fat=True, start=PART_LBA):
    mbr = bytearray(512)
    mbr[510:512] = b"\x55\xaa"
    if fat:
        mbr[446 + 4] = 0x0c
        mbr[446 + 8:446 + 12] = int(start).to_bytes(4, "little")
    return bytes(mbr)


class FakeHandle:
    """Records WriteFile / FlushFileBuffers / CloseHandle on a k32 stand-in."""
    def __init__(self):
        self.writes = []
        self.flushed = False
        self.closed = False

    def WriteFile(self, h, buf, n, written, ov):
        self.writes.append(bytes(buf))
        return 1

    def FlushFileBuffers(self, h):
        self.flushed = True
        return 1

    def CloseHandle(self, h):
        self.closed = True
        return 1


def run(mountpoints, mbr, do_finalize=True):
    """Drive prepare_raw_write (+ optional finalize) with everything below the
    platform mocked. Returns (override, invalidated?, k32, stderr, plat)."""
    plat = w.WindowsPlatform()
    plat.device_mountpoints = lambda dev: mountpoints
    plat._read_sector0 = lambda dev: mbr
    invalidated = []
    plat.invalidate_cached_partitions = lambda dev: invalidated.append(dev)
    plat._lock_volumes = lambda dev: [("\\\\.\\D:", None)]
    plat._open_handle = lambda dev, write: "H"
    plat._ioctl = lambda h, code, *a: None
    k32 = FakeHandle()

    saved_ps_reg = w.atexit.register
    saved_k32 = w._k32
    w.atexit.register = lambda *a, **k: None
    w._k32 = lambda: k32
    err = io.StringIO()
    saved_stderr, sys.stderr = sys.stderr, err
    try:
        plat.prepare_raw_write(DEV)
        override = plat.raw_part_start_override()
        if do_finalize:
            plat.finalize_raw_write(DEV)
    finally:
        sys.stderr = saved_stderr
        w.atexit.register = saved_ps_reg
        w._k32 = saved_k32
    return override, bool(invalidated), k32, err.getvalue(), plat


# --- Case 1: claimed-but-unmountable partition -> clear MBR, feed LBA ------
override, invalidated, k32, err, plat = run([], fake_mbr(), do_finalize=False)
check("no volume: MBR cleared to drop partmgr's claim", invalidated, err)
check("no volume: FAT partition LBA fed to the driver", override == PART_LBA,
      repr(override))
check("no volume: saved MBR stashed for restore", plat._saved_mbr is not None,
      repr(plat._saved_mbr))
check("no volume: user told the table is temporarily cleared",
      "temporarily cleared its partition table" in err, repr(err))

# --- Case 2: finalize restores the MBR and clears the override ------------
override, invalidated, k32, err, plat = run([], fake_mbr(), do_finalize=True)
check("finalize: original MBR written back to sector 0",
      k32.writes and k32.writes[-1] == fake_mbr(), "wrote %d sector(s)" % len(k32.writes))
check("finalize: buffers flushed and handle closed",
      k32.flushed and k32.closed, "flushed=%s closed=%s" % (k32.flushed, k32.closed))
check("finalize: override cleared, restore is now idempotent",
      plat.raw_part_start_override() is None and plat._saved_mbr is None,
      repr(plat._saved_mbr))

# --- Case 3: no FAT partition in the MBR -> refuse, don't touch anything ---
override, invalidated, k32, err, plat = run([], fake_mbr(fat=False), do_finalize=True)
check("no FAT slice: MBR NOT cleared", not invalidated, repr(invalidated))
check("no FAT slice: no override, honest message",
      override is None and "no FAT partition found" in err, repr(err))

# --- Case 4: a real lockable volume -> lock it, never touch the MBR --------
override, invalidated, k32, err, plat = run([("D:", "D:\\")], fake_mbr())
check("with volume: per-volume lock path used, MBR untouched",
      not invalidated and override is None, "inval=%s override=%s" % (invalidated, override))
check("with volume: 'locked volume' reported",
      "locked volume" in err, repr(err))

print("\nALL ASSERTIONS PASSED")
