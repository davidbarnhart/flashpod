"""Flash-card writer for early iPods — backs `flashpod flash`.

Import and call flash() / self_test(); the `flashpod` script owns the
command-line interface.

Reproduces the on-disk layout of an Apple-formatted early iPod onto a fresh
CompactFlash / SD card and installs the firmware.

Windows flavor (default):

    sector 0           DOS MBR (0xFEFFFF placeholder CHS)
    sectors 63..65598  firmware partition, type 0x00 (32 MiB)  <- raw firmware
    sectors 65599..    FAT32 data partition, type 0x0B, ending TAIL_RESERVE
                       sectors before the disk end (see below)

Mac flavor, from a real 5 GB Mac iPod (see ipod5GB* captures):

    block 0            Driver Descriptor Record  ("ER", big-endian)
    blocks 1..62       Apple_partition_map  "partition map"   (the APM itself)
    blocks 63..65598   Apple_MDFW           "firmware"        (32 MiB)  <- raw firmware
    blocks 65599..     Apple_HFS            "disk"            (rest minus
                       TAIL_RESERVE)                          <- HFS+ data

The Apple Partition Map and DDR are big-endian, 512-byte blocks; the real map
sets only pmSig/pmMapBlkCnt/pmPyPartStart/pmPartBlkCnt/pmPartName/pmParType,
leaving every boot/status field zero, so we reproduce exactly that.

Both flavors leave the last TAIL_RESERVE sectors unallocated: iPod disk paths
can report the disk smaller than a card reader does (the 3G hides one sector),
and a data partition overhanging the iPod's view makes the firmware reject it
and demand a sync (found the hard way, 2026-06-12).

SAFETY: this ERASES the selected device. The tool only offers removable/USB
disks, never the disk holding '/', unmounts first, requires an explicit typed
confirmation, and supports --dry-run. Run as root.

Default layout is Windows (MBR + FAT32); flavor "mac" gives APM + HFS+. After
the images are written the firmware region is read back and compared
byte-for-byte to the image before the card is ejected, so a bad write is
caught immediately.
"""
import json, os, struct, subprocess, sys, zipfile

from . import fat32

SECTOR        = 512
MAP_START     = 1
MAP_BLOCKS    = 62
FW_START      = 63
FW_BLOCKS     = 65536            # 32 MiB firmware partition (Mac/APM flavor)
DATA_START    = FW_START + FW_BLOCKS   # 65599 (Mac/APM flavor)
LBA28_MAX     = 0x10000000       # 2^28 sectors = 128 GiB ceiling of the iPod ATA driver
N_MAP_ENTRIES = 3
# iPod disk paths (bridge or flash adapter) can report the disk smaller than
# a card reader does — the 3G's path hides exactly 1 sector, and a partition
# that overhangs that view makes the firmware demand a sync. Apple leaves the
# last reader-visible sector unallocated; we leave 1 MiB for extra margin.
TAIL_RESERVE  = 2048             # sectors left unallocated at the disk end

C_RED, C_YEL, C_GRN, C_CYN, C_DIM, C_RST = (
    "\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[2m", "\033[0m")

def color(s, c):
    return c + s + C_RST if sys.stderr.isatty() else s

# ----------------------------------------------------------------------------
# Partition-table construction (pure, testable)
# ----------------------------------------------------------------------------
def build_ddr(total_sectors):
    """Driver Descriptor Record, block 0 (big-endian)."""
    b = bytearray(SECTOR)
    struct.pack_into(">H", b, 0, 0x4552)                  # sbSig "ER"
    struct.pack_into(">H", b, 2, SECTOR)                  # sbBlkSize
    struct.pack_into(">I", b, 4, total_sectors & 0xFFFFFFFF)  # sbBlkCount
    # sbDevType/sbDevID/sbData/sbDrvrCount all 0 -> minimal, driverless DDR
    return bytes(b)

def build_pm_entry(py_start, blk_cnt, name, ptype):
    """One Apple Partition Map entry, 512 bytes (big-endian)."""
    b = bytearray(SECTOR)
    struct.pack_into(">H", b, 0, 0x504D)                  # pmSig "PM"
    struct.pack_into(">I", b, 4, N_MAP_ENTRIES)           # pmMapBlkCnt
    struct.pack_into(">I", b, 8, py_start)                # pmPyPartStart
    struct.pack_into(">I", b, 12, blk_cnt)                # pmPartBlkCnt
    b[16:16+len(name)]  = name.encode("ascii")            # pmPartName
    b[48:48+len(ptype)] = ptype.encode("ascii")           # pmParType
    return bytes(b)

