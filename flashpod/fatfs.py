"""Userspace FAT32 driver over a raw block device or image.

Why this exists: the gen-1 iPod FireWire bridge corrupts the large, read-ahead
transfers the OS issues when it mounts a volume (it returns zeros — see
CLAUDE.md "macOS can't mount this iPod over FireWire"), so macOS cannot mount
the iPod at all. But the bridge handles small, direct transfers fine (proven:
a single-sector read/write over the raw device round-trips correctly). This
module does every FAT32 access itself, in <= max_xfer sector chunks, straight
to the raw device (/dev/rdiskNsM on macOS, /dev/sdXN on Linux) — no OS mount,
no read-ahead — so flashpod can read and write the iPod where the OS can't.
Staying unbuffered is per-platform: macOS opens the rdisk CHARACTER device
(inherently cache-bypassing); Linux block devices are buffered unless opened
O_DIRECT, which BlockDev does (see __init__) so the chunking actually reaches
the bridge as small transfers instead of being re-batched by the page cache.

This file is the READ path (it powers reading the iTunesDB for `ls`); the write
path (add/rm) layers on top of the same block + FAT primitives.

Python 3.6 compatible (the macOS 10.8 build target): no walrus/dataclasses.
"""
import collections
import mmap
import os
import stat
import struct

SECTOR = 512

ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_LFN = 0x0F            # long-filename entry (read-only, hidden, sys, vol)

FAT32_EOC_MIN = 0x0FFFFFF8  # FAT entries >= this mark end-of-chain
FAT_MASK = 0x0FFFFFFF       # the top nibble of a FAT32 entry is reserved
FREE = 0x00000000           # a free FAT entry
EOC = 0x0FFFFFFF            # end-of-chain marker written for the last cluster

# A parsed directory entry. `name` is the long name if present, else the 8.3
# name; `short` is always the raw 8.3 name (handy for matching ASCII paths).
DirEntry = collections.namedtuple("DirEntry", "name short attr first_cluster size")


