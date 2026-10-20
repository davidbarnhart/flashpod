"""A card's FAT filesystem and iPod database, driven by the userspace FAT
driver (:mod:`flashpod.fatfs`) -- no OS mount.

:class:`RawTarget` provides what the data commands need (load/save the
library, init, copy a track in, remove one, rebuild from the files on the
card, free space). The CLI uses it over a raw device; :mod:`flashpod.card`
uses it over a disk image file, opened by :func:`open_image_fat`.

Device concerns -- O_DIRECT, the FireWire bridge's transfer cap, Windows
volume locks, sudo -- belong to the CLI (``cli.open_raw_fat``), not here.
"""

import os
import struct
import sys
import time

from . import entropy
from . import fatfs
from . import itunesdb
from .errors import CorruptLibrary, NotACard
from .tracks import _library_from_located, _track_from_audio, read_audio_stream

#: Transfer size for the userspace FAT driver on an image file: there is no
#: bridge to protect (= platform.image.IMAGE_MAX_XFER).
IMAGE_MAX_XFER = 2048

FAT_TYPES = (0x0b, 0x0c)


def find_fat_partition(boot):
    """Given sector 0 of a disk, return the start LBA of its FAT data
    partition, 0 if sector 0 is itself a FAT boot sector (a bare partition),
    or None if neither. Same walk as cli.open_raw_fat."""
    is_fat = boot[82:85] == b"FAT" and boot[510:512] == b"\x55\xaa"
    if is_fat:
        return 0
    if boot[510:512] != b"\x55\xaa":
        return None
    for poff in (446, 462, 478, 494):
        if boot[poff + 4] in FAT_TYPES:
            start = int.from_bytes(boot[poff + 8:poff + 12], "little")
            if start:
                return start
    return None


def open_image_fat(fileobj):
    """A :class:`fatfs.Fat32` over an open image file object (whole disk:
    the MBR is walked to the FAT partition). Raises NotACard."""
    dev = fatfs.BlockDev(part_start=0, max_xfer=IMAGE_MAX_XFER, fileobj=fileobj)
    start = find_fat_partition(dev.read(0, 1))
    # no FAT partition: try sector 0 anyway, as cli.open_raw_fat does, so the
    # error is the FAT driver's own ("not a 512-byte-sector FAT volume")
    dev.part_start = start or 0
    try:
        return fatfs.Fat32(dev)
    except (OSError, ValueError) as exc:
        raise NotACard("couldn't read a FAT filesystem: %s" % exc,
                       reason="no_fat_partition" if start is None
                       else "bad_fat", error=str(exc))


def firmware_reader(read_sectors):
    """``read(offset, n)`` over the card's firmware partition, given
    ``read_sectors(lba, count)`` over the whole disk; None when sector 0
    holds no MBR firmware partition (type 0x00 with a start and a length --
    a Windows-flavor card; an APM card or a bare FAT partition has none)."""
    from .layout import SECTOR
    mbr = read_sectors(0, 1)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        return None
    start = None
    for poff in (446, 462, 478, 494):
        lba, count = struct.unpack_from("<II", mbr, poff + 8)
        if mbr[poff + 4] == 0x00 and lba and count:
            start = lba
            break
    if start is None:
        return None

    def read(offset, n):
        first = offset // SECTOR
        last = (offset + n - 1) // SECTOR
        data = read_sectors(start + first, last - first + 1)
        skip = offset - first * SECTOR
        return bytes(data[skip:skip + n])
    return read


