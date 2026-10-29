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

/// The Mac layout (openpod tools/ipod_flash.py build_partition_layout, read
/// off a real 5 GB Mac iPod): block 0 a driverless Driver Descriptor Record,
/// blocks 1..3 the three Apple Partition Map entries ("partition map",
/// "firmware" Apple_MDFW at 63, "disk" Apple_HFS after it), blocks 4..62
/// zero. Every boot/status field is zero, as on the real iPod. The data
/// partition ends where build_mbr_layout's does (TAIL_RESERVE, LBA28), which
/// openpod's does not. Returns (blocks 0..62, data_start, data_blocks).
pub fn build_apm_layout(total: u64, lba48: bool, max_data_sectors: Option<u64>)
    -> Result<(Vec<u8>, u64, u64), String>
{
    let (_, data_start, blocks) = build_mbr_layout(total, lba48, max_data_sectors)?;
    let mut hdr = vec![0u8; (FW_START * SECTOR) as usize];
    hdr[0..2].copy_from_slice(b"ER");
    hdr[2..4].copy_from_slice(&(SECTOR as u16).to_be_bytes());
    hdr[4..8].copy_from_slice(&(total as u32).to_be_bytes());
    let entries: [(u64, u64, &str, &str); 3] = [
        (1, 62, "partition map", "Apple_partition_map"),
        (FW_START, FW_BLOCKS, "firmware", "Apple_MDFW"),
        (data_start, blocks, "disk", "Apple_HFS"),
    ];
    for (i, (start, count, name, ptype)) in entries.iter().enumerate() {
        let e = &mut hdr[(i + 1) * SECTOR as usize..(i + 2) * SECTOR as usize];
        e[0..2].copy_from_slice(b"PM");
        e[4..8].copy_from_slice(&3u32.to_be_bytes());
        e[8..12].copy_from_slice(&(*start as u32).to_be_bytes());
        e[12..16].copy_from_slice(&(*count as u32).to_be_bytes());
        e[16..16 + name.len()].copy_from_slice(name.as_bytes());
        e[48..48 + ptype.len()].copy_from_slice(ptype.as_bytes());
    }
    Ok((hdr, data_start, blocks))
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

#[cfg(test)]
mod apm_tests {
    use super::*;

    #[test]
    fn apm_header_matches_openpod_but_for_the_tail() {
        // openpod tools/ipod_flash.py build_partition_layout(3906250), with
        // the data count it gives (3906250 - 65599) cut by TAIL_RESERVE
        let (hdr, start, n) = build_apm_layout(3_906_250, false, None).unwrap();
        assert_eq!((start, n), (65599, 3_906_250 - 65599 - TAIL_RESERVE));
        assert_eq!(hdr.len(), 63 * 512);
        assert_eq!(&hdr[0..8], b"ER\x02\x00\x00\x3b\x9a\xca");
        let e = |k: usize| &hdr[k * 512..(k + 1) * 512];
        assert_eq!(&e(1)[0..16], b"PM\0\0\0\0\0\x03\0\0\0\x01\0\0\0\x3e");
        assert_eq!(&e(1)[16..29], b"partition map");
        assert_eq!(&e(1)[48..67], b"Apple_partition_map");
        assert_eq!(&e(2)[8..16], b"\0\0\0\x3f\0\x01\0\0");
        assert_eq!(&e(2)[48..58], b"Apple_MDFW");
        assert_eq!(&e(3)[8..12], &65599u32.to_be_bytes());
        assert_eq!(&e(3)[12..16], &(n as u32).to_be_bytes());
        assert_eq!(&e(3)[16..20], b"disk");
        assert_eq!(&e(3)[48..57], b"Apple_HFS");
        assert!(hdr[4 * 512..].iter().all(|&b| b == 0));
    }
}
