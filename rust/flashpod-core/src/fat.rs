//! FAT32, read side: fatfs.BlockDev (sector reads in chunks, with its
//! short-read errors) and the read half of fatfs.Fat32. Errors are strings
//! with the Python messages, because they surface in inspect's output.

use crate::block::BlockDevice;
use crate::text::{casefold, latin1, u16_at, u32_at, utf16le_replace};
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
