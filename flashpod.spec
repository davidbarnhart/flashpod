# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the flashpod CLI — produces a single self-contained
# executable (one per OS) with the firmware images and udev rule bundled in.
#
# Build:  pyinstaller --clean --noconfirm flashpod.spec   ->  dist/flashpod
#
# Kept to the kwargs common to PyInstaller 4.x and 6.x so the same spec
# builds on modern CI (Linux/Windows) and on the legacy macOS 10.8 toolchain
# (Python 3.6 + PyInstaller 4.10).

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# Make the repo-root `flashpod` package importable during analysis (the entry
# script lives in packaging/, so its directory alone isn't enough).
ROOT = os.path.abspath(SPECPATH)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Build flavor. FLASHPOD_FLAVOR=lite produces the vintage-Mac artifact, whose
# only job is syncing music over FireWire: `flash` refuses to run there (see
# resources.is_lite), so the firmware catalog it would consult and the Linux
# udev rule are both left out. Anything else — including a plain build, a
# source checkout, and the pip package — is a full build.
FLAVOR = os.environ.get("FLASHPOD_FLAVOR", "full").strip().lower()
if FLAVOR not in ("full", "lite"):
    raise SystemExit(
        "FLASHPOD_FLAVOR must be 'full' or 'lite', got %r" % FLAVOR)

# Bundle package data so resources.py finds it under sys._MEIPASS/flashpod/...
# Only the firmware *catalog* (firmware.json) and the udev rule ship inside the
# binary; the .ipsw images are GitHub release assets fetched on demand.
if FLAVOR == "lite":
    # The marker's basename becomes the bundled name, so resources.py finds it
    # at flashpod/build_flavor.txt.
    datas = [
        (os.path.join(ROOT, "packaging", "flavor", "build_flavor.txt"),
         "flashpod"),
    ]
else:
    datas = [
        (os.path.join(ROOT, "flashpod", "firmware"), "flashpod/firmware"),
        (os.path.join(ROOT, "flashpod", "contrib"), "flashpod/contrib"),
    ]

# Pull in the whole flashpod package (the platform backends are imported
# lazily) and mutagen's dynamically-loaded format handlers.
hiddenimports = (
    collect_submodules("flashpod") + collect_submodules("mutagen")
)

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="flashpod",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
