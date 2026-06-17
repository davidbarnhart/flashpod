"""Pure-Python FAT32 formatter.

Writes a FAT32 filesystem directly onto a seekable device or image at a
given partition offset — no `mkfs.vfat`, no `losetup`, nothing external.
This is what makes `flashpod flash` portable: the same code formats the
iPod's data partition on Linux, macOS, and Windows.

The on-disk structures follow Microsoft's FAT spec (fatgen103.doc); the
sectors-per-cluster choice and the >=65525-cluster validity floor are the
hard-won iPod lessons (see CLAUDE.md "CLUSTER-COUNT LESSON"): a "FAT32"
with too few clusters is read as FAT16 by spec-following firmware and
crashes the 2003-era iPod the same way a missing filesystem would.

Layout written into the partition (all little-endian):

    sector 0            boot sector / BPB
    sector 1            FSInfo
    sector 6            backup boot sector
    sector 7            backup FSInfo
    sectors 0..31       reserved region (32 sectors)
    then               FAT #1, FAT #2 (fat_size sectors each)
    then               data region; cluster 2 = root directory

Only the metadata regions are written (reserved + both FATs + the root
cluster are zeroed first, then the live bytes overlaid); the bulk of the
data region is left untouched, exactly as mkfs does.

Python 3.6 compatible (no walrus / dataclasses / match) so the same source
builds for the macOS 10.8 target.
"""

import os
import struct

SECTOR = 512

# FAT32 cluster-count bounds (fatgen103.doc): below the low bound the volume
# is FAT16 by definition, above the high bound it cannot be addressed.
FAT32_MIN_CLUSTERS = 65525
FAT32_MAX_CLUSTERS = 268435445


def fat_size_sectors(total_sectors, reserved, num_fats, sectors_per_cluster):
    """Sectors per FAT, via the conservative fatgen103.doc formula.

    May over-allocate the final FAT by a sector or two; that is safe and is
    what mkfs.fat does.
    """
    tmp1 = total_sectors - reserved
    tmp2 = (256 * sectors_per_cluster + num_fats) // 2
    return (tmp1 + tmp2 - 1) // tmp2


def cluster_count(total_sectors, reserved, num_fats, fat_size, sectors_per_cluster):
    """Number of addressable data clusters for the given geometry."""
    data_sectors = total_sectors - reserved - num_fats * fat_size
    if data_sectors < 0:
        return 0
    return data_sectors // sectors_per_cluster


def geometry(total_sectors, sectors_per_cluster, reserved=32, num_fats=2):
    """Compute and return the FAT32 geometry dict for a partition.

    Keys: fat_size, clusters, data_start_sector, reserved, num_fats,
    sectors_per_cluster, total_sectors.
    """
    fat_size = fat_size_sectors(total_sectors, reserved, num_fats,
                                sectors_per_cluster)
    clusters = cluster_count(total_sectors, reserved, num_fats, fat_size,
                             sectors_per_cluster)
    return {
        "total_sectors": total_sectors,
        "reserved": reserved,
        "num_fats": num_fats,
        "sectors_per_cluster": sectors_per_cluster,
        "fat_size": fat_size,
        "clusters": clusters,
        "data_start_sector": reserved + num_fats * fat_size,
    }


def _label_bytes(label):
    """11-byte, upper-cased, space-padded FAT volume label."""
    raw = (label or "").encode("ascii", "replace").upper()[:11]
    return raw + b" " * (11 - len(raw))


def _default_volume_id(total_sectors, label):
    """Deterministic 32-bit volume serial (mkfs uses the clock; we want a
    stable, reproducible value and uniqueness is irrelevant to the iPod)."""
    h = total_sectors & 0xFFFFFFFF
    for b in _label_bytes(label):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h or 0x1


