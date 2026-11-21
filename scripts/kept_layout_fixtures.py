"""Card images laid out by tools other than flashpod, for keep-means-keep:
writing firmware onto such a card must use its partition table as it is.

Synthetic image files only (sparse; scaled-down sizes). Each fixture is
(name, builder, expect): expect is "keep" for a layout that only DIFFERS
from flashpod's own, or the refusal reason for one that is WRONG. A builder
takes a path and writes the image there.

    from kept_layout_fixtures import FIXTURES, outside_ranges, old_table_rewrite
"""
import os
import struct

SECTOR = 512
FW_START, FW_BLOCKS = 63, 65536
TAIL_RESERVE = 2048


def _image(path, total, entries, fats=()):
    """total sectors; MBR entries (type, start, sectors); FAT boot sectors
    (start, the FS's own total sectors). Random marks in each partition and
    the tail, so 'unchanged' covers more than zeros."""
    with open(path, "wb") as f:
        f.truncate(total * SECTOR)
        mbr = bytearray(SECTOR)
        for i, (t, s, n) in enumerate(entries):
            off = 446 + 16 * i
            mbr[off + 1:off + 4] = b"\xfe\xff\xff"
            mbr[off + 4] = t
            mbr[off + 5:off + 8] = b"\xfe\xff\xff"
            mbr[off + 8:off + 16] = struct.pack("<II", s, n)
        mbr[510:512] = b"\x55\xaa"
        f.seek(0)
        f.write(mbr)
        for start, n in fats:
            bs = bytearray(SECTOR)
            bs[0:3] = b"\xeb\x58\x90"
            bs[3:11] = b"MSWIN4.1"
            bs[11:13] = struct.pack("<H", 512)
            bs[13] = 8
            bs[32:36] = struct.pack("<I", n)
            bs[510:512] = b"\x55\xaa"
            f.seek(start * SECTOR)
            f.write(bs)
        for t, s, n in entries:
            f.seek((s + n // 2) * SECTOR)
            f.write(os.urandom(4096))
        f.seek(total * SECTOR - 4096)
        f.write(os.urandom(4096))


def _apm(path, total, fw, data):
    """An Apple Partition Map card: map, Apple_MDFW at fw, Apple_HFS at data."""
    with open(path, "wb") as f:
        f.truncate(total * SECTOR)
        hdr = bytearray(4 * SECTOR)
        hdr[0:2] = b"ER"
        hdr[2:4] = struct.pack(">H", SECTOR)
        hdr[4:8] = struct.pack(">I", total)
        for i, (s, n, name, kind) in enumerate([(1, 3, b"partition map", b"Apple_partition_map"),
                                                (fw[0], fw[1], b"firmware", b"Apple_MDFW"),
                                                (data[0], data[1], b"disk", b"Apple_HFS")]):
            e = (i + 1) * SECTOR
            hdr[e:e + 2] = b"PM"
            hdr[e + 4:e + 16] = struct.pack(">III", 3, s, n)
            hdr[e + 16:e + 16 + len(name)] = name
            hdr[e + 48:e + 48 + len(kind)] = kind
        f.seek(0)
        f.write(hdr)
        f.seek((data[0] + data[1] // 2) * SECTOR)
        f.write(os.urandom(4096))


def formula_entries(total):
    """flashpod's own MBR entries for a disk of ``total`` sectors
    (layout.build_mbr_layout, inside 128 GiB): firmware 63+65536, data from
    65599 to TAIL_RESERVE short of the end."""
    return [(0, FW_START, FW_BLOCKS), (0x0B, D, total - TAIL_RESERVE - D)]


def formula_card(path, total, made_at):
    """A card of ``total`` sectors laid out by flashpod's formula for a disk
    of ``made_at`` sectors (the transport case: made_at = total - 1)."""
    ents = formula_entries(made_at)
    _image(path, total, ents, [(D, ents[1][2])])


def _mbr(total, entries, fats=()):
    return lambda p: _image(p, total, entries, fats)


D = FW_START + FW_BLOCKS        # 65599, where flashpod's own data partition starts

FIXTURES = [
    # flashpod's own formula: data to total - TAIL_RESERVE
    ("flashpod's own", _mbr(D + 40000 + TAIL_RESERVE, [(0, 63, FW_BLOCKS), (0x0B, D, 40000)], [(D, 40000)]), "keep"),
    # the reported card, scaled: data one sector short of flashpod's formula
    ("off by one", _mbr(D + 40000 + TAIL_RESERVE + 1, [(0, 63, FW_BLOCKS), (0x0B, D, 40000)], [(D, 40000)]), "keep"),
    # the same card, two transports: laid out by flashpod's own formula on a
    # disk path that reports N-1 sectors, then written through a reader that
    # reports N
    ("flashpod's own at N-1, written at N", lambda p: formula_card(p, 120000, made_at=119999), "keep"),
    # Apple's Windows updater style: a bigger firmware partition, data to the last sector
    ("updater style", _mbr(80325 + 40000, [(0, 63, 80262), (0x0B, 80325, 40000)], [(80325, 40000)]), "keep"),
    # a bootloader installer rewrote the firmware in place; FAT smaller than its partition, type 0x0c
    ("patcher touched", _mbr(D + 60000, [(0, 63, FW_BLOCKS), (0x0C, D, 50000)], [(D, 49000)]), "keep"),
    # hand fdisk: 1 MiB-aligned data, a third partition, a big tail
    ("hand fdisk", _mbr(150000, [(0, 63, FW_BLOCKS), (0x0C, 67584, 40000), (0x83, 107584, 20000)], [(67584, 40000)]), "keep"),
    # a Mac card whose map puts the firmware away from sector 63
    ("APM, firmware elsewhere", lambda p: _apm(p, 170000, (128, 70000), (70128, 80000)), "keep"),
    # wrong:
    ("no table", lambda p: open(p, "wb").truncate(64 << 20), "no_table"),
    ("no firmware partition", _mbr(120000, [(0x0B, D, 40000)]), "no_firmware_partition"),
    ("firmware off sector 63", _mbr(120000, [(0, 2048, FW_BLOCKS), (0x0B, 2048 + FW_BLOCKS, 40000)]), "firmware_start"),
    ("overlap", _mbr(120000, [(0, 63, FW_BLOCKS), (0x0B, D - 600, 40000)]), "overlap"),
    ("data past the end", _mbr(D + 30000, [(0, 63, FW_BLOCKS), (0x0B, D, 40000)]), "past_end"),
    ("FAT bigger than its partition", _mbr(120000, [(0, 63, FW_BLOCKS), (0x0B, D, 40000)], [(D, 40001)]), "fat_larger_than_partition"),
    ("firmware partition too small", _mbr(120000, [(0, 63, 100), (0x0B, D, 40000)]), "partition_too_small"),
]


def firmware_partition(path):
    """(start, sectors) of the firmware partition, read here, not asked of flashpod."""
    with open(path, "rb") as f:
        head = f.read(4 * SECTOR)
    if head[:2] == b"ER":
        for i in range(1, 4):
            e = head[i * SECTOR:(i + 1) * SECTOR]
            if e[48:58] == b"Apple_MDFW":
                return struct.unpack(">II", e[8:16])
        return None
    for i in range(4):
        t, s, n = struct.unpack("<B3xII", head[446 + 16 * i + 4:446 + 16 * i + 16])
        if t == 0 and n:
            return s, n
    return None


def outside_ranges(path):
    """Byte ranges outside the firmware partition."""
    s, n = firmware_partition(path)
    return [(0, s * SECTOR), ((s + n) * SECTOR, os.path.getsize(path))]


def old_table_rewrite(path):
    """The CONTROL: what flash --no-format did before keep-means-keep, after
    writing the firmware: rewrite the MBR to flashpod's own formula."""
    total = os.path.getsize(path) // SECTOR
    mbr = bytearray(SECTOR)
    for i, (t, s, n) in enumerate(formula_entries(total)):
        off = 446 + 16 * i
        mbr[off + 1:off + 4] = b"\xfe\xff\xff"
        mbr[off + 4] = t
        mbr[off + 5:off + 8] = b"\xfe\xff\xff"
        mbr[off + 8:off + 16] = struct.pack("<II", s, n)
    mbr[510:512] = b"\x55\xaa"
    with open(path, "r+b") as f:
        f.seek(0)
        f.write(mbr)
