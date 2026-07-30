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

    def partition_node(self, dev, index):
        """Device node for 1-based partition ``index`` of whole disk ``dev``,
        or None where the OS exposes no such node.

        Naming is per-OS and NOT interchangeable -- Linux appends ``2`` (or
        ``p2`` after a trailing digit, e.g. mmcblk0p2) while macOS always
        appends ``s2``. Building it inline is how the post-flash init offer
        silently skipped itself on macOS: it looked for /dev/disk3p2, which
        never exists there."""
        raise NotImplementedError

    def fat_mount_cmd(self, part, mnt):
        """argv that mounts the FAT partition ``part`` at ``mnt``, or None if
        this platform can't. Linux's mount(8) probes the type itself; macOS
        needs it named (``-t msdos``)."""
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

    def init_before_mbr(self):
        """True if the post-flash init hook must run BEFORE the partition
        table is committed.

        The two families want opposite things and cannot be served by one
        ordering. Windows writes the fresh FAT through the still-open raw
        handle and must do it while no valid MBR exists, or the volume
        manager discovers the partition, mounts it, and blocks writes to
        those sectors. Linux and macOS instead mount the partition, so they
        need the MBR written and the table re-read first -- with the
        Windows ordering there is no /dev/sdb2 or /dev/diskNs2 to mount and
        the hook silently does nothing."""
        return False

    def invalidate_cached_partitions(self, dev):
        """After zeroing the partition table, tell the OS to drop cached
        volume state so raw writes to any sector succeed.  No-op except on
        Windows where the volume manager blocks writes to sectors claimed
        by online volumes."""
        pass

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

    def prepare_raw_write(self, dev):
        """Make raw WRITES to ``dev`` possible, before the writable handle
        opens. Windows overrides this to lock/dismount the disk's volumes —
        the volume manager otherwise denies writes to any sector inside a
        recognized volume (WinError 5), even a letterless one. Elsewhere:
        nothing to do."""
        return

    def raw_part_start_override(self):
        """LBA the userspace FAT driver must use as the partition start,
        overriding the MBR walk in :func:`open_raw_fat`. Windows returns this
        after :meth:`prepare_raw_write` has temporarily cleared the on-disk
        MBR (so the walk would find nothing). ``None`` means read the MBR
        normally."""
        return None

    def finalize_raw_write(self, dev):
        """Undo whatever :meth:`prepare_raw_write` changed once the raw write
        is done. Windows overrides this to restore the partition table it
        temporarily cleared. Elsewhere: nothing to do."""
        return

    def raw_open_direct(self):
        """True when the userspace FAT driver should let fatfs.BlockDev open
        the raw node itself (path mode: os.open, O_DIRECT on block devices,
        bounce-buffered I/O) instead of wrapping :meth:`open_raw`'s file
        object.

        Linux overrides this to True: a buffered open() of a Linux block
        device reads and writes through the page cache, whose readahead and
        writeback re-batch our capped transfers into exactly the large/queued
        I/O the gen-1 FireWire bridge corrupts — safe only while the device
        queue is pinned (the udev rule / per-command auto-pin). O_DIRECT
        removes that dependency, making the raw path self-sufficiently
        bridge-safe. macOS gets the same property from the rdisk character
        device (via open_raw + AlignedRawIO) and Windows from WinHandleIO,
        so they keep the file-object path. Regression history: PR #54
        (2026-07-25) rewired open_raw_fat through open_raw()/fileobj for
        Windows and silently dropped Linux's O_DIRECT — a FireWire add on
        that build crashed the bridge the next day."""
        return False

    def raw_read_node(self, dev):
        """The device path to open for UNBUFFERED reads of ``dev``. Default is
        ``dev`` itself; macOS maps ``/dev/diskN`` → ``/dev/rdiskN`` so the FAT
        driver never reads through the buffer cache (whose read-ahead is what
        the gen-1 FireWire bridge corrupts into zeros)."""
        return dev

    def raw_max_xfer(self, device=None):
        """Safe default transfer size (in 512-byte sectors) for the userspace
        FAT driver, for BOTH reads and writes. 8 = 4 KiB, the Linux-kernel-
        queue-proven ceiling for the FireWire bridge; macOS overrides this per
        transport — lower for FireWire (the raw device doesn't honour that queue
        cap, and only single-sector transfers are proven safe over the bridge),
        far higher for USB. Larger writes don't help
        anyway — the bridge is bandwidth-limited. Override via
        FLASHPOD_RAW_MAX_XFER (e.g. raise it on a USB reader).

        ``device`` lets a backend decide from the transport rather than the OS
        — the bridge constraint is FireWire's, not the platform's. macOS does
        this; Linux keeps a flat 8 until the Linux column of the transport
        matrix is measured on hardware."""
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
