//! card.inspect, as far as the port has reached. Fields the port doesn't
//! produce yet are left out (not guessed); scripts/rust_parity.py lists them
//! as pending.

use crate::block::BlockDevice;
use crate::error::Error;
use crate::layout::{self, Partition, LBA28_MAX, SECTOR};
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct CardInfo {
    pub path: String,
    pub size_bytes: u64,
    pub partition_table: Option<&'static str>,
    pub partitions: Vec<Partition>,
    pub layout_ok: bool,
    pub problems: Vec<String>,
    pub past_128gib: bool,
}

pub fn inspect(dev: &mut dyn BlockDevice, path: &str) -> Result<CardInfo, Error> {
    let size = dev.len()?;
    let mut info = CardInfo {
        path: path.to_string(),
        size_bytes: size,
        partition_table: None,
        partitions: Vec::new(),
        layout_ok: false,
        problems: Vec::new(),
        past_128gib: false,
    };
    if size < SECTOR {
        info.problems.push("empty image".to_string());
        return Ok(info);
    }
    let mbr = dev.read_vec(0, SECTOR as usize)?;
    let parts = match layout::partitions(&mbr) {
        None => {
            info.problems.push("no partition table (blank card?)".to_string());
            return Ok(info);
        }
        Some(p) => p,
    };
    info.partition_table = Some("mbr");
    let (_fw, data, problems) = layout::layout_facts(&parts, size);
    info.partitions = parts;
    info.layout_ok = problems.is_empty();
    info.problems = problems;
    if let Some(d) = data {
        info.past_128gib = d.start + d.sectors > LBA28_MAX;
    }
    Ok(info)
}
