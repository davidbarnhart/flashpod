//! Copying firmware off a card (#99): the whole firmware partition, or one
//! image from its directory (osos, aupd, rsrc), with its sha256 and a note
//! saying what it is. Read-only; works on an MBR (FAT32) card or an Apple
//! Partition Map (HFS+) card.

use crate::block::BlockDevice;
use crate::error::{Error, ErrorKind};
use crate::firmware;
use crate::layout::{self, SECTOR};
use serde_json::{json, Value};

/// Where the firmware partition is, and which table said so.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FirmwarePartition {
    pub table: &'static str,
    pub start: u64,
    pub sectors: u64,
}

/// What extract_firmware copied off.
#[derive(Debug, Clone)]
pub struct Extracted {
    pub bytes: Vec<u8>,
    pub sha256: String,
    /// The sidecar note: source table and partition, what was copied, its
    /// sha256 and size, and the installed build as `inspect` names it.
    pub note: Value,
}

fn be32(b: &[u8], off: usize) -> u64 {
    u32::from_be_bytes(b[off..off + 4].try_into().unwrap()) as u64
}

/// The firmware partition: the MBR's type-0 entry, or the Apple Partition
/// Map's Apple_MDFW entry.
pub fn firmware_partition(dev: &mut dyn BlockDevice) -> Result<FirmwarePartition, Error> {
    let head = dev.read_vec(0, 2 * SECTOR as usize)?;
    if let Some(parts) = layout::partitions(&head) {
        return parts.iter().find(|p| p.ptype == 0x00 && p.sectors != 0)
            .map(|p| FirmwarePartition { table: "mbr", start: p.start, sectors: p.sectors })
            .ok_or_else(|| Error::new(ErrorKind::NoFirmware, "no firmware partition (MBR type 0x00)"));
    }
    if head.len() >= 1024 && &head[0..2] == b"ER" && &head[512..514] == b"PM" {
        let count = be32(&head, 512 + 4).min(64);
        for k in 1..=count {
            let e = dev.read_vec(k * SECTOR, SECTOR as usize)?;
            if e.len() < 80 || &e[0..2] != b"PM" {
                break;
            }
            let ptype = &e[48..80];
            if ptype.starts_with(b"Apple_MDFW\0") {
                return Ok(FirmwarePartition { table: "apm", start: be32(&e, 8), sectors: be32(&e, 12) });
            }
        }
        return Err(Error::new(ErrorKind::NoFirmware, "no firmware partition (APM Apple_MDFW)"));
    }
    Err(Error::new(ErrorKind::NotACard, "no partition table (neither an MBR nor an Apple Partition Map)"))
}

