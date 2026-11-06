//! What the 0.x `flash` left behind for the 1.0 drive commands (#115):
//! sizes, error text, firmware loading, the arm warning.

use flashpod_core::firmware;

pub const ARM_QUESTION: &str = "Arm this card for an iPod update to disk-mode during the next boot? [y/N] ";
pub const ARM_WARNING: &str = "To perform the disk-mode update to your iPod, the iPod must be plugged into FireWire and receiving power during its next boot. Halting the disk-mode update before it completes can leave the iPod in a permanently broken state.";

/// An exit with a message on stderr (Python's `sys.exit(msg)`): rc 1.
pub struct Exit(pub String);

/// ipod_flash.fmt_size.
pub fn fmt_size(n: u64) -> String {
    let mut n = n as f64;
    for u in ["B", "KiB", "MiB", "GiB", "TiB"] {
        if n < 1024.0 || u == "TiB" {
            return format!("{:.1} {}", n, u).replace(".0 ", " ");
        }
        n /= 1024.0;
    }
    unreachable!()
}

/// Python's OSError text for a failed open: "[Errno N] message: 'path'".
pub fn os_error_text(e: &std::io::Error, path: &str) -> String {
    match e.raw_os_error() {
        Some(code) => {
            let msg = e.to_string();
            let msg = msg.split(" (os error").next().unwrap_or(&msg).to_string();
            format!("[Errno {}] {}: '{}'", code, msg, path)
        }
        None => e.to_string(),
    }
}

/// ipod_flash.load_firmware(path).
pub fn load_firmware(path: &str) -> Result<Vec<u8>, String> {
    let data = std::fs::read(path).map_err(|e| os_error_text(&e, path))?;
    firmware::load_firmware(&data)
}

/// platform.image.is_image: an existing regular file.
pub fn is_image(path: &str) -> bool {
    !path.is_empty() && std::fs::metadata(path).map(|m| m.is_file()).unwrap_or(false)
}

/// What 0.x `flash --self-test` checked: the layout and firmware-fixup
/// checks, no hardware (a test now, #115 decision 3).
#[cfg(test)]
mod tests {
    use super::*;

#[test]
fn self_test() {
    use flashpod_core::layout::{build_mbr_layout, fat_sectors_per_cluster, DATA_START, FW_BLOCKS, FW_START, LBA28_MAX, SECTOR, TAIL_RESERVE};
    let le = |b: &[u8], o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap());
    let total = 9_780_743u64;
    let (mbr, mds, mdb) = build_mbr_layout(total, false, None).unwrap();
    assert!(mds == DATA_START && DATA_START == 65599, "{}", mds);
    assert!(mbr[510..512] == [0x55, 0xaa], "MBR sig");
    assert!(mbr[446 + 4] == 0x00 && mbr[462 + 4] == 0x0b, "MBR partition types");
    assert!(le(&mbr, 446 + 8) as u64 == FW_START, "fw start");
    assert!(le(&mbr, 446 + 12) as u64 == FW_BLOCKS, "fw size");
    assert!(le(&mbr, 462 + 8) as u64 == mds);
    assert!(mds + mdb == total - TAIL_RESERVE, "MBR tail reserve");
    let (_, _, big_db) = build_mbr_layout(LBA28_MAX * 4, false, None).unwrap();
    assert!(DATA_START + big_db == LBA28_MAX, "LBA28 cap");
    let (_, _, lba48_db) = build_mbr_layout(LBA28_MAX * 4, true, None).unwrap();
    assert!(DATA_START + lba48_db == LBA28_MAX * 4 - TAIL_RESERVE, "lba48 uncapped");
    let cap = 5_000_000_000 / SECTOR;
    let (_, _, capped_db) = build_mbr_layout(LBA28_MAX * 4, false, Some(cap)).unwrap();
    assert!(capped_db == cap, "firmware data cap binds");
    let (_, _, capped48) = build_mbr_layout(LBA28_MAX * 4, true, Some(cap)).unwrap();
    assert!(capped48 == cap, "firmware data cap binds under lba48");
    let (_, _, small_db) = build_mbr_layout(2_000_000_000 / SECTOR, false, Some(cap)).unwrap();
    assert!(small_db < cap, "firmware cap does not pad a small card");
    assert!(fat_sectors_per_cluster(246_867_514) == 32, "big card keeps Apple spc");
    assert!(fat_sectors_per_cluster(1_921_531) == 16, "1 GB card shrinks clusters");
    assert!(1_921_531 / 16 >= 65525, "small-card FAT32 is valid");
    assert!(fat_sectors_per_cluster(40000) == 1, "tiny card bottoms out");
    // a synthetic format-2 image: two directory copies, ipsw-style staging
    // loadAddr, byte-sum checksum
    let mut img = vec![0u8; 0x4400 + 8];
    img[0..4].copy_from_slice(b"{{~~");
    img[0x100..0x104].copy_from_slice(b"]ih[");
    img[0x104..0x108].copy_from_slice(&0x4000u32.to_le_bytes());
    img[0x10a..0x10c].copy_from_slice(&2u16.to_le_bytes());
    let payload = b"boot";
    img[0x4400..0x4404].copy_from_slice(payload);
    let sum: u32 = payload.iter().map(|&b| b as u32).sum();
    let put = |img: &mut Vec<u8>, at: usize, words: [u32; 8]| {
        for (i, w) in words.iter().enumerate() {
            img[at + 4 * i..at + 4 * i + 4].copy_from_slice(&w.to_le_bytes());
        }
    };
    for base in [0x4000usize, 0x4200] {
        img[base..base + 4].copy_from_slice(b"!ATA");
        img[base + 4..base + 8].copy_from_slice(b"soso");
        put(&mut img, base + 8, [0, 0x4400, 4, 0x2800_0000, 0, sum, 0x210, 0x048D_0040]);
        img[base + 40..base + 44].copy_from_slice(b"!ATA");
        img[base + 44..base + 48].copy_from_slice(b"dpua");
        put(&mut img, base + 48, [0, 0x4400, 4, 0x2800_0000, 0, sum, 0x210, 0x048D_0040]);
    }
    assert!(firmware::validate_firmware(&img).is_err(), "unfixed ipsw loadAddr accepted");
    let fixed = firmware::fixup_directories(&img);
    firmware::validate_firmware(&fixed).unwrap();
    for base in [0x4000usize, 0x4200] {
        assert!(le(&fixed, base + 36) == 0xFFFF_FFFF, "osos loadAddr fixup");
        assert!(le(&fixed, base + 48) == 1, "aupd id fixup (0 = pending flash update -> boot loop)");
        assert!(le(&fixed, base + 76) == 0x2800_0000, "aupd loadAddr fixup");
    }
    let mut bad = fixed.clone();
    bad[0x4200 + 12..0x4200 + 16].copy_from_slice(&0x4a00u32.to_le_bytes());
    assert!(firmware::validate_firmware(&bad).is_err(), "osos payload not at absolute devOffset accepted");
}
}
