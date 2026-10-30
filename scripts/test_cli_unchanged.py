#!/usr/bin/env python3
"""Prove the flashpod CLI behaves exactly as a baseline tree does.

    python3 scripts/test_cli_unchanged.py --baseline ../flashpod-baseline
    python3 scripts/test_cli_unchanged.py --baseline ../flashpod-baseline --mutants

Runs the same scenarios -- sequences of real `flashpod` commands on a disk
image file -- against the BASELINE tree and the tree this script lives in
(``--tree`` overrides), and compares, step by step: exit code, stdout, stderr,
and the sha256 of the image afterwards. Any difference fails, with a diff.

Made for the library refactor (flashpod.card, 2026-10): the CLI was moved onto
the library and must not change. The baseline is the pre-refactor commit.

Inputs are synthetic only (scripts/synth_fixtures.py): a generated firmware
volume, ffmpeg-made tagged MP3s, sparse images. Never real music or cards.

Determinism: every command runs in a fresh process through a shim that seeds
``random`` and freezes ``time.time``/``time.monotonic`` before flashpod is
imported, with the same paths, environment and terminal size for both trees.
The same baseline run twice must therefore match itself; ``--self`` checks
that.

Not vacuous: a step's output may not be empty, steps marked as writes must
change the image, and ``expect`` strings must appear in the BASELINE output
(so a scenario that silently stopped doing what it says fails). ``--mutants``
applies deliberate one-line breakages to a copy of the tree under test and
requires the comparison to FAIL for each.

Terminal paths (the prompts) run under a pseudo-terminal, 80x24, answering
prompts by pattern; their transcript (stdout+stderr merged, ANSI included) is
compared whole.
"""
import argparse
import difflib
import errno
import fcntl
import hashlib
import os
import re
import select
import shlex
import layout1
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402
import result_line  # noqa: E402

RUNNER = r'''
import os, random, sys, time
sys.path.insert(0, os.environ["FP_TREE"])
random.seed(1234)
time.time = lambda: 1760000000.0
time.monotonic = lambda: 1000.0
# --mount scenarios run on a plain directory: pin its "free space" so the
# add pre-flight prints the same numbers whatever the host disk holds.
import shutil
_free = int(os.environ.get("FP_DISK_FREE", str(10 << 30)))
shutil.disk_usage = lambda p: shutil._ntuple_diskusage(_free * 2, _free, _free)
sys.argv = ["flashpod"] + sys.argv[1:]
from flashpod import cli
# the flash_catalog scenario's firmware catalog, served from 127.0.0.1
if os.environ.get("FP_MANIFEST"):
    cli.FIRMWARE_MANIFEST = os.environ["FP_MANIFEST"]
if os.environ.get("FLASHPOD_SEED"):
    # --rust: ids and the clock come from the SplitMix64 source the Rust
    # binary also uses, so written images can be compared byte for byte
    from flashpod import entropy
    with entropy.deterministic(int(os.environ["FLASHPOD_SEED"]),
                               int(os.environ["FLASHPOD_NOW"])):
        sys.exit(cli.main())
sys.exit(cli.main())
'''

RUST_MODE = False          # set by --rust
LAYOUT_1 = False           # set by --layout-1: the Rust side runs 1.0 names
NORMALIZED = [0]           # output lines whose 1.0 program names were read back
IMG = "ipod.img"
MNT = "ipod-mount"


# -- scenarios ----------------------------------------------------------------
# A step is a dict:
#   argv     flashpod arguments ("{img}", "{fw}", "{badfw}", "{music}" expand)
#   env      extra environment
#   pty      [(regex, answer), ...] -> run under a pseudo-terminal
#   write    True if the step must change the image
#   expect   substring that must appear in the baseline's output
#   py       name of a harness action instead of a command (same for both)
#   size     (for "image" py action) bytes

ESC, UP, DN, RT, LT = "\x1b", "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
SUP, SDN, PGUP, PGDN = "\x1b[1;2A", "\x1b[1;2B", "\x1b[5~", "\x1b[6~"


def K(*keys):
    """pty answers for the picker: Enter at the typed-path prompt, then one
    key per drawn frame ("" just waits for a frame)."""
    return [(r"or type a path\): ", "\r")] + [(r"\x1b\[J", k) for k in keys]


def S(argv, **kw):
    d = {"argv": shlex.split(argv)}
    d.update(kw)
    return d


