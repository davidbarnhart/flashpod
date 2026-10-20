//! FAT32, read side: fatfs.BlockDev (sector reads in chunks, with its
//! short-read errors) and the read half of fatfs.Fat32. Errors are strings
//! with the Python messages, because they surface in inspect's output.

use crate::block::BlockDevice;
use crate::text::{casefold, latin1, py_repr, u16_at, u32_at, utf16le_replace};
use std::collections::HashSet;

pub const SECTOR: u64 = 512;
pub const ATTR_VOLUME_ID: u8 = 0x08;
pub const ATTR_DIRECTORY: u8 = 0x10;
pub const ATTR_LFN: u8 = 0x0F;
pub const FAT32_EOC_MIN: u32 = 0x0FFF_FFF8;
pub const FAT_MASK: u32 = 0x0FFF_FFFF;
/// target.IMAGE_MAX_XFER: sectors per read on an image.
pub const IMAGE_MAX_XFER: u64 = 2048;

/// Sector reads relative to a partition, chunked like fatfs.BlockDev over a
/// file object (images).
pub struct Sectors<'a> {
    pub dev: &'a mut dyn BlockDevice,
    pub part_start: u64,
    pub max_xfer: u64,
}

impl<'a> Sectors<'a> {
    pub fn read(&mut self, lba: u64, count: u64) -> Result<Vec<u8>, String> {
        let mut out = Vec::with_capacity((count * SECTOR) as usize);
        let mut abs = self.part_start.wrapping_add(lba);
        let mut remaining = count;
        while remaining > 0 {
            let n = self.max_xfer.min(remaining);
            let want = (n * SECTOR) as usize;
            let buf = self.dev.read_vec(abs * SECTOR, want).map_err(|e| e.to_string())?;
            if buf.len() != want {
                return Err(format!("short read at LBA {} ({}/{})", abs, buf.len(), want));
            }
            out.extend_from_slice(&buf);
            abs += n;
            remaining -= n;
        }
        Ok(out)
    }
}

impl<'a> Sectors<'a> {
    /// fatfs.BlockDev.write: whole sectors at a partition-relative LBA.
    pub fn write(&mut self, lba: u64, data: &[u8]) -> Result<(), String> {
        if data.len() as u64 % SECTOR != 0 {
            return Err("write length not a multiple of the sector size".into());
        }
        let mut abs = self.part_start.wrapping_add(lba);
        let mut off = 0usize;
        while off < data.len() {
            let n = self.max_xfer.min((data.len() - off) as u64 / SECTOR);
            let nbytes = (n * SECTOR) as usize;
            self.dev.write_at(abs * SECTOR, &data[off..off + nbytes]).map_err(|e| e.to_string())?;
            abs += n;
            off += nbytes;
        }
        Ok(())
    }

    pub fn sync(&mut self) -> Result<(), String> {
        self.dev.flush().map_err(|e| e.to_string())
    }
}

#[derive(Debug, Clone)]
pub struct DirEntry {
    pub name: String,
    pub short: String,
    pub attr: u8,
    pub first_cluster: u32,
    pub size: u32,
}

pub struct Fat32<'a> {
    pub sec: Sectors<'a>,
    pub sectors_per_cluster: u8,
    pub reserved: u16,
    pub num_fats: u8,
    pub fat_size: u32,
    pub root_cluster: u32,
    pub total_sectors: u32,
    pub fsinfo_sector: u16,
    pub data_start: u64,
    pub clusters: u64,
    fat_cache: (u64, Vec<u8>),
    next_free: u64,
    /// None: not loaded yet; Some(-1): unknown (FSInfo has none).
    free_count: Option<i64>,
}

impl<'a> Fat32<'a> {
    pub fn open(mut sec: Sectors<'a>) -> Result<Self, String> {
        let bs = sec.read(0, 1)?;
        if u16_at(&bs, 11)? as u64 != SECTOR {
            return Err("not a 512-byte-sector FAT volume".into());
        }
        let spc = bs[13];
        let reserved = u16_at(&bs, 14)?;
        let num_fats = bs[16];
        let fat_size = u32_at(&bs, 36)?;
        let root_cluster = u32_at(&bs, 44)?;
        let total_sectors = u32_at(&bs, 32)?;
        let fsinfo_sector = u16_at(&bs, 48)?;
        if fat_size == 0 || num_fats == 0 {
            return Err("not a FAT32 volume (zero FAT size / count)".into());
        }
        if spc == 0 {
            // Python divides by zero here (ZeroDivisionError, not caught)
            return Err("integer division or modulo by zero".into());
        }
        let data_start = reserved as u64 + num_fats as u64 * fat_size as u64;
        let clusters = (total_sectors as i64 - data_start as i64).div_euclid(spc as i64);
        Ok(Fat32 {
            sec,
            sectors_per_cluster: spc,
            reserved,
            num_fats,
            fat_size,
            root_cluster,
            total_sectors,
            fsinfo_sector,
            data_start,
            clusters: clusters.max(0) as u64,
            fat_cache: (u64::MAX, Vec::new()),
            next_free: 2,
            free_count: None,
        })
    }