def build_partition_layout(total_sectors):
    """Return (header_bytes for blocks 0..DATA_START-1, data_start, data_blocks).

    The header covers the DDR + APM + the rest of the partition-map partition;
    the firmware and data regions are written separately.
    """
    data_end    = min(total_sectors - TAIL_RESERVE, LBA28_MAX)
    data_blocks = data_end - DATA_START
    if data_blocks <= 0:
        raise ValueError("device too small for an iPod data partition")
    entries = [
        build_pm_entry(MAP_START,  MAP_BLOCKS, "partition map", "Apple_partition_map"),
        build_pm_entry(FW_START,   FW_BLOCKS,  "firmware",      "Apple_MDFW"),
        build_pm_entry(DATA_START, data_blocks,"disk",          "Apple_HFS"),
    ]
    # blocks: [0]=DDR, [1..3]=entries, [4..62]=zero (rest of the 62-blk map part)
    header = bytearray(FW_START * SECTOR)
    header[0:SECTOR] = build_ddr(total_sectors)
    for i, e in enumerate(entries):
        off = (MAP_START + i) * SECTOR
        header[off:off+SECTOR] = e
    return bytes(header), DATA_START, data_blocks

def build_mbr_layout(total_sectors):
    """Windows-flavored layout: DOS MBR with firmware partition (type 0x00)
    at sector 63 and FAT32 data (type 0x0B) from sector 65599, stopping
    TAIL_RESERVE sectors short of the disk end.

    Returns (mbr_512_bytes, data_start, data_blocks).
    """
    data_end    = min(total_sectors - TAIL_RESERVE, LBA28_MAX)
    data_blocks = data_end - DATA_START
    if data_blocks <= 0:
        raise ValueError("device too small for an iPod data partition")
    mbr = bytearray(SECTOR)
    chs = b"\xfe\xff\xff"                    # LBA-only; CHS fields are vestigial
    def entry(off, ptype, start, count):
        mbr[off] = 0x00
        mbr[off+1:off+4] = chs
        mbr[off+4] = ptype
        mbr[off+5:off+8] = chs
        struct.pack_into("<II", mbr, off+8, start, count)
    entry(446, 0x00, FW_START, FW_BLOCKS)    # firmware partition
    entry(462, 0x0b, DATA_START, data_blocks)  # FAT32 data
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr), DATA_START, data_blocks

# ----------------------------------------------------------------------------
# Firmware loading / validation
# ----------------------------------------------------------------------------
def load_firmware(path):
    """Return install-ready firmware bytes from a Firmware-* file or an
    .ipsw zip: extract, apply the install-time directory fixups Apple's
    updater performs, and validate."""
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"PK":
        zf = zipfile.ZipFile(path)
        name = next(n for n in zf.namelist()
                    if os.path.basename(n).startswith("Firmware"))
        data = zf.read(name)
    else:
        data = open(path, "rb").read()
    data = fixup_directories(data)
    validate_firmware(data)
    return data

def walk_directory(fw, base):
    """Yield (entry_offset, type_4cc, fields_tuple) for the 40-byte entries
    of the firmware directory starting at `base` (up to the 10 entries the
    format allows). fields = (id, devOffset, len, addr, entryOffset,
    chksum, vers, loadAddr)."""
    for off in range(base, base + 10 * 40, 40):
        if off + 40 > len(fw) or fw[off:off+4] == b"\x00\x00\x00\x00":
            return
        typ = fw[off+4:off+8][::-1]
        yield off, typ, struct.unpack_from("<8I", fw, off+8)

