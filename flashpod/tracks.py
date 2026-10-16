"""Audio files -> iTunesDB tracks: tag reading, batch expansion, dedup.

Shared by every frontend (the CLI, :mod:`flashpod.card`) and by both card
backends (an OS mount and the userspace FAT driver). Nothing here prints
unless the caller passes no ``report`` callback, in which case the old CLI
default (stderr) applies.
"""

import os
import sys

import mutagen
try:
    from mutagen.mp3 import EasyMP3            # mutagen >= ~1.20
except ImportError:                            # older layouts
    from mutagen.easymp3 import EasyMP3

from . import itunesdb

def first(tags, key):
    val = tags.get(key)
    if isinstance(val, list):
        val = val[0] if val else None
    return str(val) if val else None


AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff", ".flac"}


# Formats that need something other than flashpod's historical defaults. Only
# FLAC is listed: every other extension keeps the filetype string and mhit
# marker it has always been given, because existing libraries were built with
# them. See itunesdb.MARKER_MP3.
#
# `.flac` presents to the 1G firmware as "fla": its extension lookup is
# strrchr('.') then strncpy(..., 3), so the type is exactly three characters and
# `.fla` and `.flac` are the same file to it (openpod wiki 20.8.0).
FORMATS = {".flac": ("FLAC audio file", itunesdb.MARKER_FLAC)}


def orunknown(s):
    return s if s else "(unknown)"


def expand(paths, report=None):
    """Expand directories into sorted recursive lists of audio files.
    ``report(msg)`` receives the "no audio files" warning (default: stderr)."""
    report = report or (lambda msg: print(msg, file=sys.stderr))
    out = []
    for p in paths:
        if not os.path.isdir(p):
            out.append(p)
            continue
        found = []
        for root, dirs, files in os.walk(p):
            dirs.sort()
            for f in sorted(files):
                if f.startswith("._"):  # macOS AppleDouble junk
                    continue
                if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                    found.append(os.path.join(root, f))
        if not found:
            report(f"warning: no audio files found under {p}")
        out.extend(found)
    return out


def read_audio(path):
    """Open `path` for tag/info reading. MP3s — the common case — skip
    mutagen's format detection, which scores every handler and reads whole
    files in the process (its APEv2 scorer reads to EOF), making it painfully
    slow over network mounts. ``mutagen.File(easy=True)`` already returns an
    EasyMP3 for an MP3, so going straight to EasyMP3 yields the identical
    object minus the sniffing. Anything else — or an MP3 that won't parse,
    e.g. a misnamed file — falls back to full format detection."""
    if os.path.splitext(path)[1].lower() == ".mp3":
        try:
            return EasyMP3(path)
        except Exception:
            pass                      # mislabeled/corrupt — sniff it properly
    return mutagen.File(path, easy=True)


def read_audio_stream(fileobj, ext):
    """Like read_audio, but for a seekable file-like with no OS path (a lazy
    fatfs._FatFile during a rebuild). mutagen reads tags by SEEKING — it touches
    only the head and tail — so handing it a lazy FAT file means we pull a
    cluster or two off the device per track instead of the whole file. Same
    MP3 fast-path; mutagen accepts a file-like for any format."""
    if ext.lower() == ".mp3":
        try:
            return EasyMP3(fileobj)
        except Exception:
            fileobj.seek(0)           # reset for the full-detection fallback
    return mutagen.File(fileobj, easy=True)


def _track_from_audio(audio, fallback_title, size, ext=None):
    """Build an itunesdb.Track from an already-loaded mutagen audio object, a
    fallback title (used when untagged), and the file's byte size. Shared by
    make_track (tags from a path) and the rebuild path (tags from bytes). The
    unique track id is assigned by the caller at commit time, not here."""
    tags = audio.tags or {}
    t = itunesdb.Track()
    t.title = first(tags, "title") or fallback_title
    t.artist = first(tags, "artist")
    t.album = first(tags, "album")
    t.genre = first(tags, "genre")
    t.composer = first(tags, "composer")
    filetype, marker = FORMATS.get((ext or "").lower(),
                                   ("MPEG audio file", itunesdb.MARKER_MP3))
    t.filetype = filetype
    t.marker = marker
    tracknr = first(tags, "tracknumber")
    if tracknr and tracknr.split("/")[0].isdigit():
        t.track_nr = int(tracknr.split("/")[0])
    date = first(tags, "date")
    if date and date[:4].isdigit():
        t.year = int(date[:4])
    info = audio.info
    t.tracklen = int(info.length * 1000)
    # FLAC's StreamInfo carries no bitrate -- it is not a constant of the format
    # -- so derive the average one rather than report 0, which the iPod shows as
    # a blank in the track info.
    bitrate = getattr(info, "bitrate", 0)
    if not bitrate and info.length:
        bitrate = int(size * 8 / info.length)
    t.bitrate = bitrate // 1000
    t.samplerate = getattr(info, "sample_rate", 0)
    t.size = size
    return t


def _library_from_located(name, located):
    """Assemble a Library from [(location, Track), ...], assigning each track a
    unique id in order (next_track_id sees the tracks already appended). Shared
    by the raw and mount rebuild paths."""
    lib = itunesdb.Library(name)
    for location, t in located:
        t.location = location
        t.id = lib.next_track_id()
        lib.tracks.append(t)
    return lib


def make_track(lib, path, nr, total, report=None):
    """Read tags from `path` and build an itunesdb.Track (location unset).
    Reports the skip reason (default: stderr) and returns None on unusable
    files."""
    report = report or (lambda msg: print(msg, file=sys.stderr))
    try:
        audio = read_audio(path)
    except Exception as exc:
        report(f"[{nr}/{total}] skipping {path}: unreadable ({exc})")
        return None
    if audio is None:
        report(f"[{nr}/{total}] skipping {path}: not a recognized audio file")
        return None
    # NB: the unique track id is assigned at append time (cmd_add), not here.
    # During a batch the tracks aren't in lib.tracks yet, so next_track_id()
    # would hand every track in the batch the same id — which collapses them
    # to one entry in the iPod's playlist (all tracks show the first one's
    # name). _track_from_audio leaves t.id at its default until committed.
    return _track_from_audio(
        audio, os.path.splitext(os.path.basename(path))[0],
        os.path.getsize(path), os.path.splitext(path)[1])


def track_key(t):
    """Dedup identity: same byte length + duration + title. Catches the same
    file added twice (e.g. a single that is also present in its album folder)
    using only metadata already in the DB and the incoming file's tags — never
    reads the iPod's stored copies back (slow always, FireWire-fatal)."""
    return (t.size, t.tracklen, (t.title or "").strip().casefold())
