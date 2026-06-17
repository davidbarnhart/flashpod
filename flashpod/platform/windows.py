"""Windows backend.

Raw disk access on Windows means opening ``\\\\.\\PhysicalDriveN`` with the
right sharing flags, locking/dismounting its volumes first, and writing in
sector-aligned chunks (handled by :class:`AlignedRawIO`). Disk enumeration
and volume mapping go through PowerShell's Storage cmdlets; the low-level
operations (length, partition-table re-read, lock/dismount, flush, eject)
go through ``DeviceIoControl`` via ctypes.

NOTE: this backend has NOT yet been validated on real Windows hardware
(tracked in the cross-platform releases issue). The module imports cleanly
on any OS — Windows-only APIs are touched only inside methods.

Python 3.6 compatible.
"""

import ctypes
import os
import re
import struct
import subprocess
import sys

from .base import Platform, AlignedRawIO, Unsupported, SECTOR

# DeviceIoControl codes
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value if False else (2 ** 64 - 1)


def _powershell(script):
    """Run a PowerShell snippet, return stdout text ('' on failure)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", "replace")
    except OSError:
        pass
    return ""


def _drive_number(dev):
    m = re.search(r"(\d+)$", dev)
    if not m:
        raise ValueError("not a PhysicalDrive path: %s" % dev)
    return int(m.group(1))


class WindowsPlatform(Platform):
    name = "windows"

    # -- privilege --------------------------------------------------------
    def is_admin(self):
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:                                       # noqa: BLE001
            return False

    def privilege_hint(self):
        return "Run from an Administrator prompt to write to a disk."

    # -- low-level handle helpers -----------------------------------------
    def _open_handle(self, path, write):
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        h = ctypes.windll.kernel32.CreateFileW(
            ctypes.c_wchar_p(path), access,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE_VALUE or h is None:
            raise OSError("CreateFileW failed for %s (err %d)"
                          % (path, ctypes.windll.kernel32.GetLastError()))
        return h

    def _ioctl(self, handle, code, out_size=0):
        buf = ctypes.create_string_buffer(out_size) if out_size else None
        returned = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, code, None, 0, buf, out_size,
            ctypes.byref(returned), None)
        if not ok:
            raise OSError("DeviceIoControl 0x%X failed (err %d)"
                          % (code, ctypes.windll.kernel32.GetLastError()))
        return buf.raw[:returned.value] if buf else b""

    # -- device discovery / selection -------------------------------------
    def _disks(self):
        """[(number, size_bytes, name, bustype, is_system)] for all disks."""
        txt = _powershell(
            "Get-Disk | ForEach-Object { "
            "\"$($_.Number)|$($_.Size)|$($_.FriendlyName)|$($_.BusType)|"
            "$($_.IsSystem)|$($_.IsBoot)\" }")
        disks = []
        for line in txt.splitlines():
            f = line.strip().split("|")
            if len(f) < 6 or not f[0].isdigit():
                continue
            disks.append((int(f[0]), int(f[1] or 0), f[2], f[3],
                          f[4].strip().lower() == "true" or f[5].strip().lower() == "true"))
        return disks

    def choose_device(self):
        from .. import ipod_flash
        color = ipod_flash.color
        cands = [d for d in self._disks()
                 if not d[4] and d[3] in ("USB", "SD", "MMC", "1394")]
        if not cands:
            cands = [d for d in self._disks() if not d[4]]
        if not cands:
            sys.exit(color("No removable disks found. Plug in the card and retry.",
                           ipod_flash.C_RED))
        print(color("\nAttached removable disks:\n", ipod_flash.C_CYN), file=sys.stderr)
        for i, (num, size, name, bus, _sys) in enumerate(cands):
            print("  [%d] \\\\.\\PhysicalDrive%-3d %10s  %s (%s)" %
                  (i, num, ipod_flash.fmt_size(size), name.strip(), bus), file=sys.stderr)
        print(file=sys.stderr)
        while True:
            sel = input(color("Select device number (or 'q' to quit): ",
                              ipod_flash.C_CYN)).strip()
            if sel.lower() in ("q", "quit", ""):
                sys.exit("Aborted.")
            if sel.isdigit() and int(sel) < len(cands):
                return "\\\\.\\PhysicalDrive%d" % cands[int(sel)][0]
            print(color("  invalid selection", ipod_flash.C_RED), file=sys.stderr)

    def device_sectors(self, dev):
        if os.path.isfile(dev):
            return os.path.getsize(dev) // SECTOR
        try:
            h = self._open_handle(dev, write=False)
            try:
                raw = self._ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, 8)
                return struct.unpack("<Q", raw)[0] // SECTOR
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except (OSError, ValueError, struct.error):
            return 0

    def device_mountpoints(self, dev):
        try:
            num = _drive_number(dev)
        except ValueError:
            return []
        txt = _powershell(
            "Get-Partition -DiskNumber %d | Where-Object DriveLetter | "
            "ForEach-Object { $_.DriveLetter }" % num)
        return [("%s:" % c.strip(), "%s:\\" % c.strip())
                for c in txt.splitlines() if c.strip()]

    def validate_target(self, dev, dry_run):
        from .. import ipod_flash
        color, red = ipod_flash.color, ipod_flash.C_RED
        if not re.match(r"^\\\\\.\\PhysicalDrive\d+$", dev) and not os.path.isfile(dev):
            sys.exit(color("expected a \\\\.\\PhysicalDriveN path; got %s" % dev, red))
        if os.path.isfile(dev):
            return
        num = _drive_number(dev)
        for d in self._disks():
            if d[0] == num and d[4]:
                sys.exit(color("refusing: PhysicalDrive%d backs the running system." % num, red))

    # -- mutation around the raw write ------------------------------------
    def unmount_all(self, dev, dry):
        if dry:
            return
        for vol, _mp in self.device_mountpoints(dev):
            try:
                h = self._open_handle("\\\\.\\%s" % vol.rstrip("\\"), write=True)
                try:
                    self._ioctl(h, FSCTL_LOCK_VOLUME)
                    self._ioctl(h, FSCTL_DISMOUNT_VOLUME)
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
            except OSError:
                pass

    def wipe_signatures(self, dev, dry):
        return  # raw zeroing in write_layout handles this

    def reread_partition_table(self, dev):
        try:
            h = self._open_handle(dev, write=True)
            try:
                self._ioctl(h, IOCTL_DISK_UPDATE_PROPERTIES)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            pass

    def flush_buffers(self, dev):
        try:
            h = self._open_handle(dev, write=True)
            try:
                ctypes.windll.kernel32.FlushFileBuffers(h)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            pass

    def eject(self, dev, dry):
        if dry:
            return
        try:
            h = self._open_handle(dev, write=True)
            try:
                self._ioctl(h, IOCTL_STORAGE_EJECT_MEDIA)
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except OSError:
            pass

    def open_raw(self, dev, mode):
        if os.path.isfile(dev):
            return open(dev, mode)
        import msvcrt
        h = self._open_handle(dev, write=("w" in mode or "+" in mode or "a" in mode))
        fd = msvcrt.open_osfhandle(h, os.O_BINARY)
        return AlignedRawIO(os.fdopen(fd, mode, buffering=0))

    # -- sync-path mount detection ----------------------------------------
    def mounted_filesystems(self):
        txt = _powershell(
            "Get-Volume | Where-Object DriveLetter | ForEach-Object { "
            "\"$($_.DriveLetter)|$($_.FileSystem)\" }")
        out = []
        for line in txt.splitlines():
            f = line.strip().split("|")
            if f and f[0]:
                out.append(("%s:" % f[0], "%s:\\" % f[0], f[1] if len(f) > 1 else ""))
        return out