    fn fat_get(&mut self, cluster: u32) -> Result<u32, String> {
        let byte = cluster as u64 * 4;
        let lba = self.reserved as u64 + byte / SECTOR;
        if lba != self.fat_cache.0 {
            let data = self.sec.read(lba, 1)?;
            self.fat_cache = (lba, data);
        }
        Ok(u32_at(&self.fat_cache.1, (byte % SECTOR) as usize)? & FAT_MASK)
    }

    pub fn chain(&mut self, start: u32) -> Result<Vec<u32>, String> {
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        let mut c = start;
        while (2..FAT32_EOC_MIN).contains(&c) {
            if !seen.insert(c) {
                return Err(format!("FAT chain loops at cluster {}", c));
            }
            out.push(c);
            c = self.fat_get(c)?;
        }
        Ok(out)
    }

    fn cluster_lba(&self, c: u32) -> u64 {
        // Python allows (c - 2) < 0 for c < 2, but chain() never yields those
        self.data_start + (c as u64 - 2) * self.sectors_per_cluster as u64
    }

    /// fatfs.read_chain: the clusters are read as the chain is walked, so a
    /// short read can come before a loop is found -- same order as Python.
    pub fn read_chain(&mut self, start: u32) -> Result<Vec<u8>, String> {
        if start < 2 {
            return Ok(Vec::new());
        }
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        let mut c = start;
        while (2..FAT32_EOC_MIN).contains(&c) {
            if !seen.insert(c) {
                return Err(format!("FAT chain loops at cluster {}", c));
            }
            let lba = self.cluster_lba(c);
            out.extend(self.sec.read(lba, self.sectors_per_cluster as u64)?);
            c = self.fat_get(c)?;
        }
        Ok(out)
    }

    fn short_name(e: &[u8]) -> String {
        let trim = |b: &[u8]| -> Vec<u8> {
            let mut v = b.to_vec();
            while v.last() == Some(&b' ') {
                v.pop();
            }
            v
        };
        let mut base = trim(&e[0..8]);
        let ext = trim(&e[8..11]);
        if base.first() == Some(&0x05) {
            base[0] = 0xe5;
        }
        let mut base = latin1(&base);
        let mut ext = latin1(&ext);
        let nt = e[12];
        if nt & 0x08 != 0 {
            base = base.to_lowercase();
        }
        if nt & 0x10 != 0 {
            ext = ext.to_lowercase();
        }
        if ext.is_empty() { base } else { format!("{}.{}", base, ext) }
    }

    fn decode_lfn(parts: &mut Vec<(u8, Vec<u8>)>) -> String {
        parts.sort_by_key(|p| p.0 & 0x1f); // stable, like Python's sort
        let raw: Vec<u8> = parts.iter().flat_map(|p| p.1.clone()).collect();
        let mut units = Vec::new();
        let mut i = 0;
        while i + 1 < raw.len() {
            let u = &raw[i..i + 2];
            if u == b"\x00\x00" || u == b"\xff\xff" {
                break;
            }
            units.extend_from_slice(u);
            i += 2;
        }
        utf16le_replace(&units)
    }

    pub fn parse_dir(data: &[u8]) -> Vec<DirEntry> {
        let mut out = Vec::new();
        let mut lfn: Vec<(u8, Vec<u8>)> = Vec::new();
        let mut off = 0;
        while off < data.len() {
            let e = &data[off..(off + 32).min(data.len())];
            off += 32;
            if e.len() < 32 || e[0] == 0x00 {
                break;
            }
            if e[0] == 0xE5 {
                lfn.clear();
                continue;
            }
            let attr = e[11];
            if attr == ATTR_LFN {
                let mut chunk = e[1..11].to_vec();
                chunk.extend_from_slice(&e[14..26]);
                chunk.extend_from_slice(&e[28..32]);
                lfn.push((e[0], chunk));
                continue;
            }
            let short = Self::short_name(e);
            let name = if lfn.is_empty() { short.clone() } else { Self::decode_lfn(&mut lfn) };
            lfn.clear();
            let hi = u16::from_le_bytes([e[20], e[21]]) as u32;
            let lo = u16::from_le_bytes([e[26], e[27]]) as u32;
            let size = u32::from_le_bytes(e[28..32].try_into().unwrap());
            out.push(DirEntry { name, short, attr, first_cluster: (hi << 16) | lo, size });
        }
        out
    }

