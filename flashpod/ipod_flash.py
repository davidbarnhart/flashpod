"""Flash-card writer for early iPods — backs `flashpod flash`.

Import and call flash() / self_test(); the `flashpod` package owns the
command-line interface.

Reproduces the on-disk layout of a Windows-formatted early iPod onto a fresh
CompactFlash / SD card and installs the firmware:

    sector 0           DOS MBR (0xFEFFFF placeholder CHS)
    sectors 63..65598  firmware partition, type 0x00 (32 MiB)  <- raw firmware
    sectors 65599..    FAT32 data partition, type 0x0B, ending TAIL_RESERVE
                       sectors before the disk end (see below)

This MBR + FAT32 layout is what these tools write; a Windows-formatted iPod
works on both Mac and Windows hosts. (The old Mac APM + HFS+ flavor was
dropped — it depended on mkfs.hfsplus and never gained a pure-Python
formatter; FAT32 covers every pre-2007 iPod.)

The layout leaves the last TAIL_RESERVE sectors unallocated: iPod disk paths
can report the disk smaller than a card reader does (the 3G hides one sector),
and a data partition overhanging the iPod's view makes the firmware reject it
and demand a sync (found the hard way, 2026-06-12).

SAFETY: this ERASES the selected device. The tool only offers removable/USB
disks, never the disk holding '/', unmounts first, requires an explicit typed
confirmation, and supports --dry-run. Run as root.

After the images are written the firmware region is read back and compared
byte-for-byte to the image before the card is ejected, so a bad write is
caught immediately.
"""
import gzip, json, os, struct, subprocess, sys, textwrap, zipfile

from . import fat32
from . import platform as _plat

SECTOR        = 512
FW_START      = 63
FW_BLOCKS     = 65536            # 32 MiB firmware partition
DATA_START    = FW_START + FW_BLOCKS   # 65599
LBA28_MAX     = 0x10000000       # 2^28 sectors = 128 GiB ceiling of the iPod ATA driver
# Early iPods address at most LBA28 = 128 GiB. A card larger than that is
# unusable: the iPod's ATA layer can't reach past 2^28 sectors, and an
# oversized card (e.g. 256 GB in a 3G) typically makes the bridge report a
# wrapped/garbage capacity and read back an unreadable filesystem. Anything
# strictly above LBA28 is therefore suspect. (Marketed sizes are decimal, so
# a "128 GB" card is ~119 GiB — comfortably under this; the next common size
# up, 256 GB, is ~238 GiB or garbage — well over.)
CAPACITY_SANE_MAX = LBA28_MAX                    # 128 GiB (the LBA28 ceiling)
# iPod disk paths (bridge or flash adapter) can report the disk smaller than
# a card reader does — the 3G's path hides exactly 1 sector, and a partition
# that overhangs that view makes the firmware demand a sync. Apple leaves the
# last reader-visible sector unallocated; we leave 1 MiB for extra margin.
TAIL_RESERVE  = 2048             # sectors left unallocated at the disk end

C_RED, C_YEL, C_GRN, C_CYN, C_DIM, C_RST = (
    "\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[2m", "\033[0m")

def color(s, c):
    return c + s + C_RST if sys.stderr.isatty() else s

def implausible_capacity(total_sectors):
    """True if the device is larger than an early iPod can address (LBA28 /
    128 GiB) — an oversized card the iPod can't use, or a bogus capacity
    reading from one."""
    return total_sectors > CAPACITY_SANE_MAX

def capacity_warning(total_sectors):
    """One-line warning if the reported capacity exceeds the iPod's limit,
    else ''."""
    if not implausible_capacity(total_sectors):
        return ""
    return ("reports %s — past the 128 GiB (LBA28) limit these early iPods can "
            "address. An oversized card (256 GB+) is the usual cause: the iPod "
            "can't reach beyond 128 GiB, so it misreports capacity (often a "
            "wrapped, much-larger number) and reads back an unreadable "
            "filesystem — even though flashpod caps the data partition at "
            "128 GiB. Use a 128 GB-or-smaller card. (A loose IDE/CF connector "
            "can produce similar bogus readings, so reseat if the card really "
            "is small enough.)" % fmt_size(total_sectors * SECTOR))

