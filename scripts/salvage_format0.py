#!/usr/bin/env python3
"""Re-lay a 2001-era format-0 firmware image into a bootable format-2 one.

The three oldest 1G images (Apple 1.0, 1.0.2, 1.0.4) were distributed in a
layout the 1G boot ROM cannot read. Their directory sits at 0x4000 with
each payload 0x800 BELOW the devOffset the entry advertises -- a logical
offset, not a physical one. The boot ROM reads devOffset absolutely, so a
verbatim write loads garbage and the iPod shows the broken-folder icon.
validate_firmware() rejects them for exactly this reason.

This produces the format-2 container those images have to be in:

    0x0000  boot block + volume header, carried over unchanged
            except fmtver -> 2 and the directory pointer -> 0x4000
    0x4000  firmware directory (staging copy)
    0x4200  firmware directory (live copy -- the one validate reads)
    0x4400  payloads, in directory order, each sector-aligned, each
            living at the absolute devOffset its entry now advertises

Nothing inside a payload is touched; this moves bytes and rewrites the
offsets that point at them. Version, checksums and load addresses carry
over, so it is the same firmware in packaging the hardware can boot.

    scripts/salvage_format0.py --verify-against iPod_1.0.4_2001_12_23.bin.gz \\
        "historic/ipsw/1st and 2nd gen/Firmware-1.1.0.4.zip"
    scripts/salvage_format0.py -o out.bin "historic/…/Firmware-1.1.0.2.bin.zip"

--verify-against is the reason to trust this. flashpod already ships
repacks of 1.0 and 1.0.4 that were made by hand and confirmed booting on
real 1G hardware; regenerating one of those byte-for-byte is what proves
the layout here is the hardware-proven one and not merely a plausible
reading of the format.
"""
import argparse
import gzip
import hashlib
import io
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flashpod.ipod_flash import validate_firmware, fixup_directories  # noqa: E402

SECTOR = 0x200
DIR_STAGING = 0x4000        # where format-0 already keeps its directory
DIR_LIVE = 0x4200           # validate_firmware reads the copy one sector on
PAYLOAD_START = 0x4400
ENTRY = 40                  # 4-byte marker + 4cc type + 8 u32 fields
MARKER = b"!ATA"

# A u16 at 0x108 that reads 0x010c in every genuine format-2 AND format-3
# image in the archive -- 1.1.1, 1.1.2, 1.1.5, 2.2.3, 4.3.1.1 -- and 0x0000
# in both format-0 ones. Whatever it denotes, it is part of the header
# contract the later formats keep, so a repack has to set it: the hand-made
# 1.0.4 repack that boots on real hardware does.
HEADER_0X108 = 0x010c

# The payloads of a format-0 image sit this far below their advertised
# devOffset. This is the whole defect.
FORMAT0_SKEW = 0x800


def unwrap(path):
    with open(path, "rb") as f:
        head = f.read(2)
    if head == b"PK":
        zf = zipfile.ZipFile(path)
        name = next(n for n in zf.namelist()
                    if os.path.basename(n).startswith("Firmware"))
        return zf.read(name)
    if head == b"\x1f\x8b":
        with gzip.open(path, "rb") as f:
            return f.read()
    return open(path, "rb").read()


def read_directory(fw, base=DIR_STAGING):
    """Yield (type_4cc, [8 fields]) for each !ATA entry.

    Not walk_directory(): that follows the pointer at 0x104, and in these
    images the pointer says 0x800 while the directory is really at 0x4000.
    """
    out = []
    for off in range(base, base + 10 * ENTRY, ENTRY):
        if off + ENTRY > len(fw) or fw[off:off + 4] != MARKER:
            break
        typ = fw[off + 4:off + 8][::-1]
        out.append((typ, list(struct.unpack_from("<8I", fw, off + 8))))
    return out


def align(n, to=SECTOR):
    return (n + to - 1) // to * to


