//! The 1.0 drive operations that touch the firmware partition and nothing
//! else (#115): `firmware write`, `bootrom arm`, `bootrom disarm`. Any
//! partition table an iPod uses (an MBR, or Apple's partition map) and any
//! firmware partition the image fits in, so a factory hard drive's layout
//! works as well as flashpod's own.
//!
//! Every write here stays inside the firmware partition. The check that a
//! firmware can address the data partition (`large_disk_problem`) is here
//! too, so a frontend asks it before writing; re-partitioning is the
//! caller's step, never this module's.

use crate::block::BlockDevice;
use crate::error::{Error, ErrorKind};
use crate::fat32fmt;
use crate::firmware;
use crate::layout::{self, Partition, LBA28_MAX, SECTOR};
use serde::Serialize;

/// The partition table on a drive: which kind, every entry, and the
/// firmware and data partitions found in it.
#[derive(Debug, Clone, Serialize)]
pub struct Table {
    /// "mbr" or "apm"
    pub kind: &'static str,
    pub partitions: Vec<Partition>,
    pub firmware: Option<Partition>,
    pub data: Option<Partition>,
}

fn be32(b: &[u8], off: usize) -> u64 {
    u32::from_be_bytes(b[off..off + 4].try_into().unwrap()) as u64
}

/// The drive's partition table, or None when it has neither an MBR nor an
/// Apple Partition Map. MBR: the firmware is the type-0x00 entry, data the
/// FAT32 one. APM: Apple_MDFW and Apple_HFS, reported with the MBR-style
/// ids inspect uses (0x00 firmware, 0xAF HFS, 0xFE the map, 0xFF other).
pub fn read_table(dev: &mut dyn BlockDevice) -> Result<Option<Table>, Error> {
    let head = dev.read_vec(0, 2 * SECTOR as usize)?;
    if let Some(parts) = layout::partitions(&head) {
        let (fw, data, _) = layout::layout_facts(&parts, dev.len()?);
        return Ok(Some(Table { kind: "mbr", partitions: parts, firmware: fw, data }));
    }
    if head.len() >= 1024 && &head[0..2] == b"ER" && &head[512..514] == b"PM" {
        let count = be32(&head, 512 + 4).min(64);
        let mut t = Table { kind: "apm", partitions: Vec::new(), firmware: None, data: None };
        for k in 1..=count {
            let e = dev.read_vec(k * SECTOR, SECTOR as usize)?;
            if e.len() < 80 || &e[0..2] != b"PM" {
                break;
            }
            let ptype = &e[48..80];
            let name = ptype.split(|&b| b == 0).next().unwrap_or(&[]);
            let id = match name {
                b"Apple_MDFW" => 0x00,
                b"Apple_HFS" => 0xAF,
                b"Apple_partition_map" => 0xFE,
                _ => 0xFF,
            };
            let p = Partition { index: k as u32, ptype: id, start: be32(&e, 8), sectors: be32(&e, 12) };
            if id == 0x00 && t.firmware.is_none() {
                t.firmware = Some(p.clone());
            }
            if id == 0xAF && t.data.is_none() {
                t.data = Some(p.clone());
            }
            t.partitions.push(p);
        }
        return Ok(Some(t));
    }
    Ok(None)
}

/// The firmware partition, or the refusal that names what's missing.
fn firmware_partition(dev: &mut dyn BlockDevice) -> Result<(Table, Partition), Error> {
    let t = read_table(dev)?.ok_or_else(|| {
        Error::new(ErrorKind::NotACard, "no partition table (neither an MBR nor an Apple Partition Map)")
            .detail("reason", "no_table")
    })?;
    let fw = t.firmware.clone().ok_or_else(|| {
        let what = if t.kind == "mbr" { "MBR type 0x00" } else { "APM Apple_MDFW" };
        Error::new(ErrorKind::NoFirmware, format!("no firmware partition ({})", what))
            .detail("reason", "no_firmware_partition").detail("table", t.kind)
    })?;
    let size = dev.len()?;
    if (fw.start + fw.sectors) * SECTOR > size {
        return Err(Error::new(ErrorKind::LayoutMismatch, format!(
            "the firmware partition (sectors {}..{}) runs past the end of the drive", fw.start, fw.start + fw.sectors - 1))
            .detail("reason", "past_end"));
    }
    Ok((t, fw))
}

/// The existing layout `firmware write` keeps as it is: the table, its
/// firmware and data partitions, and the differences worth a warning.
#[derive(Debug, Clone, Serialize)]
pub struct KeptLayout {
    pub table: Table,
    pub firmware: Partition,
    pub data: Option<Partition>,
    pub warnings: Vec<String>,
}

impl KeptLayout {
    /// One line naming what was recognised, e.g. "partition table kept as
    /// it is: MBR, firmware 63..65598, data 65599..30318590".
    pub fn recognised(&self) -> String {
        let kind = if self.table.kind == "mbr" { "MBR" } else { "Apple Partition Map" };
        let span = |p: &Partition| format!("{}..{}", p.start, p.start + p.sectors - 1);
        let data = match &self.data {
            Some(d) => format!("data {}", span(d)),
            None => "no data partition".to_string(),
        };
        format!("partition table kept as it is: {}, firmware {}, {}", kind, span(&self.firmware), data)
    }
}

fn layout_refusal(reason: &str, msg: String) -> Error {
    Error::new(ErrorKind::LayoutMismatch, msg).detail("reason", reason)
}

/// The FAT boot sector's own size of its filesystem, in 512-byte sectors,
/// or None when `bs` isn't a FAT boot sector.
fn fat_total_sectors(bs: &[u8]) -> Option<u64> {
    if bs.len() < 512 || bs[510] != 0x55 || bs[511] != 0xaa {
        return None;
    }
    let bps = u16::from_le_bytes([bs[11], bs[12]]) as u64;
    if ![512, 1024, 2048, 4096].contains(&bps) || bs[13] == 0 {
        return None;
    }
    let t16 = u16::from_le_bytes([bs[19], bs[20]]) as u64;
    let total = if t16 != 0 { t16 } else { u32::from_le_bytes(bs[32..36].try_into().unwrap()) as u64 };
    Some(total * bps / SECTOR)
}

