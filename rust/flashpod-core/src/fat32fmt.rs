//! The FAT32 formatter: fat32.py, byte for byte.

use crate::block::BlockDevice;
use std::io;

pub const SECTOR: u64 = 512;
pub const FAT32_MIN_CLUSTERS: u64 = 65525;
pub const FAT32_MAX_CLUSTERS: u64 = 268_435_445;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geometry {
    pub total_sectors: u64,
    pub reserved: u64,
    pub num_fats: u64,
    pub sectors_per_cluster: u64,
    pub fat_size: u64,
    pub clusters: u64,
    pub data_start_sector: u64,
}

/// fat32.fat_size_sectors (fatgen103's conservative formula).
pub fn fat_size_sectors(total: u64, reserved: u64, num_fats: u64, spc: u64) -> u64 {
    let tmp1 = total as i64 - reserved as i64;
    let tmp2 = ((256 * spc + num_fats) / 2) as i64;
    ((tmp1 + tmp2 - 1).div_euclid(tmp2)).max(0) as u64
}

pub fn geometry(total: u64, spc: u64) -> Geometry {
    let (reserved, num_fats) = (32u64, 2u64);
    let fat_size = fat_size_sectors(total, reserved, num_fats, spc);
    let data = total as i64 - reserved as i64 - (num_fats * fat_size) as i64;
    let clusters = if data < 0 { 0 } else { data as u64 / spc };
    Geometry {
        total_sectors: total,
        reserved,
        num_fats,
        sectors_per_cluster: spc,
        fat_size,
        clusters,
        data_start_sector: reserved + num_fats * fat_size,
    }
}

fn label_bytes(label: &str) -> [u8; 11] {
    // encode("ascii", "replace").upper()[:11], space-padded
    let mut raw: Vec<u8> = label.chars().map(|c| if c.is_ascii() { c as u8 } else { b'?' }).collect();
    raw.make_ascii_uppercase();
    raw.truncate(11);
    let mut out = [b' '; 11];
    out[..raw.len()].copy_from_slice(&raw);
    out
}

fn default_volume_id(total: u64, label: &str) -> u32 {
    let mut h = (total & 0xFFFF_FFFF) as u32;
    for b in label_bytes(label) {
        h = h.wrapping_mul(33).wrapping_add(b as u32);
    }
    if h == 0 { 1 } else { h }
}

fn boot_sector(g: &Geometry, label: &str, volume_id: u32, hidden: u32) -> [u8; 512] {
    let mut bs = [0u8; 512];
    bs[0..3].copy_from_slice(b"\xeb\x58\x90");
    bs[3..11].copy_from_slice(b"MSWIN4.1");
    bs[11..13].copy_from_slice(&512u16.to_le_bytes());
    bs[13] = g.sectors_per_cluster as u8;
    bs[14..16].copy_from_slice(&(g.reserved as u16).to_le_bytes());
    bs[16] = g.num_fats as u8;
    bs[21] = 0xF8;
    bs[24..26].copy_from_slice(&63u16.to_le_bytes());
    bs[26..28].copy_from_slice(&255u16.to_le_bytes());
    bs[28..32].copy_from_slice(&hidden.to_le_bytes());
    bs[32..36].copy_from_slice(&(g.total_sectors as u32).to_le_bytes());
    bs[36..40].copy_from_slice(&(g.fat_size as u32).to_le_bytes());
    bs[44..48].copy_from_slice(&2u32.to_le_bytes());
    bs[48..50].copy_from_slice(&1u16.to_le_bytes());
    bs[50..52].copy_from_slice(&6u16.to_le_bytes());
    bs[64] = 0x80;
    bs[66] = 0x29;
    bs[67..71].copy_from_slice(&volume_id.to_le_bytes());
    bs[71..82].copy_from_slice(&label_bytes(label));
    bs[82..90].copy_from_slice(b"FAT32   ");
    bs[510] = 0x55;
    bs[511] = 0xaa;
    bs
}

fn fsinfo_sector(g: &Geometry) -> [u8; 512] {
    let mut fs = [0u8; 512];
    fs[0..4].copy_from_slice(&0x4161_5252u32.to_le_bytes());
    fs[484..488].copy_from_slice(&0x6141_7272u32.to_le_bytes());
    let free: u32 = if g.clusters > 0 { (g.clusters - 1) as u32 } else { 0xFFFF_FFFF };
    let next: u32 = if g.clusters > 1 { 3 } else { 0xFFFF_FFFF };
    fs[488..492].copy_from_slice(&free.to_le_bytes());
    fs[492..496].copy_from_slice(&next.to_le_bytes());
    fs[508..512].copy_from_slice(&0xAA55_0000u32.to_le_bytes());
    fs
}

pub fn write_zeros(dev: &mut dyn BlockDevice, base: u64, length: u64) -> io::Result<()> {
    let chunk = vec![0u8; 1 << 20];
    let mut off = 0;
    while off < length {
        let n = (length - off).min(chunk.len() as u64) as usize;
        dev.write_at(base + off, &chunk[..n])?;
        off += n as u64;
    }
    Ok(())
}

/// Err(ValueError message) when the geometry isn't a valid FAT32; I/O
/// errors as io::Error (Python's OSError).
pub enum FormatError {
    Value(String),
    Io(io::Error),
}

/// fat32.format_fat32 with label "IPOD" defaults (hidden sectors = the
/// partition start, the deterministic volume id).
pub fn format_fat32(dev: &mut dyn BlockDevice, part_start: u64, total: u64, spc: u64, label: &str)
    -> Result<Geometry, FormatError>
{
    let g = geometry(total, spc);
    if g.clusters < FAT32_MIN_CLUSTERS {
        return Err(FormatError::Value(format!(
            "partition too small for FAT32: {} clusters at {}-byte clusters (need >= {}, else firmware reads it as FAT16)",
            g.clusters, spc * SECTOR, FAT32_MIN_CLUSTERS)));
    }
    if g.clusters > FAT32_MAX_CLUSTERS {
        return Err(FormatError::Value(format!(
            "partition too large for FAT32: {} clusters (max {})", g.clusters, FAT32_MAX_CLUSTERS)));
    }
    let vid = default_volume_id(total, label);
    let base = part_start * SECTOR;
    let io = |r: io::Result<()>| r.map_err(FormatError::Io);
    io(write_zeros(dev, base, (g.reserved + g.num_fats * g.fat_size + spc) * SECTOR))?;
    let boot = boot_sector(&g, label, vid, part_start as u32);
    let fsinfo = fsinfo_sector(&g);
    io(dev.write_at(base, &boot))?;
    io(dev.write_at(base + SECTOR, &fsinfo))?;
    io(dev.write_at(base + 6 * SECTOR, &boot))?;
    io(dev.write_at(base + 7 * SECTOR, &fsinfo))?;
    let mut head = Vec::new();
    for v in [0x0FFF_FFF8u32, 0x0FFF_FFFF, 0x0FFF_FFFF] {
        head.extend_from_slice(&v.to_le_bytes());
    }
    let fat1 = base + g.reserved * SECTOR;
    io(dev.write_at(fat1, &head))?;
    io(dev.write_at(fat1 + g.fat_size * SECTOR, &head))?;
    let mut root = [0u8; 32];
    root[0..11].copy_from_slice(&label_bytes(label));
    root[11] = 0x08;
    io(dev.write_at(base + g.data_start_sector * SECTOR, &root))?;
    io(dev.flush())?;
    Ok(g)
}
