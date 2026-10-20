#!/usr/bin/env python3
"""`flashpod flash --arm`: the card is armed only when asked, and only with a
disk-mode program flashpod knows.

    PYTHONPATH=. python3 scripts/test_flash_arm.py [--asset flashpod_1.1.6_....bin.gz]

David's design (bridge #1231): --arm arms the card -- the aupd entry's id
stays 0 ("update pending") in BOTH firmware directories, so on its next start
on FireWire power the iPod reprograms its flash chip from the card. Without
--arm an interactive flash asks, defaulting to no; a non-interactive run
never arms; --no-arm neither arms nor asks. Only a known disk-mode program
(firmware.KNOWN_AUPD: flashpod 1.6's, Apple 1.5's) may be armed.

On synthetic cards. A synthetic disk-mode program can't have a real
program's sha256, so its hash is added to KNOWN_AUPD for the run and the CLI
is run in-process (fake terminal and answers); a second synthetic program,
not added, stands for an unknown one. Checked, reading both directories off
the card:
  * --arm (no questions), or "y" at both questions: armed (id 0 in both).
    The first question is short; only a yes brings the second, which
    carries the warning (David, 2026-10-17);
  * Enter (the default) or "n" at either question, --no-arm, or no
    terminal: not armed (id 1 in both), and with --no-arm or no terminal
    nothing is asked;
  * an unknown program is never offered, and --arm with one, or with no
    program at all, is refused with the card unchanged;
  * the plan says ARMED and what that means; a dry run writes nothing;
  * an armed card differs from an unarmed one in exactly the two id words;
  * card.flash(arm=True) writes the same bytes as `flash --arm`, refuses an
    unknown program (FirmwareInvalid, reason cannot_arm), and inspect
    reports the aupd as known and armed.
With --asset (the real flashpod 1.6 file) also: its program is known and
arms; Apple's 1.5 arms and 1.4 is refused when they're in the cache.
"""
import argparse
import builtins
import hashlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, cli, firmware, ipod_flash, layout, platform  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def region(img):
    with open(img, "rb") as f:
        f.seek(layout.FW_START * 512)
        return f.read(layout.FW_BLOCKS * 512)


def armed(img):
    return firmware.aupd_armed(region(img))


