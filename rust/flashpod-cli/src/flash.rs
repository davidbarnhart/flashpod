//! `flashpod flash` onto a disk image: ipod_flash.flash with the image
//! platform (platform/image.py), message for message.

use crate::devices::capacity_warning;
use crate::term::{color, input_line, C_DIM, C_GRN, C_RED, C_YEL};
use flashpod_core::block::{BlockDevice, FileDevice};
use flashpod_core::fat32fmt::{self, FormatError};
use flashpod_core::firmware;
use flashpod_core::layout::{self, FW_BLOCKS, FW_START, LBA28_MAX, SECTOR, TAIL_RESERVE};

pub const ARM_QUESTION: &str = "Arm this card for an iPod update to disk-mode during the next boot? [y/N] ";
pub const ARM_WARNING: &str = "To perform the disk-mode update to your iPod, the iPod must be plugged into FireWire and receiving power during its next boot. Halting the disk-mode update before it completes can leave the iPod in a permanently broken state.";

pub struct Opts {
    pub device: Option<String>,
    pub firmware: String,
    pub yes: bool,
    pub dry_run: bool,
    pub no_format: bool,
    pub arm: bool,
    pub lba48: bool,
    pub max_data_gb: Option<f64>,
    /// --format hfs (#104): an Apple Partition Map and a wrapped HFS+ data
    /// volume (a Mac card), not an MBR and FAT32.
    pub hfs: bool,
}

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

/// cli.ask_arm: two yeses on a terminal, for a known disk-mode program only.
pub fn ask_arm(fw_path: &str) -> bool {
    if !crate::term::stdin_isatty() {
        return false;
    }
    let fw = match load_firmware(fw_path) {
        Ok(f) => f,
        Err(_) => return false,
    };
    match firmware::aupd_of(&fw) {
        Some((_, Some(_))) => {}
        _ => return false,
    }
    let confirm = format!("{} Continue? [y/N] ", ARM_WARNING);
    for prompt in [ARM_QUESTION, confirm.as_str()] {
        match input_line(&format!("\n{}", prompt)) {
            None => {
                println!();
                return false;
            }
            Some(ans) => {
                let a = ans.trim().to_lowercase();
                if a != "y" && a != "yes" {
                    return false;
                }
            }
        }
    }
    true
}

/// ipod_flash.device_label.
fn device_label(dev: &str) -> String {
    let d = dev.replace('\\', "/");
    let d = d.trim_end_matches('/');
    d.rsplit('/').next().unwrap_or(d).to_string()
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_arguments)]
fn confirm(dev: &str, total: u64, assume_yes: bool, lba48: bool, max_data_sectors: Option<u64>, do_format: bool, armed: Option<&str>, mounted: &[(String, String)], hfs: bool) -> Result<(), Exit> {
    let (_, data_start, data_blocks) = layout::build_mbr_layout(total, lba48, max_data_sectors).map_err(Exit)?;
    if do_format {
        eprintln!("{}", color(&format!("\n  PLAN \u{2014} this will ERASE {}", dev), C_RED));
        eprintln!("    layout      : {}", if hfs { "Apple Partition Map + HFS+ (a Mac card)" } else { "MBR + FAT32" });
    } else {
        eprintln!("{}", color(&format!("\n  PLAN \u{2014} this will REWRITE the firmware and partition table on {}; the data partition and its music are kept", dev), C_YEL));
        eprintln!("    layout      : MBR (data partition kept, not formatted)");
    }
    eprintln!("    device size : {} ({} sectors)", fmt_size(total * SECTOR), total);
    eprintln!("    firmware     : sectors {}..{}   ({})", FW_START, FW_START + FW_BLOCKS - 1, fmt_size(FW_BLOCKS * SECTOR));
    eprintln!(
        "    {} : sectors {}..{}  type {}  ({})",
        if !do_format { "data (kept) " } else if hfs { "data (HFS+) " } else { "data (FAT32)" },
        data_start,
        data_start + data_blocks - 1,
        if hfs { "Apple_HFS" } else { "0x0B" },
        fmt_size(data_blocks * SECTOR)
    );
    if lba48 {
        if total > LBA28_MAX {
            eprintln!("{}", color(&format!("    \u{26a0} EXPERIMENTAL: --lba48 \u{2014} data partition spans the FULL {}, past the 128 GiB LBA28 limit. This ONLY works on an iPod running an LBA48 patch; an unpatched iPod cannot address the extra sectors and will misread the card.", fmt_size(total * SECTOR)), C_RED));
        }
    } else if let Some(w) = capacity_warning(total) {
        eprintln!("{}", color(&format!("    \u{26a0} WARNING: {} {}", dev, w), C_RED));
        eprintln!("{}", color("    (data capped at the iPod's 128 GiB LBA28 limit; pass --lba48 to use the whole card.)", C_YEL));
    }
    if let Some(m) = max_data_sectors {
        if m != 0 && data_blocks == m && total - TAIL_RESERVE > data_start + m {
            eprintln!("{}", color(&format!("    \u{21b3} data capped at {} (--max-data-gb); the rest of the card is left unused.", fmt_size(m * SECTOR)), C_YEL));
        }
    }
    if let Some(a) = armed {
        eprintln!("{}", color(&format!("    \u{26a0} ARMED for a disk-mode update ({}). {}", a, ARM_WARNING), C_RED));
    }
    if !mounted.is_empty() {
        let m: Vec<String> = mounted.iter().map(|(p, mp)| format!("{}@{}", p, mp)).collect();
        eprintln!("{}", color(&format!("    mounted now : {}", m.join(", ")), C_YEL));
    }
    if assume_yes {
        return Ok(());
    }
    eprintln!();
    let want = format!("{} {}", if do_format { "ERASE" } else { "REWRITE" }, device_label(dev));
    // At end of input Python dies with an EOFError traceback (rc 1); David
    // chose the plain mismatch message instead (2026-10-25).
    let got = input_line(&color(&format!("  Type \"{}\" to proceed: ", want), C_RED)).unwrap_or_default();
    if got.trim() != want {
        return Err(Exit("Confirmation did not match. Aborted.".into()));
    }
    Ok(())
}

