"""flashpod as a library: every operation on a card.

This is the ONE place the card logic lives. Other programs import it (the
emulator's web app is the first), and the CLI is a frontend over it too:
`flashpod add/rm/init/rebuild` call card.plan_add / apply_add / remove /
init / rebuild, keeping only their prompts, progress lines and the
real-device handling (scripts/test_one_place.py checks that).

A ``card`` argument is an IMAGE PATH (opened and locked here) or a TARGET
object the caller opened: target.RawTarget over a raw device or an image
(the CLI's open_raw_target, or open_target() below), or target.MountTarget
over an OS-mounted iPod. The rules below about regular files and locking
apply to paths; a caller handing in a target owns its device.

    from flashpod import card
    card.create_blank("/cards/a.img", 8 * 10**9)
    card.flash("/cards/a.img", "/path/to/firmware.bin.gz")
    card.init("/cards/a.img", "My iPod")
    plan = card.plan_add("/cards/a.img", ["/music/Album"])
    report = card.add("/cards/a.img", ["/music/Album"], plan=plan)
    info = card.inspect("/cards/a.img")

RULES THIS MODULE KEEPS

* Image files only. Every call refuses anything that is not a regular file
  (``NotACard``, reason ``not_a_file``) -- checked on the open file, so a
  device node can't slip through. Nothing here writes a device, mounts,
  or asks for root.
* Nothing prints, prompts or exits. Results are objects with ``to_dict()``
  (JSON-safe); failures are :class:`flashpod.errors.FlashpodError`
  subclasses with a stable ``code`` and ``details``.
* One lock per image, part of the API: ``fcntl.flock`` on the image file,
  whole file, SHARED to read and EXCLUSIVE to write, never blocking. A
  conflict raises ``CardBusy``. Other programs touching the image (the
  emulator) take the same lock. (No-op where fcntl doesn't exist: Windows.)
* Kept from the CLI: firmware validation (checksum, install fixups, size),
  the tail reserve, adaptive FAT32 clusters and the cluster floor (refused
  up front here, where the CLI warns and leaves the partition unformatted),
  the iTunesDB size ceiling, one database write per batch, and the firmware
  partition untouched except by :func:`flash` / :func:`flash_firmware`.
* Skipped, because only devices need them: the FireWire queue settings,
  firewire_sbp2, sudo, mounting, ejecting, the small transfer cap, the LBA28
  hidden-tail check (a file shows its whole size), the multi-iPod refusal.

ADD, IN TWO STEPS OR ONE

plan_add reads the tags and dedups (writes nothing); apply_add copies a
chosen subset and saves the database once. add() does both, deciding by a
policy where the CLI asks its questions; the CLI asks them about the same
plan (which artists fit the free space; add past the DB ceiling?).

PROGRESS AND CANCEL

Long calls take ``progress(stage, done, total, item)`` and ``cancelled()``.
Stages: ``tags`` (files, during an add's tag reading), ``copy`` (files,
before each copy), ``write`` (bytes of the current file), ``rebuild``
(files). Both run on the caller's thread. ``cancelled()`` is checked between
files; a cancelled add saves exactly the tracks already copied.
"""

import errno
import hashlib
import os
import stat
import struct
import subprocess
import time

from . import __version__
from . import catalog as _catalog
from . import fat32
from . import firmware as _firmware
from . import itunesdb
from . import layout
from . import library as library_mod
from . import target as _target
# Every error a caller may catch is importable from here (card.CardBusy, ...).
from .errors import (  # noqa: F401
    AlreadyInitialized, CardBusy, CardExists, CardTooSmall, CorruptLibrary,
    DbCeiling, FirmwareInvalid, FlashpodError, LayoutMismatch, NoFirmware,
    NoSpace, NoSuchTrack, NotACard, NothingToAdd, NotInitialized, PlanStale)
from .catalog import FirmwareUnavailable  # noqa: F401
from .tracks import expand, orunknown, track_key

try:
    import fcntl
except ImportError:                     # Windows
    fcntl = None

#: major.minor. A major bump breaks callers; a minor one only adds.
API_VERSION = "1.1"

SECTOR = layout.SECTOR
DB_PATH = _target.RawTarget.DB