# ----------------------------------------------------------------------------
# Partition-table construction (pure, testable)
# ----------------------------------------------------------------------------
def build_mbr_layout(total_sectors, lba48=False, max_data_sectors=None):
    """DOS MBR with a firmware partition (type 0x00) at sector 63 and FAT32
    data (type 0x0B) from sector 65599, stopping TAIL_RESERVE sectors short
    of the disk end.

    Normally the data partition is capped at LBA28_MAX (128 GiB) because early
    iPods can't address past 2^28 sectors. `lba48=True` removes that cap and
    uses ALL remaining space — only correct on an iPod running an LBA48 patch;
    an unpatched iPod cannot reach the extra sectors. (The MBR sector-count
    field is 32-bit, so this still tops out at 2 TiB.)

    `max_data_sectors` further caps the data partition — from a firmware's
    optional `max_data_gb` manifest field or the `--max-data-gb` flag — for a
    firmware that can't address the whole card, or just to make a smaller
    partition. It binds even under `lba48`. (No firmware flashpod ships needs
    it today: 1.0/1.0.4 were hardware-tested to use a 128 GB card in full.)

    Returns (mbr_512_bytes, data_start, data_blocks).
    """
    data_end    = total_sectors - TAIL_RESERVE
    if not lba48:
        data_end = min(data_end, LBA28_MAX)
    if max_data_sectors:
        data_end = min(data_end, DATA_START + max_data_sectors)
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
    """Return install-ready firmware bytes from a raw Firmware-* image, a
    gzipped one (.bin.gz -- what the manifest ships), or an .ipsw zip:
    unwrap, apply the install-time directory fixups Apple's updater performs,
    and validate. Dispatch is by magic bytes, not extension, so a
    hand-supplied --firmware works whatever it is called."""
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"PK":                          # .ipsw (a zip of Firmware-*)
        zf = zipfile.ZipFile(path)
        name = next(n for n in zf.namelist()
                    if os.path.basename(n).startswith("Firmware"))
        data = zf.read(name)
    elif head == b"\x1f\x8b":                  # .bin.gz
        with gzip.open(path, "rb") as f:
            data = f.read()
    else:                                      # raw Firmware-* image
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
    3G card's firmware image (hardware-validated same day). That claim was
    re-tested 2026-07-16 against the reference capture and holds: the factory
    card's slot 10 is empty and its firmware partition contains no flsh table,
    even though its aupd payload carries one and its aupd id is 1. It is in
    exactly the suppressed state we write -- which is *why* we match it.

    What setting aupd id=1 actually suppresses: the aupd payload carries the
    boot-ROM images and their flsh table in staging state. On boot with id=0
    the iPod flashes them into its ROM, promotes the flsh table into main
    directory slot 10 with loadAddr rewritten to 0xFFFFFFFF, and sets id=1.
    (Confirmed in the 1G dump: the slot-10 table is byte-identical to the copy
    nested in aupd except for exactly those loadAddr words, 0x04710020 ->
    0xFFFFFFFF.) So id=1 is a CLAIM MADE TO THE DEVICE ABOUT ITS OWN HARDWARE:
    "your ROM is already current". True and harmless for stock same-generation
    restores, which is all flashpod does. The narrow failure mode: firmware
    whose flsh payload is NEWER than the device's ROM will never install it,
    silently. Two consequences follow -- a flashpod-written card never gets a
    slot-10 flsh table (the iPod does that promotion during the flash we are
    suppressing), and cards we write are pre-flash images by construction.

    Format-2/3 images carry two directory copies (a staging table at the
    header pointer and the live one a sector later); patch both. (Every
    firmware flashpod ships is format 2 or 3 -- payloads sit at their
    ABSOLUTE devOffset, which validate_firmware enforces. The 2001-era 1G
    "format-0" images distributed with a directory at 0x4000 and payloads at
    devOffset - 0x800 are NOT that physical layout; they must be re-laid-out
    into a format-2 container before they will boot, and a raw one now fails
    validation rather than producing an unbootable card.)"""
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
    # format-3 images (4G+) payloads sit one sector past their devOffset;
    # format 2 (1G-3G) stores them at devOffset directly. The osos checksum
    # is verified at the payload's ABSOLUTE devOffset -- THE guardrail that a
    # written image is one the boot ROM can actually load. (A "format-0" 1G
    # image whose payloads sit at devOffset - 0x800 fails here, as it should:
    # the boot ROM reads devOffset absolutely and would load garbage.)
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
                          universal_newlines=True, timeout=timeout)

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
def pick_device(scan, render, path_of, empty_msg):
    """Interactive device chooser shared by every platform backend.

    ``scan()`` returns the candidate list (called again on each refresh),
    ``render(items)`` prints the table, ``path_of(item)`` yields the device path
    to return.

    The loop accepts 'r' to re-scan. Without it, a reader plugged in — or a card
    inserted — after the list was drawn stays invisible until you quit and start
    the whole command over, which is a long way back when you've already picked
    a model and firmware.
    """
    items = scan()
    if not items:
        sys.exit(color(empty_msg, C_RED))
    render(items)
    while True:
        try:
            sel = input(color("Select device number "
                              "('r' to rescan, 'q' to quit): ", C_CYN)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            sys.exit("Aborted.")
        if sel in ("q", "quit", ""):
            sys.exit("Aborted.")
        if sel in ("r", "refresh", "rescan"):
            items = scan()
            if not items:
                print(color("  " + empty_msg, C_YEL), file=sys.stderr)
            else:
                render(items)
            continue
        if sel.isdigit() and int(sel) < len(items):
            return path_of(items[int(sel)])
        print(color("  invalid selection", C_RED), file=sys.stderr)


def _render_candidates(cands):
    print(color("\nAttached removable storage:\n", C_CYN), file=sys.stderr)
    # Multi-slot readers expose one /dev/sdX per slot, all with the same
    # identity strings; number the slots so they can be told apart.
    usb_dirs = [usb_device_dir(d["name"]) for d in cands]
    rows = []
    for i, d in enumerate(cands):
        ident = device_identity(d)
        if usb_dirs[i] and usb_dirs.count(usb_dirs[i]) > 1:
            slot = sum(1 for u in usb_dirs[:i + 1] if u == usb_dirs[i])
            ident += ", slot %d" % slot
        mounted = any(p.get("mountpoint") for p in d.get("children") or []) \
            or d.get("mountpoint")
        contents = describe_contents(d)
        if mounted:
            contents += " (mounted)"
        rows.append({"n": i, "dev": "/dev/" + d["name"],
                     "size": fmt_size(d["size"]), "reader": ident,
                     "contents": contents,
                     "warn": capacity_warning(int(d.get("size") or 0) // SECTOR)})
    # Primary row (#, device, size, reader) is a fixed-width table; the longer
    # contents string wraps on its own indented lines beneath, so neither long
    # column forces the table past 80 columns.
    nw = max(1, len(str(len(rows) - 1)))
    dw = max(len("Device"), max(len(r["dev"]) for r in rows))
    sw = max(len("Size"), max(len(r["size"]) for r in rows))
    rw = max(len("Reader"), max(len(r["reader"]) for r in rows))

    def prow(n, dev, size, reader):
        return ("  %*s  %-*s  %*s  %-*s"
                % (nw, n, dw, dev, sw, size, rw, reader)).rstrip()

    sub = 2 + nw + 2 + dw + 2 + sw + 2   # indent contents under the Reader column
    print(color(prow("#", "Device", "Size", "Reader"), C_CYN), file=sys.stderr)
    print(color(prow("-" * nw, "-" * dw, "-" * sw, "-" * rw), C_DIM), file=sys.stderr)
    for r in rows:
        print(prow(r["n"], r["dev"], r["size"], r["reader"]), file=sys.stderr)
        for piece in textwrap.wrap(r["contents"], max(24, 80 - sub)) or [""]:
            print(color(" " * sub + piece, C_DIM), file=sys.stderr)
        if r["warn"]:
            print(color(" " * sub + "⚠ " + r["warn"], C_YEL), file=sys.stderr)
    print(file=sys.stderr)


def choose_device():
    return pick_device(
        list_candidates, _render_candidates,
        lambda d: "/dev/" + d["name"],
        "No removable/USB disks found. Plug in the card and retry.")

def device_label(dev):
    """Short, typeable name for a device, used in the ERASE confirmation.

    Not os.path.basename: on Windows a physical disk is ``\\\\.\\PhysicalDrive2``,
    which ntpath reads as a UNC drive root with an empty tail, so basename()
    returns "" and the prompt degrades to a bare `Type "ERASE " to proceed`.
    Splitting on both separators gives PhysicalDrive2 there and sdb on Linux.
    """
    name = dev.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name or dev


def confirm(dev, total_sectors, assume_yes, lba48=False, max_data_sectors=None):
    _, data_start, data_blocks = build_mbr_layout(total_sectors, lba48, max_data_sectors)
    fw_blocks = FW_BLOCKS
    print(color("\n  PLAN — this will ERASE %s" % dev, C_RED), file=sys.stderr)
    print("    layout      : MBR + FAT32", file=sys.stderr)
    print("    device size : %s (%d sectors)" % (fmt_size(total_sectors*SECTOR), total_sectors),
          file=sys.stderr)
    print("    firmware     : sectors %d..%d   (%s)" %
          (FW_START, FW_START+fw_blocks-1, fmt_size(fw_blocks*SECTOR)), file=sys.stderr)
    print("    data (FAT32) : sectors %d..%d  type 0x0B  (%s)" %
          (data_start, data_start+data_blocks-1, fmt_size(data_blocks*SECTOR)),
          file=sys.stderr)
    if lba48:
        if implausible_capacity(total_sectors):
            print(color("    ⚠ EXPERIMENTAL: --lba48 — data partition spans the "
                        "FULL %s, past the 128 GiB LBA28 limit. This ONLY works "
                        "on an iPod running an LBA48 patch; an unpatched iPod "
                        "cannot address the extra sectors and will misread the "
                        "card." % fmt_size(total_sectors * SECTOR), C_RED),
                  file=sys.stderr)
    else:
        warn = capacity_warning(total_sectors)
        if warn:
            print(color("    ⚠ WARNING: %s %s" % (dev, warn), C_RED), file=sys.stderr)
            print(color("    (data capped at the iPod's 128 GiB LBA28 limit; "
                        "pass --lba48 to use the whole card.)", C_YEL),
                  file=sys.stderr)
    if max_data_sectors and data_blocks == max_data_sectors \
            and total_sectors - TAIL_RESERVE > data_start + max_data_sectors:
        print(color("    ↳ data capped at %s (--max-data-gb); the rest of the "
                    "card is left unused."
                    % fmt_size(max_data_sectors * SECTOR), C_YEL), file=sys.stderr)
    mps = _plat.current().device_mountpoints(dev)
    if mps:
        print(color("    mounted now : " + ", ".join("%s@%s" % m for m in mps), C_YEL),
              file=sys.stderr)
    if assume_yes:
        return
    print(file=sys.stderr)
    want = "ERASE %s" % device_label(dev)
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

def write_layout(dev, fw_bytes, total_sectors, dry, do_format, lba48=False,
                 max_data_sectors=None):
    header, data_start, data_blocks = build_mbr_layout(total_sectors, lba48, max_data_sectors)
    if dry:
        print(color("  [dry-run] would wipe signatures, write %d-byte MBR header, "
                    "%d-byte firmware @ sector %d, pure-Python FAT32 on data"
                    % (len(header), len(fw_bytes), FW_START),
                    C_DIM), file=sys.stderr)
        return
    plat = _plat.current()
    # 1. kill any existing partition signatures (MBR/GPT/FAT)
    plat.wipe_signatures(dev, False)
    with plat.open_raw(dev, "r+b") as f:
        # zero the leading area (old GPT/MBR region) and the tail (GPT backup)
        f.seek(0); f.write(b"\x00" * (data_start * SECTOR))
        f.seek(max(0, total_sectors - 34) * SECTOR); f.write(b"\x00" * (34 * SECTOR))
        # 2. partition table (MBR)
        f.seek(0); f.write(header)
        # 3. firmware image at the firmware partition's start (sector 63)
        f.seek(FW_START * SECTOR); f.write(fw_bytes)
        f.flush(); os.fsync(f.fileno())
    print(color("  wrote MBR partition table + firmware", C_GRN), file=sys.stderr)
    # 4. data filesystem written straight into the data region
    if do_format:
        format_data(dev, data_start, data_blocks)
    # 5. re-read the partition table so the OS sees the new map.
    plat.reread_partition_table(dev)

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

def format_data(dev, data_start, data_blocks):
    """Format the data partition FAT32 in pure Python — no mkfs, no losetup.
    Writes the filesystem structures straight onto the device at the partition
    offset, so this is identical on Linux, macOS, and Windows."""
    spc = fat_sectors_per_cluster(data_blocks)
    try:
        with _plat.current().open_raw(dev, "r+b") as f:
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
    plat = _plat.current()
    plat.flush_buffers(dev)                                   # drop cache -> read the real card
    n = len(fw_bytes)
    with plat.open_raw(dev, "rb") as f:
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
    # MBR + FAT32 layout
    mbr, mds, mdb = build_mbr_layout(total)
    assert mds == DATA_START == 65599, mds
    assert mbr[510:512] == b"\x55\xaa", "MBR sig"
    assert mbr[446+4] == 0x00 and mbr[462+4] == 0x0b, "MBR partition types"
    assert struct.unpack_from("<I", mbr, 446+8)[0] == FW_START, "fw start"
    assert struct.unpack_from("<I", mbr, 446+12)[0] == FW_BLOCKS, "fw size"
    assert struct.unpack_from("<I", mbr, 462+8)[0] == mds == DATA_START
    assert mds + mdb == total - TAIL_RESERVE, "MBR tail reserve"
    # large-card LBA28 cap
    _, _, big_db = build_mbr_layout(LBA28_MAX * 4)
    assert DATA_START + big_db == LBA28_MAX, "LBA28 cap"
    # --lba48: no cap, full card minus the tail reserve
    _, _, lba48_db = build_mbr_layout(LBA28_MAX * 4, lba48=True)
    assert DATA_START + lba48_db == LBA28_MAX * 4 - TAIL_RESERVE, "lba48 uncapped"
    # --max-data-gb / manifest data cap: binds on a big card...
    cap = 5_000_000_000 // SECTOR
    _, _, capped_db = build_mbr_layout(LBA28_MAX * 4, max_data_sectors=cap)
    assert capped_db == cap, "firmware data cap binds"
    # ...binds even under --lba48, and never enlarges a small card
    _, _, capped48 = build_mbr_layout(LBA28_MAX * 4, lba48=True, max_data_sectors=cap)
    assert capped48 == cap, "firmware data cap binds under lba48"
    _, _, small_db = build_mbr_layout(2_000_000_000 // SECTOR, max_data_sectors=cap)
    assert small_db < cap, "firmware cap does not pad a small card"
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
    # A mis-packed image whose osos payload does NOT sit at its absolute
    # devOffset must be REJECTED -- this is the guardrail that a written card is
    # one the boot ROM can load (the 2001-era 1G "format-0" images fail it).
    bad = bytearray(fixed)
    struct.pack_into("<I", bad, 0x4200 + 12, 0x4a00)   # live osos devOffset -> wrong
    try:
        validate_firmware(bytes(bad))
        raise AssertionError("osos payload not at absolute devOffset accepted")
    except ValueError:
        pass
    print(color("self-test OK: MBR + FAT32 layout + tail reserve; "
                "firmware loadAddr fixup + osos checksum at absolute "
                "devOffset; LBA28 cap.", C_GRN))

# ----------------------------------------------------------------------------
def flash(device=None, firmware=None,
          assume_yes=False, dry_run=False, do_format=True, before_eject=None,
          lba48=False, max_data_gb=None):
    """Flash a card end-to-end; the `ipod flash` entry point.
    Exits via sys.exit() on errors and aborted confirmations.
    `before_eject(dev)` runs after the firmware verify, while the partition
    nodes still exist (eject powers the device off) — `flashpod` uses it to
    offer running init on the fresh card.
    `max_data_gb` caps the data partition (manifest `max_data_gb` or the
    `--max-data-gb` flag); None means no cap."""
    try:
        fw = load_firmware(firmware)
    except (OSError, ValueError, StopIteration) as e:
        sys.exit(color("firmware error (%s): %s" % (firmware, e), C_RED))
    print(color("firmware OK: %s (%d bytes, structure validated)"
                % (os.path.basename(firmware), len(fw)), C_GRN), file=sys.stderr)

    # An optional ceiling in decimal GB caps the data partition; a bigger card
    # is fine, we just clamp. (No firmware needs it now; --max-data-gb sets it.)
    max_data_sectors = int(max_data_gb * 1_000_000_000 // SECTOR) if max_data_gb else None

    plat = _plat.current()
    if not plat.is_admin() and not dry_run:
        sys.exit(color(plat.privilege_hint(), C_RED))

    dev = device or plat.choose_device()
    plat.validate_target(dev, dry_run)

    total_sectors = plat.device_sectors(dev)
    if total_sectors <= 0:
        sys.exit(color("could not determine size of %s" % dev, C_RED))
    confirm(dev, total_sectors, assume_yes or dry_run, lba48, max_data_sectors)
    plat.unmount_all(dev, dry_run)
    write_layout(dev, fw, total_sectors, dry_run, do_format, lba48, max_data_sectors)
    verify_firmware(dev, fw, dry_run)
    if before_eject and not dry_run:
        before_eject(dev)
    plat.eject(dev, dry_run)
    print(color("\nDone. %s is ready — insert it into the iPod." % dev, C_GRN), file=sys.stderr)
    return 0