SCENARIOS = {
    "lifecycle": [
        {"py": "image", "size": 96 << 20},
        S("flash {img} --firmware {fw} --yes", write=True,
          expect="verify OK"),
        S("ls --raw {img}", expect="no iPod_Control directory"),
        S("init --raw {img} MyPod", write=True, expect="Initialized"),
        S("ls --raw {img}", expect='iPod "MyPod": 0 tracks'),
        S("add --raw {img} {music}", write=True,
          expect="5 tracks added, 1 failed"),
        S('add --raw {img} "{music}/Alpha Band/First Album/01 Opening.mp3"',
          expect="already on iPod"),
        S("ls --raw {img} all", expect="Second Song"),
        S("ls --raw {img} artist", expect="3 artists (5 tracks)"),
        S("ls --raw {img} artists"),
        S("ls --raw {img} album", expect="4 albums (5 tracks)"),
        S("ls --raw {img} albums"),
        S("ls --raw {img}", expect="5 tracks"),
        S("rm --raw {img} 53", write=True,
          expect="Removed: Alpha Band - Second Song"),
        S("rm --raw {img} 9999", expect="no track with id 9999"),
        S("rm --raw {img} artist", expect="name required"),
        S("rm --raw {img} abc", expect="expected track ids"),
        S("rm --raw {img} album later album", write=True,
          expect="Removed: Alpha Band - Return"),
        S("rm --raw {img} artist Nobody", expect="no tracks match"),
        S("rm --raw {img} artist beta group", write=True,
          expect="Removed: Beta Group - Third"),
        S("ls --raw {img} all"),
        S("rebuild --raw {img}", write=True, expect="Rebuilt the iTunesDB"),
        S("ls --raw {img} all", expect="2 tracks"),
        # Re-add after the rebuild: tagged files only. Re-adding the untagged
        # WAV here hit a baseline bug (a rebuilt untagged track wasn't seen as
        # a duplicate, so it was copied again); that fix has its own
        # before/after test, scripts/test_rebuild_dedup.py.
        S('add --raw {img} "{music}/Alpha Band" '
          '"{music}/Beta Group/Only Album/03 Third.mp3"', write=True,
          expect="3 tracks added, 1 skipped"),
        S("ls --raw {img} all"),
        S("rm --raw {img} 55 56", write=True, expect="Removed 2 tracks"),
        S("ls --raw {img} all", expect="3 tracks"),
    ],
    "rejections": [
        # BUG in the baseline: an image too small for any data partition died
        # with a traceback (paths + line numbers, so its stderr can't match).
        # Fixed; named below. Exit code and image must still match.
        {"py": "image", "size": 1 << 20},
        S("flash {img} --firmware {fw} --yes", intended={
            "why": "a too-small card is refused in words, not a traceback",
            "stderr": "firmware OK: fw.bin (225280 bytes, structure "
                      "validated)\nipod.img is too small for an iPod card: "
                      "1 MiB leaves no room for a data partition after the "
                      "32 MiB firmware partition.\n"}),
        {"py": "image", "size": 64 << 20},
        S("flash {img} --firmware {badfw} --yes", expect="checksum mismatch"),
        S("flash {img} --firmware {fw} --dry-run", expect="dry-run"),
        S("ls --raw {img}", expect="couldn't read a FAT"),
        S("add --raw {img} {music}", expect="couldn't open a FAT"),
        # 64 MiB leaves too few clusters for FAT32: flashed, but unformatted
        S("flash {img} --firmware {fw} --yes", write=True,
          expect="too small for FAT32"),
        S("init --raw {img}", expect="couldn't open a FAT"),
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        # BUG in the baseline: add on a card with no iTunesDB exited 1
        # SILENTLY. Fixed; the message is the one intended difference here.
        S("add --raw {img} {music}", empty_ok=True, intended={
            "why": "add on a card with no iTunesDB now says so",
            "stderr": "flashpod: no iTunesDB on ipod.img (run `flashpod init` "
                      "first?)\n"}),
        S("rm --raw {img} 1", expect="no iTunesDB"),
        S("init --raw {img}", write=True),
        S('add --raw {img} "{music}/Beta Group/broken.mp3"',
          expect="unreadable"),
        S('add --raw {img} "{music}/Alpha Band/notes.txt"',
          expect="not a recognized audio file"),
        S("add --raw {img} /nonexistent/path"),
        {"py": "corrupt_db"},
        S("ls --raw {img}", expect="failed to parse"),
        S("add --raw {img} {music}", expect="corrupt"),
        S("rm --raw {img} 1", expect="corrupt"),
    ],
    "limits": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {music}", env={"FLASHPOD_DB_LIMIT": "3000"},
          expect="would fit"),
        S("add --raw {img} {music}", env={"FLASHPOD_DB_LIMIT": "100"},
          expect="already exceeds"),
        S("add --raw {img} {music}", env={"FLASHPOD_DB_LIMIT": "0"},
          write=True, expect="tracks added"),
        S("add --raw {img} {big}", expect="free"),
    ],
    "terminal": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw}", write=True, pty=[
            (r'Type "ERASE ipod\.img" to proceed: ', "ERASE ipod.img\r"),
            (r"Run init on .* now\? \[Y/n\] ", "y\r"),
            (r"Load music onto the card now\? \[Y/n\] ", "y\r"),
            (r"or type a path\): ", "{music}\r"),
        ], expect="tracks added"),
        S("add --raw {img} {music}", pty=[], expect="already on iPod"),
        {"py": "corrupt_db"},
        S("add --raw {img} {music}", pty=[(r"\[Aye/Nay\] ", "n\r")],
          expect="belay that"),
        # BUGS in the baseline, both named here: the rebuilt untagged track
        # wasn't seen as a duplicate (so "1 track added" was a second copy),
        # and an accepted rebuild was only saved if something was added.
        S("add --raw {img} {music}", write=True,
          pty=[(r"\[Aye/Nay\] ", "\r")], expect="1 track added",
          intended={"why": "after the rebuild every file is a duplicate, and "
                           "the rebuilt database is saved anyway",
                    "stdout_has": "0 tracks added, 5 skipped (already on "
                                  "iPod), 1 failed",
                    "sha": "changed"}),
        S("ls --raw {img}", expect="tracks",
          intended={"why": "the card lists the 5 rebuilt tracks (the "
                           "baseline: 6, one a second copy)",
                    "stdout_has": '"iPod": 5 tracks, 3 artists, 4 albums',
                    "sha": "unchanged"}),
    ],
    "terminal_dblimit": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {music}", env={"FLASHPOD_DB_LIMIT": "3000"},
          pty=[(r"Add just those\? \[Y/n\] ", "n\r"),
               (r"Add ALL anyway.*\[y/N\] ", "\r")],
          expect="nothing added"),
        S("add --raw {img} {music}", env={"FLASHPOD_DB_LIMIT": "3000"},
          write=True, pty=[(r"Add just those\? \[Y/n\] ", "\r")],
          expect="dropped to fit the database ceiling"),
    ],
    "terminal_space": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {big} {music}", pty=[(r"enter add", "q")],
          expect="nothing added"),
        S("add --raw {img} {big} {music}", write=True,
          pty=[(r"enter add", "\r")], expect="dropped to fit free space"),
    ],
    # Every key of the cursor selector (rows: (unknown) 60 MiB, Alpha Band,
    # Beta Group): over budget refused, toggles, arrows, all/none; then
    # the numbered fallback, forced by a terminal too short for the list.
    "terminal_keys": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {big} {music}", write=True,
          pty=[(r"enter add", " \rj \x1b[A anx\x1b[B\x1b[B k \r")],
          expect="tracks added"),
    ],
    "terminal_lines": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {big} {music}", env={"LINES": "4"}, write=True,
          pty=[(r"q=quit: ", "1\r"), (r"q=quit: ", "\r"),
               (r"q=quit: ", "1,x\r"), (r"q=quit: ", ",2\r"),
               (r"q=quit: ", "a\r"), (r"q=quit: ", "n\r"),
               (r"q=quit: ", "3 9\r"), (r"q=quit: ", "3  2\r"),
               (r"q=quit: ", "\r")],
          expect="tracks added"),
    ],
    # A corrupt database met in a terminal: the rebuild offer, declined and
    # accepted, and `rebuild` itself drawing its progress line.
    "terminal_rebuild": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S("add --raw {img} {music}", write=True, expect="tracks added"),
        {"py": "corrupt_db"},
        S("add --raw {img} {music}", pty=[(r"\[Aye/Nay\] ", "n\r")],
          expect="belay that"),
        S("add --raw {img} {music}", write=True,
          pty=[(r"\[Aye/Nay\] ", "\r")], expect="skipped"),
        {"py": "corrupt_db"},
        S("rebuild --raw {img}", write=True, pty=[],
          expect="Rebuilt the iTunesDB"),
        S("ls --raw {img} all", expect="tracks"),
    ],
    # The --mount path (an OS-mounted iPod) on a plain directory: same
    # commands, the kernel's FAT replaced by the host filesystem.
    "mount": [
        {"py": "mkdir"},
        S("rebuild --mount {mnt}", expect="is this an iPod"),
        S("add --mount {mnt} {music}", expect="no iTunesDB"),
        S("init --mount {mnt} MountPod", write=True, expect="Initialized"),
        S("ls --mount {mnt}", expect='"MountPod": 0 tracks'),
        S("add --mount {mnt} {music}", write=True,
          expect="5 tracks added, 1 failed"),
        S('add --mount {mnt} "{music}/Alpha Band/First Album/01 Opening.mp3"',
          expect="already on iPod"),
        S("ls --mount {mnt} all", expect="Second Song"),
        S("ls --mount {mnt} artists", expect="3 artists"),
        S("ls --mount {mnt} albums", expect="4 albums"),
        S("rm --mount {mnt} 53", write=True, expect="Removed: Alpha Band"),
        S("rm --mount {mnt} 9999", expect="no track with id"),
        S("rm --mount {mnt} album later album", write=True),
        S("rm --mount {mnt} artist beta group", write=True),
        S("rebuild --mount {mnt}", write=True, expect="Rebuilt the iTunesDB"),
        S("ls --mount {mnt} all", expect="2 tracks"),
        S('add --mount {mnt} "{music}/Alpha Band" '
          '"{music}/Beta Group/Only Album/03 Third.mp3"', write=True,
          expect="tracks added"),
        S("rm --mount {mnt} 55 56", write=True, expect="Removed 2 tracks"),
        S("ls --mount {mnt} all"),
        S("add --mount {mnt} {big}", env={"FP_DISK_FREE": "20000000"},
          expect="free"),
        S('add --mount {mnt} "{music}/Alpha Band" '
          '"{music}/Beta Group/Only Album/03 Third.mp3"',
          env={"FLASHPOD_DB_LIMIT": "100"}, expect="already exceeds"),
        {"py": "corrupt_db"},
        S("ls --mount {mnt}", expect="failed to read"),
        S("add --mount {mnt} {music}", expect="failed to read"),
        S("rm --mount {mnt} 52", expect="failed to read"),
        S("init --mount {mnt} Again", write=True, expect="Initialized"),
        S("ls --mount {mnt}", expect='"Again": 0 tracks'),
    ],
    "mount_terminal_db": [
        {"py": "mkdir"},
        S("init --mount {mnt}", write=True),
        S("add --mount {mnt} {music}", env={"FLASHPOD_DB_LIMIT": "3000"},
          write=True, pty=[(r"Add just those\? \[Y/n\] ", "\r")],
          expect="dropped to fit the database ceiling"),
    ],
    "mount_terminal_space": [
        {"py": "mkdir"},
        S("init --mount {mnt}", write=True),
        S("add --mount {mnt} {big} {music}", env={"FP_DISK_FREE": "40000000"},
          write=True, pty=[(r"enter add", "\r")],
          expect="dropped to fit free space"),
    ],
    # add with no paths: the directory picker, driven key by key. Each key
    # is sent once a frame has been drawn (a frame ends with ESC[J).
    "picker": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw} --yes", write=True),
        S("init --raw {img}", write=True),
        S('add --raw {img} "{music}/Beta Group"', write=True),
        {"py": "picker_home"},
        S("add --raw {img}", pty=K(ESC), expect="cancelled"),
        S("add --raw {img}", pty=K("q"), expect="cancelled"),
        S("add --raw {img}", pty=K("\x03"), expect="cancelled"),
        S("add --raw {img}", pty=K("\r"), expect="nothing selected"),
        # a tour of every key, ending in a cancel
        S("add --raw {img}", pty=K(
            DN, DN, DN, RT,                     # Empty: nothing to open
            DN, RT,                             # HiddenOnly: only a dotfile
            ".", RT, LT, ".",                   # shown, opened, hidden again
            DN, DN, RT,                         # Many
            PGDN, PGDN, "\x1b[F", PGUP, "\x1b[H",
            "\x1b[4~", "\x1b[1~", "\x1b[6~", "\x1b[5~", "\x1bOB", "\x1bOA",
            "\x1bOF", "\x1bOH", "\x1b[B", "\x1b[A",
            SDN, SDN, SDN, SUP, "\x1b[b", "\x1b[a", " ",
            "v", DN, DN, "v", DN, "v", UP, UP, UP, "v",
            "x", "\x1bOP", "\x1b[Z", "\x1b[1;5A", "\x1bq", "\u00e9", "",
            RT,                                 # a file: nothing to open
            LT, "\x1b[H", RT, RT, " ", LT, LT,     # into the long name
            RT,                                 # (now up in work/) back down the trail
            "\x1b[H", DN, DN, RT, RT, RT, RT, RT, RT, RT,   # deep/one/.../five
            LT, LT, LT, LT, LT, LT, LT, RT,
            ESC), expect="cancelled"),
        # pick an album and a track that's already on the iPod, then add
        S("add --raw {img}", write=True, pty=K(
            "\x1b[F", UP, UP, UP, UP, RT, RT, " ", LT, DN, RT, RT, " ", "\r"),
          expect="Adding 2 selected paths"),
        # a small terminal: columns scroll off, the header path is cut
        S("add --raw {img}", env={"COLUMNS": "60", "LINES": "12"}, pty=K(
            RT, LT, DN, DN, RT, RT, RT, RT, RT, RT, PGDN, LT, LT, LT, LT, LT, LT,
            "\x1b[H", DN, DN, DN, DN, DN, DN, RT, PGDN, PGDN, ESC),
          expect="cancelled"),
        S("add --raw {img}", env={"COLUMNS": "25", "LINES": "6"},
          pty=K(DN, RT, DN, " ", LT, ESC), expect="cancelled"),
        # a home whose parent can't be listed
        S("add --raw {img}", env={"HOME": "{work}/locked/home2"},
          pty=K(DN, " ", LT, LT, RT, RT, ESC), expect="unreadable: [Errno 13]"),
        # a small DB budget: the status bar goes red as the selection grows
        S("add --raw {img}", env={"FLASHPOD_DB_LIMIT": "6000"}, pty=K(
            "\x1b[F", UP, UP, UP, UP, " ", DN, DN, " ", UP, UP, UP, UP, UP, " ",
            " ", ESC), expect="of DB budget"),
    ],
    # `terminal`'s flash -> init -> add offers, judged against the current
    # tree (that scenario's expectations are the 2686d4a baseline's), plus
    # the answers that decline each offer and a mistyped path.
    "terminal_flash": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --firmware {fw}", write=True, pty=[
            (r'Type "ERASE ipod\.img" to proceed: ', "ERASE ipod.img\r"),
            (r"Run init on .* now\? \[Y/n\] ", "y\r"),
            (r"Load music onto the card now\? \[Y/n\] ", "y\r"),
            (r"or type a path\): ", "{music}\r"),
        ], expect="tracks added"),
        S("add --raw {img}", pty=[(r"or type a path\): ", "{music}/nope\r")],
          expect="no such file"),
        S("add --raw {img}", pty=[(r"or type a path\): ", "{music}\r")],
          expect="already on iPod"),
        S("flash {img} --firmware {fw}", write=True, pty=[
            (r'Type "ERASE ipod\.img" to proceed: ', "ERASE ipod.img\r"),
            (r"Run init on .* now\? \[Y/n\] ", "n\r"),
        ], expect="Skipped"),
        S("flash {img} --firmware {fw}", write=True, pty=[
            (r'Type "ERASE ipod\.img" to proceed: ', "ERASE ipod.img\r"),
            (r"Run init on .* now\? \[Y/n\] ", "\r"),
            (r"Load music onto the card now\? \[Y/n\] ", "n\r"),
        ], expect="Initialized"),
        S("ls --raw {img}", expect='"iPod": 0 tracks'),
    ],
    # A file of many 64 KiB chunks copied over a mount in a terminal: the
    # copy's own progress throttle shows in the window.
    "mount_terminal_big": [
        {"py": "mkdir"},
        S("init --mount {mnt}", write=True),
        S("add --mount {mnt} {big}", write=True, pty=[],
          expect="1 track added"),
    ],
    # flash without --firmware: the model and firmware choosers over a
    # catalog served from 127.0.0.1, the download, the cache, checksums.
    "flash_catalog": [
        {"py": "image", "size": 80 << 20},
        S("flash {img} --yes", env={"FP_MANIFEST": "{manifest}"}, write=True,
          expect="verified and cached"),
        S("flash {img} --yes --dry-run", env={"FP_MANIFEST": "{manifest}"},
          expect="using default firmware"),
        S("flash {img} --yes --model 9G", env={"FP_MANIFEST": "{manifest}"},
          expect="unknown model"),
        S("flash {img} --yes --dry-run --model 2G", env={"FP_MANIFEST": "{manifest}"},
          pty=[(r"Select firmware \[1\]: ", "0\r")], expect="checksum"),
        S("flash {img} --yes --dry-run --model 2g", env={"FP_MANIFEST": "{manifest}"},
          pty=[(r"Select firmware \[1\]: ", "2\r")], expect="404"),
        S("flash {img} --dry-run", env={"FP_MANIFEST": "{manifest}"},
          pty=[(r"Select model: ", "1\r"), (r"Select firmware \[1\]: ", "\r")],
          expect="dry-run"),
        S("flash {img} --dry-run", env={"FP_MANIFEST": "{manifest}"},
          pty=[(r"Select model: ", "x\r")], expect="invalid selection"),
        S("flash {img} --dry-run --model 1G", env={"FP_MANIFEST": "{manifest}"},
          pty=[(r"Select firmware \[0\]: ", "7\r")], expect="invalid selection"),
    ],
    "wrong_confirm": [
        {"py": "image", "size": 64 << 20},
        S("flash {img} --firmware {fw}",
          pty=[(r"to proceed: ", "ERASE sdb\r")],
          expect="did not match"),
    ],
}


