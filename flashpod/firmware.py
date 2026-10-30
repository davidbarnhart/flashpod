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


#: The disk-mode programs (aupd payloads, as the LIVE directory points at
#: them) flashpod will ARM, by sha256. Arming makes the iPod reprogram its
#: own flash chip from the card, so only programs known to be good on a
#: 1G/2G are allowed (openpod firmware session, bridge #1227/#1231).
KNOWN_AUPD = {
    "dc68f0fc5d9aae12a30a293507969821915b432d07e1e6b83ccf861650a656fb":
        "flashpod 1.6 (openpod's disk-mode program, with large disks)",
    "b32e141d98ad7c0cb94a1829764a491fdbd8980c86982610c2663700ab85b130":
        "Apple 1.5",
}


def aupd_of(fw):
    """``{"sha256", "known"}`` of the disk-mode program (aupd) the live
    directory points at; ``known`` is its KNOWN_AUPD label or None. None
    when the firmware has no aupd."""
    import hashlib
    if fw[0x100:0x104] != b"]ih[":
        return None
    ptr, = struct.unpack_from("<I", fw, 0x104)
    fmtver, = struct.unpack_from("<H", fw, 0x10a)
    shift = 0x200 if fmtver == 3 else 0
    for _off, typ, f in walk_directory(fw, ptr + 0x200):
        if typ == b"aupd":
            blob = fw[f[1] + shift:f[1] + shift + f[2]]
            if len(blob) != f[2]:
                return None
            digest = hashlib.sha256(blob).hexdigest()
            return {"sha256": digest, "known": KNOWN_AUPD.get(digest)}
    return None


def arm_firmware(fw):
    """Install-ready firmware with the disk-mode update ARMED: the aupd
    entry's id back to 0 ("update pending") in BOTH directories, everything
    else as fixup_directories left it -- what openpod's
    tools/flash_rom_update.py writes. On its next start from the card, on
    FireWire power, the iPod reprograms its flash chip from the card's aupd
    and sets the id to 1 itself. Raises ValueError unless the aupd is one
    in KNOWN_AUPD."""
    info = aupd_of(fw)
    if info is None:
        raise ValueError("this firmware has no disk-mode program (aupd) "
                         "to arm")
    if info["known"] is None:
        raise ValueError("this firmware's disk-mode program (aupd sha256 "
                         "%s) isn't one flashpod knows is safe to arm"
                         % info["sha256"])
    fw = bytearray(fw)
    ptr, = struct.unpack_from("<I", fw, 0x104)
    n = 0
    for base in (ptr, ptr + 0x200):
        for off, typ, _f in walk_directory(fw, base):
            if typ == b"aupd":
                struct.pack_into("<I", fw, off + 8, 0)       # update pending
                n += 1
    if n != 2:
        raise ValueError("expected an aupd entry in both directories, "
                         "found %d" % n)
    return bytes(fw)


def aupd_armed(fw):
    """True if the aupd entry is armed (id 0) in both directories, False if
    in neither, None if there's no aupd or the two disagree."""
    if fw[0x100:0x104] != b"]ih[":
        return None
    ptr, = struct.unpack_from("<I", fw, 0x104)
    ids = [f[0] for base in (ptr, ptr + 0x200)
           for _o, typ, f in walk_directory(fw, base) if typ == b"aupd"]
    if len(ids) != 2 or (ids[0] == 0) != (ids[1] == 0):
        return None
    return ids[0] == 0


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
# Reading the firmware already on a card
# ----------------------------------------------------------------------------
#: osos load address of the 1G-3G (PP5002) images; the 1.x version word and
#: the openpod fixes are addressed relative to it.
PP5002_OSOS_ADDR = 0x28000000
PP502X_OSOS_ADDR = 0x10000000     # osos of 4th generation and later


def check_writable(fw):
    """1.0 writes firmware for 1st to 3rd generation iPods only. 4th
    generation and later (PP502x: osos at 0x10000000) are read-only until a
    written card has booted on one (David, 2026-10-29): inspect and extract
    still read them; card writes refuse them (ValueError). Mirrors the Rust
    core's firmware::check_writable."""
    if len(fw) < 0x10c or fw[0x100:0x104] != b"]ih[":
        return          # validate_firmware's to refuse
    ptr, = struct.unpack_from("<I", fw, 0x104)
    for _off, typ, f in walk_directory(fw, ptr + 0x200):
        if typ == b"osos":
            if f[3] == PP502X_OSOS_ADDR:
                raise ValueError(
                    "this firmware is for a 4th-generation or later iPod "
                    "(osos at 0x%08x); this version of flashpod doesn't write "
                    "those yet (untested on hardware). inspect and firmware "
                    "extract still read them." % f[3])
            return

