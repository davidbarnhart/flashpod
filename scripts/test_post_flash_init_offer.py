#!/usr/bin/env python3
"""Logic test: the post-flash "run init now?" offer actually fires.

    PYTHONPATH=. python3 scripts/test_post_flash_init_offer.py

Mocks the platform, the prompts and mount/umount, so nothing is mounted and
no device is touched.

The offer used to build its partition path inline as `dev + ("p2" if
dev[-1].isdigit() else "2")` -- the Linux convention. On macOS a whole disk is
/dev/disk3, so that produced /dev/disk3p2, which cannot exist there (macOS
names slices sN and has no pN nodes at all). os.path.exists() was therefore
False and the hook returned silently: the flash finished and the user was
never asked to init the database or load music. Partition naming and the mount
command now come from the platform backend, so each OS gets its own.
"""
import os
import sys

sys.argv = ["flashpod"]
from flashpod import cli
from flashpod.platform.linux import LinuxPlatform
from flashpod.platform.macos import MacOSPlatform
from flashpod.platform.windows import WindowsPlatform

fails = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001
        fails.append("%s: %s: %s" % (name, type(exc).__name__, exc))
        print("  FAIL  %s: %s" % (name, exc))
    else:
        print("  ok    %s" % name)


# -- naming, per platform ------------------------------------------------------
def t_macos_names_slices():
    p = MacOSPlatform()
    assert p.partition_node("/dev/disk3", 2) == "/dev/disk3s2", \
        p.partition_node("/dev/disk3", 2)
    # the whole point: the Linux form is not merely different, it is impossible
    assert "p2" not in p.partition_node("/dev/disk3", 2)