/// The before-eject hook (cli.offer_init_after_flash). An image gets the
/// still-open file and the data partition's start, before the partition
/// table is written; a device gets neither, after the table is re-read.
pub type Hook<'a> = &'a mut dyn FnMut(&str, Option<(&mut dyn BlockDevice, u64)>);

/// platform.image.is_image: an existing regular file.
pub fn is_image(path: &str) -> bool {
    !path.is_empty() && std::fs::metadata(path).map(|m| m.is_file()).unwrap_or(false)
}

/// ipod_flash.flash, with platform.current() decided once from the device
/// argument: an image file, or a Linux block device.
pub fn flash(o: &Opts, hook: Option<Hook>) -> Result<i32, Exit> {
    if o.hfs && o.no_format {
        return Err(Exit(color("--no-format keeps a FAT32 card's music; it does not work with --format hfs yet.", C_RED)));
    }
    let image = o.device.as_deref().map(is_image).unwrap_or(false);
    let mut fw = load_firmware(&o.firmware).map_err(|e| Exit(color(&format!("firmware error ({}): {}", o.firmware, e), C_RED)))?;
    firmware::check_writable(&fw).map_err(|e| Exit(color(&format!("firmware error ({}): {}", o.firmware, e), C_RED)))?;
    let base = o.firmware.rsplit('/').next().unwrap_or(&o.firmware);
    eprintln!("{}", color(&format!("firmware OK: {} ({} bytes, structure validated)", base, fw.len()), C_GRN));
    let mut armed: Option<&'static str> = None;
    if o.arm {
        fw = firmware::arm_firmware(&fw).map_err(|e| Exit(color(&format!("can't arm: {}", e), C_RED)))?;
        armed = firmware::aupd_of(&fw).and_then(|(_, k)| k);
    }
    let max_data_sectors = match o.max_data_gb {
        Some(gb) if gb != 0.0 => Some((gb * 1_000_000_000.0 / SECTOR as f64).floor() as u64),
        _ => None,
    };
    if !image && !crate::platform::current().is_admin() && !o.dry_run {
        return Err(Exit(color("Run as root (sudo) to write to a block device.", C_RED)));
    }
    let dev = match &o.device {
        Some(d) => d.clone(),
        None => crate::platform::current().choose_device()?,
    };
    let dev = dev.as_str();
    let total = if image {
        // ImagePlatform.validate_target / device_sectors
        let size = std::fs::metadata(dev).map(|m| m.len()).unwrap_or(0);
        if size < 512 {
            return Err(Exit(format!("flashpod: {} is empty -- give it the card's size first, e.g. `truncate -s 1G {}`", dev, dev)));
        }
        size / SECTOR
    } else {
        crate::platform::current().validate_target(dev)?;
        crate::platform::current().device_sectors(dev)
    };
    if total == 0 {
        return Err(Exit(color(&format!("could not determine size of {} \u{2014} is a card inserted? An empty slot in a multi-slot reader still appears as a disk, but reports size 0.", dev), C_RED)));
    }
    if layout::build_mbr_layout(total, o.lba48, max_data_sectors).is_err() {
        return Err(Exit(color(&format!("{} is too small for an iPod card: {} leaves no room for a data partition after the {} firmware partition.", dev, fmt_size(total * SECTOR), fmt_size(FW_BLOCKS * SECTOR)), C_RED)));
    }
    let do_format = !o.no_format;
    if !do_format {
        check_kept_data(dev, total, o.dry_run, o.lba48, max_data_sectors)?;
    }
    let mounted = if image { Vec::new() } else { crate::platform::current().device_mountpoints(dev) };
    confirm(dev, total, o.yes || o.dry_run, o.lba48, max_data_sectors, do_format, armed, &mounted, o.hfs)?;
    if !image {
        crate::platform::current().unmount_all(dev, o.dry_run);
    }
    let (header, data_start, data_blocks) = if o.hfs {
        layout::build_apm_layout(total, o.lba48, max_data_sectors).map_err(Exit)?
    } else {
        let (mbr, s, n) = layout::build_mbr_layout(total, o.lba48, max_data_sectors).map_err(Exit)?;
        (mbr.to_vec(), s, n)
    };
    let io = |e: std::io::Error| Exit(e.to_string());
    // write_layout: everything but the MBR, which is committed last
    let plat = crate::platform::current();
    let mut raw: Option<Box<dyn BlockDevice>> = None;
    if o.dry_run {
        if do_format {
            if o.hfs {
                eprintln!("{}", color(&format!("  [dry-run] would wipe signatures, write a {}-byte Apple Partition Map, {}-byte firmware @ sector {}, a wrapped HFS+ volume on data", header.len(), fw.len(), FW_START), C_DIM));
            } else {
                eprintln!("{}", color(&format!("  [dry-run] would wipe signatures, write {}-byte MBR header, {}-byte firmware @ sector {}, pure-Python FAT32 on data", header.len(), fw.len(), FW_START), C_DIM));
            }
        } else {
            eprintln!("{}", color(&format!("  [dry-run] would zero sectors 0..{} and the last 34, write {}-byte firmware @ sector {} and a {}-byte MBR header; the data partition (sectors {}..{}) is left as it is", data_start - 1, fw.len(), FW_START, header.len(), data_start, data_start + data_blocks - 1), C_DIM));
        }
    } else {
        let mut f: Box<dyn BlockDevice> = if image {
            Box::new(FileDevice::open(std::path::Path::new(dev), true).map_err(|e| Exit(os_error_text(&e, dev)))?)
        } else {
            plat.wipe_signatures(dev);
            plat.invalidate_cached_partitions(dev);
            plat.open_raw(dev, true).map_err(|e| Exit(os_error_text(&e, dev)))?
        };
        layout::write_layout_body(f.as_mut(), &fw, total, data_start).map_err(io)?;
        if do_format && o.hfs {
            match flashpod_core::hfsplus::format_wrapped(f.as_mut(), data_start, data_blocks, "iPod", hfs_now()) {
                Ok(g) => eprintln!("{}", color(&format!("  formatted data partition (HFS+ in an HFS wrapper, {} blocks of {}, volume 'iPod')", g.total_blocks, fmt_size(g.block_size)), C_GRN)),
                Err(e) => return Err(Exit(color(&format!("could not write HFS+ to {}: {}", dev, e), C_RED))),
            }
        } else if do_format {
            let spc = layout::fat_sectors_per_cluster(data_blocks);
            match fat32fmt::format_fat32(f.as_mut(), data_start, data_blocks, spc, "IPOD") {
                Ok(g) => {
                    let cb = spc * SECTOR;
                    let size = if cb >= 1024 { format!("{} KiB", cb / 1024) } else { format!("{} bytes", cb) };
                    eprintln!("{}", color(&format!("  formatted data partition (FAT32, {} clusters @ {}, label 'IPOD')", g.clusters, size), C_GRN));
                }
                Err(FormatError::Value(m)) => {
                    eprintln!("{}", color(&format!("  {} - skipping data format; the iPod will likely reject the card.", m), C_YEL));
                }
                Err(FormatError::Io(e)) => {
                    eprintln!("{}", color(&format!("  could not write FAT32 to {}: {} - skipping data format.", dev, e), C_YEL));
                }
            }
        }
        f.flush().map_err(io)?;
        eprintln!("{}", color("  wrote MBR partition table + firmware", C_GRN));
        raw = Some(f);
    }
    verify_firmware(dev, &fw, o.dry_run, image)?;
    let hook = if o.dry_run { None } else { hook };
    let commit = |raw: Option<Box<dyn BlockDevice>>| -> Result<(), Exit> {
        if let Some(mut f) = raw {
            f.write_at(0, &header).map_err(io)?;
            f.flush().map_err(io)?;
        }
        Ok(())
    };
    match hook {
        // Platform.init_before_mbr (images, Windows): the hook first,
        // through the open handle, then the partition table
        Some(h) if image || plat.init_before_mbr() => {
            if let Some(f) = raw.as_mut() {
                h(dev, Some((f.as_mut(), data_start)));
            }
            commit(raw)?;
            if !image {
                plat.reread_partition_table(dev);
            }
        }
        h => {
            commit(raw)?;
            if !image {
                crate::platform::current().reread_partition_table(dev);
            }
            if let Some(h) = h {
                h(dev, None);
            }
        }
    }
    if !image {
        if o.dry_run {
            eprintln!("{}", color(&format!("  flushing + ejecting {}", dev), C_DIM));
        } else {
            crate::platform::current().eject(dev);
        }
    }
    eprintln!("{}", color(&format!("\nDone. {} is ready \u{2014} insert it into the iPod.", dev), C_GRN));
    Ok(0)
}

