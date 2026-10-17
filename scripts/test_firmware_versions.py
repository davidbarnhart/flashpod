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
within each generation, version order agrees with build-date order; the
1G releases sort 1.0 < 1.0.2 < 1.0.4 < 1.1 < ... < 1.5 by version tuple,
and both firmware_catalog() and the CLI picker list them newest first.
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

#: Every 1G release in the catalog, oldest first. 1.0.4 (2001) must sort
#: before 1.1 and 1.4 -- the order the old 0.x names were protecting.
ONE_G_ORDER = ["1.0", "1.0.2", "1.0.4", "1.1", "1.2", "1.2.1", "1.2.2",
               "1.2.6", "1.3", "1.4", "1.5"]

fails = []


def vtuple(v):
    return tuple(int(p) for p in v.split("."))


def picker_versions():
    """The Version column of `flashpod flash`'s firmware chooser for a 1G,
    run in-process with a fake tty that takes the default."""
    import builtins
    import contextlib
    import io
    import json
    from flashpod import cli
    with open(os.path.join(ROOT, "flashpod", "firmware", "firmware.json")) as f:
        manifest = json.load(f)
    out = io.StringIO()
    real_isatty, real_input = sys.stdin.isatty, builtins.input
    sys.stdin.isatty, builtins.input = (lambda: True), (lambda *a: "")
    try:
        with contextlib.redirect_stdout(out):
            cli.choose_firmware(manifest, {"id": "1G", "name": "1G"})
    finally:
        sys.stdin.isatty, builtins.input = real_isatty, real_input
    rows = [ln.split() for ln in out.getvalue().splitlines()]
    return [r[1] for r in rows if len(r) > 2 and r[0].isdigit()]


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
        by_ver = [e["file"] for e in sorted(es, key=lambda e: vtuple(e["version"]))]
        check(by_date == by_ver, "%s (%d dated)" % (gen, len(es)))

    print("the 1G list runs 1.0 < 1.0.2 < 1.0.4 < 1.1 < ... < 1.5 (lead, #1204)")
    one_g = [e["version"] for e in entries if "1G" in e.get("models", [])]
    check(sorted(one_g, key=vtuple) == ONE_G_ORDER,
          "version-tuple order: %s" % " < ".join(sorted(one_g, key=vtuple)))
    check(one_g == ONE_G_ORDER[::-1],
          "firmware_catalog() lists them newest first: %s" % ", ".join(one_g))
    shown = picker_versions()
    check(shown == ONE_G_ORDER[::-1],
          "the CLI picker for a 1G shows them newest first: %s" % ", ".join(shown))

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
