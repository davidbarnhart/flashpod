//! `flashpod flash` onto a disk image: ipod_flash.flash with the image
//! platform (platform/image.py), message for message.

use crate::term::{color, input_line, C_DIM, C_GRN, C_RED, C_YEL};
use flashpod_core::block::{BlockDevice, FileDevice};
use flashpod_core::fat32fmt::{self, FormatError};
use flashpod_core::firmware;
use flashpod_core::layout::{self, FW_BLOCKS, FW_START, LBA28_MAX, SECTOR, TAIL_RESERVE};

pub const ARM_QUESTION: &str = "Arm this card for an iPod update to disk-mode during the next boot? [y/N] ";
pub const ARM_WARNING: &str = "To perform the disk-mode update to your iPod, the iPod must be plugged into FireWire and receiving power during its next boot. Halting the disk-mode update before it completes can leave the iPod in a permanently broken state.";

pub struct Opts {
    pub device: String,
    pub firmware: String,
    pub yes: bool,
    pub dry_run: bool,
    pub no_format: bool,
    pub arm: bool,
    pub lba48: bool,
    pub max_data_gb: Option<f64>,
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

fn capacity_warning(total: u64) -> Option<String> {
    if total <= LBA28_MAX {
        return None;
    }
    Some(format!(
        "reports {} \u{2014} past the 128 GiB (LBA28) limit these early iPods can address. An oversized card (256 GB+) is the usual cause: the iPod can't reach beyond 128 GiB, so it misreports capacity (often a wrapped, much-larger number) and reads back an unreadable filesystem \u{2014} even though flashpod caps the data partition at 128 GiB. Use a 128 GB-or-smaller card. (A loose IDE/CF connector can produce similar bogus readings, so reseat if the card really is small enough.)",
        fmt_size(total * SECTOR)
    ))
}

/// ipod_flash.device_label.
fn device_label(dev: &str) -> String {
    let d = dev.replace('\\', "/");
    let d = d.trim_end_matches('/');
    d.rsplit('/').next().unwrap_or(d).to_string()
}

#[allow(clippy::too_many_arguments)]
fn confirm(dev: &str, total: u64, assume_yes: bool, lba48: bool, max_data_sectors: Option<u64>, do_format: bool, armed: Option<&str>) -> Result<(), Exit> {
    let (_, data_start, data_blocks) = layout::build_mbr_layout(total, lba48, max_data_sectors).map_err(Exit)?;
    if do_format {
        eprintln!("{}", color(&format!("\n  PLAN \u{2014} this will ERASE {}", dev), C_RED));
        eprintln!("    layout      : MBR + FAT32");
    } else {
        eprintln!("{}", color(&format!("\n  PLAN \u{2014} this will REWRITE the firmware and partition table on {}; the data partition and its music are kept", dev), C_YEL));
        eprintln!("    layout      : MBR (data partition kept, not formatted)");
    }
    eprintln!("    device size : {} ({} sectors)", fmt_size(total * SECTOR), total);
    eprintln!("    firmware     : sectors {}..{}   ({})", FW_START, FW_START + FW_BLOCKS - 1, fmt_size(FW_BLOCKS * SECTOR));
    eprintln!(
        "    {} : sectors {}..{}  type 0x0B  ({})",
        if do_format { "data (FAT32)" } else { "data (kept) " },
        data_start,
        data_start + data_blocks - 1,
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

/// The before-eject hook (cli.offer_init_after_flash), given the open image.
pub type Hook<'a> = &'a mut dyn FnMut(&str, &mut FileDevice, u64) -> Result<(), Exit>;

pub fn flash(o: &Opts, hook: Option<Hook>) -> Result<i32, Exit> {
    let mut fw = load_firmware(&o.firmware).map_err(|e| Exit(color(&format!("firmware error ({}): {}", o.firmware, e), C_RED)))?;
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
    let dev = o.device.as_str();
    // ImagePlatform.validate_target / device_sectors
    let size = std::fs::metadata(dev).map(|m| m.len()).unwrap_or(0);
    if size < 512 {
        return Err(Exit(format!("flashpod: {} is empty -- give it the card's size first, e.g. `truncate -s 1G {}`", dev, dev)));
    }
    let total = size / SECTOR;
    if layout::build_mbr_layout(total, o.lba48, max_data_sectors).is_err() {
        return Err(Exit(color(&format!("{} is too small for an iPod card: {} leaves no room for a data partition after the {} firmware partition.", dev, fmt_size(total * SECTOR), fmt_size(FW_BLOCKS * SECTOR)), C_RED)));
    }
    let do_format = !o.no_format;
    if !do_format {
        check_kept_data(dev, total, o.lba48, max_data_sectors)?;
    }
    confirm(dev, total, o.yes || o.dry_run, o.lba48, max_data_sectors, do_format, armed)?;
    let (header, data_start, data_blocks) = layout::build_mbr_layout(total, o.lba48, max_data_sectors).map_err(Exit)?;
    if o.dry_run {
        if do_format {
            eprintln!("{}", color(&format!("  [dry-run] would wipe signatures, write {}-byte MBR header, {}-byte firmware @ sector {}, pure-Python FAT32 on data", header.len(), fw.len(), FW_START), C_DIM));
        } else {
            eprintln!("{}", color(&format!("  [dry-run] would zero sectors 0..{} and the last 34, write {}-byte firmware @ sector {} and a {}-byte MBR header; the data partition (sectors {}..{}) is left as it is", data_start - 1, fw.len(), FW_START, header.len(), data_start, data_start + data_blocks - 1), C_DIM));
        }
        eprintln!("{}", color("  [dry-run] would verify the firmware region byte-for-byte", C_DIM));
        eprintln!("{}", color(&format!("\nDone. {} is ready \u{2014} insert it into the iPod.", dev), C_GRN));
        return Ok(0);
    }
    let mut f = FileDevice::open(std::path::Path::new(dev), true).map_err(|e| Exit(os_error_text(&e, dev)))?;
    let io = |e: std::io::Error| Exit(e.to_string());
    if do_format {
        layout::write_layout_body(&mut f, &fw, total, data_start).map_err(io)?;
        let spc = layout::fat_sectors_per_cluster(data_blocks);
        match fat32fmt::format_fat32(&mut f, data_start, data_blocks, spc, "IPOD") {
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
    } else {
        layout::write_layout_body(&mut f, &fw, total, data_start).map_err(io)?;
    }
    f.flush().map_err(io)?;
    eprintln!("{}", color("  wrote MBR partition table + firmware", C_GRN));
    eprintln!("{}", color(&format!("  verifying firmware on the card byte-for-byte ({} bytes @ sector {}) ...", fw.len(), FW_START), C_DIM));
    if let Err(e) = layout::verify_region(&mut f, &fw) {
        let d = &e.details;
        let n = |k: &str| d.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
        if d.get("reason").and_then(|v| v.as_str()) == Some("short_read") {
            return Err(Exit(color(&format!("VERIFY FAILED: short read ({} of {} firmware bytes) from {} \u{2014} do NOT use this card.", n("got"), fw.len(), dev), C_RED)));
        }
        return Err(Exit(color(&format!("VERIFY FAILED at firmware byte 0x{:06x} (disk sector {}): card 0x{:02x} != image 0x{:02x}. The write did not land correctly \u{2014} do NOT use this card (re-seat it / try another and re-flash).", n("offset"), n("sector"), n("card"), n("image")), C_RED)));
    }
    eprintln!("{}", color(&format!("  verify OK: {} firmware bytes on the card match the image exactly.", fw.len()), C_GRN));
    // ImagePlatform.init_before_mbr: the hook first, through the open file,
    // then the partition table
    let hooked = match hook {
        Some(h) => h(dev, &mut f, data_start),
        None => Ok(()),
    };
    f.write_at(0, &header).map_err(io)?;
    f.flush().map_err(io)?;
    hooked?;
    eprintln!("{}", color(&format!("\nDone. {} is ready \u{2014} insert it into the iPod.", dev), C_GRN));
    Ok(0)
}

/// ipod_flash.check_kept_data on an image.
fn check_kept_data(dev: &str, total: u64, lba48: bool, max_data_sectors: Option<u64>) -> Result<(), Exit> {
    let mut f = FileDevice::open(std::path::Path::new(dev), false)
        .map_err(|e| Exit(color(&format!("could not read {} to check its data partition: {}", dev, os_error_text(&e, dev)), C_RED)))?;
    let problems = layout::kept_data_problems(&mut f, total, lba48, max_data_sectors);
    if !problems.is_empty() {
        return Err(Exit(color(&format!("--no-format keeps the data partition, but on {}:\n  {}\nFlashing would point the iPod at the wrong sectors. Flash without --no-format (erases the card) instead.", dev, problems.join("\n  ")), C_RED)));
    }
    eprintln!("{}", color("  data partition matches the planned layout; it will be kept", C_GRN));
    Ok(())
}
