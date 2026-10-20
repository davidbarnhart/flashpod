//! The card operations that write: card.min_card_bytes, create_blank,
//! flash, flash_firmware, format_data. Same errors (codes, reasons) and the
//! same result fields as the Python library, and the same bytes on the card.

use crate::block::BlockDevice;
use crate::error::{Error, ErrorKind};
use crate::fat32fmt::{self, FormatError, FAT32_MIN_CLUSTERS};
use crate::firmware;
use crate::layout::{self, DATA_START, FW_BLOCKS, FW_START, SECTOR, TAIL_RESERVE};
use serde::Serialize;

/// card.min_card_bytes.
pub fn min_card_bytes() -> u64 {
    let mut lo = 65600u64;
    loop {
        let g = fat32fmt::geometry(lo, layout::fat_sectors_per_cluster(lo));
        if g.clusters >= FAT32_MIN_CLUSTERS {
            break;
        }
        lo += 64;
    }
    (DATA_START + lo + TAIL_RESERVE) * SECTOR
}

fn too_small(msg: impl Into<String>) -> Error {
    Error::new(ErrorKind::CardTooSmall, msg).detail("minimum", min_card_bytes())
}

/// card.create_blank's checks and sizing on a device the caller made:
/// the size rounded down to whole sectors, CardTooSmall below the minimum.
/// (Refusing an existing path is the file wrapper's job.)
pub fn blank_size(size_bytes: u64) -> Result<u64, Error> {
    let size = size_bytes / SECTOR * SECTOR;
    if size < min_card_bytes() {
        return Err(Error::new(ErrorKind::CardTooSmall,
                              format!("{} bytes is smaller than the {} a card needs", size, min_card_bytes()))
            .detail("size", size).detail("minimum", min_card_bytes()));
    }
    Ok(size)
}

fn check_formattable(data_blocks: u64) -> Result<(), Error> {
    let g = fat32fmt::geometry(data_blocks, layout::fat_sectors_per_cluster(data_blocks));
    if g.clusters < FAT32_MIN_CLUSTERS {
        return Err(too_small(format!("data partition too small for FAT32 ({} clusters)", g.clusters)));
    }
    Ok(())
}

/// card._firmware_bytes on the file's contents (or raw bytes, which the
/// Python library fixes up and validates without unwrapping).
pub fn firmware_bytes(data: &[u8], raw: bool) -> Result<Vec<u8>, Error> {
    let r = if raw {
        let fw = firmware::fixup_directories(data);
        firmware::validate_firmware(&fw).map(|_| fw)
    } else {
        firmware::load_firmware(data)
    };
    r.map_err(|e| Error::new(ErrorKind::FirmwareInvalid, format!("firmware error: {}", e)).detail("reason", e))
}