    pub fn read_dir(&mut self, cluster: u32) -> Result<Vec<DirEntry>, String> {
        let data = self.read_chain(cluster)?;
        Ok(Self::parse_dir(&data))
    }

    pub fn resolve(&mut self, path: &str) -> Result<Option<DirEntry>, String> {
        let mut cluster = self.root_cluster;
        let mut found = None;
        for comp in path.replace('\\', "/").split('/').filter(|c| !c.is_empty()) {
            let cf = casefold(comp);
            let mut m = None;
            for e in self.read_dir(cluster)? {
                if e.attr & ATTR_VOLUME_ID != 0 {
                    continue;
                }
                if casefold(&e.name) == cf || casefold(&e.short) == cf {
                    m = Some(e);
                    break;
                }
            }
            match m {
                None => return Ok(None),
                Some(e) => {
                    cluster = e.first_cluster;
                    found = Some(e);
                }
            }
        }
        Ok(found)
    }

    pub fn exists(&mut self, path: &str) -> Result<bool, String> {
        Ok(self.resolve(path)?.is_some())
    }

    pub fn listdir(&mut self, path: &str) -> Result<Option<Vec<DirEntry>>, String> {
        let mut cluster = self.root_cluster;
        if !path.is_empty() {
            match self.resolve(path)? {
                Some(e) if e.attr & ATTR_DIRECTORY != 0 => cluster = e.first_cluster,
                _ => return Ok(None),
            }
        }
        Ok(Some(
            self.read_dir(cluster)?
                .into_iter()
                .filter(|e| e.attr & ATTR_VOLUME_ID == 0 && e.short != "." && e.short != "..")
                .collect(),
        ))
    }

    pub fn read_file(&mut self, path: &str) -> Result<Option<Vec<u8>>, String> {
        match self.resolve(path)? {
            Some(e) if e.attr & ATTR_DIRECTORY == 0 => {
                let mut data = self.read_chain(e.first_cluster)?;
                data.truncate(e.size as usize);
                Ok(Some(data))
            }
            _ => Ok(None),
        }
    }

    /// fatfs.free_bytes: from FSInfo only; None when it carries no count.
    pub fn free_bytes(&mut self) -> Option<u64> {
        if self.fsinfo_sector == 0 {
            return None;
        }
        let sec = self.sec.read(self.fsinfo_sector as u64, 1).ok()?;
        if u32_at(&sec, 0).ok()? != 0x4161_5252 {
            return None;
        }
        let v = u32_at(&sec, 488).ok()?;
        if v == 0xFFFF_FFFF {
            return None;
        }
        Some(v as u64 * self.sectors_per_cluster as u64 * SECTOR)
    }

    /// RawTarget.iter_music_files: (relpath, location) for each file under
    /// iPod_Control/Music/F##, in the Python order.
    pub fn music_files(&mut self) -> Result<Vec<(String, String)>, String> {
        let music = "iPod_Control/Music";
        let mut dirs = self.listdir(music)?.unwrap_or_default();
        dirs.sort_by(|a, b| a.name.cmp(&b.name));
        let mut out = Vec::new();
        for d in dirs {
            if d.attr & ATTR_DIRECTORY == 0 || !d.name.to_uppercase().starts_with('F') {
                continue;
            }
            let mut files = self.listdir(&format!("{}/{}", music, d.name))?.unwrap_or_default();
            files.sort_by(|a, b| a.name.cmp(&b.name));
            for f in files {
                if f.attr & ATTR_DIRECTORY != 0 {
                    continue;
                }
                out.push((
                    format!("{}/{}/{}", music, d.name, f.name),
                    format!(":iPod_Control:Music:{}:{}", d.name, f.name),
                ));
            }
        }
        Ok(out)
    }
}