class BlockDev(object):
    """Sector I/O over a seekable device or image, with every transfer capped
    at ``max_xfer`` sectors — the cap is what keeps the fragile FireWire bridge
    happy (mirrors Linux's max_sectors_kb=4 = 8 sectors). Sector numbers passed
    to :meth:`read`/:meth:`write` are RELATIVE to ``part_start`` (the
    partition's first LBA), so the FAT code can think in partition-local
    sectors regardless of whether it opened a whole disk or a partition node.
    """

    def __init__(self, path, part_start=0, max_xfer=8, writable=False):
        self.part_start = part_start
        # One transfer cap for reads AND writes. We measured (2026-06-24) that
        # the gen-1 FireWire bridge is bandwidth-limited (~270 KiB/s), so larger
        # writes don't help — and it corrupts transfers above the read-safe
        # size in BOTH directions. So writes just use the proven-safe read cap.
        self.max_xfer = max_xfer
        flags = os.O_RDWR if writable else os.O_RDONLY
        # On Linux a plain open of a BLOCK device is BUFFERED: the page cache
        # re-batches our small max_xfer transfers into large writeback flushes
        # (fsync) and prefetches read_ahead_kb on reads — exactly the big
        # transfers the gen-1 FireWire bridge corrupts/crashes on. So without
        # O_DIRECT the raw path silently depends on the pinned host queue.
        # O_DIRECT bypasses the page cache entirely: every transfer reaches the
        # device at our aligned <= max_xfer size, no read-ahead, no writeback
        # merge — making this driver genuinely unbuffered on Linux, the way
        # /dev/rdiskN already is on macOS. We use it only for real block
        # devices: regular image files (the self-test, often on tmpfs) reject
        # O_DIRECT with EINVAL, and macOS has no O_DIRECT (it opens the rdisk
        # char device, which is already unbuffered).
        self._direct = False
        self._bounce = None
        if hasattr(os, "O_DIRECT") and stat.S_ISBLK(os.stat(path).st_mode):
            try:
                self._fd = os.open(path, flags | os.O_DIRECT)
                self._direct = True
            except OSError:
                self._fd = os.open(path, flags)   # fall back to buffered
        else:
            self._fd = os.open(path, flags)
        if self._direct:
            # O_DIRECT requires the offset, the length AND the buffer address
            # to be sector-aligned. An anonymous mmap is page-aligned and our
            # transfers are <= max_xfer sectors, so one page-aligned bounce
            # buffer serves every transfer.
            self._bounce = mmap.mmap(-1, self.max_xfer * SECTOR)

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._bounce is not None:
            self._bounce.close()
            self._bounce = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, lba, count):
        """Return ``count`` sectors starting at partition-relative ``lba``."""
        out = bytearray()
        abs_lba = self.part_start + lba
        remaining = count
        while remaining > 0:
            n = min(self.max_xfer, remaining)
            want = n * SECTOR
            off = abs_lba * SECTOR
            if self._direct:
                # Aligned, page-cache-bypassing read into the bounce buffer.
                got = os.preadv(self._fd, [memoryview(self._bounce)[:want]], off)
                if got != want:
                    raise IOError("short read at LBA %d (%d/%d)"
                                  % (abs_lba, got, want))
                out += bytes(self._bounce[:want])
            else:
                os.lseek(self._fd, off, os.SEEK_SET)
                buf = b""
                while len(buf) < want:
                    chunk = os.read(self._fd, want - len(buf))
                    if not chunk:
                        raise IOError("short read at LBA %d" % abs_lba)
                    buf += chunk
                out += buf
            abs_lba += n
            remaining -= n
        return bytes(out)

    def write(self, lba, data):
        """Write ``data`` (a whole number of sectors) at partition-relative
        ``lba``, chunked to ``max_xfer`` sectors per transfer."""
        if len(data) % SECTOR:
            raise ValueError("write length not a multiple of the sector size")
        abs_lba = self.part_start + lba
        off = 0
        while off < len(data):
            n = min(self.max_xfer, (len(data) - off) // SECTOR)
            nbytes = n * SECTOR
            dst = abs_lba * SECTOR
            chunk = data[off:off + nbytes]
            if self._direct:
                # Copy into the aligned bounce buffer, then write it straight
                # to the device (no page cache, so no large writeback later).
                self._bounce[:nbytes] = chunk
                wrote = os.pwritev(
                    self._fd, [memoryview(self._bounce)[:nbytes]], dst)
            else:
                os.lseek(self._fd, dst, os.SEEK_SET)
                wrote = os.write(self._fd, chunk)
            if wrote != nbytes:
                raise IOError("short write at LBA %d" % abs_lba)
            abs_lba += n
            off += nbytes

    def sync(self):
        # With O_DIRECT every write already reached the device, so this only
        # issues a small cache-flush command (no bulk transfer the bridge could
        # choke on); on the buffered path it is what forces the writeback.
        os.fsync(self._fd)


class Fat32(object):
    """A FAT32 filesystem on a :class:`BlockDev`. Read-only for now."""

    def __init__(self, dev):
        self.dev = dev
        bs = dev.read(0, 1)
        if struct.unpack_from("<H", bs, 11)[0] != SECTOR:
            raise ValueError("not a 512-byte-sector FAT volume")
        self.sectors_per_cluster = bs[13]
        self.reserved = struct.unpack_from("<H", bs, 14)[0]
        self.num_fats = bs[16]
        self.fat_size = struct.unpack_from("<I", bs, 36)[0]
        self.root_cluster = struct.unpack_from("<I", bs, 44)[0]
        self.total_sectors = struct.unpack_from("<I", bs, 32)[0]
        self.fsinfo_sector = struct.unpack_from("<H", bs, 48)[0]
        if self.fat_size == 0 or self.num_fats == 0:
            raise ValueError("not a FAT32 volume (zero FAT size / count)")
        self.data_start = self.reserved + self.num_fats * self.fat_size
        # count of addressable data clusters; valid cluster numbers are
        # 2 .. clusters+1 (cluster 0/1 are reserved).
        self.clusters = (self.total_sectors - self.data_start) // self.sectors_per_cluster
        self._fat_cache_lba = -1
        self._fat_cache = b""
        self._next_free = 2          # allocation cursor (hint)
        self._free_count = None      # lazily computed on first allocation

    # -- FAT (cluster -> next cluster) -------------------------------------
    def _fat_get(self, cluster):
        byte = cluster * 4
        lba = self.reserved + byte // SECTOR
        if lba != self._fat_cache_lba:
            self._fat_cache = self.dev.read(lba, 1)
            self._fat_cache_lba = lba
        return struct.unpack_from("<I", self._fat_cache, byte % SECTOR)[0] & FAT_MASK

    def chain(self, start):
        """Yield the cluster numbers of the chain beginning at ``start``."""
        cluster = start
        seen = set()
        while 2 <= cluster < FAT32_EOC_MIN:
            if cluster in seen:
                raise IOError("FAT chain loops at cluster %d" % cluster)
            seen.add(cluster)
            yield cluster
            cluster = self._fat_get(cluster)

    # -- clusters ----------------------------------------------------------
    def _cluster_lba(self, cluster):
        return self.data_start + (cluster - 2) * self.sectors_per_cluster

    def read_cluster(self, cluster):
        return self.dev.read(self._cluster_lba(cluster), self.sectors_per_cluster)

    def read_chain(self, start):
        if start < 2:
            return b""
        return b"".join(self.read_cluster(c) for c in self.chain(start))

    # -- directories -------------------------------------------------------
    @staticmethod
    def _short_name(entry):
        base = entry[0:8].rstrip(b" ")
        ext = entry[8:11].rstrip(b" ")
        if base[:1] == b"\x05":               # 0x05 stands in for a leading 0xE5
            base = b"\xe5" + base[1:]
        base = base.decode("latin-1")
        ext = ext.decode("latin-1")
        # VFAT lowercase flags (byte 12): bit 0x08 = base lower, 0x10 = ext
        # lower. This is how an all-lowercase 8.3 name (e.g. our fp01.mp3) is
        # stored without a long-filename entry.
        nt = entry[12]
        if nt & 0x08:
            base = base.lower()
        if nt & 0x10:
            ext = ext.lower()
        return base + "." + ext if ext else base

    @staticmethod
    def _lfn_chunk(entry):
        # UTF-16 code units live at offsets 1..10, 14..25, 28..31.
        return entry[1:11] + entry[14:26] + entry[28:32]

    @staticmethod
    def _decode_lfn(parts):
        parts.sort(key=lambda p: p[0] & 0x1F)   # order by sequence number
        raw = b"".join(chunk for _seq, chunk in parts)
        units = []
        for i in range(0, len(raw) - 1, 2):
            u = raw[i:i + 2]
            if u == b"\x00\x00" or u == b"\xff\xff":
                break
            units.append(u)
        return b"".join(units).decode("utf-16-le", "replace")

    def parse_dir(self, data):
        """Parse raw directory bytes into a list of :class:`DirEntry`."""
        out = []
        lfn = []
        for off in range(0, len(data), 32):
            entry = data[off:off + 32]
            if len(entry) < 32 or entry[0] == 0x00:
                break                              # 0x00 = no more entries
            if entry[0] == 0xE5:                    # deleted
                lfn = []
                continue
            attr = entry[11]
            if attr == ATTR_LFN:
                lfn.append((entry[0], self._lfn_chunk(entry)))
                continue
            short = self._short_name(entry)
            name = self._decode_lfn(lfn) if lfn else short
            lfn = []
            hi = struct.unpack_from("<H", entry, 20)[0]
            lo = struct.unpack_from("<H", entry, 26)[0]
            first = (hi << 16) | lo
            size = struct.unpack_from("<I", entry, 28)[0]
            out.append(DirEntry(name, short, attr, first, size))
        return out

    def read_dir(self, cluster):
        return self.parse_dir(self.read_chain(cluster))

    def resolve(self, path):
        """Return the :class:`DirEntry` for ``path`` (slash-separated,
        case-insensitive, matched against long OR 8.3 name), or None."""
        cluster = self.root_cluster
        found = None
        for comp in (c for c in path.replace("\\", "/").split("/") if c):
            cf = comp.casefold()
            match = None
            for entry in self.read_dir(cluster):
                if entry.attr & ATTR_VOLUME_ID:
                    continue
                if entry.name.casefold() == cf or entry.short.casefold() == cf:
                    match = entry
                    break
            if match is None:
                return None
            found = match
            cluster = match.first_cluster
        return found

    def listdir(self, path=""):
        """Real entries in ``path`` (root if empty), minus the volume label
        and the '.'/'..' links. None if ``path`` is not a directory."""
        cluster = self.root_cluster
        if path:
            entry = self.resolve(path)
            if entry is None or not (entry.attr & ATTR_DIRECTORY):
                return None
            cluster = entry.first_cluster
        return [e for e in self.read_dir(cluster)
                if not (e.attr & ATTR_VOLUME_ID) and e.short not in (".", "..")]

    def read_file(self, path):
        """Return the bytes of the file at ``path``, or None if absent/a dir."""
        entry = self.resolve(path)
        if entry is None or (entry.attr & ATTR_DIRECTORY):
            return None
        return self.read_chain(entry.first_cluster)[:entry.size]

    def open_file(self, path):
        """Return a lazy, seekable :class:`_FatFile` for ``path`` (None if it's
        absent or a directory). Unlike read_file, it reads clusters ON DEMAND —
        so a consumer that only touches a file's head and tail (e.g. mutagen
        sniffing tags) pulls just those clusters off the device, not the whole
        file. The cluster chain is pre-mapped up front, but that's FAT reads
        only (cached), not data, so a seek-to-end is cheap."""
        entry = self.resolve(path)
        if entry is None or (entry.attr & ATTR_DIRECTORY):
            return None
        return _FatFile(self, path, entry.first_cluster, entry.size)

    # ======================================================================
    # WRITE PATH
    #
    # Everything below mutates the filesystem. It assumes it owns a quiescent
    # volume (no concurrent mount) and writes through both FAT copies, keeping
    # FSInfo roughly in step. The block layer (BlockDev) caps transfer sizes,
    # so this is safe over the fragile FireWire bridge too.
    # ======================================================================
    _FAT_DATE = (2026 - 1980) << 9 | 6 << 5 | 1   # 2026-06-01, a valid FAT date

    def _bytes_per_cluster(self):
        return self.sectors_per_cluster * SECTOR

    # -- FAT entry writes --------------------------------------------------
    def _fat_set(self, cluster, value):
        """Set the FAT entry for ``cluster`` to ``value`` (a cluster number or
        EOC/FREE) in EVERY FAT copy, preserving the reserved top nibble, and
        invalidate the read cache."""
        byte = cluster * 4
        off = byte % SECTOR
        for fi in range(self.num_fats):
            lba = self.reserved + fi * self.fat_size + byte // SECTOR
            sec = bytearray(self.dev.read(lba, 1))
            old = struct.unpack_from("<I", sec, off)[0]
            struct.pack_into("<I", sec, off,
                             (old & 0xF0000000) | (value & FAT_MASK))
            self.dev.write(lba, bytes(sec))
        self._fat_cache_lba = -1

    # -- cluster allocation ------------------------------------------------
    def _load_free_count(self):
        """Seed the free-cluster count from FSInfo (one sector) — NEVER by
        scanning the multi-MB FAT, which over single-sector FireWire takes
        minutes. ``None`` means "unknown" (we then just won't maintain an exact
        FSInfo count; the OS/iPod recompute it)."""
        if self._free_count is not None:
            return
        self._free_count = -1                  # sentinel: unknown
        if self.fsinfo_sector:
            try:
                sec = self.dev.read(self.fsinfo_sector, 1)
                if struct.unpack_from("<I", sec, 0)[0] == 0x41615252:
                    v = struct.unpack_from("<I", sec, 488)[0]
                    if v != 0xFFFFFFFF:
                        self._free_count = v
            except (OSError, ValueError):
                pass

    def free_bytes(self):
        """Free space on the volume, in bytes — free clusters x cluster size,
        which is the cluster-granular figure a caller's size check wants.

        The free-cluster count comes from FSInfo (one sector); we never scan the
        whole FAT, which over single-sector FireWire takes minutes. Raises
        OSError if FSInfo carries no count (so a caller can degrade gracefully
        rather than guess)."""
        self._load_free_count()
        if self._free_count < 0:
            raise OSError("free-cluster count unavailable (FSInfo has none)")
        return self._free_count * self._bytes_per_cluster()

    def _scan_free_clusters(self, n):
        """Find ``n`` free clusters, reading the FAT one SECTOR at a time (128
        entries each) starting near the allocation cursor. Returns the list."""
        last = self.clusters + 2
        spers = SECTOR // 4
        out = []

        def scan(lo, hi):
            cl = lo
            while cl < hi and len(out) < n:
                sec_off = (cl * 4) // SECTOR
                base = sec_off * spers
                sec = self.dev.read(self.reserved + sec_off, 1)
                for i in range(cl - base, spers):
                    c = base + i
                    if c >= hi:
                        break
                    if struct.unpack_from("<I", sec, i * 4)[0] & FAT_MASK == 0:
                        out.append(c)
                        if len(out) == n:
                            return
                cl = base + spers

        start = max(2, self._next_free)
        scan(start, last)
        if len(out) < n:
            scan(2, start)
        if len(out) < n:
            raise IOError("no free clusters left on the volume")
        return out

    def _apply_fat_updates(self, updates):
        """Apply {cluster: value} to every FAT copy, grouping by sector so each
        affected FAT sector is read+written ONCE (not once per entry — the
        difference between a handful of transfers and hundreds over FireWire)."""
        by_sector = {}
        for cl, val in updates.items():
            by_sector.setdefault((cl * 4) // SECTOR, {})[(cl * 4) % SECTOR] = val
        for fi in range(self.num_fats):
            for sec_off, entries in by_sector.items():
                lba = self.reserved + fi * self.fat_size + sec_off
                sec = bytearray(self.dev.read(lba, 1))
                for off, val in entries.items():
                    old = struct.unpack_from("<I", sec, off)[0]
                    struct.pack_into("<I", sec, off,
                                     (old & 0xF0000000) | (val & FAT_MASK))
                self.dev.write(lba, bytes(sec))
        self._fat_cache_lba = -1

    def _alloc_one(self):
        """Reserve a single free cluster (marked EOC) and return its number."""
        return self._alloc_chain(1)[0]

    def _alloc_chain(self, n):
        """Allocate ``n`` clusters as a linked chain; return the list. Batches
        the FAT writes (one read+write per affected sector)."""
        if n == 0:
            return []
        free = self._scan_free_clusters(n)
        updates = {cl: (free[i + 1] if i + 1 < n else EOC)
                   for i, cl in enumerate(free)}
        self._apply_fat_updates(updates)
        self._next_free = free[-1] + 1
        self._load_free_count()
        if self._free_count >= 0:
            self._free_count = max(0, self._free_count - n)
        return free

    def _free_chain(self, start):
        if start < 2:
            return
        chain = list(self.chain(start))
        self._apply_fat_updates({cl: FREE for cl in chain})
        self._load_free_count()
        if self._free_count >= 0:
            self._free_count += len(chain)
        if chain:
            self._next_free = min(self._next_free, min(chain))

    def _zero_cluster(self, cluster):
        self.dev.write(self._cluster_lba(cluster),
                       b"\x00" * self._bytes_per_cluster())

    # -- writing bytes into a cluster (sub-sector aware) -------------------
    def _write_in_cluster(self, cluster, offset, data):
        """Write ``data`` at ``offset`` bytes into ``cluster`` via read-modify-
        write of just the covered sectors (offsets need not be sector-aligned)."""
        base = self._cluster_lba(cluster) * SECTOR + offset
        first = base // SECTOR
        last = (base + len(data) - 1) // SECTOR
        buf = bytearray(self.dev.read(first, last - first + 1))
        inner = base - first * SECTOR
        buf[inner:inner + len(data)] = data
        self.dev.write(first, bytes(buf))

    # -- directory helpers -------------------------------------------------
    def _dir_chain(self, first_cluster):
        return list(self.chain(first_cluster))

    def _short_names_in(self, dir_first):
        out = set()
        for cl in self._dir_chain(dir_first):
            data = self.read_cluster(cl)
            for off in range(0, len(data), 32):
                e = data[off:off + 32]
                if e[0] == 0x00:
                    return out
                if e[0] == 0xE5 or e[11] == ATTR_LFN:
                    continue
                out.add(bytes(e[0:11]))
        return out

    @staticmethod
    def _lfn_checksum(short11):
        s = 0
        for c in short11:
            s = (((s & 1) << 7) | (s >> 1)) + c & 0xFF
        return s

    _INVALID_83 = set(b'+,;=[] ')

    def _plan_name(self, name, dir_first):
        """Return (short11, nt_flags, needs_lfn) for ``name`` in directory
        ``dir_first``. Uses a bare 8.3 entry when the name fits losslessly
        (optionally via the VFAT lowercase byte-12 flags); otherwise generates a
        unique ``BASE~N`` short name and signals that LFN entries are needed."""
        dot = name.rfind(".")
        base, ext = (name, "") if dot <= 0 else (name[:dot], name[dot + 1:])
        ascii_ok = all(ord(c) < 128 and ord(c) >= 32 and
                       ord(c) not in self._INVALID_83 for c in name)
        fits = (ascii_ok and len(base) <= 8 and len(ext) <= 3
                and "." not in base)
        if fits:
            nt = 0
            if base == base.lower() and base != base.upper():
                nt |= 0x08
            elif base != base.upper():
                fits = False
            if ext == ext.lower() and ext != ext.upper():
                nt |= 0x10
            elif ext != ext.upper():
                fits = False
        if fits:
            short = (base.upper().ljust(8) + ext.upper().ljust(3)).encode("ascii")
            if short[0] == 0xE5:
                short = b"\x05" + short[1:]
            return short, nt, False
        # need a generated 8.3 + LFN
        taken = self._short_names_in(dir_first)
        clean = "".join(c for c in base.upper()
                        if ord(c) < 128 and c not in '+,;=[]. ') or "FILE"
        cext = "".join(c for c in ext.upper()
                       if ord(c) < 128 and c not in '+,;=[]. ')[:3]
        for n in range(1, 1000000):
            tail = "~%d" % n
            stem = clean[:8 - len(tail)] + tail
            short = (stem.ljust(8) + cext.ljust(3)).encode("ascii")
            if short not in taken:
                return short, 0, True
        raise IOError("could not generate a unique short name for %r" % name)

    def _short_entry(self, short11, attr, first_cluster, size, nt=0):
        """Build a single 8.3 directory entry (32 bytes)."""
        e = bytearray(32)
        e[0:11] = short11
        e[11] = attr
        e[12] = nt
        struct.pack_into("<H", e, 14, 0)                 # create time
        struct.pack_into("<H", e, 16, self._FAT_DATE)    # create date
        struct.pack_into("<H", e, 18, self._FAT_DATE)    # access date
        struct.pack_into("<H", e, 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", e, 22, 0)                 # write time
        struct.pack_into("<H", e, 24, self._FAT_DATE)    # write date
        struct.pack_into("<H", e, 26, first_cluster & 0xFFFF)
        struct.pack_into("<I", e, 28, size)
        return bytes(e)

    def _make_entries(self, name, attr, first_cluster, size, dir_first):
        """Build the directory entries (LFN entries + the 8.3 entry) for a new
        file/dir as a single bytes blob (a multiple of 32)."""
        short11, nt, needs_lfn = self._plan_name(name, dir_first)
        entries = bytearray()
        if needs_lfn:
            csum = self._lfn_checksum(short11)
            units = name.encode("utf-16-le")
            # pad to 13-char (26-byte) boundary with 0x0000 then 0xFFFF
            chars = [units[i:i + 2] for i in range(0, len(units), 2)]
            chars.append(b"\x00\x00")
            while len(chars) % 13:
                chars.append(b"\xff\xff")
            nparts = len(chars) // 13
            for seq in range(nparts, 0, -1):
                chunk = chars[(seq - 1) * 13: seq * 13]
                e = bytearray(32)
                e[0] = seq | (0x40 if seq == nparts else 0)
                e[11] = ATTR_LFN
                e[13] = csum
                e[1:11] = b"".join(chunk[0:5])
                e[14:26] = b"".join(chunk[5:11])
                e[28:32] = b"".join(chunk[11:13])
                entries += e
        entries += self._short_entry(short11, attr, first_cluster, size, nt)
        return bytes(entries)

    def _place_entries(self, dir_first, blob):
        """Write ``blob`` (N*32 bytes of directory entries) into the directory
        chain, finding N consecutive free slots within one cluster or extending
        the directory by a cluster. Returns nothing."""
        need = len(blob) // 32
        spc_slots = self._bytes_per_cluster() // 32
        for cl in self._dir_chain(dir_first):
            data = self.read_cluster(cl)
            run = 0
            run_start = 0
            for i in range(spc_slots):
                first_byte = data[i * 32]
                free = first_byte == 0x00 or first_byte == 0xE5
                if free:
                    if run == 0:
                        run_start = i
                    run += 1
                    if run == need:
                        self._write_in_cluster(cl, run_start * 32, blob)
                        return
                    if first_byte == 0x00:
                        # 0x00 marks end-of-dir: the rest of the cluster is free
                        if spc_slots - run_start >= need:
                            self._write_in_cluster(cl, run_start * 32, blob)
                            return
                        break          # not enough room before cluster end
                else:
                    run = 0
        # no room anywhere — extend the directory with a fresh, zeroed cluster
        last = self._dir_chain(dir_first)[-1]
        newcl = self._alloc_one()
        self._zero_cluster(newcl)
        self._fat_set(last, newcl)
        self._write_in_cluster(newcl, 0, blob)

    # -- FSInfo ------------------------------------------------------------
    def _flush_fsinfo(self):
        if not self.fsinfo_sector:
            return
        try:
            sec = bytearray(self.dev.read(self.fsinfo_sector, 1))
            if struct.unpack_from("<I", sec, 0)[0] != 0x41615252:
                return                          # not a real FSInfo; leave it
            self._load_free_count()
            fc = self._free_count if self._free_count >= 0 else 0xFFFFFFFF
            struct.pack_into("<I", sec, 488, fc & 0xFFFFFFFF)
            struct.pack_into("<I", sec, 492, self._next_free & 0xFFFFFFFF)
            self.dev.write(self.fsinfo_sector, bytes(sec))
        except (OSError, ValueError):
            pass

    def sync(self):
        self._flush_fsinfo()
        self.dev.sync()

    # -- high-level operations --------------------------------------------
    def _resolve_dir(self, path):
        """Return the first cluster of directory ``path`` (root if empty)."""
        if not path:
            return self.root_cluster
        entry = self.resolve(path)
        if entry is None or not (entry.attr & ATTR_DIRECTORY):
            raise IOError("not a directory: %r" % path)
        return entry.first_cluster

    @staticmethod
    def _split(path):
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        return "/".join(parts[:-1]), parts[-1]

    def exists(self, path):
        return self.resolve(path) is not None

    def mkdir(self, path):
        """Create directory ``path`` (parent must exist). No-op if it exists."""
        if self.exists(path):
            return
        parent, name = self._split(path)
        parent_first = self._resolve_dir(parent)
        cl = self._alloc_one()
        self._zero_cluster(cl)
        # '.' and '..' are special, fixed 8.3 entries (NOT name-generated):
        # '.' -> this dir's cluster, '..' -> parent's (0 when parent is root).
        ddot_target = 0 if parent_first == self.root_cluster else parent_first
        self._write_in_cluster(cl, 0,
            self._short_entry(b".          ", ATTR_DIRECTORY, cl, 0))
        self._write_in_cluster(cl, 32,
            self._short_entry(b"..         ", ATTR_DIRECTORY, ddot_target, 0))
        blob = self._make_entries(name, ATTR_DIRECTORY, cl, 0, parent_first)
        self._place_entries(parent_first, blob)
        self._flush_fsinfo()

    def write_file(self, path, data, progress=None):
        """Create or overwrite the file ``path`` with ``data`` (bytes). The
        parent directory must already exist. ``progress(done, total)`` is
        called as data clusters are written (useful on the slow FireWire bridge,
        where a multi-MB write is many single-sector transfers)."""
        parent, name = self._split(path)
        parent_first = self._resolve_dir(parent)
        nclusters = (len(data) + self._bytes_per_cluster() - 1) // self._bytes_per_cluster()
        chain = self._alloc_chain(nclusters) if nclusters else []
        # write the data, padded to whole clusters
        bpc = self._bytes_per_cluster()
        if progress and data:
            progress(0, len(data))          # immediate feedback before I/O
        for i, cl in enumerate(chain):
            chunk = data[i * bpc:(i + 1) * bpc]
            if len(chunk) < bpc:
                chunk = chunk + b"\x00" * (bpc - len(chunk))
            self.dev.write(self._cluster_lba(cl), chunk)
            if progress:
                progress(min((i + 1) * bpc, len(data)), len(data))
        first = chain[0] if chain else 0

        existing = self.resolve(path)
        if existing is not None:
            if existing.attr & ATTR_DIRECTORY:
                raise IOError("is a directory: %r" % path)
            # rewrite the existing entry's first-cluster + size in place, then
            # free the old data chain
            self._update_entry(parent_first, name, first, len(data))
            self._free_chain(existing.first_cluster)
        else:
            blob = self._make_entries(name, 0x20, first, len(data), parent_first)
            self._place_entries(parent_first, blob)
        self._flush_fsinfo()

    def _update_entry(self, dir_first, name, first_cluster, size):
        """Patch the 8.3 entry for ``name`` in ``dir_first`` with a new first
        cluster and size (used to rewrite a file in place)."""
        cf = name.casefold()
        for cl in self._dir_chain(dir_first):
            data = self.read_cluster(cl)
            lfn = []
            for i in range(0, len(data), 32):
                e = data[i:i + 32]
                if e[0] == 0x00:
                    break
                if e[0] == 0xE5:
                    lfn = []
                    continue
                if e[11] == ATTR_LFN:
                    lfn.append((e[0], self._lfn_chunk(e)))
                    continue
                long_n = self._decode_lfn(lfn) if lfn else None
                lfn = []
                short_n = self._short_name(e)
                if (short_n.casefold() == cf or
                        (long_n and long_n.casefold() == cf)):
                    patch = bytearray(e)
                    struct.pack_into("<H", patch, 20, (first_cluster >> 16) & 0xFFFF)
                    struct.pack_into("<H", patch, 26, first_cluster & 0xFFFF)
                    struct.pack_into("<I", patch, 28, size)
                    self._write_in_cluster(cl, i, bytes(patch))
                    return
        raise IOError("entry vanished while updating: %r" % name)

    def remove(self, path):
        """Delete the file ``path``: free its clusters and mark its directory
        entries (the 8.3 entry and any preceding LFN entries) as deleted."""
        parent, name = self._split(path)
        parent_first = self._resolve_dir(parent)
        cf = name.casefold()
        for cl in self._dir_chain(parent_first):
            data = self.read_cluster(cl)
            lfn_slots = []
            for i in range(0, len(data), 32):
                e = data[i:i + 32]
                if e[0] == 0x00:
                    break
                if e[0] == 0xE5:
                    lfn_slots = []
                    continue
                if e[11] == ATTR_LFN:
                    lfn_slots.append(i)
                    continue
                short_n = self._short_name(e)
                long_n = None
                if lfn_slots:
                    parts = [(data[s], self._lfn_chunk(data[s:s + 32]))
                             for s in lfn_slots]
                    long_n = self._decode_lfn(parts)
                if (short_n.casefold() == cf or
                        (long_n and long_n.casefold() == cf)):
                    if e[11] & ATTR_DIRECTORY:
                        raise IOError("is a directory: %r" % path)
                    first = (struct.unpack_from("<H", e, 20)[0] << 16) | \
                        struct.unpack_from("<H", e, 26)[0]
                    for s in lfn_slots + [i]:
                        self._write_in_cluster(cl, s, b"\xe5")
                    self._free_chain(first)
                    self._flush_fsinfo()
                    return
                lfn_slots = []
        raise IOError("no such file: %r" % path)


class _FatFile(object):
    """A lazy, seekable, read-only file over a FAT cluster chain. Reads clusters
    ON DEMAND (caching the most recent one), so a consumer that only touches a
    file's head and tail — like mutagen sniffing an MP3's ID3 + first frame, then
    seeking to EOF for the size and a trailing ID3v1/APEv2 tag — pulls just those
    one or two clusters off the device instead of the whole multi-MB file. The
    chain is pre-mapped in __init__, but that's cached FAT reads, not data, so a
    seek-to-end jumps straight to the last cluster. Implements the subset of the
    file protocol mutagen needs: read/seek/tell/seekable (+ name, close, with)."""

    def __init__(self, fs, name, first_cluster, size):
        self._fs = fs
        self.name = name              # mutagen reads this for a format hint
        self.size = size
        # Pre-map the chain — FAT-table reads only (cheap, cached), no data.
        self._clusters = list(fs.chain(first_cluster)) if first_cluster >= 2 \
            else []
        self._cbytes = fs._bytes_per_cluster()
        self._pos = 0
        self._cache_idx = -1          # chain index currently in _cache
        self._cache = b""

    def _cluster_at(self, idx):
        if idx != self._cache_idx:
            self._cache = self._fs.read_cluster(self._clusters[idx])
            self._cache_idx = idx
        return self._cache

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self.size + offset
        else:
            raise ValueError("invalid whence: %r" % whence)
        if self._pos < 0:
            self._pos = 0
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        end = self.size if n is None or n < 0 else min(self.size, self._pos + n)
        out = bytearray()
        while self._pos < end:
            idx = self._pos // self._cbytes
            if idx >= len(self._clusters):
                break
            within = self._pos % self._cbytes
            take = min(self._cbytes - within, end - self._pos)
            out += self._cluster_at(idx)[within:within + take]
            self._pos += take
        return bytes(out)

    def close(self):
        self._cache = b""
        self._cache_idx = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Self-test: cross-validate the reader against mtools (an independent FAT
# implementation). mtools writes the volume; our driver must read it back
# byte-for-byte.
# ---------------------------------------------------------------------------
def _self_test():
    import shutil
    import subprocess
    import tempfile
    from . import fat32

    total_sectors = 300 * 1024 * 1024 // SECTOR     # 300 MiB, spc=1
    fd, img = tempfile.mkstemp(prefix="fatfs-selftest-")
    os.close(fd)
    try:
        with open(img, "wb") as f:
            f.truncate(total_sectors * SECTOR)
        with open(img, "r+b") as f:
            geo = fat32.format_fat32(f, 0, total_sectors, 1, label="IPOD")

        have_mtools = bool(shutil.which("mcopy"))
        db_bytes = bytes((i * 7 + 3) % 256 for i in range(2048 + 137))
        music_bytes = bytes((i * 5 + 1) % 251 for i in range(5000))

        # Populate the volume with mtools BEFORE we open it — our driver caches
        # FAT sectors and assumes it owns a quiescent filesystem, so all
        # external writes must land first (mirrors how flashpod opens an iPod
        # that already has its layout).
        if have_mtools:
            env = dict(os.environ, MTOOLS_SKIP_CHECK="1")

            def m(*args):
                r = subprocess.run(args, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
                if r.returncode != 0:
                    raise AssertionError("%s failed: %s"
                                         % (args[0], r.stdout.decode()))

            for d in ("::/iPod_Control", "::/iPod_Control/iTunes",
                      "::/iPod_Control/Music", "::/iPod_Control/Music/F00"):
                m("mmd", "-i", img, d)
            # multi-cluster payloads exercise FAT-chain traversal (512 B clusters)
            for data, dest in ((db_bytes, "::/iPod_Control/iTunes/iTunesDB"),
                               (music_bytes, "::/iPod_Control/Music/F00/fp01.mp3")):
                sfd, src = tempfile.mkstemp(prefix="fatfs-src-")
                os.write(sfd, data)
                os.close(sfd)
                try:
                    m("mcopy", "-i", img, src, dest)
                finally:
                    os.unlink(src)

        dev = BlockDev(img, part_start=0, max_xfer=8)
        fs = Fat32(dev)

        # geometry parsed from the BPB must match what the formatter computed
        assert fs.sectors_per_cluster == geo["sectors_per_cluster"], "spc"
        assert fs.fat_size == geo["fat_size"], "fat size"
        assert fs.reserved == geo["reserved"], "reserved"
        assert fs.data_start == geo["data_start_sector"], "data start"
        assert fs.root_cluster == 2, "root cluster"
        labels = [e for e in fs.parse_dir(fs.read_chain(fs.root_cluster))
                  if e.attr & ATTR_VOLUME_ID]
        assert labels and labels[0].short.startswith("IPOD"), "volume label"
        print("  BPB + geometry + volume label: OK")

        if not have_mtools:
            print("  (mtools absent — skipping cross-validation)")
            print("  fatfs read-path self-test passed (basic)")
            return

        got_db = fs.read_file("iPod_Control/iTunes/iTunesDB")
        assert got_db == db_bytes, "iTunesDB readback mismatch (%s vs %s bytes)" % (
            len(got_db) if got_db else None, len(db_bytes))
        got_music = fs.read_file("iPod_Control/Music/F00/fp01.mp3")
        assert got_music == music_bytes, "music readback mismatch"
        print("  cross-validated file reads vs mtools (2 multi-cluster files): OK")

        roots = [e.name for e in fs.listdir("")]
        assert "iPod_Control" in roots, "long name 'iPod_Control' not read: %r" % roots
        f00 = [e.name for e in fs.listdir("iPod_Control/Music/F00")]
        assert "fp01.mp3" in f00, "fp01.mp3 not listed: %r" % f00
        print("  LFN read ('iPod_Control') + nested listdir: OK")
        print("  fatfs read-path self-test passed")
    finally:
        os.unlink(img)


def _self_test_write():
    """Exercise the WRITE path. We write with our driver; mtools (independent)
    and — when root — the Linux kernel must read it back byte-for-byte."""
    import shutil
    import subprocess
    import tempfile
    from . import fat32

    have_mtools = bool(shutil.which("mcopy"))
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")

    def mtool(*args):
        try:
            r = subprocess.run(args, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            return -1, "(mtools call timed out / failed to run)"
        return r.returncode, r.stdout.decode("latin-1")

    def mcopy_out(img, path):
        fd, tmp = tempfile.mkstemp()
        os.close(fd)
        rc, out = mtool("mcopy", "-i", img, "::" + path, tmp)
        data = open(tmp, "rb").read() if rc == 0 else None
        os.unlink(tmp)
        return data

    total = 300 * 1024 * 1024 // SECTOR        # spc=1 -> 512B clusters
    fd, img = tempfile.mkstemp(prefix="fatfs-wtest-")
    os.close(fd)
    try:
        open(img, "wb").truncate(total * SECTOR)
        with open(img, "r+b") as f:
            fat32.format_fat32(f, 0, total, 1, label="IPOD")

        fs = Fat32(BlockDev(img, 0, 8, writable=True))
        for d in ("iPod_Control", "iPod_Control/iTunes", "iPod_Control/Device",
                  "iPod_Control/Music", "iPod_Control/Music/F00"):
            fs.mkdir(d)
        db = bytes((i * 7 + 3) % 256 for i in range(2048 + 137))
        music = bytes((i * 5 + 1) % 251 for i in range(5000))
        fs.write_file("iPod_Control/iTunes/iTunesDB", db)
        fs.write_file("iPod_Control/Music/F00/fp000123.mp3", music)
        # 40 files in one F## dir -> forces the directory across clusters
        extras = {"fp%06d.mp3" % i: bytes(((i * 13 + j) % 256)
                  for j in range(300 + i)) for i in range(40)}
        for name, data in extras.items():
            fs.write_file("iPod_Control/Music/F00/" + name, data)
        fs.sync()

        # our own reader, fresh handle
        r = Fat32(BlockDev(img, 0, 8))
        assert [e.name for e in r.listdir("iPod_Control")] == \
            ["iTunes", "Device", "Music"], "subdir order/names"
        assert r.read_file("iPod_Control/iTunes/iTunesDB") == db, "DB self readback"
        for name, data in extras.items():
            assert r.read_file("iPod_Control/Music/F00/" + name) == data, name
        print("  write: dirs + LFN + multi-cluster file + 40-file dir: OK (self)")

        # overwrite + remove
        db2 = bytes((i * 11 + 9) % 256 for i in range(9001))
        fs.write_file("iPod_Control/iTunes/iTunesDB", db2)
        fs.remove("iPod_Control/Music/F00/fp000123.mp3")
        fs.sync()
        r = Fat32(BlockDev(img, 0, 8))
        assert r.read_file("iPod_Control/iTunes/iTunesDB") == db2, "overwrite"
        assert r.read_file("iPod_Control/Music/F00/fp000123.mp3") is None, "remove"
        print("  write: overwrite (re-chain) + remove (free + 0xE5): OK (self)")

        is_root = hasattr(os, "geteuid") and os.geteuid() == 0

        # Two independent oracles, split by privilege so we never depend on
        # root's mtools config (which can mangle binary data / hang): mtools
        # when non-root, the Linux kernel's own FAT driver (loop mount) when
        # root. Each reads our writes back; together they cover the write path.
        if is_root:
            print("  (running as root — using the kernel mount as the oracle, "
                  "not mtools)", flush=True)
        elif not have_mtools:
            print("  (mtools absent — skipped independent write cross-check)",
                  flush=True)
        else:
            assert "iPod_Control" in mtool("mdir", "-i", img, "::/")[1], \
                "mtools didn't see LFN iPod_Control"
            assert mcopy_out(img, "/iPod_Control/iTunes/iTunesDB") == db2, \
                "mtools overwrite content"
            for name in ("fp000000.mp3", "fp000039.mp3"):
                assert mcopy_out(img, "/iPod_Control/Music/F00/" + name) == \
                    extras[name], "mtools content " + name
            assert "FP000123" not in mtool("mdir", "-i", img,
                "::/iPod_Control/Music/F00")[1].upper(), "mtools sees removed file"
            print("  write: cross-validated against mtools (LFN, content, remove): OK",
                  flush=True)

        # the ultimate oracle: the Linux kernel's own FAT driver (needs root)
        if is_root and shutil.which("mount"):
            print("  write: kernel-mount cross-check (loop mount)...", flush=True)
            mnt = tempfile.mkdtemp(prefix="fatfs-kmount-")
            try:
                subprocess.run(["mount", "-o", "loop", img, mnt], check=True,
                               timeout=30)
                try:
                    p = os.path.join(mnt, "iPod_Control", "iTunes", "iTunesDB")
                    assert open(p, "rb").read() == db2, "kernel DB content"
                    p = os.path.join(mnt, "iPod_Control", "Music", "F00",
                                     "fp000000.mp3")
                    assert open(p, "rb").read() == extras["fp000000.mp3"], \
                        "kernel music content"
                    nfiles = len(os.listdir(os.path.join(mnt, "iPod_Control",
                                                          "Music", "F00")))
                    assert nfiles == 40, "kernel sees %d files" % nfiles
                    print("  write: Linux kernel mounts it and reads it back: OK")
                finally:
                    subprocess.run(["umount", mnt], check=False, timeout=30)
            except Exception as e:                                   # noqa: BLE001
                print("  write: KERNEL MOUNT FAILED: %s" % e)
                raise
            finally:
                try:
                    os.rmdir(mnt)
                except OSError:
                    pass
        else:
            print("  (not root — skipped kernel-mount cross-check; "
                  "run with sudo for it)")
        print("  fatfs write-path self-test passed")
    finally:
        os.unlink(img)


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
        _self_test_write()
    else:
        print(__doc__)
        print("Run with --self-test to exercise the driver against mtools.")