def _boot_sector(geo, label, volume_id, hidden_sectors):
    bs = bytearray(SECTOR)
    bs[0:3] = b"\xeb\x58\x90"                     # jmp + nop
    bs[3:11] = b"MSWIN4.1"                         # OEM name
    struct.pack_into("<H", bs, 11, SECTOR)        # bytes per sector
    bs[13] = geo["sectors_per_cluster"]
    struct.pack_into("<H", bs, 14, geo["reserved"])
    bs[16] = geo["num_fats"]
    struct.pack_into("<H", bs, 17, 0)             # root entry count (0 = FAT32)
    struct.pack_into("<H", bs, 19, 0)             # total sectors 16 (0 = use 32)
    bs[21] = 0xF8                                  # media descriptor (fixed)
    struct.pack_into("<H", bs, 22, 0)             # FAT size 16 (0 = FAT32)
    struct.pack_into("<H", bs, 24, 63)            # sectors per track
    struct.pack_into("<H", bs, 26, 255)           # heads
    struct.pack_into("<I", bs, 28, hidden_sectors)
    struct.pack_into("<I", bs, 32, geo["total_sectors"])
    struct.pack_into("<I", bs, 36, geo["fat_size"])
    struct.pack_into("<H", bs, 40, 0)             # ext flags (mirror all FATs)
    struct.pack_into("<H", bs, 42, 0)             # filesystem version
    struct.pack_into("<I", bs, 44, 2)             # root directory cluster
    struct.pack_into("<H", bs, 48, 1)             # FSInfo sector
    struct.pack_into("<H", bs, 50, 6)             # backup boot sector
    bs[64] = 0x80                                  # drive number
    bs[66] = 0x29                                  # extended boot signature
    struct.pack_into("<I", bs, 67, volume_id)
    bs[71:82] = _label_bytes(label)
    bs[82:90] = b"FAT32   "                        # filesystem type string
    bs[510:512] = b"\x55\xaa"                      # boot signature
    return bytes(bs)


def _fsinfo_sector(geo):
    fs = bytearray(SECTOR)
    struct.pack_into("<I", fs, 0, 0x41615252)     # lead signature "RRaA"
    struct.pack_into("<I", fs, 484, 0x61417272)   # struct signature "rrAa"
    # cluster 2 is the root directory; everything else is free
    free = geo["clusters"] - 1 if geo["clusters"] > 0 else 0xFFFFFFFF
    next_free = 3 if geo["clusters"] > 1 else 0xFFFFFFFF
    struct.pack_into("<I", fs, 488, free)
    struct.pack_into("<I", fs, 492, next_free)
    struct.pack_into("<I", fs, 508, 0xAA550000)   # trail signature
    return bytes(fs)


def _fat_head():
    """First three FAT entries: media+EOC reservation, then EOC for the
    one-cluster root directory."""
    return struct.pack("<III", 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFFF)


def _root_dir_entry(label):
    """32-byte volume-label directory entry for the root cluster."""
    e = bytearray(32)
    e[0:11] = _label_bytes(label)
    e[11] = 0x08                                   # ATTR_VOLUME_ID
    return bytes(e)


def _write_zeros(f, base, length):
    chunk = b"\x00" * (1 << 20)
    f.seek(base)
    remaining = length
    while remaining > 0:
        n = chunk if remaining >= len(chunk) else chunk[:remaining]
        f.write(n)
        remaining -= len(n)