/// target.find_fat_partition: the FAT partition's start (0 for a bare FAT
/// volume), or None.
pub fn find_fat_partition(boot: &[u8]) -> Option<u64> {
    if boot.len() < 512 {
        return None;
    }
    let sig = boot[510] == 0x55 && boot[511] == 0xaa;
    if &boot[82..85] == b"FAT" && sig {
        return Some(0);
    }
    if !sig {
        return None;
    }
    for off in [446usize, 462, 478, 494] {
        if crate::layout::FAT_TYPES.contains(&boot[off + 4]) {
            let start = u32::from_le_bytes(boot[off + 8..off + 12].try_into().unwrap()) as u64;
            if start != 0 {
                return Some(start);
            }
        }
    }
    None
}

/// Why target.open_image_fat failed: NotACard's message, its reason
/// (no_fat_partition / bad_fat) and the underlying error.
#[derive(Debug, Clone)]
pub struct OpenError {
    pub message: String,
    pub reason: &'static str,
    pub error: String,
}

/// target.open_image_fat: the FAT on a whole-disk image (MBR walked).
pub fn open_image_fat(dev: &mut dyn BlockDevice) -> Result<Fat32<'_>, OpenError> {
    let mut sec = Sectors { dev, part_start: 0, max_xfer: IMAGE_MAX_XFER };
    let boot = sec.read(0, 1).map_err(|e| OpenError {
        message: format!("couldn't read a FAT filesystem: {}", e), reason: "bad_fat", error: e })?;
    let start = find_fat_partition(&boot);
    sec.part_start = start.unwrap_or(0);
    Fat32::open(sec).map_err(|e| OpenError {
        message: format!("couldn't read a FAT filesystem: {}", e),
        reason: if start.is_none() { "no_fat_partition" } else { "bad_fat" },
        error: e,
    })
}

// ---------------------------------------------------------------------------
// The write path: fatfs.Fat32's, decision for decision (allocation order,
// directory slots, short names, long-name entries, FSInfo), so the bytes on
// the card match.
// ---------------------------------------------------------------------------
pub const EOC: u32 = 0x0FFF_FFFF;
pub const FREE: u32 = 0;
const FAT_DATE: u16 = ((2026 - 1980) << 9) | (6 << 5) | 1;
const INVALID_83: &[u8] = b"+,;=[] ";

impl<'a> Fat32<'a> {
    fn bpc(&self) -> u64 {
        self.sectors_per_cluster as u64 * SECTOR
    }

    pub fn fat_set(&mut self, cluster: u32, value: u32) -> Result<(), String> {
        let byte = cluster as u64 * 4;
        let off = (byte % SECTOR) as usize;
        for fi in 0..self.num_fats as u64 {
            let lba = self.reserved as u64 + fi * self.fat_size as u64 + byte / SECTOR;
            let mut sec = self.sec.read(lba, 1)?;
            let old = u32::from_le_bytes(sec[off..off + 4].try_into().unwrap());
            sec[off..off + 4].copy_from_slice(&((old & 0xF000_0000) | (value & FAT_MASK)).to_le_bytes());
            self.sec.write(lba, &sec)?;
        }
        self.fat_cache.0 = u64::MAX;
        Ok(())
    }

    fn load_free_count(&mut self) {
        if self.free_count.is_some() {
            return;
        }
        self.free_count = Some(-1);
        if self.fsinfo_sector != 0 {
            if let Ok(sec) = self.sec.read(self.fsinfo_sector as u64, 1) {
                if u32::from_le_bytes(sec[0..4].try_into().unwrap()) == 0x4161_5252 {
                    let v = u32::from_le_bytes(sec[488..492].try_into().unwrap());
                    if v != 0xFFFF_FFFF {
                        self.free_count = Some(v as i64);
                    }
                }
            }
        }
    }

    fn scan_free_clusters(&mut self, n: usize) -> Result<Vec<u32>, String> {
        let last = self.clusters + 2;
        let spers = SECTOR / 4;
        let mut out: Vec<u32> = Vec::new();
        let scan = |me: &mut Self, lo: u64, hi: u64, out: &mut Vec<u32>| -> Result<(), String> {
            let mut cl = lo;
            while cl < hi && out.len() < n {
                let sec_off = (cl * 4) / SECTOR;
                let base = sec_off * spers;
                let sec = me.sec.read(me.reserved as u64 + sec_off, 1)?;
                for i in (cl - base)..spers {
                    let c = base + i;
                    if c >= hi {
                        break;
                    }
                    let v = u32::from_le_bytes(sec[(i * 4) as usize..(i * 4 + 4) as usize].try_into().unwrap());
                    if v & FAT_MASK == 0 {
                        out.push(c as u32);
                        if out.len() == n {
                            return Ok(());
                        }
                    }
                }
                cl = base + spers;
            }
            Ok(())
        };
        let start = self.next_free.max(2);
        scan(self, start, last, &mut out)?;
        if out.len() < n {
            scan(self, 2, start, &mut out)?;
        }
        if out.len() < n {
            return Err("no free clusters left on the volume".into());
        }
        Ok(out)
    }

