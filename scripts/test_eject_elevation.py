#!/usr/bin/env python3
"""Logic test: an eject that needs root asks for it via sudo, in the terminal.

udisksctl hands authorization to polkit, and on a desktop polkit answers with
a GRAPHICAL password dialog — so an unelevated `flashpod add` that ended in
"Eject the iPod now?" could pop a window asking for the root password (mounts
udisks didn't make need auth_admin to unmount). These tests pin the fix: every
udisksctl call in eject() carries --no-user-interaction (fail fast, no dialog),
and the fallbacks elevate through sudo — prompting on a tty, `sudo -n` without
one, and no sudo at all when already root. Everything is mocked; runs anywhere.

    PYTHONPATH=. python3 scripts/test_eject_elevation.py
"""
import subprocess
import sys

from flashpod import ipod_flash


def check(name, ok, detail=""):
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))
    assert ok, detail


class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def scenario(udisks_ok, tty, euid):
    """Run eject() with udisksctl scripted to succeed/fail; return the argvs."""
    cmds = []

    def fake_run(cmd, check=True, capture=True, timeout=None):
        cmds.append(list(cmd))
        rc = 0
        if cmd[0] == "udisksctl" and not udisks_ok:
            rc = 1                          # "not authorized" under polkit
        return subprocess.CompletedProcess(cmd, rc, "", "")

    # os.geteuid doesn't exist on Windows; eject() treats that as root
    real = (ipod_flash.run, ipod_flash.have, ipod_flash.device_mountpoints,
            getattr(ipod_flash.os, "geteuid", None), sys.stdin)
    ipod_flash.run = fake_run
    ipod_flash.have = lambda tool: True
    ipod_flash.device_mountpoints = lambda dev: [("/dev/sdz2", "/media/x/IPOD")]
    ipod_flash.os.geteuid = lambda: euid
    sys.stdin = FakeStdin(tty)
    try:
        ipod_flash.eject("/dev/sdz", False)
    finally:
        (ipod_flash.run, ipod_flash.have, ipod_flash.device_mountpoints,
         geteuid, sys.stdin) = real
        if geteuid is None:
            del ipod_flash.os.geteuid
        else:
            ipod_flash.os.geteuid = geteuid
    return cmds


print("eject elevation")

cmds = scenario(udisks_ok=True, tty=True, euid=1000)
udisks = [c for c in cmds if c[0] == "udisksctl"]
check("udisksctl never allowed to raise a polkit dialog",
      len(udisks) == 2 and all("--no-user-interaction" in c for c in udisks),
      str(udisks))
check("no sudo when udisks handles it unprivileged",
      not any(c[0] == "sudo" for c in cmds), str(cmds))

cmds = scenario(udisks_ok=False, tty=True, euid=1000)
check("refused unmount falls back to sudo umount",
      ["sudo", "umount", "/dev/sdz2"] in cmds, str(cmds))
check("refused power-off falls back to sudo eject",
      ["sudo", "eject", "/dev/sdz"] in cmds, str(cmds))
check("still no interactive udisksctl after a refusal",
      all("--no-user-interaction" in c for c in cmds if c[0] == "udisksctl"),
      str(cmds))

cmds = scenario(udisks_ok=False, tty=False, euid=1000)
check("no terminal: sudo -n, never a hanging prompt",
      ["sudo", "-n", "umount", "/dev/sdz2"] in cmds
      and ["sudo", "-n", "eject", "/dev/sdz"] in cmds, str(cmds))

cmds = scenario(udisks_ok=False, tty=True, euid=0)
check("already root: plain umount/eject, no sudo",
      ["umount", "/dev/sdz2"] in cmds and ["eject", "/dev/sdz"] in cmds
      and not any(c[0] == "sudo" for c in cmds), str(cmds))

print("all eject-elevation checks passed")
