"""macOS backend.

Uses ``diskutil`` (with its ``-plist`` output) for enumeration, info, and
unmount/eject, and the BSD ``mount`` command for mounted-filesystem
detection. Raw I/O goes through the unbuffered character device
``/dev/rdiskN`` wrapped in :class:`AlignedRawIO`, since macOS raw devices
require sector-aligned access.

Hardware-tested target: a FireWire-equipped Mac running OS X 10.8+, which
is the native environment for these iPods. Python 3.6 compatible.
"""

import os
import plistlib
import subprocess
import sys

from .base import Platform, AlignedRawIO, Unsupported, SECTOR


def _diskutil_plist(args):
    """Run `diskutil <args>` and parse its plist stdout into a dict."""
    out = subprocess.run(["diskutil"] + args + ["-plist"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0 or not out.stdout:
        raise OSError("diskutil %s failed: %s"
                      % (" ".join(args), out.stderr.decode("utf-8", "replace").strip()))
    # plistlib.loads exists on 3.4+; fall back to readPlistFromString on ancient builds
    if hasattr(plistlib, "loads"):
        return plistlib.loads(out.stdout)
    return plistlib.readPlistFromString(out.stdout)            # noqa: pragma


def _diskutil_info(dev):
    return _diskutil_plist(["info", dev])


def _whole_disk(dev):
    """/dev/disk2s1 -> disk2 ; /dev/disk2 -> disk2 ; disk2 -> disk2."""
    name = os.path.basename(dev)
    if name.startswith("disk") and "s" in name[4:]:
        name = "disk" + name[4:].split("s", 1)[0]
    return name


class MacOSPlatform(Platform):
    name = "macos"

    # -- privilege --------------------------------------------------------
    def is_admin(self):
        return os.geteuid() == 0

    def privilege_hint(self):
        return "Run with sudo to write to a disk."

    # -- device discovery / selection -------------------------------------
    def _external_disks(self):
        """List external/removable whole-disk identifiers (e.g. 'disk2')."""
        pl = _diskutil_plist(["list", "physical"])
        ids = pl.get("WholeDisks") or []
        out = []
        for d in ids:
            try:
                info = _diskutil_info("/dev/" + d)
            except OSError:
                continue
            internal = info.get("Internal", info.get("DeviceInternal", True))
            if internal:
                continue
            out.append((d, info))
        return out

    def choose_device(self):
        from .. import ipod_flash
        color = ipod_flash.color

        def render(disks):
            print(color("\nAttached external disks:\n", ipod_flash.C_CYN),
                  file=sys.stderr)
            for i, (d, info) in enumerate(disks):
                size = int(info.get("TotalSize") or info.get("Size") or 0)
                name = (info.get("MediaName") or info.get("IORegistryEntryName")
                        or "").strip() or "disk"
                print("  [%d] /dev/%-7s  %10s  %s" %
                      (i, d, ipod_flash.fmt_size(size), name), file=sys.stderr)
            print(file=sys.stderr)

        return ipod_flash.pick_device(
            self._external_disks, render,
            lambda item: "/dev/" + item[0],
            "No external disks found. Plug in the card reader and retry.")

    def device_sectors(self, dev):
        # real file/image -> just its size
        if os.path.isfile(dev):
            return os.path.getsize(dev) // SECTOR
        try:
            info = _diskutil_info(dev)
            total = int(info.get("TotalSize") or info.get("Size") or 0)
            if total:
                return total // SECTOR
        except (OSError, ValueError):
            pass
        try:
            with open(dev, "rb") as f:
                return f.seek(0, os.SEEK_END) // SECTOR
        except OSError:
            return 0

    def device_mountpoints(self, dev):
        disk = _whole_disk(dev)
        out = []
        for d, mp, _fs in self.mounted_filesystems():
            base = os.path.basename(d)
            if base == disk or base.startswith(disk + "s"):
                out.append((d, mp))
        return out

    def validate_target(self, dev, dry_run):
        from .. import ipod_flash
        color, red = ipod_flash.color, ipod_flash.C_RED
        if not (os.path.exists(dev) or os.path.exists("/dev/r" + os.path.basename(dev))):
            sys.exit(color("no such device: " + dev, red))
        name = os.path.basename(dev)
        if name.startswith("disk") and "s" in name[4:]:
            sys.exit(color("refusing a partition (%s); pass the whole disk "
                           "(/dev/%s)." % (dev, _whole_disk(dev)), red))
        # never the boot disk
        try:
            root_disk = _whole_disk(_diskutil_info("/").get("ParentWholeDisk", ""))
            if root_disk and _whole_disk(dev) == root_disk:
                sys.exit(color("refusing: %s backs the running system." % dev, red))
        except OSError:
            pass

    # -- mutation around the raw write ------------------------------------
    def unmount_all(self, dev, dry):
        if dry:
            return
        subprocess.run(["diskutil", "unmountDisk", "/dev/" + _whole_disk(dev)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wipe_signatures(self, dev, dry):
        return  # the raw zeroing in write_layout is enough on macOS

    def reread_partition_table(self, dev):
        # macOS re-probes on its own; nudge it so /Volumes repopulates.
        subprocess.run(["diskutil", "list", "/dev/" + _whole_disk(dev)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def flush_buffers(self, dev):
        subprocess.run(["sync"])

    def eject(self, dev, dry):
        if dry:
            return
        subprocess.run(["sync"])
        subprocess.run(["diskutil", "eject", "/dev/" + _whole_disk(dev)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_raw(self, dev, mode):
        # plain image file: open directly; real disk: use the raw char device
        if os.path.isfile(dev):
            return open(dev, mode)
        raw_name = dev if os.path.basename(dev).startswith("rdisk") \
            else "/dev/r" + os.path.basename(dev)
        return AlignedRawIO(open(raw_name, mode, buffering=0))

    def raw_read_node(self, dev):
        """/dev/disk2 → /dev/rdisk2 (unbuffered): reads must dodge the buffer
        cache, whose read-ahead the FireWire bridge zeroes. Files pass through."""
        if os.path.isfile(dev):
            return dev
        base = os.path.basename(dev)
        if base.startswith("rdisk"):
            return dev
        return "/dev/r" + base

    def raw_max_xfer(self):
        """Single-sector transfers, reads AND writes. The gen-1 iPod FireWire
        bridge corrupts raw transfers larger than one sector in BOTH directions
        (proven the hard way: single-sector round-trips, but 8-sector reads come
        back zeroed and 8-sector writes corrupt). Unlike Linux there's no
        per-device queue cap on macOS, so the driver self-limits. Larger writes
        wouldn't help anyway — the bridge is bandwidth-limited (~270 KiB/s).
        FLASHPOD_RAW_MAX_XFER can raise it on a USB reader (no bridge)."""
        return 1

    def fat_disk_candidates(self):
        """Every whole disk except the one backing the running system, as
        unbuffered ``/dev/rdiskN`` nodes for the caller to probe.

        Deliberately does NOT pre-filter on bus, label, or diskutil's partition
        "Content" — those proved unreliable on 10.8 (a misparsed Content made
        the iPod invisible). Our own FAT driver is the judge: open_raw_fat walks
        the MBR and rejects non-FAT disks, and the real iPod test is whether the
        FAT holds iPod_Control/iTunes/iTunesDB. Probing a non-iPod disk is a
        couple of harmless sector reads."""
        try:
            pl = _diskutil_plist(["list"])
        except OSError:
            return []
        try:
            boot = _whole_disk(_diskutil_info("/").get("ParentWholeDisk", ""))
        except OSError:
            boot = None
        out = []
        for d in pl.get("WholeDisks") or []:
            if d and d == boot:
                continue
            desc = d
            try:
                info = _diskutil_info("/dev/" + d)
                media = str(info.get("MediaName")
                            or info.get("IORegistryEntryName") or "").strip()
                bus = str(info.get("BusProtocol") or info.get("Bus") or "").strip()
                desc = ", ".join(b for b in (media, bus) if b) or d
            except OSError:
                pass
            out.append(("/dev/r" + d, desc))
        return out

    # -- sync-path mount detection ----------------------------------------
    def mounted_filesystems(self):
        """Parse `mount`:  /dev/disk2s2 on /Volumes/IPOD (msdos, local, ...)."""
        out = []
        res = subprocess.run(["mount"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode != 0:
            return out
        for line in res.stdout.decode("utf-8", "replace").splitlines():
            if " on " not in line:
                continue
            dev, rest = line.split(" on ", 1)
            mp, _, tail = rest.rpartition(" (")
            fstype = tail.split(",", 1)[0].strip(") ") if tail else ""
            out.append((dev.strip(), mp.strip(), fstype))
        return out