    fn apply_fat_updates(&mut self, updates: &[(u32, u32)]) -> Result<(), String> {
        // grouped by FAT sector, each sector read and written once per copy
        let mut by_sector: Vec<(u64, Vec<(usize, u32)>)> = Vec::new();
        for &(cl, val) in updates {
            let so = (cl as u64 * 4) / SECTOR;
            let off = ((cl as u64 * 4) % SECTOR) as usize;
            match by_sector.iter_mut().find(|(s, _)| *s == so) {
                Some((_, v)) => v.push((off, val)),
                None => by_sector.push((so, vec![(off, val)])),
            }
        }
        for fi in 0..self.num_fats as u64 {
            for (so, entries) in &by_sector {
                let lba = self.reserved as u64 + fi * self.fat_size as u64 + so;
                let mut sec = self.sec.read(lba, 1)?;
                for &(off, val) in entries {
                    let old = u32::from_le_bytes(sec[off..off + 4].try_into().unwrap());
                    sec[off..off + 4].copy_from_slice(&((old & 0xF000_0000) | (val & FAT_MASK)).to_le_bytes());
                }
                self.sec.write(lba, &sec)?;
            }
        }
        self.fat_cache.0 = u64::MAX;
        Ok(())
    }

    fn alloc_chain(&mut self, n: usize) -> Result<Vec<u32>, String> {
        if n == 0 {
            return Ok(Vec::new());
        }
        let free = self.scan_free_clusters(n)?;
        let updates: Vec<(u32, u32)> =
            free.iter().enumerate().map(|(i, &cl)| (cl, if i + 1 < n { free[i + 1] } else { EOC })).collect();
        self.apply_fat_updates(&updates)?;
        self.next_free = *free.last().unwrap() as u64 + 1;
        self.load_free_count();
        if let Some(fc) = self.free_count {
            if fc >= 0 {
                self.free_count = Some((fc - n as i64).max(0));
            }
        }
        Ok(free)
    }

    fn alloc_one(&mut self) -> Result<u32, String> {
        Ok(self.alloc_chain(1)?[0])
    }

    fn free_chain(&mut self, start: u32) -> Result<(), String> {
        if start < 2 {
            return Ok(());
        }
        let chain = self.chain(start)?;
        let updates: Vec<(u32, u32)> = chain.iter().map(|&c| (c, FREE)).collect();
        self.apply_fat_updates(&updates)?;
        self.load_free_count();
        if let Some(fc) = self.free_count {
            if fc >= 0 {
                self.free_count = Some(fc + chain.len() as i64);
            }
        }
        if let Some(&m) = chain.iter().min() {
            self.next_free = self.next_free.min(m as u64);
        }
        Ok(())
    }

    fn zero_cluster(&mut self, cl: u32) -> Result<(), String> {
        let lba = self.cluster_lba(cl);
        let z = vec![0u8; self.bpc() as usize];
        self.sec.write(lba, &z)
    }

    fn write_in_cluster(&mut self, cl: u32, offset: u64, data: &[u8]) -> Result<(), String> {
        let base = self.cluster_lba(cl) * SECTOR + offset;
        let first = base / SECTOR;
        let last = (base + data.len() as u64 - 1) / SECTOR;
        let mut buf = self.sec.read(first, last - first + 1)?;
        let inner = (base - first * SECTOR) as usize;
        buf[inner..inner + data.len()].copy_from_slice(data);
        self.sec.write(first, &buf)
    }

    fn short_names_in(&mut self, dir_first: u32) -> Result<HashSet<[u8; 11]>, String> {
        let mut out = HashSet::new();
        for cl in self.chain(dir_first)? {
            let lba = self.cluster_lba(cl);
            let data = self.sec.read(lba, self.sectors_per_cluster as u64)?;
            for e in data.chunks(32) {
                if e[0] == 0x00 {
                    return Ok(out);
                }
                if e[0] == 0xE5 || e[11] == ATTR_LFN {
                    continue;
                }
                out.insert(e[0..11].try_into().unwrap());
            }
        }
        Ok(out)
    }

    fn lfn_checksum(short: &[u8; 11]) -> u8 {
        let mut s: u32 = 0;
        for &c in short {
            s = ((((s & 1) << 7) | (s >> 1)) + c as u32) & 0xFF;
        }
        s as u8
    }