def fixup_directories(fw):
    """Apply the install-time fixups Apple's updater performs on the
    firmware directory; .ipsw resources ship in "pre-install" state and
    writing them raw leaves the iPod mid-update:

    - osos loadAddr: staging value (0x048Dxxxx) -> 0xFFFFFFFF ("nothing
      pending"); what every working installer writes (Rockbox ipodpatcher,
      iPodLinux make_fw/make_fw2).
    - aupd id: 0 -> 1. Per the iPodLinux wiki the id is "set to 1 for aupd
      once flash-ROM update has been performed"; the ipsw's 0 means a
      flash update is PENDING, so the iPod tries to flash its boot ROM on
      every boot (progress bar -> reset on power, folder-with-! icon on
      battery; found the hard way on a 3G, 2026-06-12).
    - aupd loadAddr: -> its RAM address, the post-update resting state.

    With these, our Gen3 output is byte-identical to a factory-installed
    3G card's firmware image (hardware-validated same day). Format-2
    images carry two directory copies (one at the header pointer, the
    live one a sector later); patch both."""
    if fw[0x100:0x104] != b"]ih[":
        return fw                      # not a firmware volume; let validate complain
    fw = bytearray(fw)
    ptr, = struct.unpack_from("<I", fw, 0x104)
    for base in (ptr, ptr + 0x200):
        for off, typ, (_id, devOff, length, addr, entry, ck, vers, load) \
                in walk_directory(fw, base):
            if typ == b"aupd":
                struct.pack_into("<I", fw, off + 8, 1)      # update "performed"
                struct.pack_into("<I", fw, off + 36, addr)
            else:
                struct.pack_into("<I", fw, off + 36, 0xFFFFFFFF)
    return bytes(fw)

def validate_firmware(fw):
    if fw[0:4] != b"{{~~":
        raise ValueError("not an iPod firmware image (missing STOP boot block)")
    if fw[0x100:0x104] != b"]ih[":
        raise ValueError("missing [hi] volume header at 0x100")
    ptr, = struct.unpack_from("<I", fw, 0x104)
    fmtver, = struct.unpack_from("<H", fw, 0x10a)
    # The live directory sits one sector past the header pointer (the copy
    # AT the pointer is a staging table with different devOffsets). In
    # format-3 images (4G+) payloads also sit one sector past their
    # devOffset; format 2 (1G-3G) stores them at devOffset directly.
    shift = 0x200 if fmtver == 3 else 0
    found = False
    for off, typ, (_id, devOff, length, addr, entry, cksum, vers, load) \
            in walk_directory(fw, ptr + 0x200):
        if typ == b"osos":
            found = True
            got = sum(fw[devOff+shift:devOff+shift+length]) & 0xFFFFFFFF
            if got != cksum:
                raise ValueError("osos checksum mismatch: stored 0x%08x "
                                 "got 0x%08x" % (cksum, got))
            if load != 0xFFFFFFFF:
                raise ValueError("osos loadAddr 0x%08x not fixed up "
                                 "(install-state must be 0xFFFFFFFF)" % load)
    if not found:
        raise ValueError("no osos image in the firmware directory")
    if len(fw) > FW_BLOCKS * SECTOR:
        raise ValueError("firmware (%d bytes) larger than the 32 MiB firmware partition"
                         % len(fw))

# ----------------------------------------------------------------------------
# Device discovery / safety (Linux)
# ----------------------------------------------------------------------------
def run(cmd, check=True, capture=True, timeout=None):
    return subprocess.run(cmd, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          text=True, timeout=timeout)

def root_disk_names():
    """Kernel names (e.g. {'sda'}) of disks backing '/' and /boot, to never touch."""
    out = run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT,PKNAME,TYPE"]).stdout
    names, tree = set(), json.loads(out)["blockdevices"]
    def walk(node, top):
        mp = node.get("mountpoint")
        if mp in ("/", "/boot", "/boot/efi", "[SWAP]", "/home"):
            names.add(top)
        for c in node.get("children", []) or []:
            walk(c, top)
    for d in tree:
        walk(d, d["name"])
    return names

def list_candidates():
    """Removable / USB whole disks that are safe to offer."""
    out = run(["lsblk", "-J", "-b", "-o",
               "NAME,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINT,"
               "FSTYPE,LABEL,PTTYPE"]).stdout
    protected = root_disk_names()
    cands = []
    for d in json.loads(out)["blockdevices"]:
        if d["type"] != "disk" or d["name"] in protected:
            continue
        removable = (d.get("rm") in (True, "1", 1) or
                     d.get("hotplug") in (True, "1", 1) or
                     (d.get("tran") or "") in ("usb", "mmc", "ieee1394"))
        if removable:
            cands.append(d)
    return cands

def usb_device_dir(name):
    """sysfs dir of the USB device behind /dev/<name>, or None (e.g. FireWire)."""
    p = os.path.realpath("/sys/block/%s/device" % name)
    while len(p) > 1:
        if os.path.exists(os.path.join(p, "idVendor")):
            return p
        p = os.path.dirname(p)
    return None

def _sysfs_read(d, attr):
    try:
        with open(os.path.join(d, attr)) as f:
            return f.read().strip()
    except OSError:
        return ""