# -- running ------------------------------------------------------------------
def sha(path):
    """sha256 of an image file, or of a directory tree (each file's relative
    path and bytes, in sorted order) for the --mount scenarios."""
    if not os.path.exists(path):
        return "-"
    h = hashlib.sha256()
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for fn in sorted(files):
                full = os.path.join(root, fn)
                h.update(os.path.relpath(full, path).encode() + b"\0")
                with open(full, "rb") as f:
                    h.update(f.read())
                h.update(b"\0")
            for d in dirs:
                h.update(b"D" + os.path.relpath(os.path.join(root, d),
                                                path).encode() + b"\0")
        return h.hexdigest()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def env_for(work, tree, extra):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.path.join(work, "home"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8", "TERM": "xterm",
        "FP_TREE": tree,
    }
    env.update(extra or {})
    return env


def expand_arg(a, ctx):
    return a.format(**ctx)


def run_pipe(cmd, env, cwd):
    p = subprocess.run(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=600)
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


def run_pty(cmd, env, cwd, answers, timeout=120):
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    def child_setup():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=slave, stdout=slave,
                         stderr=slave, preexec_fn=child_setup, close_fds=True)
    os.close(slave)
    out = b""
    seen = 0                       # answers match only text after the last one
    pending = list(answers)
    deadline = time.monotonic() + timeout
    note = ""
    while True:
        if pending:
            rx, ans = pending[0]
            m = re.search(rx.encode(), out[seen:])
            if m:
                os.write(master, ans.encode())
                seen += m.end()
                pending.pop(0)
                continue
        if time.monotonic() > deadline:
            note = "\n[harness] TIMEOUT waiting for %r\n" % (
                pending[0][0] if pending else "exit")
            p.kill()
            break
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError as e:
                if e.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            out += data
        elif p.poll() is not None:
            # drain whatever is left, then stop
            try:
                while select.select([master], [], [], 0.1)[0]:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    out += data
            except OSError:
                pass
            break
    rc = p.wait()
    os.close(master)
    if pending and not note:
        note = "\n[harness] never saw prompt %r\n" % pending[0][0]
    return rc, out.decode("utf-8", "replace") + note, ""