    /// fatfs._plan_name: (short name, VFAT case flags, needs long-name entries).
    fn plan_name(&mut self, name: &str, dir_first: u32) -> Result<([u8; 11], u8, bool), String> {
        let chars: Vec<char> = name.chars().collect();
        let dot = chars.iter().rposition(|&c| c == '.').map(|i| i as isize).unwrap_or(-1);
        let (base, ext): (String, String) = if dot <= 0 {
            (name.to_string(), String::new())
        } else {
            (chars[..dot as usize].iter().collect(), chars[dot as usize + 1..].iter().collect())
        };
        let ascii_ok = chars.iter().all(|&c| (c as u32) < 128 && (c as u32) >= 32 && !INVALID_83.contains(&(c as u8)));
        let mut fits = ascii_ok && base.chars().count() <= 8 && ext.chars().count() <= 3 && !base.contains('.');
        let mut nt = 0u8;
        if fits {
            let (bl, bu) = (base.to_lowercase(), base.to_uppercase());
            if base == bl && base != bu {
                nt |= 0x08;
            } else if base != bu {
                fits = false;
            }
            let (el, eu) = (ext.to_lowercase(), ext.to_uppercase());
            if ext == el && ext != eu {
                nt |= 0x10;
            } else if ext != eu {
                fits = false;
            }
        }
        if fits {
            let s = format!("{:<8}{:<3}", base.to_uppercase(), ext.to_uppercase());
            let mut short: [u8; 11] = s.as_bytes()[..11].try_into().unwrap();
            if short[0] == 0xE5 {
                short[0] = 0x05;
            }
            return Ok((short, nt, false));
        }
        let taken = self.short_names_in(dir_first)?;
        let keep = |c: char| (c as u32) < 128 && !"+,;=[]. ".contains(c);
        let mut clean: String = base.to_uppercase().chars().filter(|&c| keep(c)).collect();
        if clean.is_empty() {
            clean = "FILE".into();
        }
        let cext: String = ext.to_uppercase().chars().filter(|&c| keep(c)).take(3).collect();
        for n in 1..1_000_000u32 {
            let tail = format!("~{}", n);
            let stem: String = clean.chars().take(8 - tail.len()).collect::<String>() + &tail;
            let s = format!("{:<8}{:<3}", stem, cext);
            let short: [u8; 11] = s.as_bytes()[..11].try_into().unwrap();
            if !taken.contains(&short) {
                return Ok((short, 0, true));
            }
        }
        Err(format!("could not generate a unique short name for {:?}", name))
    }

    fn short_entry(short: &[u8; 11], attr: u8, first: u32, size: u32, nt: u8) -> [u8; 32] {
        let mut e = [0u8; 32];
        e[0..11].copy_from_slice(short);
        e[11] = attr;
        e[12] = nt;
        e[16..18].copy_from_slice(&FAT_DATE.to_le_bytes());
        e[18..20].copy_from_slice(&FAT_DATE.to_le_bytes());
        e[20..22].copy_from_slice(&(((first >> 16) & 0xFFFF) as u16).to_le_bytes());
        e[24..26].copy_from_slice(&FAT_DATE.to_le_bytes());
        e[26..28].copy_from_slice(&((first & 0xFFFF) as u16).to_le_bytes());
        e[28..32].copy_from_slice(&size.to_le_bytes());
        e
    }

    fn make_entries(&mut self, name: &str, attr: u8, first: u32, size: u32, dir_first: u32) -> Result<Vec<u8>, String> {
        let (short, nt, needs_lfn) = self.plan_name(name, dir_first)?;
        let mut out = Vec::new();
        if needs_lfn {
            let csum = Self::lfn_checksum(&short);
            let mut units: Vec<[u8; 2]> = name.encode_utf16().map(|u| u.to_le_bytes()).collect();
            units.push([0, 0]);
            while units.len() % 13 != 0 {
                units.push([0xff, 0xff]);
            }
            let nparts = units.len() / 13;
            for seq in (1..=nparts).rev() {
                let chunk = &units[(seq - 1) * 13..seq * 13];
                let mut e = [0u8; 32];
                e[0] = seq as u8 | if seq == nparts { 0x40 } else { 0 };
                e[11] = ATTR_LFN;
                e[13] = csum;
                let flat: Vec<u8> = chunk.iter().flat_map(|u| u.iter().copied()).collect();
                e[1..11].copy_from_slice(&flat[0..10]);
                e[14..26].copy_from_slice(&flat[10..22]);
                e[28..32].copy_from_slice(&flat[22..26]);
                out.extend_from_slice(&e);
            }
        }
        out.extend_from_slice(&Self::short_entry(&short, attr, first, size, nt));
        Ok(out)
    }