/// The firmware partition's bytes, or with `image` one image's payload
/// (devOffset + the format-3 0x200 shift, length from the directory).
pub fn extract_firmware(dev: &mut dyn BlockDevice, image: Option<&str>) -> Result<Extracted, Error> {
    let part = firmware_partition(dev)?;
    let base = part.start * SECTOR;
    let limit = part.sectors * SECTOR;
    let whole = dev.read_vec(base, limit as usize)?;
    if (whole.len() as u64) < limit {
        return Err(Error::new(ErrorKind::Error, format!(
            "the firmware partition runs past the end of the card ({} of {} bytes)", whole.len(), limit)));
    }
    let mut read = |off: i64, n: usize| -> Vec<u8> {
        if off < 0 || off as u64 >= limit {
            return Vec::new();
        }
        crate::text::slice(&whole, off as usize, off as usize + n).to_vec()
    };
    let installed = firmware::describe_installed(&mut read, &firmware::catalog());
    let bytes = match image {
        None => whole.clone(),
        Some(want) => {
            if installed["present"] != json!(true) {
                return Err(Error::new(ErrorKind::NoFirmware, format!(
                    "no firmware to take {} from: {}", want, installed["reason"].as_str().unwrap_or("not present")))
                    .detail("image", want));
            }
            let shift: u64 = if installed["format"] == json!(3) { 0x200 } else { 0 };
            let entry = installed["images"].as_array().into_iter().flatten()
                .find(|i| i["type"].as_str() == Some(want))
                .ok_or_else(|| Error::new(ErrorKind::NoFirmware, format!("no {} image in the firmware directory", want))
                    .detail("image", want))?;
            let (dev_off, len) = (entry["dev_offset"].as_u64().unwrap_or(0), entry["length"].as_u64().unwrap_or(0));
            let start = dev_off + shift;
            if start + len > limit {
                return Err(Error::new(ErrorKind::FirmwareInvalid, format!(
                    "{} runs past the firmware partition ({} + {} > {})", want, start, len, limit)).detail("image", want));
            }
            whole[start as usize..(start + len) as usize].to_vec()
        }
    };
    let sha256 = firmware::sha256_hex(&bytes);
    let note = json!({
        "what": image.unwrap_or("partition"),
        "bytes": bytes.len(),
        "sha256": sha256,
        "partition_table": part.table,
        "partition_start": part.start,
        "partition_sectors": part.sectors,
        "installed": installed,
    });
    Ok(Extracted { bytes, sha256, note })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::block::MemDevice;
    use crate::card::{flash, FlashOptions};

    fn synthetic_fw() -> Vec<u8> {
        // the smallest volume validate_firmware takes: STOP block, ]ih[
        // header, a format-2 directory with one osos whose checksum holds
        let mut fw = vec![0u8; 0x8000];
        fw[0..4].copy_from_slice(b"{{~~");
        fw[0x100..0x104].copy_from_slice(b"]ih[");
        fw[0x104..0x108].copy_from_slice(&0x4000u32.to_le_bytes());
        fw[0x10a..0x10c].copy_from_slice(&2u16.to_le_bytes());
        let payload: Vec<u8> = (0..0x1000u32).map(|i| (i * 7 % 251) as u8).collect();
        fw[0x5000..0x6000].copy_from_slice(&payload);
        let sum: u32 = payload.iter().map(|&b| b as u32).sum();
        for base in [0x4000usize, 0x4200] {
            let e = &mut fw[base..base + 40];
            e[0..4].copy_from_slice(b"!ATA");
            e[4..8].copy_from_slice(b"soso");
            e[12..16].copy_from_slice(&0x5000u32.to_le_bytes());
            e[16..20].copy_from_slice(&0x1000u32.to_le_bytes());
            e[28..32].copy_from_slice(&sum.to_le_bytes());
            e[36..40].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        }
        fw
    }

    fn card(mac: bool) -> MemDevice {
        let mut d = MemDevice { data: vec![0; 300 << 20] };
        let o = FlashOptions { format: true, mac, ..Default::default() };
        flash(&mut d, synthetic_fw(), o).unwrap();
        d
    }

    #[test]
    fn partition_and_osos_off_both_layouts() {
        let fw = synthetic_fw();
        for (mac, table) in [(false, "mbr"), (true, "apm")] {
            let mut d = card(mac);
            let p = extract_firmware(&mut d, None).unwrap();
            assert_eq!(p.note["partition_table"], json!(table));
            assert_eq!(p.bytes.len(), 65536 * 512);
            assert_eq!(&p.bytes[..fw.len()], &fw[..]);
            assert!(p.bytes[fw.len()..].iter().all(|&b| b == 0));
            let o = extract_firmware(&mut d, Some("osos")).unwrap();
            assert_eq!(o.bytes, &fw[0x5000..0x6000]);
            assert_eq!(o.sha256, firmware::sha256_hex(&fw[0x5000..0x6000]));
            assert_eq!(o.note["what"], json!("osos"));
            let e = extract_firmware(&mut d, Some("aupd")).unwrap_err();
            assert_eq!(e.kind, ErrorKind::NoFirmware);
        }
    }

    #[test]
    fn blank_device_is_not_a_card() {
        let mut d = MemDevice { data: vec![0; 1 << 20] };
        assert_eq!(extract_firmware(&mut d, None).unwrap_err().kind, ErrorKind::NotACard);
    }
}