def picker_home(work):
    """The home directory the picker opens on: dirs and files to sort
    (case, Unicode), an empty directory, one holding only a dotfile, a
    dotdir, a long name, a deep chain, a page-overflowing listing, a
    broken symlink and a symlinked directory, plus the synthetic music.
    work/locked (no read permission) holds a second home whose parent
    can't be listed."""
    home = os.path.join(work, "home")
    shutil.copytree(os.path.join(work, "music"), os.path.join(home, "Music"))

    def touch(*parts):
        path = os.path.join(home, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    os.makedirs(os.path.join(home, "Empty"))
    touch("HiddenOnly", ".keep")
    touch(".config", "settings.txt")
    touch("A folder whose name is far too long to fit one column", "song.mp3")
    touch("Caf\u00e9 \u00fcn\u00efcode", "na\u00efve.mp3")
    for i in range(1, 31):
        touch("Many", "Track %02d.mp3" % i)
    touch("deep", "one", "two", "three", "four", "five", "end.mp3")
    for name in ("alpha.txt", "Beta.MP3", "._junk.mp3", "notes.txt"):
        touch(name)
    os.symlink("nowhere", os.path.join(home, "dangling"))
    os.symlink("Music", os.path.join(home, "linkdir"))
    os.makedirs(os.path.join(work, "locked", "home2", "inner"))
    open(os.path.join(work, "locked", "home2", "x.mp3"), "w").close()
    os.chmod(os.path.join(work, "locked"), 0o311)


def corrupt_db_dir(mnt):
    """The --mount twin of corrupt_db: break the magic of the DB file."""
    db = os.path.join(mnt, "iPod_Control", "iTunes", "iTunesDB")
    with open(db, "r+b") as f:
        f.write(b"XXXX")


def corrupt_db(img):
    """Break the iTunesDB in place so it no longer parses: every "mhbd" magic
    in the data partition (the live DB and any stale freed copy) becomes
    "XXXX". The structure still says iPod; the database is unreadable.

    (Overwriting the body instead does NOT make it unreadable: the parser
    stops at the first unexpected header and returns an EMPTY library --
    recorded as a finding, 2026-10-16.)"""
    with open(img, "r+b") as f:
        data = f.read()
        start = 65599 * 512
        hits = 0
        i = data.find(b"mhbd", start)
        while i >= 0:
            f.seek(i)
            f.write(b"XXXX")
            hits += 1
            i = data.find(b"mhbd", i + 4)
        if not hits:
            raise RuntimeError("no iTunesDB in the image to corrupt")


def run_scenario(name, steps, tree, work, fixtures, runner, rust=None):
    locked = os.path.join(work, "locked")
    if os.path.isdir(locked):
        os.chmod(locked, 0o755)          # picker_home's unreadable directory
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "home"))
    shutil.copytree(fixtures["music"], os.path.join(work, "music"))
    ctx = {"img": IMG, "mnt": MNT, "fw": fixtures["fw"], "work": work,
           "badfw": fixtures["badfw"], "music": os.path.join(work, "music"),
           "manifest": fixtures["manifest"],
           "big": fixtures["big"]}
    # the target whose bytes each step is judged by: the image, or for the
    # --mount scenarios the directory standing in for a mounted iPod
    img = os.path.join(work, MNT if name.startswith("mount") else IMG)
    records = []
    for i, step in enumerate(steps):
        if "py" in step:
            if step["py"] == "image":
                if os.path.exists(img):
                    os.remove(img)
                with open(img, "wb") as f:
                    f.truncate(step["size"])
            elif step["py"] == "corrupt_db":
                (corrupt_db_dir if os.path.isdir(img) else corrupt_db)(img)
            elif step["py"] == "mkdir":
                os.makedirs(img)
            elif step["py"] == "picker_home":
                picker_home(work)
            records.append({"step": i, "py": step["py"], "sha": sha(img)})
            continue
        argv = [expand_arg(a, ctx) for a in step["argv"]]
        if rust and LAYOUT_1:
            argv = layout1.argv_1_0(argv)
        cmd = ([rust] if rust else [sys.executable, runner]) + argv
        extra = {k: v.format(**ctx) for k, v in (step.get("env") or {}).items()}
        if RUST_MODE:
            extra.setdefault("FLASHPOD_SEED", "1234")
            extra.setdefault("FLASHPOD_NOW", "1760000000")
            # the runner's shutil.disk_usage default, said out loud for the
            # Rust binary, which reads FP_DISK_FREE in its place
            extra.setdefault("FP_DISK_FREE", str(10 << 30))
        env = env_for(work, tree, extra)
        before = sha(img)
        if "pty" in step:
            answers = [(rx, ans.format(**ctx)) for rx, ans in step["pty"]]
            rc, out, err = run_pty(cmd, env, work, answers)
        else:
            rc, out, err = run_pipe(cmd, env, work)
        if rust and LAYOUT_1:
            out, n1 = layout1.normalize(out)
            err, n2 = layout1.normalize(err)
            NORMALIZED[0] += n1 + n2
        records.append({"step": i, "argv": " ".join(step["argv"]),
                        "rc": rc, "stdout": out, "stderr": err,
                        "sha": sha(img), "changed": sha(img) != before})
    return records


