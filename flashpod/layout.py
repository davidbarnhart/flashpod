"""The card layout flashpod writes, as pure functions over a file object.

    sector 0           DOS MBR (0xFEFFFF placeholder CHS)
    sectors 63..65598  firmware partition, type 0x00 (32 MiB)  <- raw firmware
    sectors 65599..    FAT32 data partition, type 0x0B, ending TAIL_RESERVE
                       sectors before the disk end

Used by ``ipod_flash`` (the CLI's flash, which adds the device steps and the
terminal output) and by :mod:`flashpod.card` (image files). Nothing here
prints, prompts or exits: failures are exceptions.
"""
import struct

from . import fat32
from .errors import LayoutMismatch, VerifyFailed

SECTOR        = 512
FW_START      = 63
FW_BLOCKS     = 65536            # 32 MiB firmware partition
DATA_START    = FW_START + FW_BLOCKS   # 65599
LBA28_MAX     = 0x10000000       # 2^28 sectors = 128 GiB ceiling of the iPod ATA driver

# iPod disk paths (bridge or flash adapter) can report the disk smaller than
# a card reader does — the 3G's path hides exactly 1 sector, and a partition
# that overhangs that view makes the firmware demand a sync. Apple leaves the
# last reader-visible sector unallocated; we leave 1 MiB for extra margin.
TAIL_RESERVE  = 2048             # sectors left unallocated at the disk end


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


def write_layout_body(f, fw_bytes, total_sectors, data_start):
    """Everything ``ipod_flash.write_layout`` writes before the format and
    the (deferred) MBR: zero the leading area (old GPT/MBR region) and the
    tail (GPT backup), then the firmware at the firmware partition's start."""
    f.seek(0); f.write(b"\x00" * (data_start * SECTOR))
    f.seek(max(0, total_sectors - 34) * SECTOR); f.write(b"\x00" * (34 * SECTOR))
    f.seek(FW_START * SECTOR); f.write(fw_bytes)


def format_partition(f, data_start, data_blocks):
    """FAT32 (label IPOD) on the data partition, pure Python. Returns
    ``(geometry, sectors_per_cluster)``; raises ValueError when the partition
    is too small for a valid FAT32, OSError on a write failure."""
    spc = fat_sectors_per_cluster(data_blocks)
    geo = fat32.format_fat32(f, data_start, data_blocks, spc, label="IPOD")
    return geo, spc


def verify_region(f, fw_bytes, start_sector=FW_START):
    """Read the firmware region back from ``f`` and compare it byte-for-byte
    to ``fw_bytes``. Raises VerifyFailed with ``details``: ``short_read``
    (``got``, ``want``) or ``mismatch`` (``offset``, ``sector``, ``card``,
    ``image``)."""
    n = len(fw_bytes)
    f.seek(start_sector * SECTOR)
    off = 0
    while off < n:
        want = min(1 << 20, n - off)
        chunk = f.read(want)
        if not chunk:
            raise VerifyFailed("short read", reason="short_read", got=off,
                               want=n)
        for i, b in enumerate(chunk):
            if b != fw_bytes[off + i]:
                bad = off + i
                raise VerifyFailed("mismatch", reason="mismatch", offset=bad,
                                   sector=start_sector + bad // SECTOR,
                                   card=b, image=fw_bytes[bad])
        off += len(chunk)


def write_card(f, fw_bytes, total_sectors, lba48=False, max_data_sectors=None,
               do_format=True):
    """Write a card's layout into ``f`` -- everything but the MBR: the zeroed
    lead-in and tail, the firmware, then (``do_format``) the FAT32 data
    partition. The one sequence both `flashpod flash` (ipod_flash, around its
    device steps) and card.flash run. The MBR is returned, not written, so
    the caller commits it last (write_mbr) once the firmware has verified.

    Returns a dict: ``header`` (the MBR sector), ``data_start``,
    ``data_blocks``, and ``format``: None (not asked), ``(geometry,
    sectors_per_cluster)``, or the ValueError/OSError the formatter raised
    (the partition is then left unformatted). Raises ValueError from
    build_mbr_layout when the card is too small for any data partition."""
    header, data_start, data_blocks = build_mbr_layout(
        total_sectors, lba48, max_data_sectors)
    write_layout_body(f, fw_bytes, total_sectors, data_start)
    fmt = None
    if do_format:
        try:
            fmt = format_partition(f, data_start, data_blocks)
        except (ValueError, OSError, IOError) as exc:
            fmt = exc
    return {"header": header, "data_start": data_start,
            "data_blocks": data_blocks, "format": fmt}


def _fat_total_sectors(bs):
    """A FAT boot sector's own size of its filesystem in 512-byte sectors,
    or None when ``bs`` isn't a FAT boot sector."""
    if len(bs) < SECTOR or bs[510:512] != b"\x55\xaa":
        return None
    bps = struct.unpack_from("<H", bs, 11)[0]
    if bps not in (512, 1024, 2048, 4096) or bs[13] == 0:
        return None
    total = struct.unpack_from("<H", bs, 19)[0] or struct.unpack_from("<I", bs, 32)[0]
    return total * bps // SECTOR