#: Where 1.5 keeps its version, as a word (0x01508000 = "1.5"): osos offset of
#: vaddr 0x2803174c (openpod firmware session, bridge #1083). The 1.6 version
#: fix writes 0x01608000 there.
VERSION_WORD_OFFSET = 0x3174c
VERSION_WORDS = {0x01508000: "1.5", 0x01608000: "1.6"}

#: Exact osos builds that are not stock catalog entries, by sha256 of the
#: osos payload as installed (openpod firmware session, bridge #1083).
KNOWN_OSOS = {
    # the build the emulator gate passed on 2026-10-14, with shuffle-reserve;
    # withdrawn after the 2026-10-16 hardware bisect (the catalog's 1.6 is
    # this without it)
    "109ed777110982e2a31d7f724e4a7f78b73bef17bee90952e2c35c79f0714e21":
        "openpod 1.6 gate build (withdrawn)",
}


def _fixes_table():
    """The vendored fixes table (firmware/fixes.json), or None."""
    import json
    from . import resources
    path = resources.resource_path("firmware", "fixes.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fixes_present(osos, addr, table=None, aupd=None):
    """Names of the openpod fixes whose every edit and appended code are in
    the firmware: ``osos`` loaded at ``addr``, and ``aupd`` as ``(payload,
    addr)`` (or None, unread). A fix is present iff all of its edits hold
    their patched bytes and its appended code hashes to its sha256 (bridge
    #1083). A fix with an aupd edit counts only when aupd was read, so a fix
    that lives wholly in aupd (disk-mode-large-disks) is never claimed on
    the strength of osos alone."""
    import hashlib
    table = table if table is not None else _fixes_table()
    if not table:
        return None
    images = {"osos": (osos, addr)}
    if aupd is not None:
        images["aupd"] = aupd
    found = []
    for fix in table.get("fixes", []):
        ok = True
        for e in fix["edits"]:
            img = images.get(e.get("image", "osos"))
            if img is None:
                ok = False
                break
            data, base = img
            off = int(e["at"], 16) - base
            new = bytes.fromhex(e["patched"])
            if not (0 <= off and off + len(new) <= len(data)) \
                    or data[off:off + len(new)] != new:
                ok = False
                break
        ap = fix.get("append")
        if ok and ap:
            off = int(ap["at"], 16) - addr
            blob = osos[off:off + ap["bytes"]] if off >= 0 else b""
            ok = (len(blob) == ap["bytes"]
                  and hashlib.sha256(blob).hexdigest() == ap["sha256"])
        if ok:
            found.append(fix["name"])
    return found


class _OnCard(object):
    """A firmware image on the card, read only where it is sliced: lets
    fixes_present test a fix against the card while reading just that fix's
    bytes (the whole osos is 3.6 MB; FireWire raw reads run ~270 KiB/s)."""

    def __init__(self, read, offset, length):
        self._read, self._offset, self._length = read, offset, length

    def __len__(self):
        return self._length

    def __getitem__(self, s):
        start, stop, _ = s.indices(self._length)
        if stop <= start:
            return b""
        return bytes(self._read(self._offset + start, stop - start))


def fix_on_card(read, name, table=None):
    """Whether the firmware on a card carries the openpod fix ``name``:
    True/False, or None when it can't be told (no firmware volume, no osos,
    the fix isn't in the table). ``read(offset, n)`` reads the firmware
    partition. The same test as fixes_present, reading only the bytes the
    fix's edits and appended code occupy."""
    table = table if table is not None else _fixes_table()
    fix = next((f for f in (table or {}).get("fixes", [])
                if f["name"] == name), None)
    if fix is None or read(0, 4) != b"{{~~":
        return None
    hdr = read(0x100, 0x10)
    if hdr[0:4] != b"]ih[":
        return None
    ptr, = struct.unpack_from("<I", hdr, 4)
    fmtver, = struct.unpack_from("<H", hdr, 0x0a)
    shift = 0x200 if fmtver == 3 else 0
    images = {}
    for _off, typ, f in walk_directory(read(ptr + 0x200, 10 * 40), 0):
        key = typ.decode("latin-1")
        if key in ("osos", "aupd") and key not in images \
                and f[2] <= FW_BLOCKS * SECTOR:
            images[key] = (_OnCard(read, f[1] + shift, f[2]), f[3])
    if "osos" not in images:
        return None
    osos, addr = images["osos"]
    return name in fixes_present(osos, addr, {"fixes": [fix]},
                                 aupd=images.get("aupd"))


def describe_installed(read, catalog=None):
    """What firmware a card holds. ``read(offset, n)`` returns bytes at
    ``offset`` in the firmware partition. ``catalog`` is the manifest's
    ``firmwares`` list (entries with ``osos_sha256`` are matched).

    Returns a dict: ``present`` (bool), ``reason`` when not present, else
    ``format``, ``images`` (the directory), ``osos`` (``checksum_ok``,
    ``sha256``, ``length``, ``addr``), ``build`` (a human label: "stock <version>"
    for Apple's catalog entries, "<maker> <version>" for the others such as
    "flashpod 1.6", a known openpod build, or "unrecognized"), ``maker``
    (the catalog entry's, else None), ``catalog_file``, ``version_word``
    (hex) and ``version_word_means`` ("1.5"/"1.6"/None), ``aupd``
    (``sha256``, ``length``, ``addr``, ``known`` -- its KNOWN_AUPD label or
    None -- and ``armed``, a disk-mode update pending) when there is one,
    and ``fixes``
    (openpod fixes found in osos and aupd, None without a fixes table)."""
    import hashlib
    if read(0, 4) != b"{{~~":
        return {"present": False, "reason": "no STOP boot block"}
    hdr = read(0x100, 0x10)
    if hdr[0:4] != b"]ih[":
        return {"present": False, "reason": "no [hi] volume header"}
    ptr, = struct.unpack_from("<I", hdr, 4)
    fmtver, = struct.unpack_from("<H", hdr, 0x0a)
    shift = 0x200 if fmtver == 3 else 0
    images = []
    osos = aupd = None
    for _off, typ, f in walk_directory(read(ptr + 0x200, 10 * 40), 0):
        img = {"type": typ.decode("latin-1"), "id": f[0], "dev_offset": f[1],
               "length": f[2], "addr": f[3], "checksum": f[5],
               "vers": f[6], "load_addr": f[7]}
        images.append(img)
        if typ == b"osos" and osos is None:
            osos = img
        if typ == b"aupd" and aupd is None:
            aupd = img
    out = {"present": True, "format": fmtver, "images": images}
    if osos is None:
        out.update(present=False, reason="no osos image in the directory")
        return out
    if osos["length"] > FW_BLOCKS * SECTOR:
        out.update(present=False, reason="osos length past the partition")
        return out
    payload = read(osos["dev_offset"] + shift, osos["length"])
    digest = hashlib.sha256(payload).hexdigest()
    out["osos"] = {"checksum_ok": (sum(payload) & 0xFFFFFFFF) == osos["checksum"],
                   "sha256": digest, "length": osos["length"],
                   "addr": osos["addr"]}
    word = None
    if osos["addr"] == PP5002_OSOS_ADDR and \
            len(payload) >= VERSION_WORD_OFFSET + 4:
        word, = struct.unpack_from("<I", payload, VERSION_WORD_OFFSET)
    # only meaningful on a 1.x build; elsewhere that offset is just code
    out["version_word"] = "0x%08x" % word if word in VERSION_WORDS else None
    out["version_word_means"] = VERSION_WORDS.get(word)
    match = next((e for e in (catalog or [])
                  if e.get("osos_sha256") == digest), None)
    if match:
        maker = match.get("maker", "apple")
        out["build"] = "%s %s" % ("stock" if maker == "apple" else maker,
                                  match.get("version", "?"))
        out["maker"] = maker
        out["catalog_file"] = match.get("file")
        out["models"] = match.get("models", [])
    elif digest in KNOWN_OSOS:
        out["build"] = KNOWN_OSOS[digest]
        out["catalog_file"] = None
        out["maker"] = None
    else:
        out["build"] = "unrecognized"
        out["catalog_file"] = None
        out["maker"] = None
    aupd_code = None
    if aupd is not None and 0 < aupd["length"] <= FW_BLOCKS * SECTOR:
        blob = read(aupd["dev_offset"] + shift, aupd["length"])
        if len(blob) == aupd["length"]:
            aupd_code = (blob, aupd["addr"])
            digest = hashlib.sha256(blob).hexdigest()
            # armed = a disk-mode update pending (id 0 in both directories)
            out["aupd"] = {"sha256": digest,
                           "length": aupd["length"], "addr": aupd["addr"],
                           "known": KNOWN_AUPD.get(digest),
                           "armed": aupd_armed(read(0, ptr + 0x200 + 400))}
    out["fixes"] = fixes_present(payload, osos["addr"], aupd=aupd_code) \
        if osos["addr"] == PP5002_OSOS_ADDR else None
    return out
