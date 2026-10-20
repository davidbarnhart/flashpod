#!/usr/bin/env python3
"""Three tag-reading bugs, fixed in 0.5.2.

    PYTHONPATH=. python3 scripts/test_tag_fixes.py

Found 2026-10-20 while porting tag reading to Rust:
  * adding an .aac (ADTS) file failed outright: mutagen's bitrate for it is
    a float, and the iTunesDB writer raised struct.error;
  * WAV and AIFF files lost their tags: mutagen hands back a plain ID3 tag
    for them even with easy=True, and its frame-id keys never matched
    "title", so the title was the file name and the rest empty;
  * one file mutagen can't read made a rebuild fail with mutagen's own
    exception, instead of being skipped like a bad file on an add.

On synthetic cards and files. Against 0.5.1, every check fails except the
untagged-WAV guard.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mutagen.id3 as id3  # noqa: E402
from mutagen.aiff import AIFF  # noqa: E402
from mutagen.wave import WAVE  # noqa: E402

import synth_fixtures as synth  # noqa: E402
from flashpod import card  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def ff(out, *args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1"] + list(args) + [out],
                   check=True, stdin=subprocess.DEVNULL)
    return out


def fresh(tmp, name):
    img = os.path.join(tmp, name)
    card.create_blank(img, 96 << 20)
    card.flash(img, synth.write_firmware(os.path.join(tmp, "fw.bin")))
    card.init(img)
    return img


def tagged(path, cls):
    f = cls(path)
    f.add_tags()
    f.tags.add(id3.TIT2(encoding=3, text="ID3 title"))
    f.tags.add(id3.TPE1(encoding=3, text="ID3 artist"))
    f.tags.add(id3.TCON(encoding=3, text="(17)"))
    f.tags.add(id3.TRCK(encoding=3, text="4/9"))
    f.save()
    return path


def run(tmp):
    print("an .aac file")
    aac = ff(os.path.join(tmp, "song.aac"), "-c:a", "aac", "-b:a", "128k")
    img = fresh(tmp, "aac.img")
    try:
        added = card.add(img, [aac]).to_dict()["added"]
    except Exception as exc:                       # noqa: BLE001
        added = []
        print("    add raised %s: %s" % (type(exc).__name__, exc))
    t = card.tracks(img)["tracks"]
    check(len(added) == 1 and len(t) == 1, "it is added")
    check(t and isinstance(t[0]["bitrate"], int) and 100 < t[0]["bitrate"] < 160,
          "with its bitrate in kbps: %r" % (t[0]["bitrate"] if t else None))

    print("WAV and AIFF tags")
    wav = tagged(ff(os.path.join(tmp, "wavfile.wav"), "-c:a", "pcm_s16le"), WAVE)
    aif = tagged(ff(os.path.join(tmp, "aiffile.aiff"), "-c:a", "pcm_s16be"), AIFF)
    bare = ff(os.path.join(tmp, "untagged.wav"), "-c:a", "pcm_s16le")
    img = fresh(tmp, "wav.img")
    card.add(img, [wav, aif, bare])
    got = {t["title"]: t for t in card.tracks(img)["tracks"]}
    want = {"title": "ID3 title", "artist": "ID3 artist", "genre": "Rock",
            "track_nr": 4}
    for kind in ("WAV", "AIFF"):
        t = [t for t in card.tracks(img)["tracks"]
             if t["location"].lower().endswith(".wav" if kind == "WAV" else ".aiff")
             and t["title"] != "untagged"]
        t = t[0] if t else {}
        check(all(t.get(k) == v for k, v in want.items()),
              "%s keeps title, artist, genre, track: %r"
              % (kind, {k: t.get(k) for k in want}))
    check("untagged" in got, "an untagged WAV still takes its file name")

    print("a rebuild past a file mutagen can't read")
    img = fresh(tmp, "rebuild.img")
    card.add(img, [synth.make_music(os.path.join(tmp, "music"))])
    n = len(card.tracks(img)["tracks"])
    with card.open_target(img, write=True) as c:
        c.target().fs.write_file("iPod_Control/Music/F00/broken.mp3", b"\0" * 5000)
    try:
        r = card.rebuild(img).to_dict()
    except Exception as exc:                       # noqa: BLE001
        r = None
        print("    rebuild raised %s: %s" % (type(exc).__name__, exc))
    check(r is not None and r["tracks"] == n, "the rebuild keeps the %d good tracks" % n)
    check(r is not None and any("broken.mp3" in m for m in r["skipped"]),
          "and names the bad file: %r" % (r and r["skipped"]))


def main():
    run(synth.scratch_dir("fp-tagfix-"))
    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
