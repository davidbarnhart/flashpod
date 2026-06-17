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

# Bundle package data so resources.py finds it under sys._MEIPASS/flashpod/...
# Only the firmware *catalog* (firmware.json) and the udev rule ship inside the
# binary; the .ipsw images are GitHub release assets fetched on demand.
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