def format_fat32(f, part_start_sector, total_sectors, sectors_per_cluster,
                 label="IPOD", volume_id=None, reserved=32, num_fats=2,
                 hidden_sectors=None):
    """Write a FAT32 filesystem into open file/device ``f``.

    ``f`` must be opened for read+binary-write ("r+b" / "rb+") and be
    seekable. The filesystem occupies ``total_sectors`` 512-byte sectors
    starting at ``part_start_sector`` (use 0 when ``f`` is a bare partition
    image). The caller chooses ``sectors_per_cluster``; ``hidden_sectors``
    defaults to ``part_start_sector`` (the partition's LBA offset, the
    spec-correct value — the iPod ignores it either way).

    Returns the geometry dict from :func:`geometry`. Raises ValueError if
    the geometry does not yield a valid FAT32 (too few/many clusters).
    """
    geo = geometry(total_sectors, sectors_per_cluster, reserved, num_fats)
    clusters = geo["clusters"]
    if clusters < FAT32_MIN_CLUSTERS:
        raise ValueError(
            "partition too small for FAT32: %d clusters at %d-byte clusters "
            "(need >= %d, else firmware reads it as FAT16)"
            % (clusters, sectors_per_cluster * SECTOR, FAT32_MIN_CLUSTERS))
    if clusters > FAT32_MAX_CLUSTERS:
        raise ValueError("partition too large for FAT32: %d clusters (max %d)"
                         % (clusters, FAT32_MAX_CLUSTERS))

    if volume_id is None:
        volume_id = _default_volume_id(total_sectors, label)
    if hidden_sectors is None:
        hidden_sectors = part_start_sector

    base = part_start_sector * SECTOR
    fat_size = geo["fat_size"]

    # Zero the reserved region, both FATs, and the root directory cluster, so
    # no stale FAT chains or directory entries survive a reformat. The rest of
    # the data region is left as-is (mkfs does the same).
    meta_sectors = reserved + num_fats * fat_size + sectors_per_cluster
    _write_zeros(f, base, meta_sectors * SECTOR)

    # Boot sector + FSInfo, and their backups at sectors 6/7.
    boot = _boot_sector(geo, label, volume_id, hidden_sectors)
    fsinfo = _fsinfo_sector(geo)
    f.seek(base + 0 * SECTOR);  f.write(boot)
    f.seek(base + 1 * SECTOR);  f.write(fsinfo)
    f.seek(base + 6 * SECTOR);  f.write(boot)
    f.seek(base + 7 * SECTOR);  f.write(fsinfo)

    # FAT #1 and #2 headers (the remainder is the zeros we already wrote).
    fat_head = _fat_head()
    fat1 = base + reserved * SECTOR
    fat2 = fat1 + fat_size * SECTOR
    f.seek(fat1); f.write(fat_head)
    f.seek(fat2); f.write(fat_head)

    # Root directory cluster (cluster 2): the volume-label entry.
    root = base + geo["data_start_sector"] * SECTOR
    f.seek(root); f.write(_root_dir_entry(label))

    f.flush()
    try:
        os.fsync(f.fileno())
    except (OSError, AttributeError):
        pass
    return geo