def check_meaningful(name, steps, recs):
    """Vacuity guard, applied to the BASELINE records."""
    problems = []
    for step, rec in zip(steps, recs):
        if "py" in step:
            continue
        text = rec["stdout"] + rec["stderr"]
        where = "%s step %d (%s)" % (name, rec["step"], rec["argv"])
        if not text.strip() and not step.get("empty_ok"):
            problems.append(where + ": empty output")
        if step.get("write") and not rec["changed"]:
            problems.append(where + ": marked as a write, image unchanged")
        if step.get("expect") and step["expect"] not in text:
            problems.append(where + ": expected %r in output" % step["expect"])
        if "TIMEOUT" in text or "never saw prompt" in text:
            problems.append(where + ": " + text.strip().splitlines()[-1])
    return problems


#: Named text changes applied to the BASELINE's output before comparing,
#: wherever they occur: (why, old, new). Each must occur somewhere in the
#: baseline run, or the harness fails (a stale rewrite proves nothing).
NAMED_REWRITES = [
    ("512-byte clusters read '@ 0 KiB' in the flash summary",
     " clusters @ 0 KiB,", " clusters @ 512 bytes,"),
]


def rewrite_baseline(records):
    """Apply NAMED_REWRITES to baseline records; returns the set of rewrites
    that matched somewhere."""
    used = set()
    for r in records:
        for key in ("stdout", "stderr"):
            text = r.get(key)
            if not text:
                continue
            for why, old, new in NAMED_REWRITES:
                if old in text:
                    text = text.replace(old, new)
                    used.add(why)
            r[key] = text
    return used