    fn place_entries(&mut self, dir_first: u32, blob: &[u8]) -> Result<(), String> {
        let need = blob.len() / 32;
        let slots = (self.bpc() / 32) as usize;
        for cl in self.chain(dir_first)? {
            let lba = self.cluster_lba(cl);
            let data = self.sec.read(lba, self.sectors_per_cluster as u64)?;
            let (mut run, mut run_start) = (0usize, 0usize);
            for i in 0..slots {
                let fb = data[i * 32];
                if fb == 0x00 || fb == 0xE5 {
                    if run == 0 {
                        run_start = i;
                    }
                    run += 1;
                    if run == need {
                        return self.write_in_cluster(cl, run_start as u64 * 32, blob);
                    }
                    if fb == 0x00 {
                        if slots - run_start >= need {
                            return self.write_in_cluster(cl, run_start as u64 * 32, blob);
                        }
                        break;
                    }
                } else {
                    run = 0;
                }
            }
        }
        let last = *self.chain(dir_first)?.last().unwrap();
        let newcl = self.alloc_one()?;
        self.zero_cluster(newcl)?;
        self.fat_set(last, newcl)?;
        self.write_in_cluster(newcl, 0, blob)
    }

    fn flush_fsinfo(&mut self) {
        if self.fsinfo_sector == 0 {
            return;
        }
        let lba = self.fsinfo_sector as u64;
        let mut sec = match self.sec.read(lba, 1) {
            Ok(s) => s,
            Err(_) => return,
        };
        if u32::from_le_bytes(sec[0..4].try_into().unwrap()) != 0x4161_5252 {
            return;
        }
        self.load_free_count();
        let fc = match self.free_count {
            Some(v) if v >= 0 => v as u32,
            _ => 0xFFFF_FFFF,
        };
        sec[488..492].copy_from_slice(&fc.to_le_bytes());
        sec[492..496].copy_from_slice(&((self.next_free & 0xFFFF_FFFF) as u32).to_le_bytes());
        let _ = self.sec.write(lba, &sec);
    }

    pub fn sync(&mut self) -> Result<(), String> {
        self.flush_fsinfo();
        self.sec.sync()
    }

    fn resolve_dir(&mut self, path: &str) -> Result<u32, String> {
        if path.is_empty() {
            return Ok(self.root_cluster);
        }
        match self.resolve(path)? {
            Some(e) if e.attr & ATTR_DIRECTORY != 0 => Ok(e.first_cluster),
            _ => Err(format!("not a directory: {}", py_repr(path))),
        }
    }

    fn split(path: &str) -> (String, String) {
        let parts: Vec<&str> = path.split(['/', '\\']).filter(|p| !p.is_empty()).collect();
        (parts[..parts.len() - 1].join("/"), parts[parts.len() - 1].to_string())
    }

    pub fn mkdir(&mut self, path: &str) -> Result<(), String> {
        if self.exists(path)? {
            return Ok(());
        }
        let (parent, name) = Self::split(path);
        let parent_first = self.resolve_dir(&parent)?;
        let cl = self.alloc_one()?;
        self.zero_cluster(cl)?;
        let ddot = if parent_first == self.root_cluster { 0 } else { parent_first };
        self.write_in_cluster(cl, 0, &Self::short_entry(b".          ", ATTR_DIRECTORY, cl, 0, 0))?;
        self.write_in_cluster(cl, 32, &Self::short_entry(b"..         ", ATTR_DIRECTORY, ddot, 0, 0))?;
        let blob = self.make_entries(&name, ATTR_DIRECTORY, cl, 0, parent_first)?;
        self.place_entries(parent_first, &blob)?;
        self.flush_fsinfo();
        Ok(())
    }