def usbids_vendor(vid):
    """Vendor name for a 4-hex-digit USB vendor id from the usb.ids database."""
    for path in ("/usr/share/misc/usb.ids", "/usr/share/hwdata/usb.ids"):
        try:
            with open(path, encoding="latin1") as f:
                for line in f:
                    if line[:4].lower() == vid.lower() and line[4:5] in (" ", "\t"):
                        return line[4:].strip()
        except OSError:
            continue
    return ""

def device_identity(d):
    """Human description of the hardware behind a lsblk disk entry.

    The SCSI-level MODEL from lsblk is often a generic class name
    ("MassStorageClass"); the USB product string descriptor and the usb.ids
    vendor database usually know what the enclosure actually is.
    """
    name = (d.get("model") or "").strip() or (d.get("vendor") or "").strip()
    usb = usb_device_dir(d["name"])
    if usb:
        product = _sysfs_read(usb, "product")
        if product and (not name or "class" in name.lower()):
            name = product
        brand = usbids_vendor(_sysfs_read(usb, "idVendor"))
        # "Genesys Logic, Inc." -> "Genesys Logic"
        brand = brand.split(", Inc")[0].split(", Ltd")[0].strip()
        if brand and brand.lower() not in ("generic",) \
                and brand.lower() not in name.lower():
            name += " (%s)" % brand
    tran = d.get("tran") or ""
    if tran == "ieee1394":
        name += ", FireWire"
    elif tran == "mmc":
        name += ", SD slot"
    return name

PTTYPE_NAMES = {"dos": "MBR", "gpt": "GPT", "mac": "APM"}

def describe_contents(d):
    """One-line summary of what's on the disk: partitions, fs, labels, mounts."""
    if int(d.get("size") or 0) == 0:
        return "no card inserted"
    parts = d.get("children") or []
    if not parts:
        if d.get("fstype"):
            lbl = " '%s'" % d["label"] if d.get("label") else ""
            return "%s%s, no partition table" % (d["fstype"], lbl)
        if d.get("pttype"):
            return "%s, no partitions" % PTTYPE_NAMES.get(d["pttype"], d["pttype"])
        return "blank — no partition table"
    bits = []
    for p in parts:
        b = "%s %s" % (p.get("fstype") or "unformatted", fmt_size(p["size"]))
        if p.get("label"):
            b += " '%s'" % p["label"]
        if p.get("mountpoint"):
            b += " @ %s" % p["mountpoint"]
        bits.append(b)
    s = "%s: %s" % (PTTYPE_NAMES.get(d.get("pttype"), d.get("pttype") or "?"),
                    ", ".join(bits))
    if any((p.get("label") or "").upper() == "IPOD" for p in parts):
        s += "  <- looks like an iPod card"
    return s

def fmt_size(n):
    n = int(n);
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or u == "TiB":
            return ("%.1f %s" % (n, u)).replace(".0 ", " ")
        n /= 1024

