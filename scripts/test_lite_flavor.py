#!/usr/bin/env python3
"""Test the lite/full build flavor mechanism.

A lite build (the vintage-Mac artifact) has the card-imaging half stripped:
`flash` is hidden from --help and refuses to run. Everything else -- source
checkouts, pip installs, normal binaries -- must stay full.

Simulates a lite build by dropping the marker file where PyInstaller would
unpack it. Run from the repo root:

    PYTHONPATH=. /usr/bin/python3 scripts/test_lite_flavor.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER_SRC = os.path.join(ROOT, "packaging", "flavor", "build_flavor.txt")
MARKER_DST = os.path.join(ROOT, "flashpod", "build_flavor.txt")

failures = []


def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def run(*args):
    """Run the CLI in a subprocess so argparse/flavor state is fresh."""
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.run([sys.executable, "-m", "flashpod"] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, env=env, cwd=ROOT)
    return p.returncode, p.stdout


def flavor_of_import():
    """resources.build_flavor() as a fresh interpreter sees it."""
    code = "from flashpod import resources; print(resources.build_flavor())"
    p = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE,
                       universal_newlines=True, cwd=ROOT,
                       env=dict(os.environ, PYTHONPATH=ROOT))
    return p.stdout.strip()


assert os.path.exists(MARKER_SRC), "missing marker source: " + MARKER_SRC
assert not os.path.exists(MARKER_DST), (
    "a stale lite marker is in the source tree: " + MARKER_DST)

print("FULL build (no marker -- source checkout, pip install, normal binary):")
check("build_flavor() == 'full'", flavor_of_import() == "full")
rc, out = run("--help")
check("flash listed in --help", "flash" in out)
rc, out = run("flash", "--self-test")
check("flash --self-test runs (rc=0)", rc == 0)
check("no lite refusal", "doesn't image flash cards" not in out)

try:
    shutil.copyfile(MARKER_SRC, MARKER_DST)

    print("LITE build (marker present):")
    check("build_flavor() == 'lite'", flavor_of_import() == "lite")

    rc, out = run("--help")
    # NB: can't just test `"flash" not in out` -- every "flashpod" contains it.
    # Check the three places the subcommand actually surfaces.
    check("flash gone from the usage/choices metavar",
          "rebuild,flash" not in out)
    check("flash gone from the subcommand listing",
          "write iPod firmware" not in out)
    check("flash gone from the description summary",
          "flashpod flash" not in out)
    check("no ==SUPPRESS== leaked into help", "SUPPRESS" not in out)
    check("other commands still listed", "rebuild" in out and "add" in out)

    rc, out = run("flash")
    check("flash refused (rc=1)", rc == 1)
    check("refusal explains where to image cards",
          "modern machine" in out and "FireWire syncing" in out)

    rc, out = run("flash", "--self-test")
    check("flash --self-test also refused", rc == 1)

    # The sync half must be untouched: this fails on the missing path, which
    # proves it got past flavor gating into normal command handling.
    rc, out = run("list", "--mount", "/nonexistent-flashpod-test")
    check("sync commands still work in lite", "does not exist" in out)
finally:
    if os.path.exists(MARKER_DST):
        os.remove(MARKER_DST)

print()
if failures:
    print("FAILED (%d): %s" % (len(failures), "; ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
