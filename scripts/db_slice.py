#!/usr/bin/env python3
"""Diagnostic: rewrite a mounted iPod's iTunesDB to its first N tracks.

For bisecting firmware library limits ("card boots but shows 0 songs").
Music files are untouched — only the database shrinks, so slicing back
up (or a fresh `flashpod add` of nothing) restores nothing destructive.
The full library is saved to iTunesDB.full alongside on first use and
restored with N = 'all'.

Usage:
  python3 scripts/db_slice.py /media/david/IPOD 1000
  python3 scripts/db_slice.py /media/david/IPOD all      # restore full DB
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flashpod import itunesdb  # noqa: E402


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    mount, n = argv[1], argv[2]
    dbpath = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
    backup = dbpath + ".full"
    if not os.path.exists(dbpath):
        print("no iTunesDB under %s" % mount, file=sys.stderr)
        return 1

    if n == "all":
        if not os.path.exists(backup):
            print("no %s to restore" % backup, file=sys.stderr)
            return 1
        shutil.copyfile(backup, dbpath)
        lib = itunesdb.parse(dbpath)
        print("restored full database: %d tracks" % len(lib.tracks))
    else:
        count = int(n)
        if not os.path.exists(backup):
            shutil.copyfile(dbpath, backup)
            print("full database backed up to %s" % backup)
        lib = itunesdb.parse(backup)
        total = len(lib.tracks)
        lib.tracks = lib.tracks[:count]
        itunesdb.save(lib, mount)
        print("database now lists %d of %d tracks" % (len(lib.tracks), total))

    if hasattr(os, "sync"):
        os.sync()
    print("synced — unmount/eject, then test in the iPod.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
