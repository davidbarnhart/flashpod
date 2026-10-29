//! The card layout: the constants of flashpod/layout.py and the MBR reading
//! of card._partitions / card._layout_facts.

use serde::Serialize;

pub const SECTOR: u64 = 512;
pub const FW_START: u64 = 63;
pub const FW_BLOCKS: u64 = 65536; // 32 MiB firmware partition
pub const DATA_START: u64 = FW_START + FW_BLOCKS; // 65599
pub const LBA28_MAX: u64 = 0x1000_0000; // 2^28 sectors = 128 GiB
pub const TAIL_RESERVE: u64 = 2048; // sectors left unallocated at the end
pub(crate) const FAT_TYPES: [u8; 2] = [0x0b, 0x0c];

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Partition {
    pub index: u32,
    #[serde(rename = "type")]
    pub ptype: u8,
    pub start: u64,
    pub sectors: u64,
}

/// The MBR's entries (card._partitions): None without the 0x55AA signature;
/// an entry counts when any of type, start, length is non-zero.
pub fn partitions(mbr: &[u8]) -> Option<Vec<Partition>> {
    if mbr.len() < 512 || mbr[510] != 0x55 || mbr[511] != 0xaa {
        return None;
    }
    let mut out = Vec::new();
    for (i, off) in [446usize, 462, 478, 494].iter().enumerate() {
        let ptype = mbr[off + 4];
        let start = u32::from_le_bytes(mbr[off + 8..off + 12].try_into().unwrap()) as u64;
        let count = u32::from_le_bytes(mbr[off + 12..off + 16].try_into().unwrap()) as u64;
        if ptype != 0 || start != 0 || count != 0 {
            out.push(Partition { index: i as u32 + 1, ptype, start, sectors: count });
        }
    }
    Some(out)
}

/// (firmware partition, data partition, problems) -- card._layout_facts.
pub fn layout_facts(parts: &[Partition], size: u64) -> (Option<Partition>, Option<Partition>, Vec<String>) {
    let mut problems = Vec::new();
    let fw = parts.iter().find(|p| p.ptype == 0x00 && p.sectors != 0).cloned();
    let data = parts.iter().find(|p| FAT_TYPES.contains(&p.ptype)).cloned();
    match &fw {
        None => problems.push("no firmware partition (type 0x00)".to_string()),
        Some(f) if f.start != FW_START || f.sectors < FW_BLOCKS => problems.push(format!(
            "firmware partition at sector {}, {} sectors (flashpod writes {}, {})",
            f.start, f.sectors, FW_START, FW_BLOCKS
        )),
        _ => {}
    }
    match &data {
        None => problems.push("no FAT32 data partition (type 0x0b/0x0c)".to_string()),
        Some(d) => {
            let end = d.start + d.sectors;
            let total = size / SECTOR;
            if end > total {
                problems.push("data partition runs past the end of the image".to_string());
            } else if total - end < TAIL_RESERVE {
                problems.push(format!(
                    "data partition leaves less than the {}-sector tail reserve (some iPods then demand a sync)",
                    TAIL_RESERVE
                ));
            }
        }
    }
    (fw, data, problems)
}


/// layout.build_mbr_layout: (MBR sector, data start, data sectors);
/// Err when the device is too small for a data partition.
pub fn build_mbr_layout(total: u64, lba48: bool, max_data_sectors: Option<u64>)
    -> Result<([u8; 512], u64, u64), String>
{
    let mut data_end = total as i64 - TAIL_RESERVE as i64;
    if !lba48 {
        data_end = data_end.min(LBA28_MAX as i64);
    }
    if let Some(m) = max_data_sectors.filter(|&m| m > 0) {
        data_end = data_end.min((DATA_START + m) as i64);
    }
    let blocks = data_end - DATA_START as i64;
    if blocks <= 0 {
        return Err("device too small for an iPod data partition".into());
    }
    let mut mbr = [0u8; 512];
    let entry = |mbr: &mut [u8; 512], off: usize, ptype: u8, start: u64, count: u64| {
        mbr[off] = 0;
        mbr[off + 1..off + 4].copy_from_slice(b"\xfe\xff\xff");
        mbr[off + 4] = ptype;
        mbr[off + 5..off + 8].copy_from_slice(b"\xfe\xff\xff");
        mbr[off + 8..off + 12].copy_from_slice(&(start as u32).to_le_bytes());
        mbr[off + 12..off + 16].copy_from_slice(&(count as u32).to_le_bytes());
    };
    entry(&mut mbr, 446, 0x00, FW_START, FW_BLOCKS);
    entry(&mut mbr, 462, 0x0b, DATA_START, blocks as u64);
    mbr[510] = 0x55;
    mbr[511] = 0xaa;
    Ok((mbr, DATA_START, blocks as u64))
}