def salvage(src):
    if src[0:4] != b"{{~~":
        raise SystemExit("not a firmware image (missing {{~~)")
    fmtver, = struct.unpack_from("<H", src, 0x10a)
    if fmtver != 0:
        raise SystemExit("format-%d image; this tool is only for format 0"
                         % fmtver)

    entries = read_directory(src)
    if not entries:
        raise SystemExit("no !ATA directory entries at 0x%x" % DIR_STAGING)
    if not any(t == b"osos" for t, _ in entries):
        raise SystemExit("no osos payload in the directory")

    # Lift each payload from its physical home (devOffset - 0x800) and give
    # it a new absolute one, sector-aligned, in directory order.
    placed, cursor = [], PAYLOAD_START
    for typ, f in entries:
        _id, dev_off, length = f[0], f[1], f[2]
        start = dev_off - FORMAT0_SKEW
        if start < 0 or start + length > len(src):
            raise SystemExit("%s payload at 0x%x..0x%x is outside the image"
                             % (typ.decode(), start, start + length))
        payload = src[start:start + length]
        if sum(payload) & 0xFFFFFFFF != f[5]:
            raise SystemExit(
                "%s checksum mismatch reading the SOURCE at 0x%x -- the "
                "-0x%x skew does not hold for this image, so its layout is "
                "not the one this tool knows how to fix"
                % (typ.decode(), start, FORMAT0_SKEW))
        placed.append((typ, f, cursor, payload))
        f[1] = cursor
        cursor = align(cursor + length)

    out = bytearray(align(cursor))
    out[0:DIR_STAGING] = src[0:DIR_STAGING]
    struct.pack_into("<I", out, 0x104, DIR_STAGING)   # pointer -> the directory
    struct.pack_into("<H", out, 0x108, HEADER_0X108)
    struct.pack_into("<H", out, 0x10a, 2)             # and it is format 2 now

    for base in (DIR_STAGING, DIR_LIVE):
        for i, (typ, f, _, _) in enumerate(placed):
            off = base + i * ENTRY
            out[off:off + 4] = MARKER
            out[off + 4:off + 8] = typ[::-1]
            struct.pack_into("<8I", out, off + 8, *f)

    for _, _, at, payload in placed:
        out[at:at + len(payload)] = payload

    # Bake in the install-state directory fixups. Every OTHER release asset
    # is pristine Apple bytes with these applied at flash time instead, but
    # the hand-made 1.0/1.0.4 repacks that are confirmed booting have them
    # baked in, and matching them byte-for-byte is the only evidence we have
    # that this layout is right. Harmless either way: load_firmware() runs
    # fixup_directories() again on the way to the card, and it is idempotent.
    return fixup_directories(bytes(out)), placed


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="format-0 .zip/.ipsw/raw image")
    ap.add_argument("-o", "--out", help="write the repacked image here")
    ap.add_argument("--verify-against", metavar="IMAGE",
                    help="a known-good repack this must reproduce exactly")
    args = ap.parse_args()

    src = unwrap(args.source)
    print("source  %s" % os.path.basename(args.source))
    print("        %d bytes, format 0" % len(src))

    out, placed = salvage(src)

    print("repack  %d bytes, format 2" % len(out))
    for typ, f, at, payload in placed:
        print("        %-4s -> 0x%-8x %8d bytes  ck 0x%08x"
              % (typ.decode(), at, len(payload), f[5]))

    validate_firmware(fixup_directories(out))
    print("        validate_firmware: OK")

    if args.verify_against:
        want = unwrap(args.verify_against)
        if out == want:
            print("verify  IDENTICAL to %s"
                  % os.path.basename(args.verify_against))
        else:
            print("verify  DIFFERS from %s (%d vs %d bytes)"
                  % (os.path.basename(args.verify_against), len(out), len(want)))
            n = sum(1 for a, b in zip(out, want) if a != b)
            print("        %d differing bytes in the overlap" % n)
            for i, (a, b) in enumerate(zip(out, want)):
                if a != b:
                    print("        first at 0x%x: %02x != %02x" % (i, a, b))
                    break
            return 1

    if args.out:
        with open(args.out, "wb") as f:
            f.write(out)
        print("wrote   %s  sha256 %s"
              % (args.out, hashlib.sha256(out).hexdigest()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
