#!/usr/bin/env python3
"""Logic test for multi-iPod detection (mounted card + unmounted FireWire iPod).

Mocks the platform + I/O so no real device is touched. Run with a python that
has mutagen on its path, from the repo root:

    PYTHONPATH=. /usr/bin/python3 scripts/test_multi_ipod_detect.py
"""
import sys, types, builtins
sys.argv = ["flashpod"]
from flashpod import cli


class FakePlat:
    def __init__(self, admin, mounts, disks):
        self._admin, self._mounts, self._disks = admin, mounts, disks
    def is_admin(self): return self._admin
    def mounted_filesystems(self): return self._mounts
    def fat_disk_candidates(self): return self._disks


def make_opts(cmd):
    return types.SimpleNamespace(command=cmd, files=[], what=[], field=None, name=None)


def run(name, admin, mounts, disks, mount_cands, scan_found, tty, answers):
    cli.platform.current = lambda: FakePlat(admin, mounts, disks)
    cli.candidate_mounts = lambda: mount_cands
    cli.scan_for_ipod = lambda unm: scan_found
    cli.sys.stdin = types.SimpleNamespace(isatty=lambda: tty)
    it = iter(answers)
    builtins.input = lambda prompt="": (print(prompt, end=""), next(it))[1]
    reexeced = {"v": False}
    cli._sudo_reexec = lambda extra: reexeced.__setitem__("v", True)
    res = cli.resolve_raw_target(make_opts("add"))
    print(f"\n[{name}] -> {res}  (reexec={reexeced['v']})")
    return res


# THE BUG SCENARIO (as root, post-elevation): flashed card mounted in a reader,
# FireWire iPod attached but unmounted -> must offer BOTH.
r = run("mounted card + unmounted firewire iPod: pick firewire",
        admin=True,
        mounts=[("/dev/sdb2", "/media/david/IPOD", "vfat")],
        disks=[("/dev/sdb2", "IPOD usb 64G"), ("/dev/sdc2", "IPOD sbp 64G")],
        mount_cands=[(11, "/media/david/IPOD")],
        scan_found=[("/dev/sdc2", "IPOD sbp 64G")],
        tty=True, answers=["1"])
assert r == ("raw", "/dev/sdc2"), r

r = run("same, but user picks the mounted card",
        admin=True,
        mounts=[("/dev/sdb2", "/media/david/IPOD", "vfat")],
        disks=[("/dev/sdb2", "IPOD usb 64G"), ("/dev/sdc2", "IPOD sbp 64G")],
        mount_cands=[(11, "/media/david/IPOD")],
        scan_found=[("/dev/sdc2", "IPOD sbp 64G")],
        tty=True, answers=["0"])
assert r == ("mount", "/media/david/IPOD"), r

# Regression: a single mounted card, nothing else -> fast path, no chooser/sudo.
r = run("single mounted card only (fast path unchanged)",
        admin=False,
        mounts=[("/dev/sdb2", "/media/david/IPOD", "vfat")],
        disks=[("/dev/sdb2", "IPOD usb 64G")],
        mount_cands=[(11, "/media/david/IPOD")],
        scan_found=[], tty=True, answers=["y"])
assert r == ("mount", "/media/david/IPOD"), r

# Not root, second disk present, no sudo available -> fall back to mount, warn.
r = run("not admin, can't elevate -> falls back to mounted card",
        admin=False,
        mounts=[("/dev/sdb2", "/media/david/IPOD", "vfat")],
        disks=[("/dev/sdb2", "IPOD usb 64G"), ("/dev/sdc2", "IPOD sbp 64G")],
        mount_cands=[(11, "/media/david/IPOD")],
        scan_found=[("/dev/sdc2", "IPOD sbp 64G")],
        tty=True, answers=["y"])
assert r == ("mount", "/media/david/IPOD"), r

print("\nALL ASSERTIONS PASSED")