#: streams compared so far, for the RESULT line
COMPARED = [0]


def compare(name, a, b, steps=None):
    """Differences between baseline records ``a`` and tree records ``b``. A
    step with ``intended`` = {"why", "stdout"/"stderr": exact new text} is a
    NAMED behaviour change: its exit code and image must still match the
    baseline, and the named stream must be exactly the new text."""
    diffs = []
    steps = steps or [{}] * len(a)
    for ra, rb, step in zip(a, b, steps):
        intended = step.get("intended") or {}
        for key in ("rc", "stdout", "stderr", "sha"):
            if "argv" in ra:             # a py setup step runs no command
                COMPARED[0] += 1
            if key + "_has" in intended:          # named: must contain
                if intended[key + "_has"] not in (rb.get(key) or ""):
                    diffs.append("%s step %d: intended change (%s) not as "
                                 "named: %r lacks %r" % (
                                     name, ra["step"], intended["why"],
                                     (rb.get(key) or "")[-300:],
                                     intended[key + "_has"]))
                continue
            if key == "sha" and intended.get("sha") in ("changed",
                                                         "unchanged"):
                # the image follows an earlier named change: only whether
                # THIS step wrote it is compared
                want = intended["sha"] == "changed"
                if bool(rb.get("changed")) != want:
                    diffs.append("%s step %d: intended change (%s): the image "
                                 "should%s have changed" % (
                                     name, ra["step"], intended["why"],
                                     "" if want else " not"))
                continue
            if key in intended:
                if rb.get(key) != intended[key]:
                    diffs.append("%s step %d: intended change (%s) not as "
                                 "named: %r" % (name, ra["step"],
                                                intended["why"], rb.get(key)))
                continue
            if ra.get(key) != rb.get(key):
                where = "%s step %d (%s): %s differs" % (
                    name, ra["step"], ra.get("argv", ra.get("py")), key)
                if key in ("stdout", "stderr"):
                    d = "\n".join(difflib.unified_diff(
                        str(ra[key]).splitlines(), str(rb[key]).splitlines(),
                        "baseline", "tree", lineterm="", n=2))
                    where += "\n" + d
                else:
                    where += ": %r vs %r" % (ra.get(key), rb.get(key))
                diffs.append(where)
                break
    if len(a) != len(b):
        diffs.append("%s: %d vs %d steps" % (name, len(a), len(b)))
    return diffs


