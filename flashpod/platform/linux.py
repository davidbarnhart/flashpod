"""Linux backend — the reference implementation.

Most methods delegate to the battle-tested helpers in
:mod:`flashpod.ipod_flash` (lsblk / sysfs / udisks based), so Linux
behaviour is unchanged by the introduction of the platform layer. The few
operations that used to be inline in the flash engine (signature wipe,
partition-table re-read, buffer flush) live here now.

Imports of the sibling modules are done lazily inside methods to keep the
package import graph acyclic.
"""

import os
import subprocess
import sys

from .base import Platform

# Test hook shared with the CLI: point the mount scan at a fake table.
MOUNTS_FILE = os.environ.get("FLASHPOD_MOUNTS_FILE", "/proc/mounts")


def _unescape(field):
    """/proc/mounts octal-escapes spaces etc. as \\040."""
    out = field
    for code, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"),
                     ("\\134", "\\")):
        out = out.replace(code, ch)
    return out


class LinuxPlatform(Platform):
    name = "linux"

    # -- privilege --------------------------------------------------------
    def is_admin(self):
        return os.geteuid() == 0

    def privilege_hint(self):
        return "Run as root (sudo) to write to a block device."

    # -- device discovery / selection -------------------------------------
    def choose_device(self):
        from .. import ipod_flash
        return ipod_flash.choose_device()

    def device_sectors(self, dev):
        from .. import ipod_flash
        return ipod_flash.device_sectors(dev)

    def device_mountpoints(self, dev):
        from .. import ipod_flash
        return ipod_flash.device_mountpoints(dev)

    def validate_target(self, dev, dry_run):
        from .. import ipod_flash
        color, red = ipod_flash.color, ipod_flash.C_RED
        if not os.path.exists(dev):
            sys.exit(color("no such device: " + dev, red))
        # refuse a partition node (/dev/sdb1) unless it's a whole-disk name
        # that happens to end in a digit (mmcblk0, loop0, nvme0n1)
        if dev.rstrip("0123456789") != dev \
                and not dev.startswith("/dev/mmcblk") \
                and not dev.startswith("/dev/loop") \
                and not dev.startswith("/dev/nvme"):
            sys.exit(color("refusing a partition node (%s); pass the whole disk." % dev, red))
        if os.path.basename(dev) in ipod_flash.root_disk_names():
            sys.exit(color("refusing: %s backs the running system." % dev, red))

    # -- mutation around the raw write ------------------------------------
    def unmount_all(self, dev, dry):
        from .. import ipod_flash
        ipod_flash.unmount_all(dev, dry)

    def wipe_signatures(self, dev, dry):
        from .. import ipod_flash
        if dry:
            return
        if ipod_flash.have("wipefs"):
            ipod_flash.run(["wipefs", "-a", dev], check=False)

    def reread_partition_table(self, dev):
        from .. import ipod_flash
        # BLKRRPART (fast) creates the partition nodes; partprobe then runs
        # `udevadm settle`, which can stall for minutes on a slow reader with
        # a big FAT32. Cap it and fall back to a plain re-read.
        settled = False
        if ipod_flash.have("partprobe"):
            try:
                ipod_flash.run(["partprobe", dev], check=False, timeout=15)
                settled = True
            except subprocess.TimeoutExpired:
                print(ipod_flash.color(
                    "  partprobe stalled on udev settle; the partition map is "
                    "already in place, continuing.", ipod_flash.C_YEL),
                    file=sys.stderr)
        if not settled and ipod_flash.have("blockdev"):
            ipod_flash.run(["blockdev", "--rereadpt", dev], check=False)

    def flush_buffers(self, dev):
        from .. import ipod_flash
        ipod_flash.run(["sync"], check=False)
        if ipod_flash.have("blockdev"):
            ipod_flash.run(["blockdev", "--flushbufs", dev], check=False)

    def eject(self, dev, dry):
        from .. import ipod_flash
        ipod_flash.eject(dev, dry)

    # -- sync-path mount detection ----------------------------------------
    def mounted_filesystems(self):
        out = []
        try:
            with open(MOUNTS_FILE) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    out.append((_unescape(parts[0]), _unescape(parts[1]), parts[2]))
        except OSError:
            pass
        return out
