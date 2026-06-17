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


__all__ = ["Platform", "Unsupported", "current"]