def serve_catalog(root, fw):
    """A firmware catalog like firmware/firmware.json, its images served by
    a local HTTP server for the run's lifetime: a good default, one whose
    sha256 is wrong, one the server doesn't have. Returns the manifest."""
    import hashlib
    import http.server
    import json
    import threading
    site = os.path.join(root, "site")
    os.makedirs(site, exist_ok=True)
    with open(fw, "rb") as f:
        data = f.read()
    for name in ("good.bin", "tampered.bin"):
        with open(os.path.join(site, name), "wb") as f:
            f.write(data)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=site, **kw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    sha = hashlib.sha256(data).hexdigest()
    manifest = {
        "base_url": base,
        "models": [
            {"id": "1G", "name": "1st generation", "years": "2001-02",
             "tell": "scroll wheel physically turns"},
            {"id": "2G", "name": "2nd generation", "years": "2002-03",
             "tell": "touch wheel, four buttons ring the wheel; a "
                     "description long enough that the table has to wrap"},
        ],
        "firmwares": [
            {"file": "tampered.bin", "sha256": "0" * 64, "size": len(data),
             "generation": "synthetic", "models": ["2G"], "version": "9.9",
             "maker": "test", "description": "checksum will not match"},
            {"file": "good.bin", "sha256": sha, "size": len(data),
             "generation": "synthetic", "models": ["1G", "2G"],
             "version": "1.5", "build_date": "2005-02-18",
             "description": "the synthetic image, the default",
             "default": True},
            {"file": "missing.bin", "generation": "synthetic",
             "models": ["2G"], "version": "0.1",
             "description": "the server has no such file"},
        ],
    }
    path = os.path.join(root, "manifest.json")
    with open(path, "w") as f:
        json.dump(manifest, f)
    return path


def make_fixtures(root):
    music = synth.make_music(os.path.join(root, "music"))
    fw = synth.write_firmware(os.path.join(root, "fw.bin"))
    badfw = synth.write_firmware(os.path.join(root, "badfw.bin"),
                                 bad_checksum=True)
    big = synth.big_wav(os.path.join(root, "big.wav"), 60)
    return {"music": music, "fw": fw, "badfw": badfw, "big": big,
            "manifest": serve_catalog(root, fw)}


def run_all(tree, root, fixtures, runner, only=None, rust=None):
    out = {}
    for name, steps in SCENARIOS.items():
        if only and name not in only:
            continue
        out[name] = run_scenario(name, steps, tree,
                                 os.path.join(root, "work-" + name),
                                 fixtures, runner, rust)
    return out


#: Deliberate breakages: (description, file glob under flashpod/, old, new).
#: Each must apply exactly once to the tree under test and make the comparison
#: fail -- proof the harness can see a change of that kind.
MUTANTS = [
    ("a changed message",
     "not a recognized audio file", "not a recognised audio file"),
    ("an off-by-one in the database fit count",
     "            hi = mid - 1\n    return lo\n",
     "            hi = mid - 1\n    return max(0, lo - 1)\n"),
]


