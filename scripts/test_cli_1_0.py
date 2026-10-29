#!/usr/bin/env python3
"""The flashpod 1.0 commands that have no 0.x counterpart, on synthetic
cards only (#97): what test_cli_unchanged cannot check, because the
Python 0.x tree has nothing to compare them with.

    python3 scripts/test_cli_1_0.py --rust rust/target/release/flashpod

Each check states its expected result independently of the binary (the
synthetic firmware's own bytes, Python's hashlib), so a wrong binary
fails. Ends with one PASS/FAIL line; exit 0 only on PASS.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402

FAILS = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def run(binary, *args):
    r = subprocess.run([binary] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr


def extract_checks(binary, work):
    print("firmware extract")
    fw = synth.write_firmware(os.path.join(work, "fw.bin"))
    fw_bytes = open(fw, "rb").read()   # as the file holds it, before the install fix-ups
    osos = synth.osos_payload()
    img = os.path.join(work, "card.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    rc, out, err = run(binary, "firmware", "write", img, "--firmware", fw, "--yes", "--no-arm")
    check(rc == 0, "firmware write on a blank image (rc %d)" % rc)

    o = os.path.join(work, "osos.bin")
    rc, out, err = run(binary, "firmware", "extract", img, "-o", o, "--image", "osos")
    got = open(o, "rb").read() if os.path.exists(o) else b""
    check(rc == 0 and got == osos, "--image osos is the synthetic osos payload, byte for byte")
    note = json.load(open(o + ".json")) if os.path.exists(o + ".json") else {}
    check(note.get("sha256") == hashlib.sha256(osos).hexdigest(), "the note's sha256 is the payload's")
    check(note.get("what") == "osos" and note.get("partition_table") == "mbr" and note.get("source") == img,
          "the note says what, which table and where from")

    p = os.path.join(work, "part.bin")
    rc, out, err = run(binary, "firmware", "extract", img, "-o", p)
    part = open(p, "rb").read() if os.path.exists(p) else b""
    check(rc == 0 and len(part) == 65536 * 512, "the whole partition is 32 MiB")
    # what write puts on the card: the install fix-ups applied (Python's
    # ipod_flash.load_firmware, the reference), then zeros
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import ipod_flash
    ready = ipod_flash.load_firmware(fw)
    check(ready != fw_bytes and part[:len(ready)] == ready and not part[len(ready):].strip(b"\0"),
          "the partition is the install-ready firmware (Python's load_firmware), then zeros")

    before = hashlib.sha256(open(o, "rb").read()).hexdigest()
    rc, out, err = run(binary, "firmware", "extract", img, "-o", o, "--image", "osos")
    check(rc == 1 and "already exists" in err and hashlib.sha256(open(o, "rb").read()).hexdigest() == before,
          "an existing output is refused and left as it was")

    rc, out, err = run(binary, "firmware", "extract", img, "-o", os.path.join(work, "a.bin"), "--image", "aupd")
    check(rc == 1 and "no aupd image" in err, "a missing image is refused: %r" % err.strip())

    blank = os.path.join(work, "blank.img")
    with open(blank, "wb") as f:
        f.truncate(1 << 20)
    rc, out, err = run(binary, "firmware", "extract", blank, "-o", os.path.join(work, "b.bin"))
    check(rc == 1 and "no partition table" in err, "a blank image is refused: %r" % err.strip())

    sha_img = hashlib.sha256(open(img, "rb").read()).hexdigest()
    run(binary, "firmware", "extract", img, "-o", os.path.join(work, "c.bin"))
    check(hashlib.sha256(open(img, "rb").read()).hexdigest() == sha_img, "extract leaves the card unchanged")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--keep", action="store_true")
    opts = ap.parse_args()
    work = tempfile.mkdtemp(prefix="fp-1-0-")
    try:
        extract_checks(os.path.abspath(opts.rust), work)
    finally:
        if not opts.keep:
            shutil.rmtree(work, ignore_errors=True)
    print("%s: %d failed" % ("FAIL" if FAILS else "PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