class RawTarget:
    """An iPod managed DIRECTLY over its raw device with the userspace FAT
    driver — no OS mount. Provides the read/write operations the data commands
    need (init/add/rm/ls), so the same command cores work over a mount or over
    raw. ``fs`` is a writable :class:`fatfs.Fat32`."""
    DB = "iPod_Control/iTunes/iTunesDB"

    def __init__(self, fs, node, desc=""):
        self.fs = fs
        self.node = node
        self.desc = desc

    # -- library -----------------------------------------------------------
    def load_library(self):
        """Parse the iTunesDB. Returns a Library, None if there's no DB at all,
        or raises CorruptLibrary if the DB is present but unparseable."""
        data = self.fs.read_file(self.DB)
        if data is None:
            return None
        try:
            return itunesdb.parse_bytes(data)
        except (ValueError, IndexError, struct.error) as exc:
            raise CorruptLibrary(str(exc))

    def iter_music_files(self):
        """Yield (relpath, location) for every track file physically present
        under iPod_Control/Music/F## — the raw view the rebuild reconstructs
        the database from. relpath is the FAT path (for reading); location is
        the ':'-style iTunesDB location."""
        music = "iPod_Control/Music"
        for d in sorted(self.fs.listdir(music) or [], key=lambda e: e.name):
            if not (d.attr & fatfs.ATTR_DIRECTORY) \
                    or not d.name.upper().startswith("F"):
                continue
            for f in sorted(self.fs.listdir(music + "/" + d.name) or [],
                            key=lambda e: e.name):
                if f.attr & fatfs.ATTR_DIRECTORY:
                    continue
                yield ("%s/%s/%s" % (music, d.name, f.name),
                       ":".join(["", "iPod_Control", "Music", d.name, f.name]))

    def read_track_file(self, relpath):
        """Build a Track from a file's own tags, reading it LAZILY off the FAT
        (open_file + mutagen's seek-based access pulls only the head/tail, not
        the whole file — the difference between seconds and minutes over
        FireWire). Returns None if the file isn't recognizable audio."""
        fh = self.fs.open_file(relpath)
        if fh is None:
            return None
        audio = read_audio_stream(fh, os.path.splitext(relpath)[1])
        if audio is None:
            return None
        return _track_from_audio(
            audio, os.path.splitext(os.path.basename(relpath))[0], fh.size,
            os.path.splitext(relpath)[1])

    def rebuild_library(self, name="iPod", progress=None, report=None):
        """Reconstruct the library from the track files already on the card:
        ensure the directory structure, then read every Music/F## file's tags
        and build a fresh Library pointing at them. Recovers a corrupt-DB iPod
        without re-copying or losing music. Returns the rebuilt Library (the
        caller saves it). Does NOT write the DB itself, so `add` can append to
        the result before its single save."""
        self._ensure_dirs()
        files = list(self.iter_music_files())
        located = []
        total = len(files)
        for nr, (relpath, location) in enumerate(files, 1):
            if progress:
                progress(nr, total, relpath)
            try:
                t = self.read_track_file(relpath)
            except (OSError, ValueError) as exc:
                t = None
                if report:
                    report(f"skipping {relpath}: {exc}")
            if t is not None:
                located.append((location, t))
        return _library_from_located(name, located)

    def save_library(self, lib):
        self.fs.write_file(self.DB, itunesdb.serialize(lib))
        self.fs.sync()

    # -- init --------------------------------------------------------------
    def _makedirs(self, path):
        cur = ""
        for part in path.split("/"):
            cur = cur + "/" + part if cur else part
            if not self.fs.exists(cur):
                self.fs.mkdir(cur)

    def _ensure_dirs(self):
        for sub in ["iTunes", "Device"] + ["Music/F%02d" % i for i in range(50)]:
            self._makedirs("iPod_Control/" + sub)

    def init_structure(self, name):
        self._ensure_dirs()
        self.save_library(itunesdb.Library(name))
        self.fs.sync()

    # -- add ---------------------------------------------------------------
    def copy(self, src, progress=None, ext=None):
        """Mirror itunesdb.copy_to_ipod over the raw FAT: spread across the
        Music/F## dirs, collision-proof name, return the ':'-style location."""
        music = "iPod_Control/Music"
        entries = self.fs.listdir(music) or []
        fdirs = sorted(e.name for e in entries if e.name.upper().startswith("F"))
        if not fdirs:
            raise OSError("no Music/F## directories (run init first?)")
        fdir = entropy.rng().choice(fdirs)
        ext = (ext or os.path.splitext(src)[1] or ".mp3").lower()
        timing = os.environ.get("FLASHPOD_TIMING")
        t0 = time.monotonic()
        while True:
            name = "fp%06d%s" % (entropy.rng().randrange(10 ** 6), ext)
            dst = "%s/%s/%s" % (music, fdir, name)
            if not self.fs.exists(dst):
                break
        t_setup = time.monotonic()
        with open(src, "rb") as f:
            data = f.read()
        t_read = time.monotonic()
        self.fs.write_file(dst, data, progress=progress)
        if timing:
            print("[timing] %s: setup %.2fs  source-read %.2fs  "
                  "card-write %.2fs"
                  % (os.path.basename(src), t_setup - t0, t_read - t_setup,
                     time.monotonic() - t_read), file=sys.stderr)
        return ":".join(["", "iPod_Control", "Music", fdir, name])

    # -- rm ----------------------------------------------------------------
    def remove_location(self, location):
        if not location:
            return
        path = "/".join(p for p in location.split(":") if p)
        if self.fs.exists(path):
            self.fs.remove(path)

    # -- free space (for the add pre-flight) -------------------------------
    def free_bytes(self):
        """Free space in bytes, from the FAT layer (no OS mount to ask). Raises
        OSError if it can't be determined, which the add core treats as 'skip
        the pre-flight, proceed anyway'."""
        return self.fs.free_bytes()

    # -- the card's firmware (for the add pre-flight's DB ceiling) ---------
    def firmware_read(self):
        """``read(offset, n)`` over the card's firmware partition, or None
        when this target can't reach it (opened on the data partition's own
        node, or no MBR firmware partition). The FAT's block device reads
        relative to its partition, so the disk's sector 0 is -part_start."""
        dev = self.fs.dev
        try:
            return firmware_reader(
                lambda lba, count: dev.read(lba - dev.part_start, count))
        except (OSError, ValueError):
            return None