def apply_mutant(tree, old, new):
    hits = []
    for dirpath, _, files in os.walk(os.path.join(tree, "flashpod")):
        for fn in files:
            if fn.endswith(".py"):
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    s = f.read()
                if s.count(old):
                    hits.append((p, s))
    if len(hits) != 1 or hits[0][1].count(old) != 1:
        raise RuntimeError("mutant %r applies %d times, need exactly 1"
                           % (old[:40], sum(h[1].count(old) for h in hits)))
    p, s = hits[0]
    with open(p, "w", encoding="utf-8") as f:
        f.write(s.replace(old, new))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True,
                    help="a checkout of the pre-refactor commit")
    ap.add_argument("--tree", default=os.path.dirname(HERE),
                    help="tree under test (default: this checkout)")
    ap.add_argument("--rust", metavar="BIN",
                    help="run the tree-under-test side with this Rust "
                         "flashpod binary (the baseline stays Python)")
    ap.add_argument("--layout-1", action="store_true",
                    help="with --rust: run the 1.0 command names (music ls, "
                         "firmware write, ...) against the baseline's 0.x ones; "
                         "1.0 program names in output are read back first")
    ap.add_argument("--self", action="store_true",
                    help="compare the baseline against itself (determinism)")
    ap.add_argument("--mutants", action="store_true",
                    help="also require each deliberate mutant to FAIL")
    ap.add_argument("--only", nargs="*", help="scenario names")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--dump", action="store_true",
                    help="print every baseline step's output (for writing "
                         "expectations)")
    opts = ap.parse_args()
    global RUST_MODE, LAYOUT_1
    RUST_MODE = bool(opts.rust)
    LAYOUT_1 = bool(opts.layout_1)

    baseline = os.path.abspath(opts.baseline)
    tree = os.path.abspath(baseline if opts.self else opts.tree)
    root = tempfile.mkdtemp(prefix="fp-unchanged-")
    try:
        runner = os.path.join(root, "runner.py")
        with open(runner, "w") as f:
            f.write(RUNNER)
        fixtures = make_fixtures(os.path.join(root, "fixtures"))
        base = run_all(baseline, root, fixtures, runner, opts.only)
        asked = sum(1 for r in base.values() for x in r if "argv" in x)
        bin_ = os.path.abspath(opts.rust) if opts.rust else None
        main_compared = [0]          # the mutant runs below compare too

        def result(failed, new=None, mutants=None):
            entered = 0
            if new is not None:
                entered = sum(1 for name in base
                              for ra, rb in zip(base[name], new.get(name, []))
                              if "argv" in ra and "rc" in ra and "rc" in rb)
            return result_line.emit(
                "cli_unchanged", tree, asked, entered, main_compared[0], failed,
                {"baseline": base, "tree": new}, binary=bin_, mutants=mutants)
        if opts.dump:
            for name, recs in base.items():
                for r in recs:
                    print("==== %s step %d: %s  rc=%s changed=%s" % (
                        name, r["step"], r.get("argv", r.get("py")),
                        r.get("rc"), r.get("changed")))
                    print(r.get("stdout", "") + r.get("stderr", ""))
        problems = []
        for name, recs in base.items():
            problems += check_meaningful(name, SCENARIOS[name], recs)
        if problems:
            print("BASELINE RUN IS NOT MEANINGFUL:")
            print("\n".join("  " + p for p in problems))
            return result(True)
        if opts.self:
            # the baseline against itself: no named change applies
            NAMED_REWRITES[:] = []
            for steps in SCENARIOS.values():
                for st in steps:
                    st.pop("intended", None)
        used = set()
        for recs in base.values():
            used |= rewrite_baseline(recs)
        stale = [w for w, _o, _n in NAMED_REWRITES if w not in used]
        if stale and not opts.only:
            print("NAMED REWRITE NEVER MATCHED (stale): " + "; ".join(stale))
            return result(True)
        nsteps = sum(1 for r in base.values() for x in r if "argv" in x)
        print("baseline %s: %d scenarios, %d commands, all meaningful"
              % (baseline, len(base), nsteps))

        new = run_all(tree, root, fixtures, runner, opts.only,
                      os.path.abspath(opts.rust) if opts.rust else None)
        diffs = []
        for name in base:
            diffs += compare(name, base[name], new[name], SCENARIOS[name])
        main_compared[0] = COMPARED[0]
        if diffs:
            print("DIFFERENT from baseline (%s):" % tree)
            print("\n\n".join(diffs))
            return result(True, new)
        named = [(n, i, st["intended"]["why"]) for n, steps in SCENARIOS.items()
                 if n in base for i, st in enumerate(steps) if st.get("intended")]
        print("IDENTICAL: %s matches the baseline on every step "
              "(rc, stdout, stderr, image sha256)%s" % (
                  tree, ", except %d NAMED change%s, each exactly as stated:"
                  % (len(named), "s" if len(named) != 1 else "")
                  if named else ""))
        for n, i, why in named:
            print("  - %s step %d: %s" % (n, i, why))
        for why, old, new_s in NAMED_REWRITES:
            print("  - everywhere: %r -> %r (%s)" % (old, new_s, why))
        if LAYOUT_1:
            print("  - layout 1.0: the Rust side ran the 1.0 command names; "
                  "%d output line(s) had their 1.0 program name read back"
                  % NORMALIZED[0])

        if opts.mutants:
            failed_to_catch = []
            for desc, old, new_s in MUTANTS:
                mt = os.path.join(root, "mutant")
                if os.path.exists(mt):
                    shutil.rmtree(mt)
                shutil.copytree(tree, mt, ignore=shutil.ignore_patterns(
                    ".git", "build", "dist", "__pycache__", "*.img"))
                apply_mutant(mt, old, new_s)
                got = run_all(mt, root, fixtures, runner, opts.only)
                d = []
                for name in base:
                    d += compare(name, base[name], got[name], SCENARIOS[name])
                if d:
                    print("mutant caught (%s): %s" % (desc, d[0].splitlines()[0]))
                else:
                    failed_to_catch.append(desc)
            caught = (len(MUTANTS) - len(failed_to_catch), len(MUTANTS))
            if failed_to_catch:
                print("MUTANTS NOT CAUGHT: " + "; ".join(failed_to_catch))
            else:
                print("all %d mutants caught" % len(MUTANTS))
            return result(False, new, caught)
        return result(False, new)
    finally:
        if opts.keep:
            print("kept " + root)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