def kept_layout(read, total_sectors, fw_len, lba48=False):
    """Keep means keep: can firmware of ``fw_len`` bytes be written into this
    card's firmware partition, using its partition table AS IT IS (whatever
    tool wrote it) and writing nothing else? ``read(offset, n)`` reads the
    card. The same checks as the Rust core's drive::check_kept_layout.

    A layout that only DIFFERS from flashpod's own (another tool's data
    length or tail gap, type 0x0b or 0x0c, a firmware partition bigger than
    32 MiB) is kept. One that is WRONG raises LayoutMismatch with
    ``reason``: no_table, apm (a Mac card: recognised, not handled here),
    no_firmware_partition, firmware_start (not sector 63), past_end,
    overlap, fat_larger_than_partition, partition_too_small, large_disk
    (data past 128 GiB without ``lba48``).

    Returns a dict: ``firmware`` and ``data`` ((start, sectors); data may be
    None), ``warnings`` (strings) and ``recognised`` (one line naming what
    was kept)."""
    mbr = read(0, SECTOR)
    if len(mbr) >= 2 and mbr[:2] == b"ER" and read(SECTOR, 2) == b"PM":
        raise LayoutMismatch(
            "an Apple Partition Map (Mac) card; flash --no-format keeps MBR "
            "(Windows-format) cards only", reason="apm")
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        raise LayoutMismatch("no partition table", reason="no_table")
    parts = []
    for i, off in enumerate((446, 462, 478, 494)):
        ptype = mbr[off + 4]
        start, count = struct.unpack_from("<II", mbr, off + 8)
        if count:
            parts.append((i + 1, ptype, start, count))
    fw = next(((s, n) for _, t, s, n in parts if t == 0x00), None)
    data = next(((s, n) for _, t, s, n in parts if t in (0x0B, 0x0C)), None)
    if fw is None:
        raise LayoutMismatch("no firmware partition (MBR type 0x00)",
                             reason="no_firmware_partition")
    if fw[0] != FW_START:
        raise LayoutMismatch(
            "the firmware partition starts at sector %d; flashpod writes "
            "firmware only into one that starts at sector %d, because it isn't "
            "known yet whether every iPod's boot ROM finds the firmware "
            "through the partition table rather than at sector %d"
            % (fw[0], FW_START, FW_START), reason="firmware_start")
    for i, _, s, n in parts:
        if s + n > total_sectors:
            raise LayoutMismatch(
                "partition %d (sectors %d..%d) runs past the end of the drive "
                "(%d sectors)" % (i, s, s + n - 1, total_sectors),
                reason="past_end")
    for k, (i, _, s, n) in enumerate(parts):
        for j, _, s2, n2 in parts[k + 1:]:
            if s < s2 + n2 and s2 < s + n:
                raise LayoutMismatch(
                    "partitions %d (sectors %d..%d) and %d (sectors %d..%d) "
                    "overlap" % (i, s, s + n - 1, j, s2, s2 + n2 - 1),
                    reason="overlap")
    warnings = []
    if data is not None:
        n = _fat_total_sectors(read(data[0] * SECTOR, SECTOR))
        if n is not None and n > data[1]:
            raise LayoutMismatch(
                "the data partition's FAT filesystem says it is %d sectors, "
                "but the partition is only %d" % (n, data[1]),
                reason="fat_larger_than_partition")
        gap = total_sectors - (data[0] + data[1])
        if gap < TAIL_RESERVE:
            warnings.append(
                "the data partition leaves %d sector%s unallocated at the end "
                "of the drive; flashpod's own layout leaves %d (some iPods see "
                "a disk slightly smaller than a card reader does)"
                % (gap, "" if gap == 1 else "s", TAIL_RESERVE))
    if fw_len > fw[1] * SECTOR:
        raise LayoutMismatch(
            "the firmware (%d bytes) is larger than the drive's firmware "
            "partition (%d bytes)" % (fw_len, fw[1] * SECTOR),
            reason="partition_too_small")
    if data is not None and data[0] + data[1] > LBA28_MAX and not lba48:
        raise LayoutMismatch(
            "the data partition ends at sector %d, past 128 GiB, which an "
            "iPod without LBA48 support can't read" % (data[0] + data[1]),
            reason="large_disk")
    span = lambda p: "%d..%d" % (p[0], p[0] + p[1] - 1)  # noqa: E731
    return {"firmware": fw, "data": data, "warnings": warnings,
            "recognised": "partition table kept as it is: MBR, firmware %s, %s"
                          % (span(fw), "data " + span(data) if data else
                             "no data partition")}


def write_firmware_into(f, fw_bytes, start=FW_START, sectors=FW_BLOCKS):
    """Write firmware into its partition and nothing else: the partition
    zeroed, then the firmware at its start (what a fresh flash leaves
    there). The caller verifies (verify_region)."""
    if len(fw_bytes) > sectors * SECTOR:
        raise LayoutMismatch("firmware larger than its partition",
                             reason="partition_too_small")
    zero = b"\x00" * (1 << 20)
    f.seek(start * SECTOR)
    left = sectors * SECTOR
    while left:
        n = min(left, len(zero))
        f.write(zero[:n])
        left -= n
    f.seek(start * SECTOR)
    f.write(fw_bytes)
    f.flush()


def write_mbr(f, header):
    """Commit the partition table: the last write of a flash."""
    f.seek(0)
    f.write(header)
    f.flush()