/// Is the drive's existing layout one `firmware write` can keep, writing
/// firmware `fw` into its firmware partition and nothing else? A layout
/// that merely differs from flashpod's own (another tool's data length,
/// tail gap, a bigger firmware partition) is kept; one that is wrong is
/// refused, with `reason` in the error's details:
///   no_table, no_firmware_partition, past_end, overlap, fat_larger_than_partition, partition_too_small,
///   and last large_disk (so every other check has passed when a caller
///   sees it and offers to re-partition).
pub fn check_kept_layout(dev: &mut dyn BlockDevice, fw: &[u8]) -> Result<KeptLayout, Error> {
    let (t, fwp) = firmware_partition(dev)?;
    let total = dev.len()? / SECTOR;
    let parts: Vec<&Partition> = t.partitions.iter().filter(|p| p.sectors > 0).collect();
    for p in &parts {
        if p.start + p.sectors > total {
            return Err(layout_refusal("past_end", format!(
                "partition {} (sectors {}..{}) runs past the end of the drive ({} sectors)",
                p.index, p.start, p.start + p.sectors - 1, total)));
        }
    }
    for (i, a) in parts.iter().enumerate() {
        for b in &parts[i + 1..] {
            if a.start < b.start + b.sectors && b.start < a.start + a.sectors {
                return Err(layout_refusal("overlap", format!(
                    "partitions {} (sectors {}..{}) and {} (sectors {}..{}) overlap",
                    a.index, a.start, a.start + a.sectors - 1, b.index, b.start, b.start + b.sectors - 1)));
            }
        }
    }
    let data = t.data.clone();
    let mut warnings = Vec::new();
    if let Some(d) = &data {
        if t.kind == "mbr" {
            let bs = dev.read_vec(d.start * SECTOR, SECTOR as usize)?;
            if let Some(n) = fat_total_sectors(&bs) {
                if n > d.sectors {
                    return Err(layout_refusal("fat_larger_than_partition", format!(
                        "the data partition's FAT filesystem says it is {} sectors, but the partition is only {}",
                        n, d.sectors)));
                }
            }
        }
        let gap = total - (d.start + d.sectors);
        if gap < layout::TAIL_RESERVE {
            warnings.push(format!(
                "the data partition leaves {} sector{} unallocated at the end of the drive; flashpod's own layout \
                 leaves {} (some iPods see a disk slightly smaller than a card reader does)",
                gap, if gap == 1 { "" } else { "s" }, layout::TAIL_RESERVE));
        }
    }
    if fw.len() as u64 > fwp.sectors * SECTOR {
        return Err(layout_refusal("partition_too_small", format!(
            "the firmware ({} bytes) is larger than the drive's firmware partition ({} bytes)",
            fw.len(), fwp.sectors * SECTOR)));
    }
    if let Some(p) = large_disk_problem(&t, fw) {
        let why = if p.support == Some(false) { "can't" } else { "may not be able to" };
        return Err(layout_refusal("large_disk", format!(
            "the data partition ends at sector {}, past 128 GiB, which this firmware {} read", p.data_end_sector, why))
            .detail("support", match p.support { Some(b) => serde_json::json!(b), None => serde_json::Value::Null })
            .detail("data_end_sector", p.data_end_sector));
    }
    Ok(KeptLayout { table: t, firmware: fwp, data, warnings })
}

/// Can firmware `fw` (install-ready bytes) address a data partition past
/// 128 GiB? Some(true) when it carries the large-disks fix, Some(false)
/// for a build flashpod recognizes without it, None when flashpod can't
/// tell (an unrecognized build).
pub fn large_disk_support(fw: &[u8]) -> Option<bool> {
    let d = describe(fw);
    if d["fixes"].as_array().map(|a| a.iter().any(|f| f == "large-disks")).unwrap_or(false) {
        return Some(true);
    }
    match d["build"].as_str() {
        Some("unrecognized") | None => None,
        Some(_) => Some(false),
    }
}

/// describe_installed on firmware bytes in memory.
pub fn describe(fw: &[u8]) -> serde_json::Value {
    let mut read = |off: i64, n: usize| -> Vec<u8> {
        if off < 0 || off as usize >= fw.len() {
            return Vec::new();
        }
        let o = off as usize;
        fw[o..(o + n).min(fw.len())].to_vec()
    };
    firmware::describe_installed(&mut read, &firmware::catalog())
}

/// Why this firmware can't use this drive's data partition, or None when
/// it can (or the data partition ends inside 128 GiB). `support` is
/// large_disk_support's answer.
#[derive(Debug, Clone, Serialize)]
pub struct LargeDiskProblem {
    /// false: the firmware is known to lack the fix; null: can't tell.
    pub support: Option<bool>,
    pub data_end_sector: u64,
    pub limit_sector: u64,
}

pub fn large_disk_problem(table: &Table, fw: &[u8]) -> Option<LargeDiskProblem> {
    let d = table.data.as_ref()?;
    let end = d.start + d.sectors;
    if end <= LBA28_MAX {
        return None;
    }
    let support = large_disk_support(fw);
    if support == Some(true) {
        return None;
    }
    Some(LargeDiskProblem { support, data_end_sector: end, limit_sector: LBA28_MAX })
}

#[derive(Debug, Serialize)]
pub struct WriteResult {
    pub firmware_bytes: usize,
    pub table: &'static str,
    pub partition_start: u64,
    pub partition_sectors: u64,
    /// The firmware it replaced had a disk-mode update armed; the new one
    /// doesn't.
    pub cleared_arm: bool,
}

