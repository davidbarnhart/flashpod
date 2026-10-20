#!/usr/bin/env python3
"""flashpod 1.6 flashes from the catalog and is recognised (local check).

    PYTHONPATH=. python3 scripts/test_flashpod_16.py --asset flashpod_1.1.6_2026_10_16.bin.gz

Needs the catalog's 1.6 asset (it is not in the repo; until it is published
on the `firmware` release, pass the built file). The asset is copied into a
temporary XDG cache, so nothing is downloaded and ~/.cache is not touched.

  * the asset matches the catalog entry (sha256, size);
  * the library: card.fetch_firmware finds it in the cache, card.flash
    writes it to a synthetic card, and card.inspect names it "flashpod 1.6",
    maker flashpod, version word 1.6, with all 23 fixes;
  * the CLI: `flashpod flash IMG --model 1G` on a terminal offers 1.6 first,
    labelled "(flashpod)" and NOT AN APPLE RELEASE; choosing it asks whether
    to arm (Enter: no), flashes the card unarmed, and the firmware region is
    byte-identical to the library's.
"""
import argparse
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def sha(path, start=0, length=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        h.update(f.read() if length is None else f.read(length))
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    opts = ap.parse_args()
    tmp = synth.scratch_dir("fp-16-")
    cache = os.path.join(tmp, "xdg")
    os.makedirs(os.path.join(cache, "flashpod", "firmware"))
    os.environ["XDG_CACHE_HOME"] = cache

    from flashpod import card, layout
    entry = next((e for e in card.firmware_catalog()
                  if e.get("maker") == "flashpod"), None)
    check(entry is not None and entry["version"] == "1.6",
          "the catalog has flashpod 1.6")
    if entry is None:
        return 1
    check(sha(opts.asset) == entry["sha256"]
          and os.path.getsize(opts.asset) == entry["size"],
          "the asset matches the entry's sha256 and size")
    shutil.copy(opts.asset, os.path.join(cache, "flashpod", "firmware",
                                         entry["file"]))

    print("the library")
    path = card.fetch_firmware(entry["file"])
    check(os.path.dirname(path).startswith(cache), "found in the cache")
    img = os.path.join(tmp, "lib.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, path)
    f = card.inspect(img).firmware
    check(f["build"] == "flashpod 1.6" and f["maker"] == "flashpod"
          and f["catalog_file"] == entry["file"],
          "inspect: %r, maker %r" % (f["build"], f["maker"]))
    check(f["version_word_means"] == "1.6" and len(f["fixes"]) == 23,
          "version word %s, %d fixes" % (f["version_word_means"],
                                         len(f["fixes"])))

    print("the CLI picker")
    from test_cli_unchanged import run_pty
    cimg = os.path.join(tmp, "card.img")
    card.create_blank(cimg, 96 << 20)
    env = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1", TERM="dumb")
    rc, out, _ = run_pty([sys.executable, "-m", "flashpod", "flash", cimg,
                       "--model", "1G"], env, ROOT,
                      [(r"Select firmware \[\d+\]: ", "0\n"),
                       (r"Arm this card for an iPod update to disk-mode "
                        r"during the next boot\? \[y/N\] ", "\n"),
                       (r'Type "ERASE card\.img" to proceed: ',
                        "ERASE card.img\n"),
                       (r"\[Y/n\]", "n\n")])
    row = next((ln for ln in out.splitlines() if ln.strip().startswith("0 ")),
               "")
    check("1.6 (flashpod)" in row and "NOT AN APPLE RELEASE" in row,
          "row 0 is 1.6 (flashpod), NOT AN APPLE RELEASE: %r" % row.strip())
    check("Arm this card for an iPod update to disk-mode during the next "
          "boot? [y/N]" in out
          and "ARMED" not in out,
          "it asks whether to arm (1.6's disk-mode program is known); "
          "Enter leaves it unarmed")
    check(rc == 0, "flash exits 0 (rc %r)%s" % (
        rc, "" if rc == 0 else ": " + out.strip().splitlines()[-1]))
    f2 = card.inspect(cimg).firmware
    check(f2.get("build") == "flashpod 1.6"
          and f2.get("aupd", {}).get("armed") is False,
          "inspect after the CLI flash: %r, armed %r"
          % (f2.get("build"), f2.get("aupd", {}).get("armed")))
    n = layout.DATA_START * 512
    check(sha(cimg, 0, n) == sha(img, 0, n),
          "MBR + firmware region identical to the library's flash")

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
