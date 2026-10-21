#!/usr/bin/env python3
"""The card logic lives in ONE place: flashpod.card (and the modules under it).

    PYTHONPATH=. python3 scripts/test_one_place.py

David, on PR #76: "I just want one place for the logic to exist." The CLI
keeps argument parsing, prompts, progress printing and real-device handling;
every operation on a card goes through flashpod.card. Checked two ways:

STATIC -- cli.py contains none of the calls that write a card's FAT, its
iTunesDB or its layout (they belong to card.* and the modules it drives), and
ipod_flash.py writes a card only through layout.write_card / write_mbr (the
sequence card.flash runs too). The shared steps are each defined exactly once.

DYNAMIC -- the CLI is run in-process on a synthetic image and on a --mount
directory, with card's entry points wrapped to count calls, and every data
command must reach its card.* function: init -> card.init, add ->
card.plan_add + card.apply_add, rm -> card.remove, rebuild -> card.rebuild.
(A static check alone could pass with the logic merely renamed; this shows
the CLI really runs the library's code.)
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, cli, platform  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def code_of(path):
    """The module's code with comments and docstrings blanked, so a word in
    prose can't trip (or satisfy) a check."""
    import io
    import tokenize
    out = []
    with open(path, "rb") as f:
        toks = list(tokenize.tokenize(f.readline))
    prev = None
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (tokenize.INDENT,
                                                    tokenize.NEWLINE,
                                                    tokenize.NL, None):
            prev = tok.type
            continue                     # a docstring / bare string statement
        if tok.type not in (tokenize.ENCODING,):
            out.append(tok.string)
        prev = tok.type
    return " ".join(out)


WRITES = [
    "itunesdb . save", "itunesdb . init_ipod", "itunesdb . copy_to_ipod",
    ". save_library (", ". init_structure (", ". remove_location (",
    ". rebuild_library (", "copy_batch", "read_batch", ". write_file (",
    "write_layout_body", "format_partition", "format_fat32", "write_card",
    "write_mbr", "os . unlink (", ". mkdir (",
]


def static():
    print("static: cli.py writes no card itself")
    cli_code = code_of(os.path.join(ROOT, "flashpod", "cli.py"))
    for w in WRITES:
        check(w not in cli_code, "cli.py has no %r" % w.replace(" ", ""))
    print("static: ipod_flash.py writes a card only via layout.write_card")
    fl = code_of(os.path.join(ROOT, "flashpod", "ipod_flash.py"))
    for w in ("write_layout_body", "format_partition", "format_fat32",
              ". write_file ("):
        check(w not in fl, "ipod_flash.py has no %r" % w.replace(" ", ""))
    check("layout . write_card (" in fl and "layout . write_mbr (" in fl,
          "ipod_flash.py uses layout.write_card and layout.write_mbr")
    card_code = code_of(os.path.join(ROOT, "flashpod", "card.py"))
    check("layout . write_card (" in card_code
          and "layout . write_mbr (" in card_code,
          "card.flash uses the same two")
    print("static: each shared step is defined once")
    defs = {}
    for fn in os.listdir(os.path.join(ROOT, "flashpod")):
        if fn.endswith(".py"):
            src = open(os.path.join(ROOT, "flashpod", fn)).read()
            for m in re.finditer(r"^def (\w+)\(", src, re.M):
                defs.setdefault(m.group(1), []).append(fn)
    for name in ("write_card", "write_mbr", "verify_region", "read_batch",
                 "copy_batch", "select_for_removal", "ceiling", "counts",
                 "make_track", "track_key", "expand", "plan_add", "apply_add"):
        check(len(defs.get(name, [])) == 1,
              "def %s: %s" % (name, defs.get(name)))


def dynamic():
    print("dynamic: every CLI data command reaches card.*")
    tmp = synth.scratch_dir("fp-oneplace-")
    music = synth.make_music(os.path.join(tmp, "music"))
    fw = synth.write_firmware(os.path.join(tmp, "fw.bin"))
    img = os.path.join(tmp, "card.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, fw)
    mnt = os.path.join(tmp, "mount")
    os.makedirs(mnt)

    calls = []
    for name in ("init", "plan_add", "apply_add", "remove", "rebuild",
                 "library"):
        real = getattr(card, name)

        def wrap(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)
        setattr(card, name, wrap)

    def run(*argv):
        del calls[:]
        platform._cached = None
        saved, sys.argv = sys.argv, ["flashpod"] + list(argv)
        devnull = open(os.devnull, "w")
        so, se, sys.stdout, sys.stderr = sys.stdout, sys.stderr, devnull, devnull
        try:
            rc = cli.main()
        finally:
            sys.argv, sys.stdout, sys.stderr = saved, so, se
            devnull.close()
        return rc, list(calls)

    for where, flag in (("image", ["--raw", img]), ("mount", ["--mount", mnt])):
        rc, c = run("init", *flag)
        check(rc == 0 and "init" in c, "%s init -> card.init %r" % (where, c))
        rc, c = run("add", *flag, music)
        check("plan_add" in c and "apply_add" in c,
              "%s add -> card.plan_add + card.apply_add %r" % (where, c))
        rc, c = run("rm", *flag, "52")
        check(rc == 0 and "remove" in c, "%s rm -> card.remove %r" % (where, c))
        rc, c = run("rebuild", *flag)
        check(rc == 0 and "rebuild" in c,
              "%s rebuild -> card.rebuild %r" % (where, c))
        rc, c = run("ls", *flag)
        check(rc == 0, "%s ls runs (rc %r)" % (where, rc))

    print("dynamic: the CLI takes the library's lock on an image")
    import fcntl
    import hashlib
    holder = open(img, "rb")
    fcntl.flock(holder.fileno(), fcntl.LOCK_SH)    # e.g. a booted emulator
    before = hashlib.sha256(open(img, "rb").read()).hexdigest()
    rc, c = run("add", "--raw", img, music)
    holder.close()
    after = hashlib.sha256(open(img, "rb").read()).hexdigest()
    check(rc == 1 and not c and before == after,
          "add --raw on an image another program holds: refused, untouched")


def main():
    static()
    dynamic()
    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
