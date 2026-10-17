#!/usr/bin/env python3
"""After a rebuild, re-adding an untagged song must not copy it again.

    PYTHONPATH=. python3 scripts/test_rebuild_dedup.py

Found 2026-10-16: `rebuild` titles an untagged track after its file on the
card ("fp123456"), while `add` titles it after the source file ("untagged").
The dedup key (size, duration, title) then differed, and re-adding the
original after a rebuild put a second copy on the card. A track whose title
is such a card file name now matches on size + duration alone.

On a synthetic card, through both frontends (flashpod.card and the CLI):
  * add a tagged MP3 and an untagged WAV, rebuild, add both again: nothing
    is added, both are duplicates;
  * a DIFFERENT untagged file is still added (the match isn't too loose);
  * without a rebuild, nothing changes (titles still match normally).
Against the tree before the fix the first check fails (1 track re-added).
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def fresh(tmp, name, fw):
    img = os.path.join(tmp, name)
    card.create_blank(img, 96 << 20)
    card.flash(img, fw)
    card.init(img)
    return img


def main():
    tmp = tempfile.mkdtemp(prefix="fp-rebuild-")
    fw = synth.write_firmware(os.path.join(tmp, "fw.bin"))
    music = synth.make_music(os.path.join(tmp, "music"))
    tagged = os.path.join(music, "Alpha Band", "First Album", "01 Opening.mp3")
    untagged = os.path.join(music, "Beta Group", "Only Album", "untagged.wav")
    other = os.path.join(tmp, "another.wav")
    synth._wav(other, 0.7, 500)

    print("flashpod.card")
    img = fresh(tmp, "a.img", fw)
    card.add(img, [tagged, untagged])
    card.rebuild(img)
    titles = sorted(t["title"] for t in card.tracks(img)["tracks"])
    check(titles[0] == "Opening" and titles[1].startswith("fp"),
          "rebuild: the untagged track is titled %r" % titles[1])
    rep = card.add(img, [tagged, untagged])
    check(not rep.added and len(rep.duplicates) == 2,
          "re-adding both after the rebuild adds nothing (%d added)"
          % len(rep.added))
    rep = card.add(img, [other])
    check(len(rep.added) == 1, "a different untagged file is still added")
    img2 = fresh(tmp, "b.img", fw)
    card.add(img2, [tagged, untagged])
    rep = card.add(img2, [tagged, untagged])
    check(not rep.added and len(rep.duplicates) == 2,
          "without a rebuild: still duplicates by title")

    print("CLI")
    img3 = fresh(tmp, "c.img", fw)
    card.add(img3, [tagged, untagged])
    card.rebuild(img3)
    p = subprocess.run([sys.executable, "-m", "flashpod", "add", "--raw", img3,
                        tagged, untagged], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                       cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT))
    out = p.stdout.decode()
    check("0 tracks added, 2 skipped (already on iPod)" in out,
          "add --raw after a rebuild: %r" % out.strip().splitlines()[-1])
    check(len(card.tracks(img3)["tracks"]) == 2, "the card still lists 2 tracks")

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
