"""Platform abstraction: ``current()`` returns the backend for this OS.

Backends are imported lazily so importing this package is cheap and never
drags in another OS's dependencies.
"""

import sys

from .base import Platform, Unsupported

_cached = None


def current():
    """Return the cached :class:`Platform` backend for the running OS."""
    global _cached
    if _cached is None:
        _cached = _detect()
    return _cached


def _detect():
    plat = sys.platform
    if plat.startswith("linux"):
        from .linux import LinuxPlatform
        return LinuxPlatform()
    if plat == "darwin":
        from .macos import MacOSPlatform
        return MacOSPlatform()
    if plat in ("win32", "cygwin", "msys"):
        from .windows import WindowsPlatform
        return WindowsPlatform()
    # Unknown Unix-like: the Linux backend's POSIX paths are the best bet.
    from .linux import LinuxPlatform
    return LinuxPlatform()


def use_image(path):
    """If ``path`` is a regular file, make :func:`current` return the image
    backend for the rest of the run (see :mod:`.image`) and return True.
    Otherwise change nothing and return False."""
    global _cached
    from .image import ImagePlatform, is_image
    if not is_image(path):
        return False
    if not getattr(current(), "is_image", False):
        _cached = ImagePlatform(current())
    return True


__all__ = ["Platform", "Unsupported", "current", "use_image"]