# ----------------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------------
class Result(object):
    """Attribute bag with ``to_dict()``. Fields are documented per call."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def to_dict(self):
        return {k: _plain(v) for k, v in self.__dict__.items()
                if not k.startswith("_")}

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, ", ".join(
            "%s=%r" % kv for kv in sorted(self.to_dict().items())))


def _plain(v):
    if isinstance(v, Result):
        return v.to_dict()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


class CardInfo(Result):
    pass


class AddPlan(Result):
    pass


class AddReport(Result):
    pass


def track_dict(t):
    """A track as plain data: what a frontend lists and removes by."""
    return {"id": t.id, "title": t.title, "artist": t.artist,
            "album": t.album, "genre": t.genre, "composer": t.composer,
            "track_nr": t.track_nr, "year": t.year,
            "length_ms": t.tracklen, "size": t.size, "bitrate": t.bitrate,
            "samplerate": t.samplerate, "filetype": t.filetype,
            "location": t.location}


def version():
    """``{"version", "commit", "dirty", "api"}`` of this flashpod. ``commit``
    is the git commit of the checkout it was imported from (None outside a
    git checkout); ``dirty`` is True when that checkout has local changes."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    commit = dirty = None
    try:
        commit = subprocess.check_output(
            ["git", "-C", here, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL).decode().strip() or None
        if commit:
            dirty = bool(subprocess.check_output(
                ["git", "-C", here, "status", "--porcelain", "--", "flashpod"],
                stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"version": __version__, "commit": commit, "dirty": dirty,
            "api": API_VERSION}


# ----------------------------------------------------------------------------
# Opening an image: regular file only, locked
# ----------------------------------------------------------------------------
class _Image(object):
    """An open, locked image file. ``with _Image(path, write=True) as img:``"""

    def __init__(self, path, write=False):
        self.path = path
        self.write = write
        self.f = None

    def __enter__(self):
        if not os.path.lexists(self.path):
            raise NotACard("%s does not exist" % self.path, reason="missing")
        try:
            self.f = open(self.path, "r+b" if self.write else "rb")
        except IsADirectoryError:
            raise NotACard("%s is a directory" % self.path,
                           reason="not_a_file")
        except OSError as exc:
            raise NotACard("can't open %s: %s" % (self.path, exc),
                           reason="unopenable")
        try:
            st = os.fstat(self.f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise NotACard("%s is not a regular file (a device?) -- "
                               "flashpod's library only works on image files"
                               % self.path, reason="not_a_file")
            self.size = st.st_size
            _lock(self.f, self.write, self.path)
        except BaseException:
            self.f.close()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.write:
                self.f.flush()
                os.fsync(self.f.fileno())
        finally:
            self.f.close()            # closing drops the flock
        return False

    def read(self, offset, n):
        self.f.seek(offset)
        return self.f.read(n)

    def fat(self):
        return _target.open_image_fat(self.f)

    def target(self):
        return _target.RawTarget(self.fat(), self.path)


def _lock(f, exclusive, path):
    if fcntl is None:
        return
    try:
        fcntl.flock(f.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                    | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise CardBusy("%s is in use by another program" % path,
                           wanted="exclusive" if exclusive else "shared")
        raise


# ----------------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------------
def min_card_bytes():
    """The smallest image :func:`flash` accepts: the firmware partition, the
    tail reserve and a data partition just big enough for a valid FAT32."""
    lo = 65600                          # data sectors; FAT32 needs >= 65525 clusters
    while True:
        geo = fat32.geometry(lo, layout.fat_sectors_per_cluster(lo))
        if geo["clusters"] >= fat32.FAT32_MIN_CLUSTERS:
            break
        lo += 64
    return (layout.DATA_START + lo + layout.TAIL_RESERVE) * SECTOR


def create_blank(path, size_bytes):
    """Create a sparse image of ``size_bytes`` (rounded down to whole 512-byte
    sectors). Refuses an existing path (CardExists) and anything smaller than
    :func:`min_card_bytes` (CardTooSmall). Returns the inspect() of it."""
    size = int(size_bytes) // SECTOR * SECTOR
    if size < min_card_bytes():
        raise CardTooSmall("%d bytes is smaller than the %d a card needs"
                           % (size, min_card_bytes()), size=size,
                           minimum=min_card_bytes())
    try:
        f = open(path, "xb")
    except FileExistsError:
        raise CardExists("%s already exists" % path)
    with f:
        f.truncate(size)
    return inspect(path)


# ----------------------------------------------------------------------------
# Inspect
# ----------------------------------------------------------------------------
def _partitions(mbr):
    if mbr[510:512] != b"\x55\xaa":
        return None
    parts = []
    for i, off in enumerate((446, 462, 478, 494)):
        ptype = mbr[off + 4]
        start, count = struct.unpack_from("<II", mbr, off + 8)
        if ptype or start or count:
            parts.append({"index": i + 1, "type": ptype, "start": start,
                          "sectors": count})
    return parts


def _layout_facts(parts, size):
    """(firmware partition, data partition, problems) for an MBR's entries."""
    problems = []
    fw = next((p for p in parts if p["type"] == 0x00 and p["sectors"]), None)
    data = next((p for p in parts if p["type"] in _target.FAT_TYPES), None)
    if fw is None:
        problems.append("no firmware partition (type 0x00)")
    elif fw["start"] != layout.FW_START or fw["sectors"] < layout.FW_BLOCKS:
        problems.append("firmware partition at sector %d, %d sectors "
                        "(flashpod writes %d, %d)" % (
                            fw["start"], fw["sectors"], layout.FW_START,
                            layout.FW_BLOCKS))
    if data is None:
        problems.append("no FAT32 data partition (type 0x0b/0x0c)")
    else:
        end = data["start"] + data["sectors"]
        total = size // SECTOR
        if end > total:
            problems.append("data partition runs past the end of the image")
        elif total - end < layout.TAIL_RESERVE:
            problems.append("data partition leaves less than the %d-sector "
                            "tail reserve (some iPods then demand a sync)"
                            % layout.TAIL_RESERVE)
    return fw, data, problems


def _manifest_firmwares():
    try:
        return _catalog.load_manifest().get("firmwares", [])
    except FlashpodError:
        return []


def inspect(path):
    """Everything a card page shows, read-only (shared lock). CardInfo:

    * ``path``, ``size_bytes``
    * ``partition_table``: "mbr" or None; ``partitions``: MBR entries
    * ``layout_ok``: firmware + FAT32 partitions where flashpod puts them;
      ``problems``: what's off, in words
    * ``firmware``: :func:`flashpod.firmware.describe_installed`, or
      ``{"present": False, "reason": ...}``
    * ``filesystem``: ``{"type": "FAT32", "label", "total_bytes",
      "free_bytes", "cluster_bytes"}`` or ``{"error": ...}`` or None
    * ``itunesdb``: ``{"state": "absent"|"corrupt"|"ok", ...}`` -- for ok:
      ``name``, ``tracks``, ``artists``, ``albums``, ``db_bytes``, and
      ``ceiling``: ``kind`` "bytes" (``limit`` and ``headroom`` in bytes;
      ``limit`` None when FLASHPOD_DB_LIMIT=0 disables it) or "tracks" when
      the card's firmware has the library-leak fix (flashpod 1.6):
      ``limit`` 32,000 tracks, ``headroom`` tracks
    * ``music_files``: files under iPod_Control/Music/F##; ``orphans``: of
      those, files no track points at; ``missing``: tracks whose file isn't
      there
    * ``past_128gib``: the data partition reaches past LBA28, which stock
      1.x firmware can't address
    * ``ipod_compatible``: layout_ok and a readable FAT32 data partition;
      ``ready``: also firmware present (checksum ok) and an ok iTunesDB
    """
    with _Image(path) as img:
        size = img.size
        info = CardInfo(path=path, size_bytes=size, partition_table=None,
                        partitions=[], layout_ok=False, problems=[],
                        firmware={"present": False, "reason": "no layout"},
                        filesystem=None, itunesdb={"state": "absent"},
                        music_files=0, orphans=[], missing=[],
                        past_128gib=False, ipod_compatible=False, ready=False)
        if size < SECTOR:
            info.problems.append("empty image")
            return info
        parts = _partitions(img.read(0, SECTOR))
        if parts is None:
            info.problems.append("no partition table (blank card?)")
            return info
        info.partition_table = "mbr"
        info.partitions = parts
        fw, data, problems = _layout_facts(parts, size)
        info.problems.extend(problems)
        info.layout_ok = not problems
        if fw is not None:
            base = fw["start"] * SECTOR
            limit = fw["sectors"] * SECTOR

            def read_fw(off, n, _base=base, _limit=limit):
                if off < 0 or off >= _limit:
                    return b""
                return img.read(_base + off, min(n, _limit - off))
            info.firmware = _firmware.describe_installed(
                read_fw, _manifest_firmwares())
        if data is not None:
            info.past_128gib = data["start"] + data["sectors"] > layout.LBA28_MAX
            try:
                fs = img.fat()
                cb = fs.sectors_per_cluster * SECTOR
                try:
                    free = fs.free_bytes()
                except OSError:
                    free = None
                info.filesystem = {"type": "FAT32",
                                   "label": _volume_label(img, data),
                                   "total_bytes": fs.clusters * cb,
                                   "free_bytes": free, "cluster_bytes": cb}
            except NotACard as exc:
                info.filesystem = {"error": str(exc)}
                fs = None
            if fs is not None:
                _inspect_library(info, _target.RawTarget(fs, path))
        info.ipod_compatible = bool(info.layout_ok and info.filesystem
                                    and "error" not in info.filesystem)
        info.ready = bool(info.ipod_compatible
                          and info.firmware.get("present")
                          and info.firmware.get("osos", {}).get("checksum_ok")
                          and info.itunesdb.get("state") == "ok")
        return info


def _volume_label(img, data):
    boot = img.read(data["start"] * SECTOR, SECTOR)
    return boot[71:82].decode("latin-1").rstrip() or None


def _inspect_library(info, tgt):
    fs = tgt.fs
    try:
        if not fs.exists("iPod_Control"):
            info.itunesdb = {"state": "absent", "reason": "no iPod_Control"}
            return
        raw = fs.read_file(DB_PATH)
        on_card = {loc for _rel, loc in tgt.iter_music_files()}
    except (OSError, ValueError) as exc:
        info.itunesdb = {"state": "unreadable", "error": str(exc)}
        return
    info.music_files = len(on_card)
    if raw is None:
        info.itunesdb = {"state": "absent", "reason": "no iTunesDB"}
        info.orphans = sorted(on_card)
        return
    try:
        lib = itunesdb.parse_bytes(raw)
    except (ValueError, IndexError, struct.error) as exc:
        info.itunesdb = {"state": "corrupt", "error": str(exc),
                         "db_bytes": len(raw)}
        return
    limit = library_mod._db_size_limit() or None
    ntracks, nartists, nalbums = library_mod.counts(lib)
    ceiling = {"kind": "bytes", "limit": limit,
               "headroom": (limit - len(raw)) if limit else None}
    if library_mod.LIBRARY_FIX in ((info.firmware or {}).get("fixes") or []) \
            and "FLASHPOD_DB_LIMIT" not in os.environ:
        # the card's firmware has the library-leak fix: a track cap instead
        cap = library_mod.IPOD_PATCHED_TRACK_LIMIT
        ceiling = {"kind": "tracks", "limit": cap, "headroom": cap - ntracks}
    info.itunesdb = {
        "state": "ok", "name": lib.name, "tracks": ntracks,
        "artists": nartists, "albums": nalbums,
        "db_bytes": len(raw), "ceiling": ceiling}
    in_db = {t.location for t in lib.tracks}
    info.orphans = sorted(on_card - in_db)
    info.missing = sorted(l for l in in_db if l and l not in on_card)


# ----------------------------------------------------------------------------
# Flash, format, firmware
# ----------------------------------------------------------------------------
def _firmware_bytes(firmware):
    """Install-ready firmware from a path (raw, .bin.gz, .ipsw) or bytes."""
    try:
        if isinstance(firmware, (bytes, bytearray)):
            fw = _firmware.fixup_directories(bytes(firmware))
            _firmware.validate_firmware(fw)
            return fw
        return _firmware.load_firmware(firmware)
    except (OSError, ValueError, StopIteration) as exc:
        raise FirmwareInvalid("firmware error: %s" % exc,
                              reason=str(exc))


def _check_formattable(data_blocks):
    spc = layout.fat_sectors_per_cluster(data_blocks)
    geo = fat32.geometry(data_blocks, spc)
    if geo["clusters"] < fat32.FAT32_MIN_CLUSTERS:
        raise CardTooSmall("data partition too small for FAT32 (%d clusters)"
                           % geo["clusters"], minimum=min_card_bytes())


def flash(path, firmware, lba48=False, max_data_gb=None, format=True,
          arm=False):
    """Lay the card out from scratch -- MBR, firmware partition, FAT32 data
    partition -- exactly as ``flashpod flash IMAGE --yes`` does (same bytes),
    then verify the firmware region. ERASES the image. ``firmware`` is a
    path (raw / .bin.gz / .ipsw) or bytes. ``arm`` arms the disk-mode update
    as ``flash --arm`` does (FirmwareInvalid, reason ``cannot_arm``, unless
    the firmware's disk-mode program is a known one). Returns a Result with
    ``firmware_bytes``, ``data_start``, ``data_sectors``, ``clusters``,
    ``cluster_bytes`` (None when ``format=False``)."""
    fw = _firmware_bytes(firmware)
    if arm:
        try:
            fw = _firmware.arm_firmware(fw)
        except ValueError as exc:
            raise FirmwareInvalid(str(exc), reason="cannot_arm")
    max_data_sectors = int(max_data_gb * 1000000000 // SECTOR) \
        if max_data_gb else None
    with _Image(path, write=True) as img:
        total = img.size // SECTOR
        try:
            header, data_start, data_blocks = layout.build_mbr_layout(
                total, lba48, max_data_sectors)
        except ValueError as exc:
            raise CardTooSmall(str(exc), minimum=min_card_bytes())
        if format:
            _check_formattable(data_blocks)
        f = img.f
        # the same sequence `flashpod flash` runs (ipod_flash.write_layout)
        done = layout.write_card(f, fw, total, lba48, max_data_sectors,
                                 format)
        if isinstance(done["format"], Exception):
            raise done["format"]        # _check_formattable makes this rare
        geo, spc = done["format"] if format else (None, None)
        f.flush()
        layout.verify_region(f, fw)
        layout.write_mbr(f, header)
    return Result(firmware_bytes=len(fw), data_start=data_start,
                  data_sectors=data_blocks,
                  clusters=geo["clusters"] if geo else None,
                  cluster_bytes=spc * SECTOR if spc else None)


def flash_firmware(path, firmware):
    """Replace ONLY the firmware partition, keeping the music: the card must
    already have flashpod's layout (LayoutMismatch otherwise). The firmware
    region ends up byte-identical to a fresh :func:`flash` with the same
    firmware; the MBR and the data partition are not touched."""
    fw = _firmware_bytes(firmware)
    with _Image(path, write=True) as img:
        parts = _partitions(img.read(0, SECTOR))
        if parts is None:
            raise LayoutMismatch("no partition table", reason="no_mbr")
        fwp, data, problems = _layout_facts(parts, img.size)
        if fwp is None or fwp["start"] != layout.FW_START \
                or fwp["sectors"] != layout.FW_BLOCKS \
                or data is None or data["start"] < layout.DATA_START:
            raise LayoutMismatch("not flashpod's layout: %s"
                                 % ("; ".join(problems) or "partitions differ"),
                                 reason="layout", problems=problems)
        f = img.f
        # what a fresh flash leaves in the firmware partition: zeros, then fw
        f.seek(layout.FW_START * SECTOR)
        f.write(b"\x00" * (layout.FW_BLOCKS * SECTOR))
        f.seek(layout.FW_START * SECTOR)
        f.write(fw)
        f.flush()
        layout.verify_region(f, fw)
    return Result(firmware_bytes=len(fw))


def format_data(path):
    """A fresh FAT32 on the data partition (ERASES the music and the
    iTunesDB); the firmware partition and the MBR are untouched. Returns a
    Result with ``clusters`` and ``cluster_bytes``."""
    with _Image(path, write=True) as img:
        parts = _partitions(img.read(0, SECTOR))
        if parts is None:
            raise NotACard("no partition table", reason="no_mbr")
        _fw, data, _p = _layout_facts(parts, img.size)
        if data is None:
            raise NotACard("no FAT32 data partition", reason="no_fat_partition")
        _check_formattable(data["sectors"])
        geo, spc = layout.format_partition(img.f, data["start"],
                                           data["sectors"])
    return Result(clusters=geo["clusters"], cluster_bytes=spc * SECTOR)


def firmware_catalog():
    """The stock firmware flashpod offers: the manifest's ``firmwares``, each
    with ``local`` (a verified offline copy's path, or None)."""
    out = []
    for e in _manifest_firmwares():
        d = dict(e)
        d["local"] = _catalog.local_path(e)
        out.append(d)
    return out


def fetch_firmware(entry, progress=None):
    """Local path of a catalog entry (a dict from firmware_catalog(), or its
    ``file`` name), downloading and verifying it if need be.
    ``progress(done, total)`` during a download."""
    if isinstance(entry, str):
        entry = next((e for e in _manifest_firmwares() if e["file"] == entry),
                     None)
        if entry is None:
            raise _catalog.FirmwareUnavailable("no such catalog entry",
                                               reason="unknown")
    return _catalog.fetch(entry, progress=progress)


# ----------------------------------------------------------------------------
# Targets: an image path, or a target object the caller opened
# ----------------------------------------------------------------------------
class _Given(object):
    """Context wrapper for a target the caller opened (a raw device or a
    mount, from the CLI): no locking, no closing -- the caller owns it."""

    def __init__(self, tgt):
        self.tgt = tgt

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def target(self):
        return self.tgt


def _open(card, write=False):
    """``card`` is an image path (opened and locked here) or a target object
    with RawTarget's interface (target.RawTarget over a raw device, or
    target.MountTarget over an OS mount), which the caller opened and owns."""
    if hasattr(card, "load_library"):
        return _Given(card)
    return _Image(card, write=write)


def open_target(path, write=False):
    """Open an image file as a target for several calls in a row, locked
    (shared to read, exclusive to write) until the returned context exits:
    ``with card.open_target(p, write=True) as img: card.add(img.target(), ...)``.
    """
    return _Image(path, write=write)


# ----------------------------------------------------------------------------
# The iTunesDB
# ----------------------------------------------------------------------------
def _load(tgt):
    """The card's Library; NotInitialized / CorruptLibrary otherwise."""
    lib = tgt.load_library()            # raises CorruptLibrary itself
    if lib is None:
        raise NotInitialized("no iTunesDB on the card (init it first)")
    return lib


def library(card):
    """The card's parsed iTunesDB (an itunesdb.Library). NotInitialized /
    CorruptLibrary when there is none / it won't parse."""
    with _open(card) as c:
        return _load(c.target())


def init(card, name="iPod", overwrite=False):
    """Create iPod_Control and an empty iTunesDB. Refuses a card that already
    has an iTunesDB (AlreadyInitialized) unless ``overwrite`` -- which, as
    `flashpod init` does, replaces the database but not the music files."""
    with _open(card, write=True) as c:
        tgt = c.target()
        if not overwrite:
            try:
                existing = tgt.load_library()
            except CorruptLibrary:
                existing = True
            if existing is not None:
                raise AlreadyInitialized("the card already has an iTunesDB")
        tgt.init_structure(name)
    return Result(name=name)


def tracks(card):
    """``{"name": library name, "tracks": [track_dict, ...]}``."""
    lib = library(card)
    return {"name": lib.name, "tracks": [track_dict(t) for t in lib.tracks]}


def rebuild(card, name="iPod", progress=None, report=None, save=True):
    """Rebuild the iTunesDB from the music files on the card (their tags), as
    `flashpod rebuild` does, and save it (``save=False``: just return it, for
    a caller that adds to it first). ``progress(stage, done, total, item)``
    with stage "rebuild"; ``report(msg)`` for each unreadable file. Returns a
    Result with ``tracks``, ``skipped`` and the Library as ``_library``."""
    skipped = []

    def say(msg):
        skipped.append(msg)
        if report:
            report(msg)
    with _open(card, write=True) as c:
        tgt = c.target()
        lib = tgt.rebuild_library(
            name,
            progress=(lambda nr, total, rp: progress("rebuild", nr, total, rp))
            if progress else None,
            report=say)
        if save:
            tgt.save_library(lib)
    return Result(tracks=len(lib.tracks), skipped=skipped, _library=lib)


def remove(card, ids=None, artist=None, album=None, what=None,
           on_removed=None, on_error=None):
    """Delete tracks (their files and their entries) and save the database
    once. Give ``ids`` (a list), or ``artist`` / ``album`` (case-insensitive
    exact name), or ``what`` in the CLI's form (ids, or ["artist"|"album",
    word, ...]). NoSuchTrack (nothing removed) if the selection doesn't match.
    ``on_removed(track)`` after each; ``on_error(track, exc)`` when its file
    can't be deleted (the entry is still dropped) -- without it the OSError
    propagates. Returns the removed tracks as dicts."""
    if what is None:
        if ids is not None:
            what = list(ids)
        elif artist is not None:
            what = ["artist", artist]
        elif album is not None:
            what = ["album", album]
        else:
            raise ValueError("remove needs ids, artist, album or what")
    with _open(card, write=True) as c:
        tgt = c.target()
        lib = _load(tgt)
        victims = library_mod.select_for_removal(lib, what)
        for t in victims:
            try:
                tgt.remove_location(t.location)
            except OSError as exc:
                if on_error is None:
                    raise
                on_error(t, exc)
            lib.tracks.remove(t)
            if on_removed:
                on_removed(t)
        tgt.save_library(lib)
    return [track_dict(t) for t in victims]


# ----------------------------------------------------------------------------
# Add: plan, decide, apply
# ----------------------------------------------------------------------------
def _prefix_fit(pending, budget):
    n, used = 0, 0
    for _, t in pending:
        used += t.size or 0
        if used > budget:
            break
        n += 1
    return n


def _fingerprint(lib, new_tracks):
    return hashlib.sha256(repr(
        (len(lib.tracks), sorted(track_key(t) for t in lib.tracks),
         [track_key(t) for t in new_tracks])).encode()).hexdigest()


def plan_add(card, files, progress=None, cancelled=None, report=None,
             on_duplicate=None, lib=None, expand_dirs=True, assess=True):
    """What :func:`add` would do with ``files`` (paths and/or directories),
    without writing anything. AddPlan fields:

    * ``files``: audio files found; ``new``: the tracks that would be added
      (track dicts + ``path``), in order; ``duplicates``: paths already on
      the card; ``unreadable``: messages for files that aren't usable audio
      (``failed`` has their paths); ``warnings``
    * ``batch_bytes``, ``free_bytes`` (None if unknown; ``free_error`` says
      why), ``budget`` (free minus headroom)
    * ``space_fit``: how many of ``new`` (in order) fit the free space
    * ``db``: ``limit`` (bytes, None if disabled), ``projected`` size,
      ``fit`` (how many fit under the limit)
    * ``fits`` = min(space_fit, db fit); ``fits_all``
    * ``fingerprint``: pass the plan to add(plan=...) and it refuses
      (PlanStale) if the card or the selection changed in between.

    For a frontend that asks its own questions (the CLI): ``report(msg)`` and
    ``on_duplicate(nr, total, path)`` see each file as it is read (progress
    stage "tags" announces it); ``lib`` supplies an already-loaded (e.g.
    just rebuilt) library; ``expand_dirs=False`` takes ``files`` as already
    expanded; ``assess=False`` leaves out the space/DB arithmetic (the
    caller decides after trimming). The (path, Track) pairs are kept on the
    plan for :func:`apply_add` (not in to_dict)."""
    warnings = []
    if expand_dirs:
        paths = expand(files, report=lambda m: (warnings.append(m),
                                                report and report(m)))
    else:
        paths = list(files)
    if not paths:
        raise NothingToAdd("no audio files in that selection",
                           warnings=warnings)
    unreadable = []

    def unusable(msg):
        unreadable.append(msg)
        if report:
            report(msg)
    with _open(card) as c:
        tgt = c.target()
        if lib is None:
            lib = _load(tgt)
        pending, failed, dups = library_mod.read_batch(
            lib, paths,
            on_reading=(lambda nr, n, p: progress("tags", nr, n, p))
            if progress else None,
            report=unusable, on_duplicate=on_duplicate, cancelled=cancelled)
        free = free_error = None
        if pending:
            try:
                free = tgt.free_bytes()
            except OSError as exc:
                free_error = str(exc)
        # while the card is open: past the byte limit this reads its firmware
        # (memoized, so add() reuses the answer instead of reading again)
        fixed = library_mod.library_fix_probe(tgt)
        db = library_mod.ceiling(lib, [t for _, t in pending], fixed) \
            if assess and pending else None
    budget = free - library_mod._ADD_HEADROOM if free is not None else None
    batch = library_mod.batch_bytes(pending)
    new_tracks = [t for _, t in pending]
    plan = AddPlan(
        files=len(paths), new=[dict(track_dict(t), path=p) for p, t in pending],
        duplicates=dups, unreadable=unreadable, failed=failed,
        warnings=warnings, batch_bytes=batch, free_bytes=free,
        free_error=free_error, budget=budget,
        cancelled=bool(cancelled and cancelled()),
        _items=pending, _lib=lib, _fixed=fixed)
    if assess:
        space_fit = len(pending) if budget is None or batch <= budget \
            else _prefix_fit(pending, budget)
        limit, projected, db_fit = db if db is not None \
            else (library_mod._db_size_limit(), None, 0)
        plan.space_fit = space_fit
        plan.db = {"limit": limit or None, "projected": projected,
                   "fit": db_fit,
                   "kind": db.kind if db is not None else "bytes",
                   "library_fix": db.firmware if db is not None else None}
        plan.fits = min(space_fit, db_fit)
        plan.fits_all = plan.fits == len(pending)
        plan.fingerprint = _fingerprint(lib, new_tracks)
    return plan


def apply_add(card, plan, keep=None, save_anyway=False, on_start=None,
              on_progress=None, on_copied=None, on_failed=None,
              progress=None, cancelled=None):
    """Copy ``keep`` (a list of the plan's (path, Track) pairs; default all
    of them) onto the card and save the database ONCE -- or not at all when
    nothing was copied, unless ``save_anyway`` (a just-rebuilt library the
    user accepted). Callbacks as in library.copy_batch; ``progress`` gets
    stages "copy" (files) and "write" (bytes). Returns an AddReport."""
    items = plan._items if keep is None else keep
    lib = plan._lib
    start = time.monotonic()

    def _start(nr, n, t):
        if progress:
            progress("copy", nr, n, t.title)
        if on_start:
            on_start(nr, n, t)

    def _progress_for(nr, n, t):
        if on_progress:
            return on_progress(nr, n, t)
        if progress:
            return lambda done, total: progress("write", done, total, t.title)
        return None
    with _open(card, write=True) as c:
        tgt = c.target()
        copied, failed = library_mod.copy_batch(
            lib, items, tgt.copy, on_start=_start, on_progress=_progress_for,
            on_copied=on_copied, on_failed=on_failed, cancelled=cancelled)
        if copied or save_anyway:
            tgt.save_library(lib)
    return AddReport(
        added=[track_dict(t) for t in copied], duplicates=plan.duplicates,
        unreadable=plan.unreadable,
        failed=[{"path": p, "error": str(e)} for p, e in failed],
        dropped_space=0, dropped_ceiling=0,
        cancelled=bool(cancelled and cancelled())
        and len(copied) + len(failed) < len(items),
        bytes=sum(t.size or 0 for t in copied),
        seconds=round(time.monotonic() - start, 3), _copied=copied,
        _failed=failed)


DEFAULT_POLICY = {"full": "stop", "ceiling": "refuse"}


def add(card, files, policy=None, plan=None, progress=None, cancelled=None):
    """Add ``files`` to the card in one database write, deciding by
    ``policy`` where the CLI would ask:

    * ``full``: "stop" (default: raise NoSpace if the batch doesn't fit the
      free space) or "fit" (add the leading tracks that fit);
    * ``ceiling``: "refuse" (default: raise DbCeiling past the iTunesDB size
      limit), "fit" (add the leading tracks under it) or "force" (add all;
      the firmware may then show NO songs).

    The order is the CLI's: trim for space first, then check the database
    size of what is left. With ``plan`` (from plan_add), the batch must still
    be that plan (PlanStale otherwise). Raises NotInitialized /
    CorruptLibrary for a card without a usable database. Returns an
    AddReport: ``added`` (track dicts), ``duplicates``, ``unreadable``,
    ``failed`` (copy errors), ``dropped_space``, ``dropped_ceiling``,
    ``cancelled``, ``bytes``, ``seconds``."""
    pol = dict(DEFAULT_POLICY, **(policy or {}))
    start = time.monotonic()
    with _open(card, write=True) as c:
        tgt = c.target()
        now = plan_add(tgt, files, progress=progress, cancelled=cancelled)
        if now.cancelled:               # during tag reading: nothing written
            return AddReport(added=[], duplicates=now.duplicates,
                             unreadable=now.unreadable, failed=[],
                             dropped_space=0, dropped_ceiling=0,
                             cancelled=True, bytes=0,
                             seconds=round(time.monotonic() - start, 3))
        if plan is not None and plan.fingerprint != now.fingerprint:
            raise PlanStale("the card or the selection changed since the plan")
        items = list(now._items)
        dropped_space = dropped_ceiling = 0
        if now.space_fit < len(items):
            if pol["full"] != "fit":
                raise NoSpace("the batch needs %d bytes; %d free"
                              % (now.batch_bytes, now.budget or 0),
                              batch_bytes=now.batch_bytes, budget=now.budget,
                              fit=now.space_fit)
            dropped_space = len(items) - now.space_fit
            items = items[:now.space_fit]
        if items:
            db = library_mod.ceiling(now._lib, [t for _, t in items],
                                     now._fixed)
            limit, projected, fit = db
            if fit < len(items):
                if pol["ceiling"] == "refuse" or (pol["ceiling"] == "fit"
                                                  and fit <= 0):
                    if db.kind == "tracks":
                        msg = ("the library would hold %d tracks, past the "
                               "%d the firmware lists"
                               % (len(now._lib.tracks) + len(items), limit))
                    else:
                        msg = ("the iTunesDB would be %d bytes, past the "
                               "%d-byte limit" % (projected, limit))
                    raise DbCeiling(msg, limit=limit, projected=projected,
                                    fit=fit, kind=db.kind)
                if pol["ceiling"] == "fit":
                    dropped_ceiling = len(items) - fit
                    items = items[:fit]
        rep = apply_add(tgt, now, keep=items, progress=progress,
                        cancelled=cancelled)
    rep.dropped_space = dropped_space
    rep.dropped_ceiling = dropped_ceiling
    rep.seconds = round(time.monotonic() - start, 3)
    return rep
