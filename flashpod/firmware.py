"""iPod firmware images: load (raw, .bin.gz, .ipsw), apply the install-time
directory fixups, validate, and walk the directory.

Pure functions; failures raise ValueError (as the CLI has always caught).
"""
import gzip
import os
import struct
import zipfile

from .layout import FW_BLOCKS, SECTOR

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
