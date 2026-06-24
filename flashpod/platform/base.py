"""The platform abstraction interface.

Everything OS-specific in flashpod is funnelled through a single
:class:`Platform` object obtained from :func:`flashpod.platform.current`.
The rest of the codebase (the flash engine, the CLI) is OS-agnostic and
talks only to this interface, so porting to a new OS means writing one new
backend, not hunting through the whole tree.

Backends live alongside this module: ``linux.py``, ``macos.py``,
``windows.py``. Linux is the reference implementation; macOS and Windows
implement the same contract.

Methods that genuinely cannot be carried out on a platform raise
:class:`Unsupported`; callers surface that as a clean error rather than a
traceback.

Python 3.6 compatible.
"""


class Unsupported(NotImplementedError):
    """Raised when an operation isn't available on the current platform."""


SECTOR = 512


class AlignedRawIO(object):
    """Sector-aligning wrapper around a raw device handle.

    macOS character devices (``/dev/rdiskN``) and Windows physical drives
    (``\\\\.\\PhysicalDriveN``) only accept reads/writes whose offset *and*
    length are multiples of the sector size. The flash code, however, makes
    small writes (the 12-byte FAT headers, the 32-byte volume-label entry).
    This wrapper turns those into read-modify-write cycles on whole sectors,
    while passing already-aligned bulk writes straight through.

    Linux block devices accept arbitrary writes through the page cache, so
    the Linux backend opens the device directly and does not use this.
    """

    def __init__(self, raw):
        self._raw = raw          # an open binary file (buffering=0 recommended)
        self._pos = 0

    # -- positioning ------------------------------------------------------
    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._raw.seek(0, 2)
            self._pos = self._raw.tell() + offset
        return self._pos

    def tell(self):
        return self._pos

    # -- I/O --------------------------------------------------------------
    def write(self, data):
        n = len(data)
        if n == 0:
            return 0
        # fast path: aligned offset and length -> pass through
        if self._pos % SECTOR == 0 and n % SECTOR == 0:
            self._raw.seek(self._pos)
            self._raw.write(data)
            self._pos += n
            return n
        start = self._pos
        end = start + n
        first = start // SECTOR
        last = (end - 1) // SECTOR
        span = (last - first + 1) * SECTOR
        self._raw.seek(first * SECTOR)
        buf = bytearray(self._raw.read(span))
        if len(buf) < span:                       # past current end of media/file
            buf.extend(b"\x00" * (span - len(buf)))
        off = start - first * SECTOR
        buf[off:off + n] = data
        self._raw.seek(first * SECTOR)
        self._raw.write(bytes(buf))
        self._pos = end
        return n

    def read(self, n):
        start = self._pos
        end = start + n
        first = start // SECTOR
        last = (end - 1) // SECTOR if end > start else first
        span = (last - first + 1) * SECTOR
        self._raw.seek(first * SECTOR)
        chunk = self._raw.read(span)
        off = start - first * SECTOR
        out = chunk[off:off + n]
        self._pos += len(out)
        return out

    # -- lifecycle --------------------------------------------------------
    def flush(self):
        self._raw.flush()

    def fileno(self):
        return self._raw.fileno()

    def close(self):
        try:
            self._raw.flush()
        finally:
            self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Platform(object):
    """OS-specific operations needed to sync and flash iPods.

    A backend overrides every method below. Geometry/firmware logic is NOT
    here — only things that touch the OS differently per platform.
    """

    #: short identifier, e.g. "linux"
    name = "base"

    # -- privilege ---------------------------------------------------------
    def is_admin(self):
        """True if the process can write to raw block devices."""
        raise NotImplementedError

    def privilege_hint(self):
        """One-line message telling the user how to gain privilege."""
        return "run with elevated privileges to write to a block device."

    # -- device discovery / selection -------------------------------------
    def choose_device(self):
        """Interactively pick a removable disk; return its raw path
        (e.g. ``/dev/sdb``, ``/dev/disk2``, ``\\\\.\\PhysicalDrive1``).
        May ``sys.exit`` if the user aborts or nothing is attached."""
        raise NotImplementedError

    def device_sectors(self, dev):
        """Total 512-byte sectors of a device or image file."""
        raise NotImplementedError

    def device_mountpoints(self, dev):
        """List of ``(partition_path, mountpoint)`` currently mounted off
        ``dev``; empty if none."""
        raise NotImplementedError

    def validate_target(self, dev, dry_run):
        """Safety gate before writing: confirm ``dev`` exists, is a whole
        disk (not a partition), and is not the disk backing the running
        system. ``sys.exit`` with a clear message on any failure."""
        raise NotImplementedError

    # -- mutation around the raw write ------------------------------------
    def unmount_all(self, dev, dry):
        """Unmount every filesystem currently mounted off ``dev``."""
        raise NotImplementedError

    def wipe_signatures(self, dev, dry):
        """Best-effort removal of stale partition/filesystem signatures so
        the OS doesn't cling to the old layout. May be a no-op."""
        raise NotImplementedError

    def reread_partition_table(self, dev):
        """Tell the OS to re-read ``dev``'s partition table after writing."""
        raise NotImplementedError

    def flush_buffers(self, dev):
        """Flush OS caches for ``dev`` so a subsequent read hits the media."""
        raise NotImplementedError

    def eject(self, dev, dry):
        """Flush and power off / eject ``dev``."""
        raise NotImplementedError

    def open_raw(self, dev, mode):
        """Open ``dev`` for raw binary I/O. Default works for real files and
        POSIX device nodes; Windows overrides for ``\\\\.\\PhysicalDriveN``."""
        return open(dev, mode)

    def raw_read_node(self, dev):
        """The device path to open for UNBUFFERED reads of ``dev``. Default is
        ``dev`` itself; macOS maps ``/dev/diskN`` → ``/dev/rdiskN`` so the FAT
        driver never reads through the buffer cache (whose read-ahead is what
        the gen-1 FireWire bridge corrupts into zeros)."""
        return dev

    def raw_max_xfer(self):
        """Safe default transfer size (in 512-byte sectors) for the userspace
        FAT driver, for BOTH reads and writes. 8 = 4 KiB, the Linux-kernel-
        queue-proven ceiling for the FireWire bridge; macOS overrides this lower
        (the raw device doesn't honour that queue cap, and only single-sector
        transfers are proven safe over the bridge). Larger writes don't help
        anyway — the bridge is bandwidth-limited. Override via
        FLASHPOD_RAW_MAX_XFER (e.g. raise it on a USB reader)."""
        return 8

    # -- sync-path mount detection ----------------------------------------
    def mounted_filesystems(self):
        """All mounted filesystems as ``(device, mountpoint, fstype)`` tuples
        — used to auto-detect an already-mounted iPod."""
        raise NotImplementedError

    def fat_disk_candidates(self):
        """Attached external/removable disks that have a FAT slice — the disks
        worth PROBING for an iPod (the actual test, done by the caller, is
        whether the FAT holds iPod_Control/iTunes/iTunesDB; a volume label or
        bus type is too fragile to rely on).

        Returns ``(node, description)`` tuples, where ``node`` is what
        :func:`flashpod.cli.open_raw_fat` should open (the unbuffered raw node —
        e.g. ``/dev/rdisk2`` on macOS — so OS read-ahead never re-enters the
        picture). This step needs no root; reading the FATs does. Default:
        nothing (platform can't enumerate)."""
        return []
