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
fixes are reported: all 23 (the catalog's flashpod 1.6), all but `version`,
only `library-leak`. Give --openpod a checkout of the release's commit.
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import synth_fixtures as synth  # noqa: E402
from flashpod import card  # noqa: E402

fails = []

#: The 1.7 folder's own fixes: `version` is said again as `version-1.7`, and
#: Refresh Library is new; what a card of each flashpod release carries.
ONLY_17 = {"version-1.7", "refresh-library"}
FIXES = {
    "1.6": lambda t: sorted(x["name"] for x in t["fixes"]
                            if x["name"] not in ONLY_17),
    "1.7": lambda t: sorted(x["name"] for x in t["fixes"]
                            if x["name"] != "version"),
}


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
    tmp = synth.scratch_dir("fp-fwid-")
    entries = card.firmware_catalog()
    local = [e for e in entries if e["local"]]
    if not local:
        print("SKIP: no catalog firmware cached locally")
        return 0
    print("stock images (%d of %d cached)" % (len(local), len(entries)))
    for e in local:
        f = firmware_of(tmp, e["local"], e["file"])
        check(f["present"] and f["osos"]["checksum_ok"]
              and f["build"] == "%s %s" % (
                  "stock" if e.get("maker", "apple") == "apple"
                  else e["maker"], e["version"])
              and f["catalog_file"] == e["file"],
              "%s -> %s" % (e["file"], f["build"]))
        apple = {"iPod_1.0.0_": "1.0", "iPod_1.0.2_": "1.0.2",
                 "iPod_1.0.4_": "1.0.4"}.get(e["file"][:11])
        if apple:
            check(f["build"] == "stock " + apple,
                  "Apple's version, not the file name's: %s" % f["build"])
        if e.get("maker") == "flashpod":
            table = json.load(open(os.path.join(ROOT, "flashpod", "firmware",
                                                "fixes.json")))
            v = e["version"]
            want = FIXES[v](table)
            check(sorted(f["fixes"]) == want and f["version_word_means"] == v,
                  "flashpod %s: all %d fixes, version word %s"
                  % (v, len(f["fixes"]), f["version_word_means"]))
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
        all_fixes = FIXES["1.6"](table)
        f17 = os.path.join(opts.openpod, "firmware", "1", "1.7")
        cases = [
            ("all", [], "flashpod 1.6", "1.6", all_fixes),
            ("noversion", ["--without", "version"], "unrecognized", "1.5",
             [n for n in all_fixes if n != "version"]),
            ("libleak", ["--only", "library-leak"], "unrecognized", "1.5",
             ["library-leak"]),
        ]
        if os.path.isdir(f17):
            cases += [
                ("all17", ["--fixes", f17], "flashpod 1.7", "1.7",
                 FIXES["1.7"](table)),
                ("norefresh17", ["--fixes", f17, "--without", "refresh-library"],
                 "unrecognized", "1.7",
                 [n for n in FIXES["1.7"](table) if n != "refresh-library"]),
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