    pub fn write_file(&mut self, path: &str, data: &[u8]) -> Result<(), String> {
        let (parent, name) = Self::split(path);
        let parent_first = self.resolve_dir(&parent)?;
        let bpc = self.bpc() as usize;
        let n = data.len().div_ceil(bpc);
        let chain = self.alloc_chain(n)?;
        for (i, &cl) in chain.iter().enumerate() {
            let start = i * bpc;
            let end = ((i + 1) * bpc).min(data.len());
            let mut chunk = data[start..end].to_vec();
            chunk.resize(bpc, 0);
            let lba = self.cluster_lba(cl);
            self.sec.write(lba, &chunk)?;
        }
        let first = chain.first().copied().unwrap_or(0);
        match self.resolve(path)? {
            Some(e) => {
                if e.attr & ATTR_DIRECTORY != 0 {
                    return Err(format!("is a directory: {}", py_repr(path)));
                }
                self.update_entry(parent_first, &name, first, data.len() as u32)?;
                self.free_chain(e.first_cluster)?;
            }
            None => {
                let blob = self.make_entries(&name, 0x20, first, data.len() as u32, parent_first)?;
                self.place_entries(parent_first, &blob)?;
            }
        }
        self.flush_fsinfo();
        Ok(())
    }

    fn update_entry(&mut self, dir_first: u32, name: &str, first: u32, size: u32) -> Result<(), String> {
        let cf = casefold(name);
        for cl in self.chain(dir_first)? {
            let lba = self.cluster_lba(cl);
            let data = self.sec.read(lba, self.sectors_per_cluster as u64)?;
            let mut lfn: Vec<(u8, Vec<u8>)> = Vec::new();
            for (k, e) in data.chunks(32).enumerate() {
                if e[0] == 0x00 {
                    break;
                }
                if e[0] == 0xE5 {
                    lfn.clear();
                    continue;
                }
                if e[11] == ATTR_LFN {
                    lfn.push((e[0], lfn_chunk(e)));
                    continue;
                }
                let long_n = if lfn.is_empty() { None } else { Some(Self::decode_lfn(&mut lfn)) };
                lfn.clear();
                let short_n = Self::short_name(e);
                if casefold(&short_n) == cf || long_n.as_deref().map(casefold).as_deref() == Some(cf.as_str()) {
                    let mut patch = e.to_vec();
                    patch[20..22].copy_from_slice(&(((first >> 16) & 0xFFFF) as u16).to_le_bytes());
                    patch[26..28].copy_from_slice(&((first & 0xFFFF) as u16).to_le_bytes());
                    patch[28..32].copy_from_slice(&size.to_le_bytes());
                    return self.write_in_cluster(cl, k as u64 * 32, &patch);
                }
            }
        }
        Err(format!("entry vanished while updating: {}", py_repr(name)))
    }

    pub fn remove(&mut self, path: &str) -> Result<(), String> {
        let (parent, name) = Self::split(path);
        let parent_first = self.resolve_dir(&parent)?;
        let cf = casefold(&name);
        for cl in self.chain(parent_first)? {
            let lba = self.cluster_lba(cl);
            let data = self.sec.read(lba, self.sectors_per_cluster as u64)?;
            let mut lfn_slots: Vec<usize> = Vec::new();
            for (k, e) in data.chunks(32).enumerate() {
                let i = k * 32;
                if e[0] == 0x00 {
                    break;
                }
                if e[0] == 0xE5 {
                    lfn_slots.clear();
                    continue;
                }
                if e[11] == ATTR_LFN {
                    lfn_slots.push(i);
                    continue;
                }
                let short_n = Self::short_name(e);
                let long_n = if lfn_slots.is_empty() {
                    None
                } else {
                    let mut parts: Vec<(u8, Vec<u8>)> =
                        lfn_slots.iter().map(|&s| (data[s], lfn_chunk(&data[s..s + 32]))).collect();
                    Some(Self::decode_lfn(&mut parts))
                };
                if casefold(&short_n) == cf || long_n.as_deref().map(casefold).as_deref() == Some(cf.as_str()) {
                    if e[11] & ATTR_DIRECTORY != 0 {
                        return Err(format!("is a directory: {}", py_repr(path)));
                    }
                    let first = ((u16::from_le_bytes([e[20], e[21]]) as u32) << 16) | u16::from_le_bytes([e[26], e[27]]) as u32;
                    let mut slots = lfn_slots.clone();
                    slots.push(i);
                    for s in slots {
                        self.write_in_cluster(cl, s as u64, b"\xe5")?;
                    }
                    self.free_chain(first)?;
                    self.flush_fsinfo();
                    return Ok(());
                }
                lfn_slots.clear();
            }
        }
        Err(format!("no such file: {}", py_repr(path)))
    }
}

fn lfn_chunk(e: &[u8]) -> Vec<u8> {
    let mut c = e[1..11].to_vec();
    c.extend_from_slice(&e[14..26]);
    c.extend_from_slice(&e[28..32]);
    c
}