def t_linux_names_partitions():
    p = LinuxPlatform()
    assert p.partition_node("/dev/sdb", 2) == "/dev/sdb2"
    # a trailing digit needs the 'p' separator
    assert p.partition_node("/dev/mmcblk0", 2) == "/dev/mmcblk0p2"
    assert p.partition_node("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert p.partition_node("/dev/loop0", 2) == "/dev/loop0p2"


def t_windows_has_no_node():
    assert WindowsPlatform().partition_node(r"\\.\PhysicalDrive2", 2) is None


def t_mount_cmds():
    # macOS will not probe an unmounted FAT slice; the type must be named
    assert MacOSPlatform().fat_mount_cmd("/dev/disk3s2", "/mnt") == \
        ["mount", "-t", "msdos", "/dev/disk3s2", "/mnt"]
    assert LinuxPlatform().fat_mount_cmd("/dev/sdb2", "/mnt") == \
        ["mount", "/dev/sdb2", "/mnt"]
    assert WindowsPlatform().fat_mount_cmd("x", "/mnt") is None


# -- the offer itself ----------------------------------------------------------
class FakePlat:
    def __init__(self, node, mounts=None, cmd=("mount", "X", "Y")):
        self._node, self._mounts, self._cmd = node, mounts or [], list(cmd)

    def partition_node(self, dev, index):
        return self._node

    def device_mountpoints(self, dev):
        return self._mounts

    def fat_mount_cmd(self, part, mnt):
        return self._cmd


def drive(node, exists=True, mounts=None, answers=("y", "n")):
    """Run the Unix arm of the init offer, returning the recorded events.

    Calls _offer_init_after_flash_unix directly rather than the public
    dispatcher: since #54 the dispatcher forks on sys.platform, so going
    through it would run the Windows raw-FAT path on a Windows runner and
    never reach the partition-naming logic these cases exist to check.
    t_dispatch_by_platform covers the routing itself.
    """
    events = []
    it = iter(answers)

    cli.platform.current = lambda: FakePlat(node, mounts)
    cli.os.path.exists = lambda p: exists
    cli.ask_yes = lambda prompt: (events.append(("asked", prompt.strip()[:28])),
                                  next(it, "n").lower().startswith("y"))[1]
    cli._run_quiet_mount = lambda cmd, check=False: events.append(("mount", list(cmd)))
    cli.itunesdb.init_ipod = lambda mnt, name: events.append(("init", mnt))
    cli.subprocess.run = lambda cmd, **kw: events.append((cmd[0], list(cmd)[1:]))
    try:
        cli._offer_init_after_flash_unix("/dev/fake")
    finally:
        cli.os.path.exists = _real_exists
    return events


_real_exists = os.path.exists


def t_offer_fires_when_partition_exists():
    ev = drive("/dev/disk3s2")
    assert any(e[0] == "asked" for e in ev), "never asked about init: %r" % (ev,)
    assert any(e[0] == "init" for e in ev), "init never ran: %r" % (ev,)


def t_offer_skipped_when_no_node():
    ev = drive(None)
    assert ev == [], "asked despite no partition node: %r" % (ev,)


def t_mount_command_comes_from_platform():
    ev = drive("/dev/disk3s2")
    mounts = [e[1] for e in ev if e[0] == "mount"]
    assert mounts == [["mount", "X", "Y"]], \
        "did not use the platform's mount argv: %r" % (mounts,)


def t_reuses_existing_mount():
    """macOS auto-mounts the fresh FAT32; mounting it again would fail busy."""
    ev = drive("/dev/disk3s2", mounts=[("/dev/disk3s2", "/Volumes/IPOD")])
    assert not any(e[0] == "mount" for e in ev), \
        "re-mounted an already-mounted volume: %r" % (ev,)
    assert ("init", "/Volumes/IPOD") in ev, "did not init at the live mount: %r" % (ev,)
    # a mount we did not make must not be unmounted from under the user
    assert not any(e[0] == "umount" for e in ev), "unmounted someone else's mount: %r" % (ev,)


def t_declining_skips_init():
    ev = drive("/dev/disk3s2", answers=("n",))
    assert not any(e[0] == "init" for e in ev), "init ran after declining: %r" % (ev,)


def t_dispatch_by_platform():
    """The public hook routes to the raw-FAT path on Windows, mount elsewhere.

    #54 split offer_init_after_flash in two; nothing covered the fork itself.
    Windows must NOT take the mount path (it never mounts -- it writes through
    the still-open handle), and Linux/macOS must not take the raw-FAT one.
    """
    calls = []
    saved = (cli._offer_init_after_flash_win, cli._offer_init_after_flash_unix,
             cli.sys.platform)
    cli._offer_init_after_flash_win = lambda d, r=None, s=None: calls.append("win")
    cli._offer_init_after_flash_unix = lambda d: calls.append("unix")
    try:
        for plat in ("win32", "darwin", "linux"):
            cli.sys.platform = plat
            cli.offer_init_after_flash("/dev/fake")
    finally:
        (cli._offer_init_after_flash_win, cli._offer_init_after_flash_unix,
         cli.sys.platform) = saved
    assert calls == ["win", "unix", "unix"], \
        "dispatcher routed wrong: %r (want win, unix, unix)" % (calls,)


print("post-flash init offer")
for name, fn in [
    ("macOS names slices sN", t_macos_names_slices),
    ("Linux names partitions N / pN", t_linux_names_partitions),
    ("Windows exposes no partition node", t_windows_has_no_node),
    ("mount argv per platform", t_mount_cmds),
    ("offer fires when the partition exists", t_offer_fires_when_partition_exists),
    ("offer skips when there is no node", t_offer_skipped_when_no_node),
    ("mount argv comes from the platform", t_mount_command_comes_from_platform),
    ("reuses an existing mount", t_reuses_existing_mount),
    ("declining skips init", t_declining_skips_init),
    ("dispatcher routes win -> raw-FAT, unix -> mount", t_dispatch_by_platform),
]:
    check(name, fn)

if fails:
    print("\npost-flash init offer: %d FAILED" % len(fails), file=sys.stderr)
    for f in fails:
        print("  - " + f, file=sys.stderr)
    sys.exit(1)
print("\nALL ASSERTIONS PASSED")
