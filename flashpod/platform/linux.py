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

    def partition_node(self, dev, index):
        # sdb -> sdb2, but mmcblk0/nvme0n1/loop0 -> mmcblk0p2: a trailing digit
        # in the disk name needs the 'p' separator to stay unambiguous.
        sep = "p" if dev[-1:].isdigit() else ""
        return "%s%s%d" % (dev, sep, index)

    def fat_mount_cmd(self, part, mnt):
        return ["mount", part, mnt]        # util-linux probes the type itself

    def raw_open_direct(self):
        # Linux has no /dev/rdiskN: unbuffered block I/O means O_DIRECT,
        # which lives in fatfs.BlockDev's path mode. See base for why.
        return True

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

    def fat_disk_candidates(self):
        """External disks worth probing for an iPod filesystem, as raw nodes
        for the caller to probe.

        Partitions lsblk/udev already identify as FAT (vfat/exfat) are
        returned directly (e.g. ``/dev/sdb2``). But udev is only trusted
        POSITIVELY, never for absence: its attach-time blkid probe reads the
        device through the buffered block layer, and the gen-1 FireWire
        bridge can zero those reads — the partition then carries no recorded
        fstype at all, and requiring one made the iPod invisible while our
        own O_DIRECT driver could read it perfectly. So a disk with no
        identified FAT partition is offered as its WHOLE-disk node
        (``/dev/sdb``) for the caller's FAT driver to judge (open_raw_fat
        walks the MBR itself) — the same probe-everything approach the macOS
        backend uses. Only a disk whose every filesystem slot is positively
        identified as something non-FAT (an ext4 USB stick) is skipped.
        Restricted to external/removable transports so we never read the
        system disk; the real iPod test (iPod_Control/iTunes/iTunesDB) is
        done by the caller, not by any label or transport heuristic."""
        import json
        import time
        # One retry: lsblk can fail transiently while a just-attached disk
        # (an sbp2 login, a reader power-up) is mid-enumeration, and a single
        # failed listing here silently turned a present iPod into "no
        # hardware found" (the fallback mount path then raced ahead of the
        # queue pinning).
        for attempt in (0, 1):
            try:
                out = subprocess.run(
                    ["lsblk", "-J", "-o",
                     "NAME,TYPE,FSTYPE,LABEL,TRAN,RM,HOTPLUG,SIZE"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, check=True).stdout
                break
            except (OSError, subprocess.CalledProcessError):
                if attempt:
                    return []
                time.sleep(0.5)
        FAT = ("vfat", "exfat")
        cands = []

        def describe(node, tran):
            # lsblk reports FireWire as the kernel transport "sbp" (or the
            # legacy "ieee1394"); show the friendly name in the chooser.
            tran_label = "FireWire" if tran in ("sbp", "ieee1394") else tran
            bits = [b for b in (node.get("label") or "", tran_label,
                                node.get("size")) if b]
            return " ".join(bits)

        def partitions(node):
            out = []
            for child in node.get("children") or []:
                if child.get("type") == "part":
                    out.append(child)
                out.extend(partitions(child))
            return out

        for disk in json.loads(out)["blockdevices"]:
            # "rbc": early iPods speak SCSI Reduced Block Commands over
            # SBP-2, and lsblk reports that device type verbatim — the
            # FireWire iPod is NOT type "disk". (Filtering on "disk" alone
            # made the iPod invisible; found live 2026-07-26.)
            if disk.get("type") not in ("disk", "rbc"):
                continue                     # rom/loop can't be an iPod
            tran = disk.get("tran") or ""
            removable = bool(disk.get("rm")) or bool(disk.get("hotplug"))
            if not (removable or tran in ("usb", "sbp", "ieee1394")):
                continue                     # internal disk: never probed
            parts = partitions(disk)
            fat_parts = [p for p in parts
                         if (p.get("fstype") or "") in FAT]
            if fat_parts:
                for p in fat_parts:
                    cands.append(("/dev/" + p["name"], describe(p, tran)))
                continue
            if (disk.get("fstype") or "") in FAT:
                # partitionless "superfloppy" FAT directly on the disk
                cands.append(("/dev/" + disk["name"], describe(disk, tran)))
                continue
            # No FAT identified. If every filesystem slot is positively
            # identified as something else, the disk truly isn't an iPod;
            # otherwise udev may just be blind (zeroed probe) — offer the
            # whole disk and let the caller's driver decide.
            fstypes = [p.get("fstype") or "" for p in parts] \
                or [disk.get("fstype") or ""]
            if all(fstypes):
                continue
            cands.append(("/dev/" + disk["name"], describe(disk, tran)))
        return cands
