//! The card layout: the constants of flashpod/layout.py and the MBR reading
//! of card._partitions / card._layout_facts.

use serde::Serialize;

pub const SECTOR: u64 = 512;
pub const FW_START: u64 = 63;
pub const FW_BLOCKS: u64 = 65536; // 32 MiB firmware partition
pub const DATA_START: u64 = FW_START + FW_BLOCKS; // 65599
pub const LBA28_MAX: u64 = 0x1000_0000; // 2^28 sectors = 128 GiB
pub const TAIL_RESERVE: u64 = 2048; // sectors left unallocated at the end
pub const FAT_TYPES: [u8; 2] = [0x0b, 0x0c];

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