class MountTarget(object):
    """An iPod reached through an OS MOUNT (the kernel's FAT driver): the same
    operations as :class:`RawTarget`, on a directory. Moved here from the
    CLI's mount-path commands (cmd_add / cmd_rm / cmd_rebuild / init) so the
    library drives both backends with one set of operations."""

    def __init__(self, mount, firmware_read=None):
        self.mount = mount
        self.node = mount
        self.desc = ""
        # A mount has no raw access of its own: the CLI may supply a
        # () -> read(offset, n) over the card's firmware partition (it knows
        # the device and can elevate to read it).
        self._firmware_read = firmware_read

    def _path(self, location):
        return os.path.join(self.mount, *location.lstrip(":").split(":"))

    # -- library -----------------------------------------------------------
    def load_library(self):
        """Parse the iTunesDB. None if there is none (or no such directory);
        CorruptLibrary if it won't parse; OSError (e.g. EIO) propagates."""
        try:
            return itunesdb.load(self.mount)
        except FileNotFoundError:
            return None
        except (ValueError, IndexError, struct.error) as exc:
            raise CorruptLibrary(str(exc))

    def save_library(self, lib):
        itunesdb.save(lib, self.mount)
        # SYNC LESSON (CLAUDE.md): the kernel keeps FAT directory entries and
        # tables in page cache; without a system-wide sync the iPod can read a
        # stale filesystem. `add` and `rebuild` always synced; `rm` didn't.
        if hasattr(os, "sync"):
            os.sync()

    def music_dir_exists(self):
        return os.path.isdir(os.path.join(self.mount, "iPod_Control", "Music"))

    def rebuild_library(self, name="iPod", progress=None, report=None):
        """Rebuild from the track files under Music/F## (their tags), as
        `flashpod rebuild` over a mount always did. The caller saves."""
        from .tracks import make_track
        music = os.path.join(self.mount, "iPod_Control", "Music")
        files = []
        for d in sorted(os.listdir(music)):
            ddir = os.path.join(music, d)
            if not (d.upper().startswith("F") and os.path.isdir(ddir)):
                continue
            for fn in sorted(os.listdir(ddir)):
                fp = os.path.join(ddir, fn)
                if os.path.isfile(fp):
                    files.append((fp, ":".join(["", "iPod_Control", "Music",
                                                d, fn])))
        located = []
        total = len(files)
        for nr, (fp, location) in enumerate(files, 1):
            if progress:
                progress(nr, total, fp)
            t = make_track(None, fp, nr, total, report=report)
            if t is not None:
                located.append((location, t))
        return _library_from_located(name, located)

    # -- init / add / rm ---------------------------------------------------
    def init_structure(self, name):
        itunesdb.init_ipod(self.mount, name)

    def copy(self, src, progress=None, ext=None):
        return itunesdb.copy_to_ipod(self.mount, src, ext=ext,
                                     progress=progress)

    def remove_location(self, location):
        if not location:
            return
        path = self._path(location)
        if os.path.exists(path):
            os.unlink(path)

    def free_bytes(self):
        import shutil
        return shutil.disk_usage(self.mount).free

    def firmware_read(self):
        """``read(offset, n)`` over the card's firmware partition, if the
        caller supplied a way to get one; None otherwise."""
        if self._firmware_read is None:
            return None
        try:
            return self._firmware_read()
        except (OSError, ValueError):
            return None