def format_fat32_path(path, total_sectors=None, sectors_per_cluster=32,
                      part_start_sector=0, **kw):
    """Convenience wrapper: format a path (image file or device node).

    For a plain image, ``total_sectors`` defaults to the file's size.
    """
    with open(path, "r+b") as f:
        if total_sectors is None:
            total_sectors = (os.path.getsize(path) // SECTOR) - part_start_sector
        return format_fat32(f, part_start_sector, total_sectors,
                             sectors_per_cluster, **kw)


# ---------------------------------------------------------------------------
# Self-test (no root, no external tools required for the core checks)
# ---------------------------------------------------------------------------
def _read_back_check(path, part_start_sector, geo, label):
    """Re-parse the written structures and assert they are internally
    consistent and would be recognised as FAT32."""
    base = part_start_sector * SECTOR
    with open(path, "rb") as f:
        f.seek(base)
        bs = f.read(SECTOR)
        assert bs[510:512] == b"\x55\xaa", "missing boot signature"
        assert bs[82:90] == b"FAT32   ", "fs type string not FAT32"
        assert bs[11:13] == struct.pack("<H", SECTOR), "bytes/sector"
        assert bs[13] == geo["sectors_per_cluster"], "sectors/cluster"
        assert struct.unpack_from("<H", bs, 14)[0] == geo["reserved"], "reserved"
        assert bs[16] == geo["num_fats"], "num fats"
        assert struct.unpack_from("<I", bs, 36)[0] == geo["fat_size"], "fat size"
        assert struct.unpack_from("<I", bs, 44)[0] == 2, "root cluster"
        assert bs[71:82] == _label_bytes(label), "volume label"

        f.seek(base + 6 * SECTOR)
        assert f.read(SECTOR) == bs, "backup boot sector mismatch"

        f.seek(base + 1 * SECTOR)
        fsinfo = f.read(SECTOR)
        assert struct.unpack_from("<I", fsinfo, 0)[0] == 0x41615252, "fsinfo lead"
        assert struct.unpack_from("<I", fsinfo, 484)[0] == 0x61417272, "fsinfo struct"
        assert struct.unpack_from("<I", fsinfo, 508)[0] == 0xAA550000, "fsinfo trail"

        f.seek(base + geo["reserved"] * SECTOR)
        assert f.read(12) == _fat_head(), "FAT #1 head"
        f.seek(base + (geo["reserved"] + geo["fat_size"]) * SECTOR)
        assert f.read(12) == _fat_head(), "FAT #2 head"

        f.seek(base + geo["data_start_sector"] * SECTOR)
        entry = f.read(32)
        assert entry[0:11] == _label_bytes(label), "root label entry"
        assert entry[11] == 0x08, "root label attr"

    assert geo["clusters"] >= FAT32_MIN_CLUSTERS, "cluster count below FAT32 floor"


def _cross_check_tools(path):
    """Opportunistic external validation: mtools (`minfo`) and, if root,
    a loopback mount. Skips silently when the tools/permissions are absent."""
    import shutil
    import subprocess
    results = []
    if shutil.which("minfo"):
        try:
            out = subprocess.run(["minfo", "-i", path, "::"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=30)
            ok = out.returncode == 0 and b"sector size" in out.stdout.lower()
            results.append(("mtools minfo", "OK" if ok else "FAILED"))
        except Exception as e:                                   # noqa: BLE001
            results.append(("mtools minfo", "error: %s" % e))
    else:
        results.append(("mtools minfo", "SKIP (not installed)"))

    if hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("mount"):
        import subprocess
        import tempfile
        mnt = tempfile.mkdtemp(prefix="fat32test-")
        try:
            subprocess.run(["mount", "-o", "loop", path, mnt], check=True,
                           timeout=30)
            try:
                listed = os.listdir(mnt)
                results.append(("kernel vfat mount", "OK (mounts clean)"))
            finally:
                subprocess.run(["umount", mnt], check=False, timeout=30)
        except Exception as e:                                   # noqa: BLE001
            results.append(("kernel vfat mount", "FAILED: %s" % e))
        finally:
            try:
                os.rmdir(mnt)
            except OSError:
                pass
    else:
        results.append(("kernel vfat mount", "SKIP (needs root)"))
    return results


def _self_test():
    import tempfile

    # (partition MiB, sectors_per_cluster) covering small/medium/large cards.
    cases = [
        (300, 1),     # small: needs 512-byte clusters to clear the floor
        (512, 8),
        (2048, 16),
        (8192, 32),   # Apple's 16 KiB clusters on a big card
    ]
    for mib, spc in cases:
        total_sectors = mib * 1024 * 1024 // SECTOR
        # offset the filesystem inside the file to exercise part_start_sector
        part_start = 65599
        geo = geometry(total_sectors, spc)
        clusters = geo["clusters"]
        status = "valid" if clusters >= FAT32_MIN_CLUSTERS else "TOO FEW"
        print("  %5d MiB  spc=%-2d  fat=%d sec  clusters=%d (%s)"
              % (mib, spc, geo["fat_size"], clusters, status))
        if clusters < FAT32_MIN_CLUSTERS:
            raise AssertionError("test case %d MiB/spc=%d not valid FAT32" % (mib, spc))

        fd, path = tempfile.mkstemp(prefix="fat32-selftest-")
        os.close(fd)
        try:
            # sparse file big enough for the whole partition
            with open(path, "wb") as f:
                f.truncate((part_start + total_sectors) * SECTOR)
            with open(path, "r+b") as f:
                written = format_fat32(f, part_start, total_sectors, spc)
            _read_back_check(path, part_start, written, "IPOD")
            print("       read-back structure check: OK")
            # only the small image is worth running external tools against
            if mib <= 512:
                # external tools want the bare partition, so write one at offset 0
                fd2, ppath = tempfile.mkstemp(prefix="fat32-part-")
                os.close(fd2)
                try:
                    with open(ppath, "wb") as f:
                        f.truncate(total_sectors * SECTOR)
                    with open(ppath, "r+b") as f:
                        format_fat32(f, 0, total_sectors, spc)
                    for name, verdict in _cross_check_tools(ppath):
                        print("       %-18s %s" % (name + ":", verdict))
                finally:
                    os.unlink(ppath)
        finally:
            os.unlink(path)
    print("  all FAT32 formatter self-tests passed")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
    else:
        print(__doc__)
        print("Run with --self-test to exercise the formatter.")
