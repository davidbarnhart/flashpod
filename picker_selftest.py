#!/usr/bin/env python3
"""Headless self-test for pathpicker: exercises the model logic (no
terminal needed) so ports can be sanity-checked before interactive use.

Run: python picker_selftest.py     -> prints "picker selftest: OK"
"""

import os
import re
import shutil
import sys
import tempfile

from pathpicker import DirectoryLister, DirectoryPicker, SelectionSet

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip(line):
    return _ANSI.sub("", line)


def build_tree():
    root = tempfile.mkdtemp(prefix="pickertest-")
    os.makedirs(os.path.join(root, "Music", "New Order"))
    os.makedirs(os.path.join(root, "Empty"))
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        open(os.path.join(root, "Music", "New Order", name), "w").close()
    open(os.path.join(root, "notes.txt"), "w").close()
    open(os.path.join(root, ".hidden"), "w").close()
    return root


def names(picker):
    return [os.path.basename(p) for p in picker.selection.paths()]


def check_lister(root):
    entries, err = DirectoryLister().list(root)
    assert err is None
    assert [e.name for e in entries] == ["Empty", "Music", "notes.txt"]
    assert [e.is_dir for e in entries] == [True, True, False]
    entries, _ = DirectoryLister(show_hidden=True).list(root)
    assert ".hidden" in [e.name for e in entries]
    entries, err = DirectoryLister().list(os.path.join(root, "nope"))
    assert entries == [] and err


def check_selection():
    s = SelectionSet()
    s.toggle("b"); s.toggle("a"); s.toggle("b"); s.add("c"); s.add("c")
    s.discard("nope")
    assert s.paths() == ["a", "c"] and "a" in s and "b" not in s


def check_trail_and_sweep(root):
    p = DirectoryPicker(os.path.join(root, "Music"))
    p._handle("right")                       # into New Order
    assert p.active == 1
    p._handle("shift+down"); p._handle("shift+down")
    assert names(p) == ["a.mp3", "b.mp3", "c.mp3"]
    p._handle("shift+up"); p._handle("shift+up")   # sweep back off
    assert names(p) == []
    p._handle("space")                       # a.mp3 back on
    p._handle("left"); p._handle("left")     # to Music, then prepend root
    assert p.active == 0 and len(p.columns) == 3
    assert p.columns[0].path == root
    p._handle("v"); p._handle("down")        # sweep lock: Music + notes.txt...
    assert p.sweep
    p._handle("v")
    assert not p.sweep and len(p.selection) >= 2


def check_arrows_and_bump(root):
    p = DirectoryPicker(root)                # cursor on Empty/
    frame = [strip(l) for l in p._frame(80, 24)]
    row = [l for l in frame if "Empty" in l][0]
    assert "←" in row and "→" not in row     # parent exists; dir is empty
    p._handle("right")                       # blocked
    assert p._bump and p.active == 0 and len(p.columns) == 1
    p._bump = False
    p._handle("down")                        # Music/
    frame = [strip(l) for l in p._frame(80, 24)]
    row = [l for l in frame if "Music" in l and "selected" not in l][0]
    assert "→" in row                        # has contents
    p._handle("right")
    assert not p._bump and p.active == 1
    p._handle("down"); p._handle("right")    # onto a file eventually
    # cursor is on a dir or file; force the file case explicitly:
    p2 = DirectoryPicker(root)
    p2._handle("down"); p2._handle("down")   # notes.txt
    p2._handle("right")
    assert p2._bump                          # files never open

    drive_root = os.path.splitdrive(os.path.abspath(root))[0] + os.sep
    pr = DirectoryPicker(drive_root)         # "/" on posix, "C:\" on Windows
    frame = [strip(l) for l in pr._frame(80, 24)]
    assert not any("←" in l for l in frame)  # nowhere up from the root
    pr._handle("left")
    assert pr._bump and pr.active == 0 and len(pr.columns) == 1

    b = pr._bumped(pr._frame(80, 24))
    f = pr._frame(80, 24)
    assert b[0] == f[0] and b[-2:] == f[-2:]
    assert all(x == "  " + y for x, y in zip(b[2:-2], f[2:-2]))


def main():
    root = build_tree()
    try:
        check_lister(root)
        check_selection()
        check_trail_and_sweep(root)
        check_arrows_and_bump(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("picker selftest: OK (%s, python %d.%d.%d)"
          % (sys.platform, *sys.version_info[:3]))


if __name__ == "__main__":
    main()