/// A card image as a file, a device through the platform's raw open.
fn open_read(dev: &str) -> std::io::Result<Box<dyn BlockDevice>> {
    if is_image(dev) {
        Ok(Box::new(FileDevice::open(std::path::Path::new(dev), false)?))
    } else {
        crate::platform::current().open_raw(dev, false)
    }
}

/// The HFS+ volume's dates: now (FLASHPOD_NOW pins it, for the tests), in
/// seconds since 1904.
fn hfs_now() -> u32 {
    let unix = std::env::var("FLASHPOD_NOW").ok().and_then(|v| v.parse::<u64>().ok()).unwrap_or_else(|| {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
    });
    (unix + flashpod_core::hfsplus::MAC_EPOCH) as u32
}

/// ipod_flash.verify_firmware: read the firmware region back through a
/// fresh handle (after flushing the device's buffer cache) and compare.
fn verify_firmware(dev: &str, fw: &[u8], dry: bool, image: bool) -> Result<(), Exit> {
    if dry {
        eprintln!("{}", color("  [dry-run] would verify the firmware region byte-for-byte", C_DIM));
        return Ok(());
    }
    eprintln!("{}", color(&format!("  verifying firmware on the card byte-for-byte ({} bytes @ sector {}) ...", fw.len(), FW_START), C_DIM));
    if !image {
        crate::platform::current().flush_buffers(dev);
    }
    let mut f = open_read(dev).map_err(|e| Exit(os_error_text(&e, dev)))?;
    if let Err(e) = layout::verify_region(f.as_mut(), fw) {
        let d = &e.details;
        let n = |k: &str| d.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
        if d.get("reason").and_then(|v| v.as_str()) == Some("short_read") {
            return Err(Exit(color(&format!("VERIFY FAILED: short read ({} of {} firmware bytes) from {} \u{2014} do NOT use this card.", n("got"), fw.len(), dev), C_RED)));
        }
        return Err(Exit(color(&format!("VERIFY FAILED at firmware byte 0x{:06x} (disk sector {}): card 0x{:02x} != image 0x{:02x}. The write did not land correctly \u{2014} do NOT use this card (re-seat it / try another and re-flash).", n("offset"), n("sector"), n("card"), n("image")), C_RED)));
    }
    eprintln!("{}", color(&format!("  verify OK: {} firmware bytes on the card match the image exactly.", fw.len()), C_GRN));
    Ok(())
}

