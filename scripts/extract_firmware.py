#!/usr/bin/env python3
"""Turn Apple firmware distributions into flashpod release assets.

The "format" of a release asset is not a format at all: it is the raw
Firmware-* image straight out of Apple's .ipsw, gzipped. Nothing is
rewritten. The install-time directory fixups live in load_firmware() and
run when the image is flashed, not when it is packaged -- which is why
`gunzip iPod_2.2.3.bin.gz` is byte-identical to the Firmware-2.2.3 inside
Apple's zip. Verified against the shipped 1.1.5, 2.2.3 and 4.3.1.1 assets.

So this script is mostly a validator with a gzip on the end. What it is
actually for is refusing to package something that would produce an
unbootable card, and emitting a firmware.json entry whose mechanical
fields nobody has to type by hand.

    scripts/extract_firmware.py -o out/ "historic/ipsw/3rd gen/Firmware-2.2.3"
    scripts/extract_firmware.py -o out/ historic/ipsw/1st\\ and\\ 2nd\\ gen/*

The one field it will NOT invent, because it cannot be recovered from the
bytes, is `description` -- Apple's release notes. That is emitted as a
TODO marker rather than guessed at.

Format-0 images are rejected rather than packaged. The 2001-era 1G
distributions carry payloads 0x800 below their absolute devOffset, and
the 1G boot ROM reads devOffset absolutely -- packaging one would ship a
card that broken-folders. Those need the format-2 salvage repack that
firmware.json's _salvage_comment describes; this script is not that tool
and says so instead of guessing.
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import re
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flashpod.ipod_flash import (fixup_directories, validate_firmware,  # noqa: E402
                                 walk_directory)

# Apple's ipsw family byte -> what flashpod calls the hardware. Families 4/10
# and 5/11 are two board revisions each, shipping byte-identical firmware
# (firmware.json collapses them into one entry; the archive confirms it --
# Firmware-4.3.1 and Firmware-10.3.1 hash the same).
FAMILIES = {
    1:  ("1st/2nd generation (FireWire)", ["1G", "2G"]),
    2:  ("3rd generation (dock connector)", ["3G"]),
    4:  ("4th generation (grayscale)", ["4G"]),
    10: ("4th generation (grayscale)", ["4G"]),
    5:  ("4th generation (iPod photo / color display)", ["photo"]),
    11: ("4th generation (iPod photo / color display)", ["photo"]),
}

# HARDWARE BOUNDARY, and the reason this script encodes model lists at all:
# 2G (touch wheel) support first appeared in family-1 firmware 1.2. Anything
# older is 1G-ONLY, and the `generation`/`models` strings are the only
# guidance the picker gives -- claiming 2G for a 2001 image would leave a 2G
# unusable. Getting this wrong is the expensive mistake, so it is derived
# from the version rather than typed per entry.
FAMILY1_2G_FROM = (1, 2)

# Families flashpod has no picker entry for. Validating clean is not the same
# as being flashable: no 5G has been through the hardware testing every
# current manifest entry has.
UNSUPPORTED = {
    13: "5th generation (video) -- flashpod has no 5G model entry and no 5G "
        "has been hardware-tested; validating clean is not support",
}

# Anything claiming to be a build date outside this window is not one.
ERA = (datetime.datetime(2001, 1, 1), datetime.datetime(2008, 1, 1))

# The real build date is compiled INTO the image: these binaries carry a C
# __DATE__ string ("Nov 01 2001", day space-padded). That is the primary
# source, and it is verifiable -- it reproduces the dates the manifest
# already recorded for 0.0, 0.4, 1.4 and 1.5, two of which were established
# long before this script existed and had no zip timestamp to come from.
#
# An inner-zip timestamp is the fallback, and it is the PACKAGING date, not
# the build: where the two disagree the zip always runs a few days late
# (1.2 was built 2002-07-24 and packaged 2002-07-31). Some copies are also
# modern repacks whose stamp is the repack -- the 1.0.2 zip says 2025, the
# 2.2.0.0 zip says 2023 -- which is what ERA screens out.
BUILD_DATE = re.compile(
    rb"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ?([ 0-3]?\d) (200\d)")


def embedded_build_date(raw):
    """The compiled-in __DATE__ stamp, or None."""
    m = BUILD_DATE.search(raw)
    if not m:
        return None
    try:
        stamp = datetime.datetime.strptime(
            "%s %s %s" % (m.group(1).decode(), m.group(2).decode().strip(),
                          m.group(3).decode()), "%b %d %Y")
    except ValueError:
        return None
    return stamp if ERA[0] <= stamp < ERA[1] else None


class Rejected(Exception):
    pass


def unwrap(path):
    """Return (raw image bytes, build_date or None, note or None).

    Dispatch is by magic bytes, matching load_firmware(), so it does not
    matter what the file is called -- the archive has .zip, .ipsw and bare
    Firmware-* all holding the same thing.
    """
    with open(path, "rb") as f:
        head = f.read(2)

    if head == b"PK":
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist()
                 if os.path.basename(n).startswith("Firmware")]
        if not names:
            raise Rejected("no Firmware-* member in the zip")
        info = zf.getinfo(names[0])
        stamp = datetime.datetime(*info.date_time)
        if ERA[0] <= stamp < ERA[1]:
            return zf.read(names[0]), stamp, None
        return zf.read(names[0]), None, (
            "zip timestamp %s is outside 2001-2007; treating as a repack "
            "date, not a build date" % stamp.strftime("%Y-%m-%d"))

    if head == b"\x1f\x8b":
        with gzip.open(path, "rb") as f:
            return f.read(), None, "already gzipped; no build date available"

    return open(path, "rb").read(), None, None


def inspect(raw):
    """Parse the header and run flashpod's own acceptance check.

    Using validate_firmware() rather than a private reimplementation is the
    point: if the flasher would refuse the image, packaging must refuse it
    too, and the two can never drift apart.
    """
    if raw[0:4] != b"{{~~":
        raise Rejected("missing {{~~ STOP boot block -- not a firmware image")
    if raw[0x100:0x104] != b"]ih[":
        raise Rejected("missing [hi] volume header at 0x100")

    ptr, = struct.unpack_from("<I", raw, 0x104)
    fmtver, = struct.unpack_from("<H", raw, 0x10a)

    if fmtver not in (2, 3):
        raise Rejected(
            "format-%d image: payloads sit below their absolute devOffset, "
            "which the boot ROM reads absolutely. Needs the format-2 salvage "
            "repack (see firmware.json _salvage_comment), not packaging"
            % fmtver)

    types = [t.decode("latin1")
             for _, t, _ in walk_directory(raw, ptr + 0x200)]

    # The real gate. fixup_directories() first, because validate_firmware()
    # requires the install-state loadAddr the flasher writes.
    validate_firmware(fixup_directories(raw))
    return fmtver, types


def family_of(path):
    """Apple named these Firmware-<family>.<version>; the family is the part
    that says which hardware it is for."""
    stem = os.path.basename(path)
    for suffix in (".zip", ".ipsw", ".bin.zip"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    if not stem.startswith("Firmware-"):
        raise Rejected("cannot read a family from %r (expected Firmware-N.…)"
                       % stem)
    rest = stem[len("Firmware-"):]
    head, _, tail = rest.partition(".")
    try:
        return int(head), tail
    except ValueError:
        raise Rejected("cannot read a family from %r" % stem)


def flashpod_version(family, version):
    """Apple's version string -> the one the picker shows.

    They differ in exactly one place. Apple's family-1 line ran 1.0, 1.0.2,
    1.0.3, 1.0.4, then 1.1, 1.2, 1.3... -- so a plain string sort puts the
    2001 launch images in the middle of the 2002-05 ones, and "1.0.4" reads
    as newer than "1.4". flashpod renumbers the 1.0.x series to 0.x, which
    is why firmware.json calls the launch firmware 0.0 while noting that
    Apple labelled it 1.0.
    """
    if family == 1 and (version == "1.0" or version.startswith("1.0.")):
        return "0." + version[len("1.0."):] if version != "1.0" else "0.0"
    return version


def models_for(family, version):
    if family in UNSUPPORTED:
        raise Rejected(UNSUPPORTED[family])
    if family not in FAMILIES:
        raise Rejected("unknown ipsw family %d" % family)
    generation, models = FAMILIES[family]
    if family == 1:
        parts = []
        for chunk in version.split("."):
            try:
                parts.append(int(chunk))
            except ValueError:
                parts.append(0)
        if tuple(parts[:2]) < FAMILY1_2G_FROM:
            # Pre-1.2: 1G only. See FAMILY1_2G_FROM.
            return "1st generation only (predates 2G support)", ["1G"]
    return generation, models


def gz(raw):
    """Deterministic gzip -- mtime 0 and no embedded filename, so the sha256
    in the manifest is a function of the image alone. Re-running this must
    reproduce the same asset byte for byte, or the checksum means nothing."""
    buf = io.BytesIO()
    f = gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                      fileobj=buf, mtime=0)
    f.write(raw)
    f.close()
    return buf.getvalue()


def process(path, outdir):
    family, version = family_of(path)
    raw, zip_date, note = unwrap(path)
    fmtver, types = inspect(raw)
    generation, models = models_for(family, version)

    # Prefer the compiled-in date; keep the zip stamp only as a fallback and
    # say so when they disagree, since the difference is build vs packaging.
    build_date = embedded_build_date(raw)
    if build_date and zip_date and build_date != zip_date:
        note = ("built %s, packaged %s -- using the compiled-in build date"
                % (build_date.strftime("%Y-%m-%d"),
                   zip_date.strftime("%Y-%m-%d")))
    elif not build_date and zip_date:
        build_date = zip_date
        note = "no compiled-in date found; falling back to the zip timestamp"

    label = flashpod_version(family, version)
    # Assets are named for the version the picker shows, not Apple's -- the
    # shipped iPod_1.0.4_2001_12_23.bin.gz is family 1 at flashpod's 0.4.
    stem = "iPod_%d.%s" % (family, label)
    if build_date:
        stem += build_date.strftime("_%Y_%m_%d")
    name = stem + ".bin.gz"

    blob = gz(raw)
    if outdir:
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(blob)

    entry = {
        "file": name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "size": len(blob),
        "generation": generation,
        "models": models,
        "version": label,
        "description": "TODO -- Apple release notes; not recoverable "
                       "from the image",
    }
    if build_date:
        entry["build_date"] = build_date.strftime("%Y-%m-%d")

    return {
        "source": path,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_size": len(raw),
        "format": fmtver,
        "types": types,
        "note": note,
        "entry": entry,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help=".ipsw, .zip, .bin.gz or raw Firmware-*")
    ap.add_argument("-o", "--outdir", help="write .bin.gz assets here")
    ap.add_argument("--json", action="store_true",
                    help="emit only the firmware.json entries")
    args = ap.parse_args()

    if args.outdir and not os.path.isdir(args.outdir):
        os.makedirs(args.outdir)

    ok, rejected, by_raw = [], [], {}
    for path in args.inputs:
        if os.path.basename(path).startswith("."):
            continue
        try:
            ok.append(process(path, args.outdir))
        except Rejected as e:
            rejected.append((path, str(e)))
        except Exception as e:                       # noqa: BLE001
            rejected.append((path, "%s: %s" % (type(e).__name__, e)))

    # Identical images under different names are common here (Apple shipped
    # the same bytes under two family numbers). Packaging both would put two
    # assets with one sha256 on the release.
    for r in ok:
        by_raw.setdefault(r["raw_sha256"], []).append(r)

    if args.json:
        json.dump([r["entry"] for r in ok], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0 if not rejected else 1

    for r in ok:
        e = r["entry"]
        print("%-28s fmt %d  %-9s %s" %
              (e["file"], r["format"], ",".join(r["types"]), e["generation"]))
        print("    sha256 %s  %d bytes" % (e["sha256"], e["size"]))
        if r["note"]:
            print("    note: %s" % r["note"])

    for sha, group in sorted(by_raw.items()):
        if len(group) > 1:
            print("\nDUPLICATE (%s…): identical images, package one" % sha[:12])
            for r in group:
                print("    %s" % r["source"])

    if rejected:
        print("\nrejected:")
        for path, why in rejected:
            print("  %s\n      %s" % (os.path.basename(path), why))

    print("\n%d packaged, %d rejected" % (len(ok), len(rejected)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