/// layout.fat_sectors_per_cluster.
pub fn fat_sectors_per_cluster(data_blocks: u64) -> u64 {
    let mut spc = 32;
    while spc > 1 && data_blocks / spc < 65600 {
        spc /= 2;
    }
    spc
}

/// layout.write_layout_body: zero the lead-in and the last 34 sectors,
/// then the firmware at sector 63.
pub fn write_layout_body(dev: &mut dyn crate::BlockDevice, fw: &[u8], total: u64, data_start: u64)
    -> std::io::Result<()>
{
    crate::fat32fmt::write_zeros(dev, 0, data_start * SECTOR)?;
    let tail = total.saturating_sub(34);
    crate::fat32fmt::write_zeros(dev, tail * SECTOR, 34 * SECTOR)?;
    dev.write_at(FW_START * SECTOR, fw)
}

/// layout.verify_region: Err(VerifyFailed) with its details.
pub fn verify_region(dev: &mut dyn crate::BlockDevice, fw: &[u8]) -> Result<(), crate::Error> {
    use crate::error::{Error, ErrorKind};
    let n = fw.len();
    let mut off = 0usize;
    while off < n {
        let want = (1usize << 20).min(n - off);
        let chunk = dev.read_vec(FW_START * SECTOR + off as u64, want)?;
        if chunk.is_empty() {
            return Err(Error::new(ErrorKind::VerifyFailed, "short read")
                .detail("reason", "short_read").detail("got", off).detail("want", n));
        }
        for (i, &b) in chunk.iter().enumerate() {
            if b != fw[off + i] {
                let bad = off + i;
                return Err(Error::new(ErrorKind::VerifyFailed, "mismatch")
                    .detail("reason", "mismatch").detail("offset", bad)
                    .detail("sector", FW_START + bad as u64 / SECTOR)
                    .detail("card", b).detail("image", fw[bad]));
            }
        }
        off += chunk.len();
    }
    Ok(())
}

/// layout.kept_data_problems: why a flash that keeps the data partition
/// would be unsafe on this card; empty when it is safe.
pub fn kept_data_problems(dev: &mut dyn crate::BlockDevice, total: u64, lba48: bool, max_data_sectors: Option<u64>) -> Vec<String> {
    let (_, data_start, data_blocks) = match build_mbr_layout(total, lba48, max_data_sectors) {
        Ok(l) => l,
        Err(e) => return vec![e],
    };
    let mbr = dev.read_vec(0, SECTOR as usize).unwrap_or_default();
    if mbr.len() < SECTOR as usize || mbr[510..512] != [0x55, 0xaa] {
        return vec!["no partition table, so no data partition to keep".into()];
    }
    let mut data = None;
    for off in [446usize, 462, 478, 494] {
        if FAT_TYPES.contains(&mbr[off + 4]) {
            let u = |o: usize| u32::from_le_bytes([mbr[o], mbr[o + 1], mbr[o + 2], mbr[o + 3]]) as u64;
            data = Some((u(off + 8), u(off + 12)));
            break;
        }
    }
    let (start, len) = match data {
        None => return vec!["no FAT32 data partition (type 0x0b/0x0c) to keep".into()],
        Some(d) => d,
    };
    let mut problems = Vec::new();
    if (start, len) != (data_start, data_blocks) {
        problems.push(format!(
            "the data partition is at sectors {}..{}, but this flash would put it at {}..{}",
            start, (start + len) as i64 - 1, data_start, data_start + data_blocks - 1
        ));
    }
    let boot = dev.read_vec(start * SECTOR, SECTOR as usize).unwrap_or_default();
    if boot.len() < SECTOR as usize || boot[510..512] != [0x55, 0xaa] || &boot[82..87] != b"FAT32" {
        problems.push(format!("no FAT32 filesystem at sector {}", start));
    }
    problems
}
