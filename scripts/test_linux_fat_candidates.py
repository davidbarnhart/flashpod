#!/usr/bin/env python3
"""Logic test for Linux disk-candidate enumeration (fat_disk_candidates).

Why this exists: udev's attach-time blkid probe reads through the buffered
block layer, and the gen-1 FireWire bridge can zero those reads — the iPod's
partition then has no recorded fstype, and the old enumeration (which REQUIRED
lsblk to say vfat) went blind even though flashpod's own O_DIRECT driver could
read the disk perfectly (2026-07 incident). The rule now mirrors macOS: trust
udev positively, never for absence — a disk with no identified FAT partition
is offered whole for our own driver to probe; only a disk whose filesystems
are all positively identified as non-FAT is skipped.

Mocks lsblk, touches no real device; runs on any OS:

    PYTHONPATH=. python3 scripts/test_linux_fat_candidates.py
"""
import json
import sys
import types

sys.argv = ["flashpod"]
from flashpod import cli                    # noqa: E402
from flashpod.platform import linux         # noqa: E402


def fake_lsblk(*devices):
    payload = json.dumps({"blockdevices": list(devices)})

    def run(argv, **kw):
        assert argv[0] == "lsblk", argv
        return types.SimpleNamespace(stdout=payload, returncode=0)

    linux.subprocess.run = run


def disk(name, tran=None, rm=0, size="119.1G", fstype=None, label=None,
         dtype="disk", children=()):
    return {"name": name, "type": dtype, "tran": tran, "rm": rm, "hotplug": 0,
            "size": size, "fstype": fstype, "label": label,
            "children": list(children)}


def part(name, fstype=None, label=None, size="10G"):
    return {"name": name, "type": "part", "fstype": fstype, "label": label,
            "size": size}


def cands(*devices):
    fake_lsblk(*devices)
    return linux.LinuxPlatform().fat_disk_candidates()


def nodes(*devices):
    return [n for n, _ in cands(*devices)]


def check(name, got, want):
    ok = got == want
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name,
                               "" if ok else "got %r, want %r" % (got, want)))
    assert ok


print("fat_disk_candidates:")

# The everyday case: healthy iPod, udev probed it fine -> the vfat partition,
# not the whole disk, and not the fs-less firmware partition beside it.
check("healthy FireWire iPod -> data partition only",
      nodes(disk("sdb", tran="sbp",
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")])),
      ["/dev/sdb2"])

# THE INCIDENT: udev's probe was zeroed by the bridge, no fstype recorded
# anywhere -> the whole disk is offered for our own driver to probe.
check("udev-blind iPod -> whole disk offered",
      nodes(disk("sdb", tran="sbp",
                 children=[part("sdb1"), part("sdb2")])),
      ["/dev/sdb"])

# Positive identification is still trusted: every slot known, none FAT.
check("ext4 USB stick -> skipped",
      nodes(disk("sdc", tran="usb", rm=1, children=[part("sdc1", "ext4")])),
      [])

# Internal disks are never probed, whatever their state.
check("internal sata disk with vfat -> skipped",
      nodes(disk("sda", tran="sata",
                 children=[part("sda1", "vfat", "EFI")])),
      [])

# Partitionless media.
check("superfloppy vfat USB stick -> whole disk",
      nodes(disk("sdd", tran="usb", rm=1, fstype="vfat", label="STICK")),
      ["/dev/sdd"])
check("superfloppy ntfs disk -> skipped",
      nodes(disk("sdd", tran="usb", rm=1, fstype="ntfs")),
      [])
check("blank/unprobed USB disk -> whole disk (driver decides)",
      nodes(disk("sdd", tran="usb", rm=1)),
      ["/dev/sdd"])

# A found FAT partition wins over an unknown sibling (the firmware partition
# never gets its own candidate row).
check("vfat partition + unknown sibling -> partition only",
      nodes(disk("sdb", tran="usb", rm=1,
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")])),
      ["/dev/sdb2"])

# Non-disk devices can't be iPods.
check("cd-rom -> skipped",
      nodes(disk("sr0", tran="usb", rm=1, dtype="rom")),
      [])

# THE REAL THING: the FireWire iPod reports SCSI type RBC, so lsblk says
# "rbc", not "disk" — verbatim shape captured live from lsblk 2026-07-26.
# Filtering on type "disk" alone made the iPod invisible to detection.
check("FireWire iPod is type rbc, not disk -> data partition",
      nodes(disk("sdb", tran="sbp", rm=1, dtype="rbc",
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")])),
      ["/dev/sdb2"])
check("udev-blind rbc iPod -> whole disk offered",
      nodes(disk("sdb", tran="sbp", rm=1, dtype="rbc",
                 children=[part("sdb1"), part("sdb2")])),
      ["/dev/sdb"])

# THE GHOST (2026-07-30): with both leads of a dual-plug cable attached, the
# iPod routes data over USB but still logs in over FireWire as a 0-byte SBP-2
# target — verbatim lsblk shape captured live. Raw-probing it hangs in
# uninterruptible I/O, so a 0-byte disk is never a candidate (matching the
# Windows backend, which has always skipped empty 0-byte reader slots).
check("0-byte FireWire ghost login -> skipped",
      nodes(disk("sdc", tran="sbp", rm=1, dtype="rbc", size="0B")),
      [])
check("0-byte ghost beside the USB-attached iPod -> iPod only",
      nodes(disk("sdb", tran="usb", rm=1,
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")]),
            disk("sdc", tran="sbp", rm=1, dtype="rbc", size="0B")),
      ["/dev/sdb2"])

# Several externals at once: each judged independently.
check("iPod + ext4 stick + blind disk -> iPod part + blind whole disk",
      nodes(disk("sdb", tran="sbp",
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")]),
            disk("sdc", tran="usb", rm=1, children=[part("sdc1", "ext4")]),
            disk("sdd", tran="usb", rm=1, children=[part("sdd1")])),
      ["/dev/sdb2", "/dev/sdd"])

# Descriptions still carry label/transport/size for the choosers.
check("description: label + friendly transport + size",
      cands(disk("sdb", tran="sbp",
                 children=[part("sdb1"), part("sdb2", "vfat", "IPOD")])),
      [("/dev/sdb2", "IPOD FireWire 10G")])


print("_unmounted_disks (whole-disk candidates vs the mount table):")
# On Linux, realpath passes the (non-existent) fake nodes through unchanged;
# on Windows it would prepend a drive letter (D:\dev\sdb) and break the
# comparisons, so pin it to the Linux behavior — the code under test only
# ever runs on Linux.
cli.os.path.realpath = lambda p: p
cli._mounted_devices = lambda: {"/dev/sdb2"}
check("whole-disk candidate with a mounted partition -> filtered",
      cli._unmounted_disks([("/dev/sdb", "FireWire 119.1G")]),
      [])
check("exact mounted partition -> filtered",
      cli._unmounted_disks([("/dev/sdb2", "IPOD usb 64G")]),
      [])
check("unrelated disk -> kept",
      cli._unmounted_disks([("/dev/sdc", "usb 64G")]),
      [("/dev/sdc", "usb 64G")])
check("prefix-sharing other disk (sdba) -> kept",
      cli._unmounted_disks([("/dev/sdba", "usb 64G")]),
      [("/dev/sdba", "usb 64G")])

cli._mounted_devices = lambda: {"/dev/nvme0n1p2"}
check("nvme whole-disk candidate with mounted pN partition -> filtered",
      cli._unmounted_disks([("/dev/nvme0n1", "usb 64G")]),
      [])

print("\nALL ASSERTIONS PASSED")
