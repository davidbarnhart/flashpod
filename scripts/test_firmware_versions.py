#!/usr/bin/env python3
"""The catalog names every firmware by Apple's own version.

    PYTHONPATH=. python3 scripts/test_firmware_versions.py

Found 2026-10-16 (emupod, bridge #1198): firmware_catalog() and inspect's
build called Apple's 1.0, 1.0.2 and 1.0.4 "0.0", "0.2" and "0.4" -- a
picker-era renumbering that the asset file names still carry
(iPod_1.0.4_... is family 1 + 0.4). The catalog's ``version`` is now Apple's,
which inspect reports as "stock <version>"; the file names stay, because
they are published release assets.

Apple's numbers for the three, by image sha256: 1.0 is the launch firmware
(its Mac OS 9 updater's Info.plist reads '1.0'), and 1.0.2 / 1.0.4 come from
Apple's Firmware-1.1.0.2 / Firmware-1.1.0.4 distribution names (family 1,
version 1.0.x), dated 2001-11-28 and 2001-12-23.

Checks, on the shipped manifest through flashpod.card (no network): the three
carry Apple's version; no entry's version is 0.x (the renumbering's mark);
within each generation, version order agrees with build-date order.
Against the manifest before the fix, 4 checks fail (3 versions, and the
0.x scan).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from flashpod import card  # noqa: E402

APPLE = {
    "bd47f0d8de764e2aa1c64bf0e8d78a685d0b2e43f9abaaededaa7b532028023d": "1.0",
    "0219800e4e9d10cb0e18ec83418553a341f581976414c70747c200bdf1afa048": "1.0.2",
    "d898113c4f0af7d3885e6ab14e8054e5d2efc10f050d80a46f892d13a33709cc": "1.0.4",
}

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def main():
    entries = card.firmware_catalog()
    by_sha = {e["sha256"]: e for e in entries}
    print("the 1.0 line carries Apple's version")
    for sha, want in sorted(APPLE.items(), key=lambda kv: kv[1]):
        e = by_sha.get(sha)
        check(e is not None and e["version"] == want,
              "%s: %r (want %r)" % (e and e["file"], e and e["version"], want))
    print("no 0.x versions")
    zero = [(e["file"], e["version"]) for e in entries
            if e["version"].startswith("0.")]
    check(not zero, "none in %d entries %r" % (len(entries), zero))
    print("version order agrees with build-date order")
    groups = {}
    for e in entries:
        if e.get("build_date"):
            groups.setdefault(e["generation"], []).append(e)
    for gen, es in sorted(groups.items()):
        by_date = [e["file"] for e in sorted(es, key=lambda e: e["build_date"])]
        by_ver = [e["file"] for e in sorted(
            es, key=lambda e: tuple(int(p) for p in e["version"].split(".")))]
        check(by_date == by_ver, "%s (%d dated)" % (gen, len(es)))

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