/// The armed state of the firmware now in partition `p`, from its
/// directories only (a few sectors).
fn armed_now(dev: &mut dyn BlockDevice, p: &Partition) -> Result<Option<bool>, Error> {
    let base = p.start * SECTOR;
    let hdr = dev.read_vec(base, 0x200)?;
    if hdr.len() < 0x108 || &hdr[0x100..0x104] != b"]ih[" {
        return Ok(None);
    }
    let ptr = u32::from_le_bytes(hdr[0x104..0x108].try_into().unwrap()) as u64;
    let want = ptr + 0x200 + 400;
    if want > p.sectors * SECTOR {
        return Ok(None);
    }
    let head = dev.read_vec(base, want as usize)?;
    Ok(firmware::aupd_armed(&head))
}

/// The firmware write itself, given its partition: zero the partition,
/// write, verify. Nothing outside the partition is written.
pub fn write_into(dev: &mut dyn BlockDevice, p: &Partition, fw: &[u8]) -> Result<(), Error> {
    if fw.len() as u64 > p.sectors * SECTOR {
        return Err(Error::new(ErrorKind::LayoutMismatch, format!(
            "the firmware ({} bytes) is larger than the drive's firmware partition ({} bytes)",
            fw.len(), p.sectors * SECTOR)).detail("reason", "partition_too_small"));
    }
    fat32fmt::write_zeros(dev, p.start * SECTOR, p.sectors * SECTOR)?;
    dev.write_at(p.start * SECTOR, fw)?;
    dev.flush()?;
    layout::verify_region_at(dev, p.start, fw)?;
    Ok(())
}

/// `firmware write`: install-ready firmware `fw` (firmware_bytes) into the
/// drive's firmware partition, whatever the table. Refuses firmware
/// flashpod doesn't write (check_writable) and any layout check_kept_layout
/// refuses, including a data partition past what the firmware can read
/// (callers that offer to re-partition ask large_disk_problem first).
pub fn write_firmware(dev: &mut dyn BlockDevice, fw: &[u8]) -> Result<WriteResult, Error> {
    firmware::check_writable(fw)
        .map_err(|e| Error::new(ErrorKind::FirmwareInvalid, e).detail("reason", "unsupported_generation"))?;
    let kept = check_kept_layout(dev, fw)?;
    let (t, p) = (kept.table, kept.firmware);
    let was_armed = armed_now(dev, &p)? == Some(true);
    let now_armed = firmware::aupd_armed(fw) == Some(true);
    write_into(dev, &p, fw)?;
    Ok(WriteResult {
        firmware_bytes: fw.len(),
        table: t.kind,
        partition_start: p.start,
        partition_sectors: p.sectors,
        cleared_arm: was_armed && !now_armed,
    })
}

#[derive(Debug, Serialize)]
pub struct ArmResult {
    /// The disk-mode program's name, when flashpod knows it.
    pub program: Option<&'static str>,
    /// Sectors rewritten (the two directory copies' sectors); 0 when it was
    /// already in the asked-for state.
    pub sectors_written: u64,
    pub armed: bool,
}

/// The firmware in partition `p`, read up to the end of its last image.
fn read_installed(dev: &mut dyn BlockDevice, p: &Partition) -> Result<Vec<u8>, Error> {
    let base = p.start * SECTOR;
    let limit = p.sectors * SECTOR;
    let hdr = dev.read_vec(base, 0x200)?;
    if hdr.len() < 0x10c || &hdr[0..4] != b"{{~~" || &hdr[0x100..0x104] != b"]ih[" {
        return Err(Error::new(ErrorKind::NoFirmware, "no firmware in the firmware partition")
            .detail("reason", "no_firmware"));
    }
    let ptr = u32::from_le_bytes(hdr[0x104..0x108].try_into().unwrap()) as u64;
    let fmtver = u16::from_le_bytes(hdr[0x10a..0x10c].try_into().unwrap());
    let shift = if fmtver == 3 { 0x200 } else { 0 };
    let dir_end = ptr + 0x200 + 400;
    if dir_end > limit {
        return Err(Error::new(ErrorKind::NoFirmware, "the firmware's directory is past the partition")
            .detail("reason", "no_firmware"));
    }
    let head = dev.read_vec(base, dir_end as usize)?;
    let mut end = dir_end;
    for base_dir in [ptr as usize, ptr as usize + 0x200] {
        for (_o, _t, f) in firmware::walk_directory(&head, base_dir) {
            end = end.max(f[1] as u64 + f[2] as u64 + shift);
        }
    }
    let end = end.min(limit);
    Ok(dev.read_vec(base, end as usize)?)
}

/// Write back only the sectors of `new` that differ from `old`, then read
/// them back.
fn write_changed(dev: &mut dyn BlockDevice, p: &Partition, old: &[u8], new: &[u8]) -> Result<u64, Error> {
    let base = p.start * SECTOR;
    let mut n = 0;
    let s = SECTOR as usize;
    let mut changed = Vec::new();
    for (i, (a, b)) in old.chunks(s).zip(new.chunks(s)).enumerate() {
        if a != b {
            dev.write_at(base + (i * s) as u64, b)?;
            changed.push(i);
            n += 1;
        }
    }
    dev.flush()?;
    for i in changed {
        let want = &new[i * s..((i + 1) * s).min(new.len())];
        let got = dev.read_vec(base + (i * s) as u64, want.len())?;
        if got != want {
            return Err(Error::new(ErrorKind::VerifyFailed, "mismatch")
                .detail("reason", "mismatch").detail("sector", p.start + i as u64));
        }
    }
    Ok(n)
}

/// `bootrom arm`: arm the disk-mode update of the firmware already on the
/// drive. Refused for a disk-mode program flashpod doesn't know.
pub fn arm(dev: &mut dyn BlockDevice) -> Result<ArmResult, Error> {
    let (_t, p) = firmware_partition(dev)?;
    let old = read_installed(dev, &p)?;
    let new = firmware::arm_firmware(&old)
        .map_err(|e| Error::new(ErrorKind::FirmwareInvalid, e).detail("reason", "cannot_arm"))?;
    let program = firmware::aupd_of(&old).and_then(|(_, k)| k);
    let n = write_changed(dev, &p, &old, &new)?;
    Ok(ArmResult { program, sectors_written: n, armed: true })
}