def run(argv, tty=True, answers=()):
    """cli.main in-process; returns (rc, output, questions asked)."""
    pending = list(answers)
    asked = []

    def fake_input(prompt=""):
        asked.append(prompt)
        out.write(prompt)
        return pending.pop(0) if pending else ""

    out = io.StringIO()
    platform._cached = None
    saved = (sys.argv, sys.stdout, sys.stderr, sys.stdin.isatty,
             builtins.input)
    sys.argv = ["flashpod"] + argv
    sys.stdout = sys.stderr = out
    sys.stdin.isatty = lambda: tty
    builtins.input = fake_input
    try:
        try:
            rc = cli.main()
        except SystemExit as e:              # sys.exit("message") prints it
            rc = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                out.write(e.code + "\n")
    finally:
        (sys.argv, sys.stdout, sys.stderr, sys.stdin.isatty,
         builtins.input) = saved
    return rc, out.getvalue(), [a for a in asked
                                if a.strip() in (ipod_flash.ARM_QUESTION.strip(),
                                                 ipod_flash.ARM_CONFIRM.strip())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default=None)
    opts = ap.parse_args()
    tmp = synth.scratch_dir("fp-arm-")
    known = bytes(range(256)) * 64
    unknown = bytes(reversed(range(256))) * 64
    firmware.KNOWN_AUPD[hashlib.sha256(known).hexdigest()] = "synthetic test"
    fw_known = os.path.join(tmp, "known.bin")
    fw_unknown = os.path.join(tmp, "unknown.bin")
    fw_none = os.path.join(tmp, "none.bin")
    open(fw_known, "wb").write(synth.firmware_bytes(aupd=known))
    open(fw_unknown, "wb").write(synth.firmware_bytes(aupd=unknown))
    synth.write_firmware(fw_none)

    def blank(name):
        p = os.path.join(tmp, name)
        card.create_blank(p, 96 << 20)
        return p

    print("arming happens only when asked")
    img = blank("a.img")
    rc, out, q = run(["flash", img, "--firmware", fw_known, "--yes", "--arm"])
    check(rc == 0 and armed(img) is True and not q,
          "--arm: armed in both directories, no question (rc %r)" % rc)
    check("ARMED for a disk-mode update (synthetic test)" in out
          and ipod_flash.ARM_WARNING in out,
          "the plan says ARMED, with the warning")
    armed_img = img
    question = ipod_flash.ARM_QUESTION.strip()
    confirm = ipod_flash.ARM_CONFIRM.strip()
    for answers, want, what in (
            (["y", "y"], True, "'y' then 'y'"),
            (["y", "n"], False, "'y' then 'n'"),
            (["y", ""], False, "'y' then Enter (default no)"),
            ([""], False, "Enter at the question (default no)"),
            (["n"], False, "'n' at the question")):
        img = blank("q%s.img" % "-".join(a or "enter" for a in answers))
        rc, out, q = run(["flash", img, "--firmware", fw_known, "--yes"],
                         answers=answers)
        asked = [a.strip() for a in q]
        expect = [question, confirm][:2 if answers[0] == "y" else 1]
        check(rc == 0 and asked == expect and armed(img) is want,
              "%s: asked %d, armed=%s" % (what, len(asked), armed(img)))
        if answers == ["y", "y"]:
            check(ipod_flash.ARM_WARNING in confirm
                  and "plugged into FireWire" in confirm
                  and "permanently broken" in confirm
                  and confirm.endswith("Continue? [y/N]"),
                  "the second question carries the warning")
            check("disk-mode program flashpod can arm" not in out
                  and "external chip programmer" not in out,
                  "no wall of text before the question")
    unarmed_img = img
    img = blank("noarm.img")
    rc, out, q = run(["flash", img, "--firmware", fw_known, "--yes",
                      "--no-arm"])
    check(rc == 0 and not q and armed(img) is False,
          "--no-arm: not asked, not armed")
    img = blank("notty.img")
    rc, out, q = run(["flash", img, "--firmware", fw_known, "--yes"],
                     tty=False)
    check(rc == 0 and not q and armed(img) is False,
          "no terminal: not asked, not armed")

    print("an armed card differs only in the two id words")
    a, b = region(armed_img), region(unarmed_img)
    diff = [i for i in range(0, len(a), 4) if a[i:i + 4] != b[i:i + 4]]
    ptr = 0x4000
    check(diff == [ptr + 48, ptr + 0x200 + 48],
          "differing words at %s" % [hex(d) for d in diff])

    print("only a known disk-mode program")
    img = blank("unk.img")
    rc, out, q = run(["flash", img, "--firmware", fw_unknown, "--yes"],
                     answers=["y"])
    check(rc == 0 and not q and armed(img) is False,
          "an unknown program is never offered")
    for fw, what in ((fw_unknown, "an unknown program"),
                     (fw_none, "no program at all")):
        img = blank("ref-%s.img" % os.path.basename(fw))
        before = sha(img)
        rc, out, q = run(["flash", img, "--firmware", fw, "--yes", "--arm"])
        check(rc != 0 and "can't arm" in out and sha(img) == before,
              "--arm with %s: refused, card unchanged (rc %r)" % (what, rc))

    print("a dry run")
    img = blank("dry.img")
    before = sha(img)
    rc, out, q = run(["flash", img, "--firmware", fw_known, "--arm",
                      "--dry-run"])
    check(rc == 0 and "ARMED" in out and sha(img) == before,
          "plan says ARMED, nothing written")

    print("the library")
    img = blank("lib.img")
    card.flash(img, fw_known, arm=True)
    check(region(img) == region(armed_img),
          "card.flash(arm=True) writes the same firmware region as --arm")
    info = card.inspect(img).firmware["aupd"]
    check(info["armed"] is True and info["known"] == "synthetic test",
          "inspect: aupd known and armed")
    img = blank("lib2.img")
    card.flash(img, fw_known)
    check(card.inspect(img).firmware["aupd"]["armed"] is False,
          "card.flash without arm: inspect says not armed")
    try:
        card.flash(blank("lib3.img"), fw_unknown, arm=True)
        ok = False
    except card.FirmwareInvalid as e:
        ok = e.details.get("reason") == "cannot_arm"
    check(ok, "card.flash(arm=True) with an unknown program: cannot_arm")

    if opts.asset:
        print("real firmware")
        del firmware.KNOWN_AUPD[hashlib.sha256(known).hexdigest()]
        fw16 = firmware.load_firmware(opts.asset)
        info = firmware.aupd_of(fw16)
        check(info["known"] and info["known"].startswith("flashpod 1.6"),
              "flashpod 1.6's program is known: %s" % info["known"])
        check(firmware.aupd_armed(firmware.arm_firmware(fw16)) is True
              and firmware.aupd_armed(fw16) is False,
              "it arms; unarmed as flashpod normally writes it")
        cache = os.path.join(os.path.expanduser("~"), ".cache", "flashpod",
                             "firmware")
        for name, ok in (("iPod_1.1.5_2005_02_18.bin.gz", True),
                         ("iPod_1.1.4_2004_04_23.bin.gz", False)):
            path = os.path.join(cache, name)
            if not os.path.exists(path):
                print("  SKIP %s: not cached" % name)
                continue
            try:
                firmware.arm_firmware(firmware.load_firmware(path))
                got = True
            except ValueError:
                got = False
            check(got is ok, "%s: %s" % (name, "arms" if ok else "refused"))

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