#[derive(Debug, Serialize)]
pub struct FlashResult {
    pub firmware_bytes: usize,
    pub data_start: u64,
    pub data_sectors: u64,
    pub clusters: Option<u64>,
    pub cluster_bytes: Option<u64>,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct FlashOptions {
    pub lba48: bool,
    pub max_data_gb: Option<f64>,
    pub format: bool,
    pub arm: bool,
}

fn io_err(e: std::io::Error) -> Error {
    Error::from(e)
}

/// card.flash on a device: `fw` is install-ready firmware (firmware_bytes).
pub fn flash(dev: &mut dyn BlockDevice, fw: Vec<u8>, o: FlashOptions) -> Result<FlashResult, Error> {
    let fw = if o.arm {
        firmware::arm_firmware(&fw)
            .map_err(|e| Error::new(ErrorKind::FirmwareInvalid, e).detail("reason", "cannot_arm"))?
    } else {
        fw
    };
    let max_data_sectors = match o.max_data_gb {
        Some(gb) if gb != 0.0 => Some((gb * 1_000_000_000.0 / SECTOR as f64).floor() as u64),
        _ => None,
    };
    let total = dev.len()? / SECTOR;
    let (header, data_start, data_blocks) =
        layout::build_mbr_layout(total, o.lba48, max_data_sectors).map_err(too_small)?;
    if o.format {
        check_formattable(data_blocks)?;
    }
    // layout.write_card: lead-in, tail, firmware, then the format
    layout::write_layout_body(dev, &fw, total, data_start).map_err(io_err)?;
    let mut geo = None;
    if o.format {
        let spc = layout::fat_sectors_per_cluster(data_blocks);
        match fat32fmt::format_fat32(dev, data_start, data_blocks, spc, "IPOD") {
            Ok(g) => geo = Some(g),
            Err(FormatError::Value(m)) => return Err(Error::new(ErrorKind::Error, m)),
            Err(FormatError::Io(e)) => return Err(io_err(e)),
        }
    }
    dev.flush()?;
    layout::verify_region(dev, &fw)?;
    dev.write_at(0, &header)?;
    dev.flush()?;
    Ok(FlashResult {
        firmware_bytes: fw.len(),
        data_start,
        data_sectors: data_blocks,
        clusters: geo.map(|g| g.clusters),
        cluster_bytes: geo.map(|g| g.sectors_per_cluster * SECTOR),
    })
}

fn partitions_or(dev: &mut dyn BlockDevice) -> Result<(Vec<layout::Partition>, u64), Error> {
    let size = dev.len()?;
    let mbr = dev.read_vec(0, SECTOR as usize)?;
    match layout::partitions(&mbr) {
        Some(p) => Ok((p, size)),
        None => Err(Error::new(ErrorKind::Error, "no partition table")),
    }
}

#[derive(Debug, Serialize)]
pub struct FirmwareResult {
    pub firmware_bytes: usize,
}

/// card.flash_firmware: only the firmware partition; LayoutMismatch unless
/// the card has flashpod's layout.
pub fn flash_firmware(dev: &mut dyn BlockDevice, fw: Vec<u8>) -> Result<FirmwareResult, Error> {
    let (parts, size) = partitions_or(dev)
        .map_err(|_| Error::new(ErrorKind::LayoutMismatch, "no partition table").detail("reason", "no_mbr"))?;
    let (fwp, data, problems) = layout::layout_facts(&parts, size);
    let ok = matches!(&fwp, Some(f) if f.start == FW_START && f.sectors == FW_BLOCKS)
        && matches!(&data, Some(d) if d.start >= DATA_START);
    if !ok {
        let joined = if problems.is_empty() { "partitions differ".to_string() } else { problems.join("; ") };
        return Err(Error::new(ErrorKind::LayoutMismatch, format!("not flashpod's layout: {}", joined))
            .detail("reason", "layout").detail("problems", problems));
    }
    fat32fmt::write_zeros(dev, FW_START * SECTOR, FW_BLOCKS * SECTOR)?;
    dev.write_at(FW_START * SECTOR, &fw)?;
    dev.flush()?;
    layout::verify_region(dev, &fw)?;
    dev.flush()?;
    Ok(FirmwareResult { firmware_bytes: fw.len() })
}

#[derive(Debug, Serialize)]
pub struct FormatResult {
    pub clusters: u64,
    pub cluster_bytes: u64,
}

/// card.format_data: a fresh FAT32 on the data partition.
pub fn format_data(dev: &mut dyn BlockDevice) -> Result<FormatResult, Error> {
    let (parts, size) = partitions_or(dev)
        .map_err(|_| Error::new(ErrorKind::NotACard, "no partition table").detail("reason", "no_mbr"))?;
    let (_fw, data, _p) = layout::layout_facts(&parts, size);
    let d = data.ok_or_else(|| {
        Error::new(ErrorKind::NotACard, "no FAT32 data partition").detail("reason", "no_fat_partition")
    })?;
    check_formattable(d.sectors)?;
    let spc = layout::fat_sectors_per_cluster(d.sectors);
    let g = match fat32fmt::format_fat32(dev, d.start, d.sectors, spc, "IPOD") {
        Ok(g) => g,
        Err(FormatError::Value(m)) => return Err(Error::new(ErrorKind::Error, m)),
        Err(FormatError::Io(e)) => return Err(io_err(e)),
    };
    dev.flush()?;
    Ok(FormatResult { clusters: g.clusters, cluster_bytes: spc * SECTOR })
}