/// `bootrom disarm`: undo arm (the aupd id back to 1, "performed").
pub fn disarm(dev: &mut dyn BlockDevice) -> Result<ArmResult, Error> {
    let (_t, p) = firmware_partition(dev)?;
    let old = read_installed(dev, &p)?;
    let new = firmware::disarm_firmware(&old)
        .map_err(|e| Error::new(ErrorKind::FirmwareInvalid, e).detail("reason", "cannot_disarm"))?;
    let program = firmware::aupd_of(&old).and_then(|(_, k)| k);
    let n = write_changed(dev, &p, &old, &new)?;
    Ok(ArmResult { program, sectors_written: n, armed: false })
}

/// Is the firmware on the drive armed? None when it can't be told.
pub fn armed(dev: &mut dyn BlockDevice) -> Result<Option<bool>, Error> {
    let (_t, p) = firmware_partition(dev)?;
    armed_now(dev, &p)
}

/// `drive partition`'s choices.
#[derive(Debug, Clone, Copy, Default)]
pub struct PartitionOptions {
    /// Cap the data partition at 128 GiB (2^28 sectors). Without it the
    /// data partition takes the whole drive.
    pub cap_128g: bool,
    pub max_data_gb: Option<f64>,
    /// An Apple Partition Map and HFS+ (a Mac drive) instead of MBR + FAT32.
    pub mac: bool,
    /// The HFS+ volume's date, seconds since 1904 (the library reads no clock).
    pub hfs_time: u32,
}

#[derive(Debug, Serialize)]
pub struct PartitionResult {
    pub table: &'static str,
    pub firmware_start: u64,
    pub firmware_sectors: u64,
    pub data_start: u64,
    pub data_sectors: u64,
    /// The data partition ends past 128 GiB: only firmware with large-disk
    /// support reads it.
    pub past_128gib: bool,
    pub clusters: Option<u64>,
    pub cluster_bytes: Option<u64>,
}

/// The layout `partition` would write: (header, data start, data sectors).
pub fn plan_partition(total_sectors: u64, o: &PartitionOptions) -> Result<(Vec<u8>, u64, u64), Error> {
    let max_data_sectors = match o.max_data_gb {
        Some(gb) if gb != 0.0 => Some((gb * 1_000_000_000.0 / SECTOR as f64).floor() as u64),
        _ => None,
    };
    let r = if o.mac {
        layout::build_apm_layout(total_sectors, !o.cap_128g, max_data_sectors)
    } else {
        layout::build_mbr_layout(total_sectors, !o.cap_128g, max_data_sectors).map(|(m, s, n)| (m.to_vec(), s, n))
    };
    let (hdr, start, n) = r.map_err(|m| Error::new(ErrorKind::CardTooSmall, m).detail("minimum", crate::card::min_card_bytes()))?;
    if !o.mac {
        let g = fat32fmt::geometry(n, layout::fat_sectors_per_cluster(n));
        if g.clusters < fat32fmt::FAT32_MIN_CLUSTERS {
            return Err(Error::new(ErrorKind::CardTooSmall, format!("data partition too small for FAT32 ({} clusters)", g.clusters))
                .detail("minimum", crate::card::min_card_bytes()));
        }
    }
    Ok((hdr, start, n))
}

