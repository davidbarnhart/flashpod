#!/usr/bin/env python3
"""Copy an ipod-patches checkout into flashpod's bundled snapshot
(flashpod/firmware/patches), the library flashpod falls back to offline.

    scripts/sync_patches_snapshot.py --from ~/source/ipod-patches

The checkout must check out clean against its own index.json (every listed
file present with the listed size and sha256); only listed files are copied.
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from flashpod import patchlib  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="src", required=True,
                    help="an ipod-patches checkout")
    args = ap.parse_args()
    lib = patchlib.open_dir(os.path.expanduser(args.src), "checkout")
    dst = patchlib.bundled_dir()
    if os.path.isdir(dst):
        if not os.path.exists(os.path.join(dst, "index.json")):
            sys.exit("%s exists but is not a snapshot; not replacing it" % dst)
        shutil.rmtree(dst)
    for path in sorted(lib.files):
        patchlib._write(dst, path, lib.read(path))
    shutil.copyfile(os.path.join(lib.root, "index.json"),
                    os.path.join(dst, "index.json"))
    patchlib.open_dir(dst, "bundled")
    print("snapshot: %d files -> %s" % (len(lib.files), dst))


if __name__ == "__main__":
    main()
