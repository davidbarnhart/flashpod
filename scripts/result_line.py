"""The one-line verdict every harness ends with, so a reader can check it
with one command instead of reading the prose above it:

  RESULT flashpod@<commit> <gate> <PASS|FAIL|NOT-JUDGED> asked=N entered=M
         compared=K sha=<log sha8> log=<path> [bin=<sha8>] [mutants=C/T]

asked    = commands the run set out to compare
entered  = commands that actually ran on both sides
compared = output streams compared (rc, stdout, ... per command)
sha      = first 8 hex of the sha256 of the log file, which holds every
           record both sides produced, as JSON

A run that entered nothing, or fewer commands than it asked, is NOT-JUDGED
and never PASS; the caller exits non-zero for anything but PASS.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import time


def _commit(tree):
    try:
        head = subprocess.run(["git", "-C", tree, "rev-parse", "--short", "HEAD"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", tree, "status", "--porcelain",
                                "--untracked-files=no"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               universal_newlines=True).stdout.strip()
    except OSError:
        return "unknown"
    return (head or "unknown") + ("-dirty" if dirty else "")


def sha8(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def _plain(o):
    if isinstance(o, bytes):
        return o.decode("latin-1")
    if isinstance(o, (set, tuple)):
        return list(o)
    return repr(o)


def verdict(asked, entered, failed):
    if entered == 0 or entered < asked:
        return "NOT-JUDGED"
    return "FAIL" if failed else "PASS"


def emit(gate, tree, asked, entered, compared, failed, records,
         binary=None, mutants=None):
    """Write the log, print the RESULT line, and return the exit code:
    0 for PASS, 1 otherwise. ``records`` is anything JSON-able (bytes are
    kept byte for byte as latin-1); ``mutants`` is (caught, total)."""
    out_dir = os.environ.get("FLASHPOD_RESULT_DIR") or os.path.join(
        tempfile.gettempdir(), "flashpod-results")
    os.makedirs(out_dir, exist_ok=True)
    log = os.path.join(out_dir, "%s-%s-%d.json" % (
        gate, time.strftime("%Y%m%dT%H%M%S"), os.getpid()))
    with open(log, "w") as f:
        json.dump({"gate": gate, "asked": asked, "entered": entered,
                   "compared": compared, "records": records},
                  f, default=_plain, sort_keys=True)
    v = verdict(asked, entered, failed or (mutants and mutants[0] < mutants[1]))
    line = "RESULT flashpod@%s %s %s asked=%d entered=%d compared=%d sha=%s log=%s" % (
        _commit(tree), gate, v, asked, entered, compared, sha8(log), log)
    if binary:
        line += " bin=%s" % sha8(binary)
    if mutants:
        line += " mutants=%d/%d" % mutants
    print(line)
    return 0 if v == "PASS" else 1