/// `drive partition`: a new partition table, an empty firmware partition
/// and an empty data filesystem. ERASES the whole drive: the lead-in
/// (old table, old firmware) and the last 34 sectors are zeroed, the data
/// partition formatted; the table is written last.
pub fn partition(dev: &mut dyn BlockDevice, o: PartitionOptions) -> Result<PartitionResult, Error> {
    let total = dev.len()? / SECTOR;
    let (header, data_start, data_blocks) = plan_partition(total, &o)?;
    layout::write_layout_body(dev, &[], total, data_start)?;
    let mut geo = None;
    if o.mac {
        crate::hfsplus::format_wrapped(dev, data_start, data_blocks, "iPod", o.hfs_time)?;
    } else {
        let spc = layout::fat_sectors_per_cluster(data_blocks);
        match fat32fmt::format_fat32(dev, data_start, data_blocks, spc, "IPOD") {
            Ok(g) => geo = Some((g.clusters, spc * SECTOR)),
            Err(fat32fmt::FormatError::Value(m)) => return Err(Error::new(ErrorKind::Error, m)),
            Err(fat32fmt::FormatError::Io(e)) => return Err(e.into()),
        }
    }
    dev.flush()?;
    dev.write_at(0, &header)?;
    dev.flush()?;
    Ok(PartitionResult {
        table: if o.mac { "apm" } else { "mbr" },
        firmware_start: layout::FW_START,
        firmware_sectors: layout::FW_BLOCKS,
        data_start,
        data_sectors: data_blocks,
        past_128gib: data_start + data_blocks > LBA28_MAX,
        clusters: geo.map(|g| g.0),
        cluster_bytes: geo.map(|g| g.1),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::MemDevice;
    use crate::card::{self, FlashOptions};
    use crate::layout::{FW_BLOCKS, FW_START};

    /// synth_fixtures.firmware_bytes: a format-2 volume in Apple's
    /// pre-install state, osos then (optionally) an aupd.
    fn synth(seed: u32, aupd: Option<&[u8]>) -> Vec<u8> {
        const DEV: usize = 0x5000;
        let mut payload = vec![0u8; 0x32000];
        let mut x: u32 = seed.max(1);
        for i in (0..payload.len()).step_by(4) {
            x = x.wrapping_mul(1103515245).wrapping_add(12345);
            payload[i..i + 4].copy_from_slice(&x.to_le_bytes());
        }
        payload[0x3174c..0x31750].copy_from_slice(&0x0150_8000u32.to_le_bytes());
        let mut fw = vec![0u8; DEV];
        fw[0..4].copy_from_slice(b"{{~~");
        fw[0x100..0x104].copy_from_slice(b"]ih[");
        fw[0x104..0x108].copy_from_slice(&0x4000u32.to_le_bytes());
        fw[0x10a..0x10c].copy_from_slice(&2u16.to_le_bytes());
        let sum = |b: &[u8]| b.iter().map(|&v| v as u64).sum::<u64>() as u32;
        let ck = sum(&payload);
        let plen = payload.len();
        fw.extend_from_slice(&payload);
        let put = |fw: &mut Vec<u8>, at: usize, w: [u32; 8]| {
            for (i, v) in w.iter().enumerate() {
                fw[at + 4 * i..at + 4 * i + 4].copy_from_slice(&v.to_le_bytes());
            }
        };
        for (base, off) in [(0x4000usize, DEV - 0x200), (0x4200, DEV)] {
            fw[base..base + 4].copy_from_slice(b"ksid");
            fw[base + 4..base + 8].copy_from_slice(b"soso");
            put(&mut fw, base + 8, [0, off as u32, plen as u32, 0x2800_0000, 0, ck, 0x10000, 0x048D_0000]);
        }
        if let Some(a) = aupd {
            let a_off = fw.len();
            fw.extend_from_slice(a);
            for (base, off) in [(0x4000usize, a_off - 0x200), (0x4200, a_off)] {
                fw[base + 40..base + 44].copy_from_slice(b"ksid");
                fw[base + 44..base + 48].copy_from_slice(b"dpua");
                put(&mut fw, base + 48, [0, off as u32, a.len() as u32, 0x4000_0000, 0, sum(a), 0x10000, 0x048D_0040]);
            }
        }
        firmware::load_firmware(&fw).unwrap()
    }

    fn card(mac: bool, fw: &[u8]) -> MemDevice {
        let mut d = MemDevice { data: vec![0u8; card::min_card_bytes() as usize + (4 << 20)] };
        card::flash(&mut d, fw.to_vec(), FlashOptions { format: true, mac, ..Default::default() }).unwrap();
        d
    }

    fn outside_equal(a: &[u8], b: &[u8], start: u64, sectors: u64) -> bool {
        let (s, e) = ((start * SECTOR) as usize, ((start + sectors) * SECTOR) as usize);
        a.len() == b.len() && a[..s] == b[..s] && a[e..] == b[e..]
    }

    /// A synthetic card of `total` sectors filled with 0xA5 (so any stray
    /// write shows), an MBR of `entries` (type, start, sectors), and a FAT
    /// boot sector at each (start, its own total sectors) in `fats`.
    fn mbr_card(total: u64, entries: &[(u8, u64, u64)], fats: &[(u64, u64)]) -> MemDevice {
        let mut d = MemDevice { data: vec![0xA5u8; (total * SECTOR) as usize] };
        let m = &mut d.data[..512];
        m.fill(0);
        for (i, &(t, s, n)) in entries.iter().enumerate() {
            let off = 446 + 16 * i;
            m[off + 4] = t;
            m[off + 8..off + 12].copy_from_slice(&(s as u32).to_le_bytes());
            m[off + 12..off + 16].copy_from_slice(&(n as u32).to_le_bytes());
        }
        m[510] = 0x55;
        m[511] = 0xaa;
        for &(start, n) in fats {
            let b = &mut d.data[(start * SECTOR) as usize..((start + 1) * SECTOR) as usize];
            b.fill(0);
            b[0..3].copy_from_slice(&[0xeb, 0x58, 0x90]);
            b[11..13].copy_from_slice(&512u16.to_le_bytes());
            b[13] = 8;
            b[32..36].copy_from_slice(&(n as u32).to_le_bytes());
            b[510] = 0x55;
            b[511] = 0xaa;
        }
        d
    }

    /// A card of `total` sectors whose MBR is flashpod's own formula for a
    /// disk of `made_at` sectors (layout::build_mbr_layout), FAT boot sector
    /// to match.
    fn formula_card(total: u64, made_at: u64) -> MemDevice {
        let (mbr, start, n) = layout::build_mbr_layout(made_at, false, None).unwrap();
        let mut d = mbr_card(total, &[], &[(start, n)]);
        d.data[..512].copy_from_slice(&mbr);
        d
    }

    /// An Apple Partition Map card: map, Apple_MDFW at (fw_start, fw_n),
    /// Apple_HFS at (data_start, data_n).
    fn apm_card(total: u64, fw_start: u64, fw_n: u64, data_start: u64, data_n: u64) -> MemDevice {
        let mut d = MemDevice { data: vec![0xA5u8; (total * SECTOR) as usize] };
        d.data[..4 * SECTOR as usize].fill(0);
        d.data[0..2].copy_from_slice(b"ER");
        d.data[2..4].copy_from_slice(&512u16.to_be_bytes());
        let entries = [(1u64, 3u64, "Apple_partition_map"), (fw_start, fw_n, "Apple_MDFW"), (data_start, data_n, "Apple_HFS")];
        for (i, (s, n, t)) in entries.iter().enumerate() {
            let e = &mut d.data[(i + 1) * 512..(i + 2) * 512];
            e[0..2].copy_from_slice(b"PM");
            e[4..8].copy_from_slice(&3u32.to_be_bytes());
            e[8..12].copy_from_slice(&(*s as u32).to_be_bytes());
            e[12..16].copy_from_slice(&(*n as u32).to_be_bytes());
            e[48..48 + t.len()].copy_from_slice(t.as_bytes());
        }
        d
    }

    /// What `flash --no-format` did before keep-means-keep: write the
    /// firmware, then rewrite the table to flashpod's own formula. Only the
    /// CONTROL uses it, to prove the identity check catches a table write.
    fn old_table_rewrite(d: &mut MemDevice) {
        let total = d.data.len() as u64 / SECTOR;
        if &d.data[0..2] == b"ER" {
            let (hdr, _, _) = layout::build_apm_layout(total, true, None).unwrap();
            d.data[..hdr.len()].copy_from_slice(&hdr);
        } else {
            let (mbr, _, _) = layout::build_mbr_layout(total, true, None).unwrap();
            d.data[..512].copy_from_slice(&mbr);
        }
    }

    /// Write `fw` and report whether everything outside the firmware
    /// partition is byte-identical and the firmware landed in it (with
    /// `rewrite`, the old behaviour: the CONTROL).
    fn kept_identical(mut d: MemDevice, fw: &[u8], rewrite: bool) -> (bool, KeptLayout) {
        let before = d.data.clone();
        let kept = check_kept_layout(&mut d, fw).unwrap();
        let r = write_firmware(&mut d, fw).unwrap();
        assert_eq!((r.partition_start, r.partition_sectors), (kept.firmware.start, kept.firmware.sectors));
        if rewrite {
            old_table_rewrite(&mut d);
        }
        let s = (kept.firmware.start * SECTOR) as usize;
        let landed = d.data[s..s + fw.len()] == fw[..];
        (landed && outside_equal(&before, &d.data, kept.firmware.start, kept.firmware.sectors), kept)
    }

    /// The accepted fixtures: (name, card, is flashpod's own formula).
    fn accepted_fixtures(old: &[u8]) -> Vec<(&'static str, MemDevice, bool)> {
        let t = 65_599 + 20_000 + 2049;
        vec![
            ("flashpod's own (MBR)", card(false, old), true),
            ("flashpod's own (APM)", card(true, old), true),
            // the reported card, scaled: data one sector short of flashpod's formula
            ("off by one", mbr_card(t, &[(0x00, 63, 65_536), (0x0b, 65_599, 20_000)], &[(65_599, 20_000)]), false),
            // Apple's Windows updater style: bigger firmware partition, data to the last sector
            ("updater style", mbr_card(80_325 + 30_000, &[(0x00, 63, 80_262), (0x0b, 80_325, 30_000)], &[(80_325, 30_000)]), false),
            // firmware partition rewritten in place by a bootloader installer; FAT smaller than its partition
            ("patcher touched", mbr_card(120_000, &[(0x00, 63, 65_536), (0x0c, 65_599, 40_000)], &[(65_599, 39_000)]), false),
            // hand fdisk: 1 MiB-aligned data, a gap, a third partition, a big tail
            ("hand fdisk", mbr_card(140_000, &[(0x00, 63, 65_536), (0x0c, 67_584, 40_000), (0x83, 107_584, 20_000)], &[(67_584, 40_000)]), false),
            // the same card, two transports: flashpod's own layout for a disk
            // path that reports N-1 sectors, written through a reader reporting N
            ("flashpod's own at N-1, written at N", formula_card(120_000, 119_999), false),
            // APM with the firmware where the map says, not at 63 (no regression)
            ("APM elsewhere", apm_card(160_000, 128, 70_000, 70_128, 80_000), false),
            // MBR with the firmware partition at 1 MiB (sector 2048): kept, any start
            ("MBR firmware at 2048", mbr_card(160_000, &[(0x00, 2048, 65_536), (0x0b, 67_584, 80_000)], &[(67_584, 80_000)]), false),
        ]
    }

    #[test]
    fn keep_means_keep_on_every_accepted_layout() {
        let (old, new) = (synth(1, Some(&[0x33u8; 0x4000])), synth(7, None));
        for (name, d, _) in accepted_fixtures(&old) {
            let (ok, kept) = kept_identical(d, &new, false);
            assert!(ok, "{}: something outside the firmware partition changed", name);
            eprintln!("{}: {}", name, kept.recognised());
        }
    }

    #[test]
    fn control_the_old_table_rewrite_fails_the_identity_check() {
        // A check that passed on these too would prove nothing. flashpod's
        // own layouts are the one case where the old rewrite was harmless.
        let (old, new) = (synth(1, Some(&[0x33u8; 0x4000])), synth(7, None));
        for (name, d, own) in accepted_fixtures(&old) {
            let (ok, _) = kept_identical(d, &new, true);
            assert_eq!(ok, own, "{}: control", name);
        }
    }

    #[test]
    fn what_is_recognised_and_warned() {
        let fw = synth(7, None);
        let mut d = mbr_card(65_599 + 20_000 + 2049, &[(0x00, 63, 65_536), (0x0b, 65_599, 20_000)], &[(65_599, 20_000)]);
        let k = check_kept_layout(&mut d, &fw).unwrap();
        assert_eq!(k.recognised(), "partition table kept as it is: MBR, firmware 63..65598, data 65599..85598");
        assert!(k.warnings.is_empty());
        let mut d = mbr_card(80_325 + 30_000, &[(0x00, 63, 80_262), (0x0b, 80_325, 30_000)], &[(80_325, 30_000)]);
        let k = check_kept_layout(&mut d, &fw).unwrap();
        assert_eq!(k.warnings.len(), 1);
        assert!(k.warnings[0].contains("leaves 0 sectors"), "{}", k.warnings[0]);
        let mut d = apm_card(160_000, 128, 70_000, 70_128, 80_000);
        let k = check_kept_layout(&mut d, &fw).unwrap();
        assert_eq!(k.recognised(), "partition table kept as it is: Apple Partition Map, firmware 128..70127, data 70128..150127");
    }

    #[test]
    fn every_broken_layout_is_refused_and_untouched() {
        let fw = synth(7, None);
        let cases: Vec<(&str, MemDevice)> = vec![
            ("no_table", MemDevice { data: vec![0u8; 1 << 20] }),
            ("no_firmware_partition", mbr_card(100_000, &[(0x0b, 65_599, 20_000)], &[])),
            ("overlap", mbr_card(100_000, &[(0x00, 63, 65_536), (0x0b, 65_000, 20_000)], &[])),
            ("past_end", mbr_card(80_000, &[(0x00, 63, 65_536), (0x0b, 65_599, 20_000)], &[])),
            ("fat_larger_than_partition", mbr_card(100_000, &[(0x00, 63, 65_536), (0x0b, 65_599, 20_000)], &[(65_599, 20_001)])),
            ("partition_too_small", mbr_card(100_000, &[(0x00, 63, 100), (0x0b, 65_599, 20_000)], &[])),
        ];
        for (reason, mut d) in cases {
            let before = d.data.clone();
            let e = check_kept_layout(&mut d, &fw).unwrap_err();
            assert_eq!(e.details["reason"], reason, "{}", e.message);
            let e = write_firmware(&mut d, &fw).unwrap_err();
            assert_eq!(e.details["reason"], reason);
            assert!(before == d.data, "{}: refused but changed", reason);
        }
    }

    #[test]
    fn the_reported_card_at_full_size() {
        // The real geometry: disk 30,320,640 sectors in a card reader, data
        // 65599..30318590. That is flashpod's own layout for 30,320,639
        // sectors, what the same card reports over the iPod's own disk path.
        let (_, s0, n0) = layout::build_mbr_layout(30_320_639, false, None).unwrap();
        assert_eq!((s0, s0 + n0 - 1), (65_599, 30_318_590));
        // A sparse image file.
        let dir = std::env::temp_dir().join(format!("flashpod-keep-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("card.img");
        let total: u64 = 30_320_640;
        let small = mbr_card(66_000, &[(0x00, 63, 65_536), (0x0b, 65_599, 30_318_590 - 65_599 + 1)], &[(65_599, 30_318_590 - 65_599 + 1)]);
        {
            let f = std::fs::File::create(&path).unwrap();
            f.set_len(total * SECTOR).unwrap();
            use std::os::unix::fs::FileExt;
            f.write_all_at(&small.data[..512], 0).unwrap();
            f.write_all_at(&small.data[(65_599 * SECTOR) as usize..(65_600 * SECTOR) as usize], 65_599 * SECTOR).unwrap();
        }
        let fw = synth(7, None);
        let mut d = crate::block::FileDevice::open(&path, true).unwrap();
        let k = check_kept_layout(&mut d, &fw).unwrap();
        assert_eq!(k.recognised(), "partition table kept as it is: MBR, firmware 63..65598, data 65599..30318590");
        assert!(k.warnings.is_empty(), "a 2049-sector tail needs no warning");
        write_firmware(&mut d, &fw).unwrap();
        drop(d);
        use std::os::unix::fs::FileExt;
        let f = std::fs::File::open(&path).unwrap();
        let mut b = vec![0u8; 512];
        f.read_exact_at(&mut b, 0).unwrap();
        assert_eq!(b, small.data[..512]);
        f.read_exact_at(&mut b, 65_599 * SECTOR).unwrap();
        assert_eq!(b, small.data[(65_599 * SECTOR) as usize..(65_600 * SECTOR) as usize]);
        let mut fwb = vec![0u8; fw.len()];
        f.read_exact_at(&mut fwb, 63 * SECTOR).unwrap();
        assert_eq!(fwb, fw);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn write_touches_only_the_firmware_partition_mbr_and_apm() {
        // the old firmware is longer: its tail must be zeroed
        let (old, new) = (synth(1, Some(&[0x33u8; 0x4000])), synth(7, None));
        assert!(old.len() > new.len());
        for mac in [false, true] {
            let mut d = card(mac, &old);
            let before = d.data.clone();
            let r = write_firmware(&mut d, &new).unwrap();
            assert_eq!(r.table, if mac { "apm" } else { "mbr" });
            assert_eq!((r.partition_start, r.partition_sectors), (FW_START, FW_BLOCKS));
            assert!(outside_equal(&before, &d.data, FW_START, FW_BLOCKS), "mac={}", mac);
            let s = (FW_START * SECTOR) as usize;
            assert_eq!(&d.data[s..s + new.len()], &new[..]);
            assert!(d.data[s + new.len()..s + (FW_BLOCKS * SECTOR) as usize].iter().all(|&b| b == 0));
        }
    }

    #[test]
    fn an_mbr_firmware_partition_at_any_start_is_written_there() {
        // The firmware goes where the card's own table puts it, with no
        // check or warning on the start sector (a 1st-gen iPod boots with
        // the firmware partition at sector 63 and at 2048).
        let fw = synth(3, None);
        for start in [63u64, 64, 2048, 4096] {
            let data_start = start + 80_000;
            let mut d = mbr_card(200_000, &[(0x00, start, 80_000), (0x0b, data_start, 60_000)], &[(data_start, 60_000)]);
            let before = d.data.clone();
            let k = check_kept_layout(&mut d, &fw).unwrap();
            assert!(k.warnings.is_empty(), "start {}: {:?}", start, k.warnings);
            let r = write_firmware(&mut d, &fw).unwrap();
            assert_eq!((r.partition_start, r.partition_sectors), (start, 80_000));
            let s = (start * SECTOR) as usize;
            assert_eq!(&d.data[s..s + fw.len()], &fw[..], "start {}", start);
            assert!(outside_equal(&before, &d.data, start, 80_000), "start {}", start);
        }
    }

    #[test]
    fn refusals_change_nothing() {
        let fw = synth(1, None);
        let mut blank = MemDevice { data: vec![0u8; 1 << 20] };
        let e = write_firmware(&mut blank, &fw).unwrap_err();
        assert_eq!(e.details["reason"], "no_table");
        assert!(blank.data.iter().all(|&b| b == 0));
        // a partition too small for the firmware
        let mut d = MemDevice { data: vec![0u8; 8 << 20] };
        d.data[446 + 4] = 0;
        d.data[446 + 8..446 + 12].copy_from_slice(&63u32.to_le_bytes());
        d.data[446 + 12..446 + 16].copy_from_slice(&100u32.to_le_bytes());
        d.data[510] = 0x55;
        d.data[511] = 0xaa;
        let before = d.data.clone();
        let e = write_firmware(&mut d, &fw).unwrap_err();
        assert_eq!(e.details["reason"], "partition_too_small");
        assert_eq!(before, d.data);
        // an MBR with no type-0 entry
        d.data[446 + 4] = 0x0b;
        let e = write_firmware(&mut d, &fw).unwrap_err();
        assert_eq!(e.details["reason"], "no_firmware_partition");
        assert_eq!(&before[512..], &d.data[512..]);
    }

    #[test]
    fn disarm_and_unknown_arm_touch_only_the_id_words() {
        let fw = synth(1, Some(&[0x11u8; 0x400]));
        let mut d = card(false, &fw);
        assert_eq!(armed(&mut d).unwrap(), Some(false));
        // arm by hand (the program is unknown, so `arm` must refuse)
        let before = d.data.clone();
        let e = arm(&mut d).unwrap_err();
        assert_eq!(e.details["reason"], "cannot_arm");
        assert_eq!(before, d.data);
        let base = (FW_START * SECTOR) as usize;
        for dir in [0x4000usize, 0x4200] {
            d.data[base + dir + 48..base + dir + 52].copy_from_slice(&0u32.to_le_bytes());
        }
        assert_eq!(armed(&mut d).unwrap(), Some(true));
        let before = d.data.clone();
        let r = disarm(&mut d).unwrap();
        assert_eq!(r.sectors_written, 2);
        assert_eq!(armed(&mut d).unwrap(), Some(false));
        let diff: Vec<usize> = (0..d.data.len()).filter(|&i| d.data[i] != before[i]).collect();
        assert_eq!(diff, vec![base + 0x4000 + 48, base + 0x4200 + 48]);
        // disarming a disarmed drive writes nothing
        assert_eq!(disarm(&mut d).unwrap().sectors_written, 0);
    }

    #[test]
    fn write_reports_clearing_an_armed_update() {
        let fw = synth(1, Some(&[0x11u8; 0x400]));
        let mut d = card(false, &fw);
        let base = (FW_START * SECTOR) as usize;
        for dir in [0x4000usize, 0x4200] {
            d.data[base + dir + 48..base + dir + 52].copy_from_slice(&0u32.to_le_bytes());
        }
        assert!(write_firmware(&mut d, &fw).unwrap().cleared_arm);
        assert!(!write_firmware(&mut d, &fw).unwrap().cleared_arm);
    }

    #[test]
    fn partition_then_write_is_the_old_flash_byte_for_byte() {
        let fw = synth(5, Some(&[0x22u8; 0x400]));
        let size = card::min_card_bytes() as usize + (4 << 20);
        for (mac, cap) in [(false, false), (false, true), (true, false)] {
            let mut old = MemDevice { data: vec![0x5Au8; size] };
            card::flash(&mut old, fw.clone(), FlashOptions { format: true, mac, lba48: !cap, hfs_time: 7, ..Default::default() }).unwrap();
            let mut new = MemDevice { data: vec![0x5Au8; size] };
            let r = partition(&mut new, PartitionOptions { cap_128g: cap, mac, hfs_time: 7, ..Default::default() }).unwrap();
            assert!(!r.past_128gib);
            write_firmware(&mut new, &fw).unwrap();
            assert!(old.data == new.data, "mac={} cap={}", mac, cap);
        }
    }

    #[test]
    fn partition_refuses_a_drive_too_small_and_writes_nothing() {
        let mut d = MemDevice { data: vec![0x5Au8; 40 << 20] };
        let e = partition(&mut d, PartitionOptions::default()).unwrap_err();
        assert_eq!(e.kind, ErrorKind::CardTooSmall);
        assert!(d.data.iter().all(|&b| b == 0x5A));
    }

    #[test]
    fn plan_partition_whole_drive_by_default() {
        let total = LBA28_MAX * 2;
        let (_, s, n) = plan_partition(total, &PartitionOptions::default()).unwrap();
        assert_eq!(s + n, total - layout::TAIL_RESERVE);
        let (_, s, n) = plan_partition(total, &PartitionOptions { cap_128g: true, ..Default::default() }).unwrap();
        assert_eq!(s + n, LBA28_MAX);
    }

    #[test]
    fn large_disk_problem_only_past_128_gib() {
        let fw = synth(1, None);
        assert_eq!(large_disk_support(&fw), None, "a synthetic build is unrecognized");
        let p = |start: u64, sectors: u64| Partition { index: 2, ptype: 0x0b, start, sectors };
        let t = |d: Partition| Table { kind: "mbr", partitions: vec![], firmware: None, data: Some(d) };
        assert!(large_disk_problem(&t(p(65599, LBA28_MAX - 65599)), &fw).is_none());
        let pr = large_disk_problem(&t(p(65599, LBA28_MAX)), &fw).unwrap();
        assert_eq!(pr.support, None);
        assert_eq!(pr.data_end_sector, 65599 + LBA28_MAX);
    }
}

/// On real firmware, when FLASHPOD_REAL_FW (a directory of catalog files)
/// is set: Apple's builds lack large-disk support, flashpod 1.6 has it.
#[cfg(test)]
mod real {
    #[test]
    fn real_builds() {
        let Ok(dir) = std::env::var("FLASHPOD_REAL_FW") else { return };
        for (name, want) in [("iPod_1.1.5_2005_02_18.bin.gz", Some(false)), ("iPod_2.2.2_2004_04_23.bin.gz", Some(false)),
                             ("flashpod_1.1.6.bin", Some(true))] {
            let p = std::path::Path::new(&dir).join(name);
            let Ok(data) = std::fs::read(&p) else { continue };
            let fw = crate::firmware::load_firmware(&data).unwrap();
            assert_eq!(super::large_disk_support(&fw), want, "{}", name);
            eprintln!("checked {}", name);
        }
    }
}
