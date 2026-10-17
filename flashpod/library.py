"""Library operations shared by every frontend: the add batch (tag reading,
dedup, free-space and database-ceiling arithmetic, the copy loop) and
track selection for removal.

Nothing here prints or prompts. Where the CLI shows progress or asks a
question, these functions take callbacks or return the numbers the question
is about; :mod:`flashpod.card` drives the same functions with a policy.
"""

import os
import re
import struct

from . import itunesdb
from .errors import NoSuchTrack
from .tracks import make_track, track_key

# Leave a little slack free so the iTunesDB rewrite and FAT cluster overhead
# don't tip a "just fits" batch over the edge.
_ADD_HEADROOM = 2 << 20


#: Serialized-iTunesDB byte budget the target iPod's firmware can load.
#: Measured on real 1G hardware (2026-07-29): 3,776,000 bytes loads and
#: plays; 3,776,684 makes the iPod show an EMPTY library (no error).
#: Bisected to a one-track edge, then pinned to bytes — not track or
#: record count — with slim/fat DB experiments (scripts/db_slice.py).
#: Default keeps a safety margin under the proven-good point.
#: FLASHPOD_DB_LIMIT overrides; 0 disables the check.
IPOD_DB_SIZE_LIMIT = 3600000


def _db_size_limit():
    env = os.environ.get("FLASHPOD_DB_LIMIT")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            pass
    return IPOD_DB_SIZE_LIMIT


def _projected_db_size(lib, new_tracks):
    """Serialized iTunesDB size after adding ``new_tracks`` to ``lib``."""
    tmp = itunesdb.Library(lib.name)
    tmp.tracks = list(lib.tracks) + list(new_tracks)
    return len(itunesdb.serialize(tmp))