/// ipod_flash.check_kept_data. A dry run that can't read the card says so
/// and carries on.
fn check_kept_data(dev: &str, total: u64, dry: bool, lba48: bool, max_data_sectors: Option<u64>) -> Result<(), Exit> {
    let mut f = match open_read(dev) {
        Ok(f) => f,
        Err(e) if dry => {
            // e.strerror: the message alone, no errno or path
            let m = e.to_string();
            let m = m.split(" (os error").next().unwrap_or(&m).to_string();
            eprintln!("{}", color(&format!("  [dry-run] could not read {} to check that its data partition matches ({}); a real run checks before writing.", dev, m), C_YEL));
            return Ok(());
        }
        Err(e) => return Err(Exit(color(&format!("could not read {} to check its data partition: {}", dev, os_error_text(&e, dev)), C_RED))),
    };
    let problems = layout::kept_data_problems(f.as_mut(), total, lba48, max_data_sectors);
    if !problems.is_empty() {
        return Err(Exit(color(&format!("--no-format keeps the data partition, but on {}:\n  {}\nFlashing would point the iPod at the wrong sectors. Flash without --no-format (erases the card) instead.", dev, problems.join("\n  ")), C_RED)));
    }
    eprintln!("{}", color("  data partition matches the planned layout; it will be kept", C_GRN));
    Ok(())
}

/// ipod_flash.self_test: the layout and firmware-fixup checks, no hardware.
pub fn self_test() -> i32 {
    use flashpod_core::layout::{build_mbr_layout, fat_sectors_per_cluster, DATA_START};
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
    println!("{}", color("self-test OK: MBR + FAT32 layout + tail reserve; firmware loadAddr fixup + osos checksum at absolute devOffset; LBA28 cap.", C_GRN));
    0
}
