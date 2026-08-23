#!/usr/bin/env python3
"""Logic test for FLAC support in the library builder and the iTunesDB writer.

Why this exists: the 1G firmware can be patched to decode FLAC (openpod #38),
but the gen-1 UI is iTunesDB-driven -- there is no file browser -- so a FLAC on
the disk is invisible unless flashpod puts it in the database. Three things had
to be true and none was: `.flac` had to be an audio extension, the track needed
its own filetype string and mhit type marker, and FLAC's StreamInfo carries no
bitrate, so a track built from one reported 0 kbps.

The other half of the test is that NOTHING ELSE MOVED. Every other extension
still gets the filetype string and the "\\0MP3" marker flashpod has always
written, because libraries in the field were built with them and this is not
the change to discover what depends on that.

Synthesises its own FLAC with `flac` if present, otherwise skips the tag half
and still checks the marker/filetype wiring. Touches no device:

    PYTHONPATH=. python3 scripts/test_flac_track.py
"""
import os
import struct
import subprocess
import sys
import tempfile

sys.argv = ["flashpod"]
from flashpod import cli, itunesdb          # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def make_flac(tmp):
    """A second of tagged audio, or None if the `flac` encoder is absent."""
    raw = os.path.join(tmp, "a.raw")
    out = os.path.join(tmp, "a.flac")
    with open(raw, "wb") as fh:
        fh.write(b"".join(struct.pack("<hh", (i * 37) % 9001 - 4500,
                                      (i * 53) % 8001 - 4000)
                          for i in range(44100)))
    try:
        subprocess.run(["flac", "-s", "-f", "--force-raw-format",
                        "--endian=little", "--sign=signed", "--channels=2",
                        "--bps=16", "--sample-rate=44100",
                        "-T", "TITLE=A Title", "-T", "ARTIST=An Artist",
                        "-o", out, raw], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return out


print("flac track self-test")
check(".flac" in cli.AUDIO_EXTS, "'.flac' is an audio extension")
check(".mp3" in cli.AUDIO_EXTS and ".m4a" in cli.AUDIO_EXTS,
      "and the existing extensions are still there")

with tempfile.TemporaryDirectory() as tmp:
    path = make_flac(tmp)
    if path is None:
        print("  .... no `flac` encoder; skipping the tag half")
    else:
        audio = cli.read_audio(path)
        check(audio is not None, "mutagen reads a .flac (%s)"
              % type(audio).__name__)
        t = cli._track_from_audio(audio, "fallback", os.path.getsize(path),
                                  ".flac")
        check(t.title == "A Title" and t.artist == "An Artist",
              "its Vorbis comments become title and artist")
        check(t.filetype == "FLAC audio file", "filetype is %r" % t.filetype)
        check(t.marker == itunesdb.MARKER_FLAC, "the mhit marker is %r" % t.marker)
        check(t.samplerate == 44100, "the sample rate survives")
        check(t.bitrate > 0,
              "a bitrate is derived (%d kbps) -- FLAC stores none" % t.bitrate)

        # and the same audio, claimed as .mp3, must be untouched
        t2 = cli._track_from_audio(audio, "fallback", 1000, ".mp3")
        check(t2.filetype == "MPEG audio file" and t2.marker == itunesdb.MARKER_MP3,
              "an .mp3 still gets the historical filetype and marker")

        lib = itunesdb.Library("iPod")
        t.location = ":iPod_Control:Music:F00:a.flac"
        t.id = lib.next_track_id()
        lib.tracks.append(t)
        t2.title, t2.location = "An MP3", ":iPod_Control:Music:F00:b.mp3"
        t2.id = lib.next_track_id()
        lib.tracks.append(t2)
        blob = itunesdb.serialize(lib)
        back = itunesdb.parse_bytes(blob)
        check(len(back.tracks) == 2, "a mixed library round-trips")
        kinds = {tr.title: tr.filetype for tr in back.tracks}
        check(kinds.get("A Title") == "FLAC audio file"
              and kinds.get("An MP3") == "MPEG audio file",
              "each track keeps its own filetype through the writer and reader")
        i = blob.find(b"mhit")
        j = blob.find(b"mhit", i + 1)
        check(blob[i + 24:i + 28] == itunesdb.MARKER_FLAC
              and blob[j + 24:j + 28] == itunesdb.MARKER_MP3,
              "and the mhit markers are per-track in the bytes")

print("%d failed" % len(fails))
sys.exit(1 if fails else 0)