def device_mountpoints(dev):
    res = run(["lsblk", "-J", "-o", "NAME,MOUNTPOINT", dev], check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    out = res.stdout
    mps = []
    def walk(n):
        if n.get("mountpoint"):
            mps.append((("/dev/" + n["name"]), n["mountpoint"]))
        for c in n.get("children", []) or []:
            walk(c)
    for d in json.loads(out)["blockdevices"]:
        walk(d)
    return mps

# ----------------------------------------------------------------------------
# Interactive selection + confirmation
# ----------------------------------------------------------------------------
def choose_device():
    cands = list_candidates()
    if not cands:
        sys.exit(color("No removable/USB disks found. Plug in the card and retry.", C_RED))
    print(color("\nAttached removable storage:\n", C_CYN), file=sys.stderr)
    # Multi-slot readers expose one /dev/sdX per slot, all with the same
    # identity strings; number the slots so they can be told apart.
    usb_dirs = [usb_device_dir(d["name"]) for d in cands]
    for i, d in enumerate(cands):
        ident = device_identity(d)
        if usb_dirs[i] and usb_dirs.count(usb_dirs[i]) > 1:
            slot = sum(1 for u in usb_dirs[:i + 1] if u == usb_dirs[i])
            ident += ", slot %d" % slot
        mounted = any(p.get("mountpoint") for p in d.get("children") or []) \
                  or d.get("mountpoint")
        print("  [%d] /dev/%-6s  %10s  %s %s" % (
            i, d["name"], fmt_size(d["size"]), ident,
            color("(mounted)", C_YEL) if mounted else ""), file=sys.stderr)
        print(color("        %s" % describe_contents(d), C_DIM), file=sys.stderr)
    print(file=sys.stderr)
    while True:
        sel = input(color("Select device number (or 'q' to quit): ", C_CYN)).strip()
        if sel.lower() in ("q", "quit", ""):
            sys.exit("Aborted.")
        if sel.isdigit() and int(sel) < len(cands):
            return "/dev/" + cands[int(sel)]["name"]
        print(color("  invalid selection", C_RED), file=sys.stderr)

def confirm(dev, total_sectors, assume_yes, flavor):
    if flavor == "windows":
        _, data_start, data_blocks = build_mbr_layout(total_sectors)
        scheme, fs, dtype = "MBR (Windows)", "FAT32", "type 0x0B"
    else:
        _, data_start, data_blocks = build_partition_layout(total_sectors)
        scheme, fs, dtype = "APM (Mac)", "HFS+", "Apple_HFS"
    fw_blocks = FW_BLOCKS
    print(color("\n  PLAN — this will ERASE %s" % dev, C_RED), file=sys.stderr)
    print("    flavor      : %s" % scheme, file=sys.stderr)
    print("    device size : %s (%d sectors)" % (fmt_size(total_sectors*SECTOR), total_sectors),
          file=sys.stderr)
    print("    firmware     : sectors %d..%d   (%s)" %
          (FW_START, FW_START+fw_blocks-1, fmt_size(fw_blocks*SECTOR)), file=sys.stderr)
    print("    data (%-5s) : sectors %d..%d  %s  (%s)" %
          (fs, data_start, data_start+data_blocks-1, dtype, fmt_size(data_blocks*SECTOR)),
          file=sys.stderr)
    if total_sectors > LBA28_MAX:
        print(color("    NOTE: card exceeds 128 GiB; data capped at the iPod's LBA28 limit.",
                    C_YEL), file=sys.stderr)
    mps = device_mountpoints(dev)
    if mps:
        print(color("    mounted now : " + ", ".join("%s@%s" % m for m in mps), C_YEL),
              file=sys.stderr)
    if assume_yes:
        return
    print(file=sys.stderr)
    want = "ERASE %s" % os.path.basename(dev)
    got = input(color('  Type "%s" to proceed: ' % want, C_RED)).strip()
    if got != want:
        sys.exit("Confirmation did not match. Aborted.")

# ----------------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------------
def have(tool):
    return subprocess.run(["sh", "-c", "command -v " + tool],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def device_sectors(dev):
    """Total 512-byte sectors of a device or image, without depending on PATH.

    Prefers /sys/block (readable without root), then a seek-to-end of the
    device, then blockdev, then file size.
    """
    sysf = "/sys/block/%s/size" % os.path.basename(dev)
    if os.path.exists(sysf):
        try:
            return int(open(sysf).read().strip())     # already in 512B units
        except (OSError, ValueError):
            pass
    try:
        with open(dev, "rb") as f:
            return f.seek(0, os.SEEK_END) // SECTOR
    except OSError:
        pass
    if have("blockdev"):
        try:
            return int(run(["blockdev", "--getsz", dev]).stdout)
        except Exception:
            pass
    return os.path.getsize(dev) // SECTOR

def unmount_all(dev, dry):
    for part, mp in device_mountpoints(dev):
        print(color("  unmounting %s (%s)" % (part, mp), C_DIM), file=sys.stderr)
        if not dry:
            run(["umount", part], check=False)

def write_layout(dev, fw_bytes, total_sectors, dry, do_format, flavor):
    if flavor == "windows":
        header, data_start, data_blocks = build_mbr_layout(total_sectors)
        mkfs_name = "pure-Python FAT32"
    else:
        header, data_start, data_blocks = build_partition_layout(total_sectors)
        mkfs_name = "mkfs.hfsplus (HFS+)"
    if dry:
        print(color("  [dry-run] would wipe signatures, write %d-byte %s header, "
                    "%d-byte firmware @ sector %d, %s on data"
                    % (len(header), flavor, len(fw_bytes), FW_START, mkfs_name),
                    C_DIM), file=sys.stderr)
        return
    # 1. kill any existing partition signatures (MBR/GPT/FAT/HFS)
    if have("wipefs"):
        run(["wipefs", "-a", dev], check=False)
    with open(dev, "r+b") as f:
        # zero the leading area (old GPT/MBR/APM region) and the tail (GPT backup)
        f.seek(0); f.write(b"\x00" * (data_start * SECTOR))
        f.seek(max(0, total_sectors - 34) * SECTOR); f.write(b"\x00" * (34 * SECTOR))
        # 2. partition table (DDR+APM for Mac, MBR for Windows)
        f.seek(0); f.write(header)
        # 3. firmware image at the firmware partition's start (sector 63)
        f.seek(FW_START * SECTOR); f.write(fw_bytes)
        f.flush(); os.fsync(f.fileno())
    print(color("  wrote %s partition table + firmware"
                % ("MBR" if flavor == "windows" else "DDR+APM"), C_GRN), file=sys.stderr)
    # 4. data filesystem via a loop mapping of the data region
    if do_format:
        format_data(dev, data_start, data_blocks, flavor)
    # 5. re-read the partition table so the kernel sees the new map. The
    # BLKRRPART ioctl that creates the partition nodes is fast, but partprobe
    # then runs `udevadm settle`, which can stall for minutes per partition on
    # a slow reader with a large FAT32 (the node is already there by then).
    # Cap partprobe and fall through to `blockdev --rereadpt`, which does the
    # same re-read without waiting on udev.
    settled = False
    if have("partprobe"):
        try:
            run(["partprobe", dev], check=False, timeout=15)
            settled = True
        except subprocess.TimeoutExpired:
            print(color("  partprobe stalled on udev settle; the partition map "
                        "is already in place, continuing.", C_YEL), file=sys.stderr)
    if not settled and have("blockdev"):
        run(["blockdev", "--rereadpt", dev], check=False)

def fat_sectors_per_cluster(data_blocks):
    """Largest Apple-style cluster size that still yields a VALID FAT32.
    Apple uses 32 sectors (16 KiB) on real iPods, but FAT32 requires at
    least 65525 clusters — the FAT spec derives the filesystem TYPE from
    the cluster count, so an undersized "FAT32" is read as FAT16 by
    spec-following code (2003 iPod firmware included) and crashes it.
    Small cards therefore get smaller clusters (65600 keeps a safety
    margin above the boundary)."""
    spc = 32
    while spc > 1 and data_blocks // spc < 65600:
        spc //= 2
    return spc

def format_data(dev, data_start, data_blocks, flavor):
    if flavor == "windows":
        format_fat32_data(dev, data_start, data_blocks)
        return
    # Mac flavor: HFS+ via mkfs.hfsplus over a loop mapping. Linux-only and
    # slated for removal (see issue #1: the Mac/HFS+ flavor is being dropped).
    if not have("mkfs.hfsplus"):
        print(color("  mkfs.hfsplus not found (install hfsprogs) - skipping data "
                    "format; iPod may show 'use iTunes to restore'.", C_YEL),
              file=sys.stderr)
        return
    if not have("losetup"):
        print(color("  losetup not found - skipping data format.", C_YEL), file=sys.stderr)
        return
    off, size = data_start * SECTOR, data_blocks * SECTOR
    lo = run(["losetup", "--find", "--show", "--offset", str(off),
              "--sizelimit", str(size), dev]).stdout.strip()
    try:
        run(["mkfs.hfsplus", "-v", "iPod", lo])
        print(color("  formatted data partition (HFS+, label 'iPod')", C_GRN),
              file=sys.stderr)
    finally:
        run(["losetup", "-d", lo], check=False)

def format_fat32_data(dev, data_start, data_blocks):
    """Format the data partition FAT32 in pure Python — no mkfs.vfat, no
    losetup. Writes the filesystem structures straight onto the device at the
    partition offset, so this is identical on Linux, macOS, and Windows."""
    spc = fat_sectors_per_cluster(data_blocks)
    try:
        with open(dev, "r+b") as f:
            geo = fat32.format_fat32(f, data_start, data_blocks, spc, label="IPOD")
    except ValueError as e:
        print(color("  %s - skipping data format; the iPod will likely reject "
                    "the card." % e, C_YEL), file=sys.stderr)
        return
    except (OSError, IOError) as e:
        print(color("  could not write FAT32 to %s: %s - skipping data format."
                    % (dev, e), C_YEL), file=sys.stderr)
        return
    print(color("  formatted data partition (FAT32, %d clusters @ %d KiB, "
                "label 'IPOD')" % (geo["clusters"], spc * SECTOR // 1024), C_GRN),
          file=sys.stderr)

def verify_firmware(dev, fw_bytes, dry):
    """Read the firmware region back off the card and compare it byte-for-byte to the image we
    wrote. Runs after the images are written but before eject, so a bad write (flaky card, short
    write, bad cable) is caught here instead of bricking the boot. The buffer cache is flushed
    first (blockdev --flushbufs) so the read hits the physical card, not the pages we just wrote."""
    if dry:
        print(color("  [dry-run] would verify the firmware region byte-for-byte", C_DIM),
              file=sys.stderr)
        return
    print(color("  verifying firmware on the card byte-for-byte (%d bytes @ sector %d) ..."
                % (len(fw_bytes), FW_START), C_DIM), file=sys.stderr)
    run(["sync"], check=False)
    if have("blockdev"):
        run(["blockdev", "--flushbufs", dev], check=False)   # drop cache -> read the real card
    n = len(fw_bytes)
    with open(dev, "rb") as f:
        f.seek(FW_START * SECTOR)
        off = 0
        while off < n:
            want = min(1 << 20, n - off)
            chunk = f.read(want)
            if not chunk:
                sys.exit(color("VERIFY FAILED: short read (%d of %d firmware bytes) from %s — "
                               "do NOT use this card." % (off, n, dev), C_RED))
            for i, b in enumerate(chunk):
                if b != fw_bytes[off + i]:
                    bad = off + i
                    sys.exit(color("VERIFY FAILED at firmware byte 0x%06x (disk sector %d): card "
                                   "0x%02x != image 0x%02x. The write did not land correctly — do "
                                   "NOT use this card (re-seat it / try another and re-flash)."
                                   % (bad, FW_START + bad // SECTOR, b, fw_bytes[bad]), C_RED))
            off += len(chunk)
    print(color("  verify OK: %d firmware bytes on the card match the image exactly." % n, C_GRN),
          file=sys.stderr)

def eject(dev, dry):
    print(color("  flushing + ejecting %s" % dev, C_DIM), file=sys.stderr)
    if dry:
        return
    run(["sync"], check=False)
    if have("udisksctl"):
        run(["udisksctl", "power-off", "-b", dev], check=False)
    elif have("eject"):
        run(["eject", dev], check=False)

# ----------------------------------------------------------------------------
# Self-test (no root / hardware): round-trip the layout and check fields
# ----------------------------------------------------------------------------
def self_test():
    total = 9780743                       # the captured 5 GB iPod's total sectors
    header, ds, db = build_partition_layout(total)
    assert ds == 65599, ds
    assert db == total - TAIL_RESERVE - 65599, "APM tail reserve"
    # DDR
    assert header[0:2] == b"ER", "DDR sig"
    assert struct.unpack_from(">H", header, 2)[0] == 512
    assert struct.unpack_from(">I", header, 4)[0] == total
    # APM entries match the real captured iPod (starts/names/types; the data
    # partition is TAIL_RESERVE shorter than the capture by design)
    expect = [(1, 62, "partition map", "Apple_partition_map"),
              (63, 65536, "firmware", "Apple_MDFW"),
              (65599, db, "disk", "Apple_HFS")]
    for i, (st, cnt, nm, tp) in enumerate(expect):
        b = header[(1+i)*SECTOR:(2+i)*SECTOR]
        assert b[0:2] == b"PM"
        assert struct.unpack_from(">I", b, 4)[0] == 3
        assert struct.unpack_from(">I", b, 8)[0] == st, (nm, "start")
        assert struct.unpack_from(">I", b, 12)[0] == cnt, (nm, "count")
        assert b[16:48].split(b"\0")[0].decode() == nm
        assert b[48:80].split(b"\0")[0].decode() == tp
    # large-card LBA28 cap
    _, _, big_db = build_partition_layout(LBA28_MAX * 4)
    assert DATA_START + big_db == LBA28_MAX, "LBA28 cap"
    # Windows MBR layout
    mbr, mds, mdb = build_mbr_layout(total)
    assert mbr[510:512] == b"\x55\xaa", "MBR sig"
    assert mbr[446+4] == 0x00 and mbr[462+4] == 0x0b, "MBR partition types"
    assert struct.unpack_from("<I", mbr, 446+8)[0] == FW_START, "fw start"
    assert struct.unpack_from("<I", mbr, 446+12)[0] == FW_BLOCKS, "fw size"
    assert struct.unpack_from("<I", mbr, 462+8)[0] == mds == DATA_START
    assert mds + mdb == total - TAIL_RESERVE, "MBR tail reserve"
    # FAT32 cluster sizing: Apple's 16 KiB on big cards, shrunk below the
    # 65525-cluster FAT32 validity floor (978 MiB card needs 8 KiB clusters)
    assert fat_sectors_per_cluster(246867514) == 32, "big card keeps Apple spc"
    assert fat_sectors_per_cluster(1921531) == 16, "1 GB card shrinks clusters"
    assert 1921531 // 16 >= 65525, "small-card FAT32 is valid"
    assert fat_sectors_per_cluster(40000) == 1, "tiny card bottoms out"
    # firmware directory fixup + validation (synthetic format-2 image:
    # two directory copies, ipsw-style staging loadAddr, byte-sum checksum)
    fwimg = bytearray(0x4400 + 8)
    fwimg[0:4] = b"{{~~"
    fwimg[0x100:0x104] = b"]ih["
    struct.pack_into("<I", fwimg, 0x104, 0x4000)
    struct.pack_into("<H", fwimg, 0x10a, 2)
    payload = b"boot"
    fwimg[0x4400:0x4404] = payload
    for base in (0x4000, 0x4200):
        fwimg[base:base+4] = b"!ATA"
        fwimg[base+4:base+8] = b"soso"          # 4cc stored byte-reversed
        struct.pack_into("<8I", fwimg, base+8, 0, 0x4400, len(payload),
                         0x28000000, 0, sum(payload), 0x210, 0x048D0040)
        # aupd entry in ipsw pre-install state (id=0 = "update pending")
        fwimg[base+40:base+44] = b"!ATA"
        fwimg[base+44:base+48] = b"dpua"
        struct.pack_into("<8I", fwimg, base+48, 0, 0x4400, len(payload),
                         0x28000000, 0, sum(payload), 0x210, 0x048D0040)
    try:
        validate_firmware(bytes(fwimg))
        raise AssertionError("unfixed ipsw loadAddr accepted")
    except ValueError:
        pass
    fixed = fixup_directories(bytes(fwimg))
    validate_firmware(fixed)
    for base in (0x4000, 0x4200):
        assert struct.unpack_from("<I", fixed, base + 36)[0] == 0xFFFFFFFF, \
            "osos loadAddr fixup"
        assert struct.unpack_from("<I", fixed, base + 48)[0] == 1, \
            "aupd id fixup (0 = pending flash update -> boot loop)"
        assert struct.unpack_from("<I", fixed, base + 76)[0] == 0x28000000, \
            "aupd loadAddr fixup"
    print(color("self-test OK: APM+DDR layout; MBR layout + tail reserve; "
                "firmware loadAddr fixup + checksum; LBA28 cap.", C_GRN))

# ----------------------------------------------------------------------------
def flash(device=None, firmware=None, flavor="windows",
          assume_yes=False, dry_run=False, do_format=True, before_eject=None):
    """Flash a card end-to-end; the `ipod flash` entry point.
    Exits via sys.exit() on errors and aborted confirmations.
    `before_eject(dev)` runs after the firmware verify, while the partition
    nodes still exist (eject powers the device off) — `flashpod` uses it to
    offer running init on the fresh card."""
    try:
        fw = load_firmware(firmware)
    except (OSError, ValueError, StopIteration) as e:
        sys.exit(color("firmware error (%s): %s" % (firmware, e), C_RED))
    print(color("firmware OK: %s (%d bytes, structure validated)"
                % (os.path.basename(firmware), len(fw)), C_GRN), file=sys.stderr)

    if os.geteuid() != 0 and not dry_run:
        sys.exit(color("Run as root (sudo) to write to a block device.", C_RED))

    dev = device or choose_device()
    if not os.path.exists(dev):
        sys.exit(color("no such device: " + dev, C_RED))
    if dev.rstrip("0123456789") != dev and not dev.startswith("/dev/mmcblk") \
       and not dev.startswith("/dev/loop"):
        sys.exit(color("refusing a partition node (%s); pass the whole disk." % dev, C_RED))
    if os.path.basename(dev) in root_disk_names():
        sys.exit(color("refusing: %s backs the running system." % dev, C_RED))

    total_sectors = device_sectors(dev)
    if total_sectors <= 0:
        sys.exit(color("could not determine size of %s" % dev, C_RED))
    confirm(dev, total_sectors, assume_yes or dry_run, flavor)
    unmount_all(dev, dry_run)
    write_layout(dev, fw, total_sectors, dry_run, do_format, flavor)
    verify_firmware(dev, fw, dry_run)
    if before_eject and not dry_run:
        before_eject(dev)
    eject(dev, dry_run)
    print(color("\nDone. %s is ready — insert it into the iPod." % dev, C_GRN), file=sys.stderr)
    return 0
