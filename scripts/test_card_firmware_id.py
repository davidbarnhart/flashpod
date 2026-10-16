#!/usr/bin/env python3
"""card.inspect names real firmware correctly (local check, needs the images).

    PYTHONPATH=. python3 scripts/test_card_firmware_id.py [--openpod ~/source/openpod-gen1]

Apple's firmware is not in the repo (it is downloaded, then cached), so this
runs only where the catalog images are cached, and says SKIP otherwise -- CI
covers identification with synthetic firmware in scripts/test_card.py.

Flashes each stock image onto a fresh image file with card.flash and checks
inspect() names it by its catalog version. With openpod's tools/fw_fix.py
available it also builds three 1.6 variants into a temp dir (never into the
openpod tree) and checks the build label, the version word, and exactly which
fixes are reported: all 23, all but `version`, only `library-leak`.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from flashpod import card, catalog  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def firmware_of(tmp, fw, name):
    img = os.path.join(tmp, name + ".img")
    card.create_blank(img, card.min_card_bytes())
    card.flash(img, fw)
    return card.inspect(img).firmware


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openpod", default=os.path.expanduser("~/source/openpod-gen1"))
    opts = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="fp-fwid-")
    entries = card.firmware_catalog()
    local = [e for e in entries if e["local"]]
    if not local:
        print("SKIP: no catalog firmware cached locally")
        return 0
    print("stock images (%d of %d cached)" % (len(local), len(entries)))
    for e in local:
        f = firmware_of(tmp, e["local"], e["file"])
        check(f["present"] and f["osos"]["checksum_ok"]
              and f["build"] == "stock " + e["version"]
              and f["catalog_file"] == e["file"],
              "%s -> %s" % (e["file"], f["build"]))
        if e["file"].startswith("iPod_1.1.5"):
            check(f["fixes"] == [] and f["version_word_means"] == "1.5",
                  "stock 1.5: no openpod fixes, version word 1.5")

    tool = os.path.join(opts.openpod, "tools", "fw_fix.py")
    if not os.path.exists(tool):
        print("SKIP 1.6 variants: no %s" % tool)
    else:
        print("openpod 1.6 builds")
        table = json.load(open(os.path.join(ROOT, "flashpod", "firmware",
                                            "fixes.json")))
        all_fixes = sorted(f["name"] for f in table["fixes"])
        cases = [
            ("all", [], "openpod 1.6 release", "1.6", all_fixes),
            ("noversion", ["--without", "version"], "unrecognized", "1.5",
             [n for n in all_fixes if n != "version"]),
            ("libleak", ["--only", "library-leak"], "unrecognized", "1.5",
             ["library-leak"]),
        ]
        for name, args, build, word, want in cases:
            out = os.path.join(tmp, name + ".ipsw")
            subprocess.run([sys.executable, tool, "--out", out] + args,
                           cwd=tmp, check=True, stdout=subprocess.DEVNULL)
            f = firmware_of(tmp, out, name)
            check(f["build"] == build and f["version_word_means"] == word
                  and sorted(f["fixes"]) == want,
                  "%s: build %r, word %s, %d fixes" % (
                      name, f["build"], f["version_word_means"],
                      len(f["fixes"])))
        src = table["source"]
        print("  (fixes.json from openpod %s)" % src["openpod_commit"][:12])
    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
