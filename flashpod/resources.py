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
