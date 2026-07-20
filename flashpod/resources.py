"""Locate bundled data files (firmware images, the udev rule) whether
flashpod is run from source or from a PyInstaller-frozen binary.

When frozen, PyInstaller unpacks bundled data under ``sys._MEIPASS``; the
build must place the package data there with::

    --add-data "flashpod/firmware:flashpod/firmware"
    --add-data "flashpod/contrib:flashpod/contrib"

(use ``;`` instead of ``:`` on Windows). From source, the data lives next
to this module inside the package.

Python 3.6 compatible.
"""

import os
import sys


def _base_dir():
    """Directory that bundled package data lives under."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # PyInstaller: data added under "flashpod/..." unpacks to _MEIPASS/flashpod
        return os.path.join(meipass, "flashpod")
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    """Absolute path to a bundled resource, e.g. resource_path("firmware")."""
    return os.path.join(_base_dir(), *parts)


def firmware_dir():
    return resource_path("firmware")


def firmware_manifest():
    return resource_path("firmware", "firmware.json")


def udev_rule():
    return resource_path("contrib", "99-flashpod-firewire-ipod.rules")


def build_flavor():
    """Which build this is: ``"full"`` or ``"lite"``.

    A *lite* build ships without the card-imaging half of flashpod. It exists
    for the vintage-Mac (OS X 10.8) artifact, whose only job is syncing music
    to the iPod over FireWire — imaging a card is done on a modern computer
    with a USB card reader, so `flash` is dead weight there.

    The flavor is baked in at build time as a marker file bundled next to the
    other package data (see flashpod.spec, FLASHPOD_FLAVOR=lite). When the
    marker is absent — running from source, from a pip install, or from a
    normal binary — the build is full. That default is deliberate: only an
    explicitly-built lite artifact is ever degraded.
    """
    try:
        with open(resource_path("build_flavor.txt")) as f:
            flavor = f.read().strip().lower()
    except OSError:
        return "full"
    return flavor if flavor in ("full", "lite") else "full"


def is_lite():
    """True when this build has the card-imaging half stripped out."""
    return build_flavor() == "lite"