def _fit_count(lib, new_tracks, limit):
    """How many of ``new_tracks`` (in order) fit under the DB byte limit.
    0 when the existing library alone is already over it."""
    if _projected_db_size(lib, []) > limit:
        return 0
    lo, hi = 0, len(new_tracks)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _projected_db_size(lib, new_tracks[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


#: The title a rebuild gives an untagged track: the name flashpod gave its
#: file on the card (target.RawTarget.copy / itunesdb.copy_to_ipod).
_REBUILT_TITLE = re.compile(r"fp\d{6}$")


def read_batch(lib, files, on_reading=None, report=None, on_duplicate=None,
               cancelled=None):
    """Pass 1 of an add: read every file's tags and drop the ones already on
    the iPod (by :func:`track_key`, including duplicates within the batch),
    so the batch's true size is known before a byte is copied.

    Callbacks, in the order the CLI shows them for each file:
    ``on_reading(nr, total, path)`` before its tags are read;
    ``report(msg)`` with make_track's reason when it is unusable;
    ``on_duplicate(nr, total, path)`` when it is already on the iPod.
    ``cancelled()`` is checked before each file.

    Returns ``(pending, failed, skipped)``: ``pending`` is
    ``[(path, track), ...]`` in file order (tracks have no id or location
    yet), ``failed`` the paths that aren't usable audio, ``skipped`` the
    duplicates' paths."""
    seen = {track_key(t) for t in lib.tracks}
    # A rebuild names an UNTAGGED file after its card file ("fp123456"), so
    # its key no longer matches the original's ("untagged", from the source
    # file name) and re-adding the original copied it again. Such a track
    # matches on size + duration alone.
    rebuilt = {(t.size, t.tracklen) for t in lib.tracks
               if _REBUILT_TITLE.match(t.title or "")}
    nfiles = len(files)
    pending, failed, skipped = [], [], []
    for nr, path in enumerate(files, 1):
        if cancelled is not None and cancelled():
            break
        if on_reading:
            on_reading(nr, nfiles, path)
        track = make_track(lib, path, nr, nfiles, report=report)
        if not track:
            failed.append(path)
            continue
        key = track_key(track)
        if key in seen or (track.size, track.tracklen) in rebuilt:
            if on_duplicate:
                on_duplicate(nr, nfiles, path)
            skipped.append(path)
            continue
        seen.add(key)
        pending.append((path, track))
    return pending, failed, skipped


def batch_bytes(pending):
    """Total size of ``[(path, track), ...]``."""
    return sum(t.size or 0 for _, t in pending)


def copy_batch(lib, pending, copy, on_start=None, on_progress=None,
               on_copied=None, on_failed=None, cancelled=None):
    """Pass 2 of an add: copy each pending track onto the card and append it
    to ``lib`` (the caller saves the library once, afterwards).

    ``copy(path, progress) -> location`` is the backend's copy;
    ``on_start(nr, total, track)`` runs before each file and
    ``on_progress(nr, total, track)`` returns the per-byte progress callback
    handed to copy (or None); ``on_copied(nr, total, track)`` after a
    success; ``on_failed(nr, total, path, track, exc)`` when copy raises
    OSError. ``cancelled()`` is checked before each file: a cancelled batch
    stops there, and what was already copied stays in ``lib``, so saving it
    leaves a consistent card (no orphan files, no missing ones).

    Returns ``(added, failed)``: lists of tracks and of (path, exc)."""
    npend = len(pending)
    added, failed = [], []
    for nr, (path, track) in enumerate(pending, 1):
        if cancelled is not None and cancelled():
            break
        if on_start:
            on_start(nr, npend, track)
        progress = on_progress(nr, npend, track) if on_progress else None
        try:
            track.location = copy(path, progress)
        except OSError as exc:
            failed.append((path, exc))
            if on_failed:
                on_failed(nr, npend, path, track, exc)
            continue
        # Assign the id now that the track is actually being committed: each
        # next_track_id() sees the tracks appended earlier in this batch, so
        # the ids are unique. (Failed copies above are skipped and burn no id.)
        track.id = lib.next_track_id()
        lib.tracks.append(track)
        added.append(track)
        if on_copied:
            on_copied(nr, npend, track)
    return added, failed


def select_for_removal(lib, what):
    """The tracks ``rm`` deletes, from ``what``: a list of ids (ints or
    digit strings), or ``["artist"|"album", word, ...]`` (case-insensitive
    exact name; the words are joined with spaces).

    Raises NoSuchTrack with ``details["reason"]`` one of ``name_required``,
    ``no_match``, ``bad_ids`` (``details["field"]`` / ``["missing"]`` say
    more), so each frontend words it its own way."""
    if what and what[0] in ("artist", "album"):
        if len(what) < 2:
            raise NoSuchTrack("name required", reason="name_required",
                              field=what[0])
        name = " ".join(what[1:]).casefold()
        victims = [t for t in lib.tracks
                   if (getattr(t, what[0]) or "").casefold() == name]
        if not victims:
            raise NoSuchTrack("no tracks match that name", reason="no_match",
                              field=what[0], name=" ".join(what[1:]))
        return victims
    try:
        ids = [int(i) for i in what]
    except ValueError:
        raise NoSuchTrack("expected track ids", reason="bad_ids")
    by_id = {t.id: t for t in lib.tracks}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise NoSuchTrack("no track with id %s" % ", ".join(map(str, missing)),
                          reason="missing", missing=missing)
    return [by_id[i] for i in ids]


#: Tracks the 1G/2G firmware lists once it carries openpod's library-leak
#: fix (flashpod 1.6 does). With the leak gone the byte ceiling above no
#: longer applies; what's left is the firmware's playlist cap -- a
#: 33,000-track library boots and lists 32,000. Emulator-measured
#: (openpod-gen1 wiki 26); hardware-run to 31,958 songs (2026-10-16).
IPOD_PATCHED_TRACK_LIMIT = 32000

#: The openpod fix (firmware/fixes.json) that removes the byte ceiling.
LIBRARY_FIX = "library-leak"


class Ceiling(object):
    """The iTunesDB size check for an add (see :func:`ceiling`)."""

    def __init__(self, kind, limit, projected, fit, firmware):
        self.kind = kind            # "bytes", "tracks", or None (disabled)
        self.limit = limit          # bytes, or tracks when kind == "tracks"
        self.projected = projected  # serialized size in bytes (None: not run)
        self.fit = fit              # how many of the new tracks fit, in order
        self.firmware = firmware    # library fix on the card: True/False/None
                                    # (None: couldn't read it, or not asked)

    def __iter__(self):             # the old (limit, projected, fit) shape
        return iter((self.limit, self.projected, self.fit))


def ceiling(lib, new_tracks, fixed=None):
    """The iTunesDB size check for adding ``new_tracks`` to ``lib``.

    Stock 1G/2G firmware shows zero songs past ~3.78 MB of iTunesDB, so the
    limit is bytes (IPOD_DB_SIZE_LIMIT; FLASHPOD_DB_LIMIT overrides, 0
    disables). Firmware with openpod's library-leak fix has no byte ceiling,
    only the 32,000-track playlist cap. ``fixed`` is a () -> True/False/None
    that reads the card's firmware for the fix; it's called only when the
    projection passes the byte limit (reading can cost a sudo prompt and,
    over FireWire, time) and never when FLASHPOD_DB_LIMIT is set, which is
    taken at its word. Returns a :class:`Ceiling`; ``fit`` is all of
    ``new_tracks`` when they fit."""
    limit = _db_size_limit()
    n = len(new_tracks)
    if not limit:
        # disabled: don't even serialize (it draws random ids, and the CLI's
        # output has always been reproducible without that draw)
        return Ceiling(None, limit, None, n, None)
    projected = _projected_db_size(lib, new_tracks)
    if projected <= limit:
        return Ceiling("bytes", limit, projected, n, None)
    firmware = None
    if fixed is not None and "FLASHPOD_DB_LIMIT" not in os.environ:
        firmware = fixed()
    if firmware:
        cap = IPOD_PATCHED_TRACK_LIMIT
        fit = n if len(lib.tracks) + n <= cap \
            else max(0, cap - len(lib.tracks))
        return Ceiling("tracks", cap, projected, fit, True)
    return Ceiling("bytes", limit, projected,
                   _fit_count(lib, new_tracks, limit), firmware)


def library_fix_probe(target):
    """A memoized () -> True/False/None: does the firmware on ``target``'s
    card carry the library-leak fix? None when the target can't read its
    firmware partition, or it holds no recognizable firmware. Nothing is
    read until it's called, and then only once."""
    from . import firmware as _firmware
    memo = []

    def probe():
        if not memo:
            read = target.firmware_read() \
                if hasattr(target, "firmware_read") else None
            try:
                memo.append(_firmware.fix_on_card(read, LIBRARY_FIX)
                            if read else None)
            except (OSError, ValueError, struct.error):
                memo.append(None)
        return memo[0]
    return probe


def counts(lib):
    """``(tracks, artists, albums)`` the way `flashpod ls` and card.inspect
    count them: names case-insensitive, missing ones as "(unknown)", an album
    being an (artist, album) pair."""
    from .tracks import orunknown
    artists = {orunknown(t.artist).casefold() for t in lib.tracks}
    albums = {(orunknown(t.artist).casefold(), orunknown(t.album).casefold())
              for t in lib.tracks}
    return len(lib.tracks), len(artists), len(albums)
