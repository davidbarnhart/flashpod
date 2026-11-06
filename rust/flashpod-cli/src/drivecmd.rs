//! The 1.0 commands that write a drive's layout or firmware (#115):
//! `drive partition`, `firmware write`, `bootrom arm`, `bootrom disarm`.
//! Each takes its drive as `--target T` (an image file or a whole disk),
//! and each writes only what its name says: `firmware write` touches the
//! firmware partition and nothing else, unless the person confirms a
//! re-partition when the firmware can't read the data partition.

use crate::argparse::{Ns, Val};
use crate::flash::{fmt_size, is_image, os_error_text, ARM_QUESTION, ARM_WARNING};
use crate::term::{color, input_line, stdin_isatty, C_DIM, C_GRN, C_RED, C_YEL};
use flashpod_core::block::{BlockDevice, FileDevice};
use flashpod_core::drive::{self, LargeDiskProblem, PartitionOptions, Table};
use flashpod_core::firmware;
use flashpod_core::layout::{LBA28_MAX, SECTOR};

fn s(ns: &Ns, k: &str) -> Option<String> {
    match ns.get(k) {
        Some(Val::Str(v)) if !v.is_empty() => Some(v.clone()),
        _ => None,
    }
}

fn b(ns: &Ns, k: &str) -> bool {
    matches!(ns.get(k), Some(Val::Bool(true)))
}

fn f(ns: &Ns, k: &str) -> Option<f64> {
    match ns.get(k) {
        Some(Val::Float(v)) => Some(*v),
        _ => None,
    }
}

/// The drive a command acts on.
struct Target {
    path: String,
    image: bool,
}

fn say_target(t: &Target) {
    eprintln!("Target: {} ({})", t.path, if t.image { "image file" } else { "drive" });
}

/// --target, or on a terminal the chooser over removable disks. Without a
/// terminal a missing --target is refused (rc 2).
fn resolve_target(ns: &Ns, cmd: &str) -> Result<Target, i32> {
    if let Some(p) = s(ns, "target") {
        if is_image(&p) {
            return Ok(Target { path: p, image: true });
        }
        if !std::path::Path::new(&p).exists() {
            eprintln!("flashpod {}: no such file or disk: {}", cmd, p);
            return Err(1);
        }
        return disk(p);
    }
    if !stdin_isatty() {
        eprintln!("flashpod {}: name the drive or image with --target (no terminal to choose one on).", cmd);
        return Err(2);
    }
    match crate::platform::current().choose_device() {
        Ok(d) => disk(d),
        Err(crate::flash::Exit(m)) => {
            eprintln!("{}", m);
            Err(1)
        }
    }
}

/// A disk passes the device rails first: no partition node, no system
/// disk (platform.validate_target, as 0.x flash did).
fn disk(p: String) -> Result<Target, i32> {
    match crate::platform::current().validate_target(&p) {
        Ok(()) => Ok(Target { path: p, image: false }),
        Err(crate::flash::Exit(m)) => {
            eprintln!("{}", m);
            Err(1)
        }
    }
}

/// A disk needs root: re-run as root with `argv` (the command spelled out
/// in full, so nothing is asked twice). Returns only when it can't.
fn need_root(t: &Target, cmd: &str, argv: &[String]) -> Result<(), i32> {
    if t.image || crate::platform::current().is_admin() {
        return Ok(());
    }
    if stdin_isatty() {
        eprintln!("flashpod {}: {} is a disk; that needs root \u{2014} elevating via sudo...", cmd, t.path);
        crate::platform::current().elevate(argv);
    }
    eprintln!("flashpod {}: {} is a disk; that needs root.\n  sudo {}", cmd, t.path,
              crate::host::self_cmd().into_iter().chain(argv.iter().cloned()).collect::<Vec<_>>().join(" "));
    Err(1)
}

fn open(t: &Target, write: bool) -> Result<Box<dyn BlockDevice>, String> {
    let r: std::io::Result<Box<dyn BlockDevice>> = if t.image {
        FileDevice::open(std::path::Path::new(&t.path), write).map(|d| Box::new(d) as Box<dyn BlockDevice>)
    } else {
        crate::platform::current().open_raw(&t.path, write)
    };
    r.map_err(|e| os_error_text(&e, &t.path))
}

fn label(dev: &str) -> String {
    let d = dev.replace('\\', "/");
    let d = d.trim_end_matches('/');
    d.rsplit('/').next().unwrap_or(d).to_string()
}

/// What an erase of this drive loses, in words: the track count when
/// there is a FAT32 library to count.
fn what_is_lost(dev: &mut dyn BlockDevice, table: Option<&Table>) -> String {
    match table.map(|t| t.kind) {
        None => return "everything on it".to_string(),
        Some("apm") => return "its music (flashpod can't count an HFS+ library; it is lost all the same)".to_string(),
        _ => {}
    }
    let mut ent = flashpod_core::entropy::Entropy::new(1, 0);
    let n = match flashpod_core::ops::open_fat(dev) {
        Ok(mut fs) => match flashpod_core::ops::load_library(&mut fs, &mut ent) {
            Ok(Some(lib)) => Some(lib.tracks.len()),
            Ok(None) => Some(0),
            Err(_) => None,
        },
        Err(_) => None,
    };
    match n {
        Some(0) => "its data partition (no music database on it)".to_string(),
        Some(1) => "1 track of music and everything else on it".to_string(),
        Some(n) => format!("{} tracks of music and everything else on it", n),
        None => "its music (the database couldn't be read to count it)".to_string(),
    }
}

/// The typed confirmation for an erase. --yes never reaches this for the
/// re-partition offer.
fn typed_erase(dev: &str) -> bool {
    let want = format!("ERASE {}", label(dev));
    let got = input_line(&color(&format!("  Type \"{}\" to proceed: ", want), C_RED)).unwrap_or_default();
    got.trim() == want
}

fn yes_no(prompt: &str) -> bool {
    match input_line(prompt) {
        Some(a) => matches!(a.trim().to_lowercase().as_str(), "y" | "yes"),
        None => {
            println!();
            false
        }
    }
}

fn hfs_now() -> u32 {
    let unix = std::env::var("FLASHPOD_NOW").ok().and_then(|v| v.parse::<u64>().ok()).unwrap_or_else(|| {
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
    });
    (unix + flashpod_core::hfsplus::MAC_EPOCH) as u32
}

/// Before an erasing write to a disk: unmount it, clear old signatures.
fn prepare_disk_for_erase(t: &Target) {
    if !t.image {
        let p = crate::platform::current();
        p.unmount_all(&t.path, false);
        p.wipe_signatures(&t.path);
        p.invalidate_cached_partitions(&t.path);
    }
}

fn after_table_write(t: &Target) {
    if !t.image {
        let p = crate::platform::current();
        p.flush_buffers(&t.path);
        p.reread_partition_table(&t.path);
    }
}

/// The partition plan, printed (stderr).
fn print_partition_plan(dev: &str, total: u64, o: &PartitionOptions, lost: &str) -> Result<(), String> {
    let (_, start, n) = drive::plan_partition(total, o).map_err(|e| e.message)?;
    eprintln!("{}", color(&format!("\n  PLAN \u{2014} this will ERASE {}: {} will be lost", dev, lost), C_RED));
    eprintln!("    layout      : {}", if o.mac { "Apple Partition Map + HFS+ (a Mac drive)" } else { "MBR + FAT32" });
    eprintln!("    drive size  : {} ({} sectors)", fmt_size(total * SECTOR), total);
    eprintln!("    firmware    : sectors {}..{}, left empty ({})", flashpod_core::layout::FW_START,
              flashpod_core::layout::FW_START + flashpod_core::layout::FW_BLOCKS - 1,
              fmt_size(flashpod_core::layout::FW_BLOCKS * SECTOR));
    eprintln!("    data        : sectors {}..{}, {} ({})", start, start + n - 1,
              if o.mac { "HFS+" } else { "FAT32" }, fmt_size(n * SECTOR));
    if start + n > LBA28_MAX {
        eprintln!("{}", color("    \u{26a0} the data partition runs past 128 GiB: only firmware with large-disk support can read it (Apple's firmware can't). --128g caps it at 128 GiB.", C_YEL));
    } else if o.cap_128g && total > LBA28_MAX {
        eprintln!("{}", color(&format!("    data capped at 128 GiB (--128g); {} of the drive is left unused.",
                                       fmt_size((total - start - n) * SECTOR)), C_YEL));
    }
    Ok(())
}

/// Partition `dev` (already open for writing): the library call and its
/// summary line.
fn do_partition(dev: &mut dyn BlockDevice, o: PartitionOptions) -> Result<drive::PartitionResult, String> {
    let r = drive::partition(dev, o).map_err(|e| e.message)?;
    match r.clusters {
        Some(c) => eprintln!("{}", color(&format!("  partitioned: firmware partition (empty), data partition FAT32 ({} clusters of {}, label 'IPOD')",
                                                    c, fmt_size(r.cluster_bytes.unwrap_or(0))), C_GRN)),
        None => eprintln!("{}", color("  partitioned: firmware partition (empty), data partition HFS+ (volume 'iPod')", C_GRN)),
    }
    Ok(r)
}

/// `drive partition [--target T] [--128g] [--max-data-gb N] [--format fat32|hfs] [--yes] [--dry-run]`
pub fn cmd_partition(ns: &Ns) -> i32 {
    let cmd = "drive partition";
    let t = match resolve_target(ns, cmd) {
        Ok(t) => t,
        Err(rc) => return rc,
    };
    let o = PartitionOptions {
        cap_128g: b(ns, "cap_128g"),
        max_data_gb: f(ns, "max_data_gb"),
        mac: s(ns, "format").as_deref() == Some("hfs"),
        hfs_time: hfs_now(),
    };
    let mut argv = vec!["drive".to_string(), "partition".into(), "--target".into(), t.path.clone()];
    for (flag, k) in [("--128g", "cap_128g"), ("--yes", "yes"), ("--dry-run", "dry_run")] {
        if b(ns, k) {
            argv.push(flag.into());
        }
    }
    if let Some(g) = o.max_data_gb {
        argv.extend(["--max-data-gb".into(), g.to_string()]);
    }
    if let Some(fmt) = s(ns, "format") {
        argv.extend(["--format".into(), fmt]);
    }
    if let Err(rc) = need_root(&t, cmd, &argv) {
        return rc;
    }
    say_target(&t);
    let dry = b(ns, "dry_run");
    let mut dev = match open(&t, !dry) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    let total = dev.len().unwrap_or(0) / SECTOR;
    if total == 0 && t.image {
        eprintln!("flashpod {}: {} is empty -- give it the card's size first, e.g. `flashpod card new {} 1G`", cmd, t.path, t.path);
        return 1;
    }
    if total == 0 {
        eprintln!("flashpod {}: {} reports size 0 \u{2014} is a card inserted? An empty reader slot shows up as a 0-byte disk.", cmd, t.path);
        return 1;
    }
    let table = drive::read_table(dev.as_mut()).ok().flatten();
    let lost = what_is_lost(dev.as_mut(), table.as_ref());
    if let Err(m) = print_partition_plan(&t.path, total, &o, &lost) {
        eprintln!("{}", color(&format!("flashpod {}: {} can't hold an iPod layout: {}", cmd, t.path, m), C_RED));
        return 1;
    }
    if dry {
        eprintln!("{}", color("  [dry-run] nothing written", C_DIM));
        return 0;
    }
    if !b(ns, "yes") {
        if !stdin_isatty() {
            eprintln!("flashpod {}: this erases {}; without a terminal to confirm on, pass --yes.", cmd, t.path);
            return 1;
        }
        eprintln!();
        if !typed_erase(&t.path) {
            eprintln!("Confirmation did not match. Aborted.");
            return 1;
        }
    }
    drop(dev);
    prepare_disk_for_erase(&t);
    let mut dev = match open(&t, true) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    if let Err(m) = do_partition(dev.as_mut(), o) {
        eprintln!("{}", color(&format!("flashpod {}: {}: {}", cmd, t.path, m), C_RED));
        return 1;
    }
    drop(dev);
    after_table_write(&t);
    eprintln!("{}", color(&format!("\nDone. Next: flashpod firmware write --target {} FIRMWARE", t.path), C_GRN));
    0
}

/// The firmware argument, else the catalog choosers (as the invoking
/// user: a download lands in their cache). A FIRMWARE that isn't a
/// regular file is refused, so a disk named there is never read whole.
fn resolve_firmware(ns: &Ns) -> Result<String, i32> {
    if let Some(p) = s(ns, "firmware_file") {
        if !std::path::Path::new(&p).is_file() {
            eprintln!("flashpod firmware write: {} is not a firmware file. Name the drive with --target, the firmware file as the argument.", p);
            return Err(2);
        }
        return Ok(p);
    }
    let manifest = crate::catalog::load_manifest().ok_or(1)?;
    let model = match crate::catalog::choose_model(&manifest, s(ns, "model").as_deref()) {
        crate::catalog::Model::Bail => return Err(1),
        crate::catalog::Model::Any => None,
        crate::catalog::Model::One(m) => Some(m),
    };
    let entry = crate::catalog::choose_firmware(&manifest, model.as_ref()).ok_or(1)?;
    crate::catalog::ensure_firmware(&entry, manifest["base_url"].as_str().unwrap_or("")).ok_or(1)
}

fn build_name(fw: &[u8]) -> String {
    drive::describe(fw)["build"].as_str().unwrap_or("unrecognized").to_string()
}

/// The large-disk refusal or offer. Ok(true): re-partition with --128g
/// was confirmed; Ok(false): no problem; Err(rc): stop, nothing written.
fn large_disk_gate(ns: &Ns, t: &Target, fw_path: &str, build: &str, p: &LargeDiskProblem,
                   dev: &mut dyn BlockDevice, table: &Table) -> Result<bool, i32> {
    let past = fmt_size(p.data_end_sector * SECTOR);
    match p.support {
        Some(false) => eprintln!("{}", color(&format!(
            "\nThis firmware ({}) can't read a data partition past 128 GiB, and {}'s data partition ends at {}.",
            build, t.path, past), C_RED)),
        _ => eprintln!("{}", color(&format!(
            "\nflashpod can't tell whether this firmware ({}) can read a data partition past 128 GiB, and {}'s data partition ends at {}.",
            build, t.path, past), C_YEL)),
    }
    let fix = format!("  flashpod drive partition --target {} --128g\n  flashpod firmware write --target {} {}",
                      t.path, t.path, fw_path);
    if b(ns, "dry_run") || !stdin_isatty() {
        eprintln!("Nothing written. To fit the drive to this firmware (this ERASES it), run:\n{}", fix);
        return Err(1);
    }
    let lost = what_is_lost(dev, Some(table));
    eprintln!("{}", color(&format!(
        "Re-partitioning with --128g first makes the drive readable by this firmware. That ERASES {}: {} will be lost.",
        t.path, lost), C_RED));
    let q = if p.support.is_none() { "Re-partition anyway? [y/N] " } else { "Re-partition, then write the firmware? [y/N] " };
    if !yes_no(&format!("\n{}", q)) {
        eprintln!("Nothing written.");
        return Err(1);
    }
    eprintln!();
    if !typed_erase(&t.path) {
        eprintln!("Confirmation did not match. Nothing written.");
        return Err(1);
    }
    Ok(true)
}

/// `firmware write [FIRMWARE] [--target T] [--model M] [--yes] [--dry-run]`
pub fn cmd_firmware_write(ns: &Ns) -> i32 {
    let cmd = "firmware write";
    if let Some(rc) = moved_flash_flags(ns) {
        return rc;
    }
    let fw_path = match resolve_firmware(ns) {
        Ok(p) => p,
        Err(rc) => return rc,
    };
    let t = match resolve_target(ns, cmd) {
        Ok(t) => t,
        Err(rc) => return rc,
    };
    let mut argv = vec!["firmware".to_string(), "write".into(), fw_path.clone(), "--target".into(), t.path.clone()];
    for (flag, k) in [("--yes", "yes"), ("--dry-run", "dry_run")] {
        if b(ns, k) {
            argv.push(flag.into());
        }
    }
    if let Err(rc) = need_root(&t, cmd, &argv) {
        return rc;
    }
    say_target(&t);
    let fw = match crate::flash::load_firmware(&fw_path).and_then(|fw| firmware::check_writable(&fw).map(|_| fw)) {
        Ok(fw) => fw,
        Err(e) => {
            eprintln!("{}", color(&format!("firmware error ({}): {}", fw_path, e), C_RED));
            return 1;
        }
    };
    let build = build_name(&fw);
    let base = fw_path.rsplit(['/', '\\']).next().unwrap_or(&fw_path);
    eprintln!("{}", color(&format!("firmware OK: {} ({}, {} bytes, structure validated)", base, build, fw.len()), C_GRN));
    let dry = b(ns, "dry_run");
    let mut dev = match open(&t, false) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    let table = match drive::read_table(dev.as_mut()) {
        Ok(Some(tb)) if tb.firmware.is_some() => tb,
        Ok(_) => {
            eprintln!("{}", color(&format!(
                "flashpod {}: {} has no firmware partition. Partition it first (this erases it):\n  flashpod drive partition --target {}",
                cmd, t.path, t.path), C_RED));
            return 1;
        }
        Err(e) => {
            eprintln!("flashpod {}: can't read {}: {}", cmd, t.path, e.message);
            return 1;
        }
    };
    let fwp = table.firmware.clone().unwrap();
    let repartition = match drive::large_disk_problem(&table, &fw) {
        None => false,
        Some(p) => match large_disk_gate(ns, &t, &fw_path, &build, &p, dev.as_mut(), &table) {
            Ok(r) => r,
            Err(rc) => return rc,
        },
    };
    let armed = drive::armed(dev.as_mut()).ok().flatten() == Some(true);
    if !repartition {
        eprintln!("{}", color(&format!("\n  PLAN \u{2014} write the firmware into {}'s firmware partition; nothing else on the drive changes", t.path), C_YEL));
        eprintln!("    table       : {}", if table.kind == "mbr" { "MBR" } else { "Apple Partition Map" });
        eprintln!("    firmware    : sectors {}..{} ({}), {} bytes of firmware, the rest zeroed",
                  fwp.start, fwp.start + fwp.sectors - 1, fmt_size(fwp.sectors * SECTOR), fw.len());
        if armed {
            eprintln!("{}", color("    \u{26a0} the firmware there now has a disk-mode update ARMED; writing this clears it", C_YEL));
        }
        if dry {
            eprintln!("{}", color("  [dry-run] nothing written", C_DIM));
            return 0;
        }
        if !b(ns, "yes") {
            if !stdin_isatty() {
                eprintln!("flashpod {}: without a terminal to confirm on, pass --yes.", cmd);
                return 1;
            }
            if !yes_no(&format!("\nWrite the firmware to {}? [y/N] ", t.path)) {
                eprintln!("Nothing written.");
                return 1;
            }
        }
    }
    drop(dev);
    if repartition {
        prepare_disk_for_erase(&t);
    }
    let mut dev = match open(&t, true) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    if repartition {
        let o = PartitionOptions { cap_128g: true, max_data_gb: None, mac: table.kind == "apm", hfs_time: hfs_now() };
        if let Err(m) = do_partition(dev.as_mut(), o) {
            eprintln!("{}", color(&format!("flashpod {}: {}: {}", cmd, t.path, m), C_RED));
            return 1;
        }
    }
    eprintln!("{}", color(&format!("  writing {} bytes of firmware and verifying them ...", fw.len()), C_DIM));
    match drive::write_firmware(dev.as_mut(), &fw) {
        Ok(r) => {
            eprintln!("{}", color(&format!("  verify OK: {} firmware bytes at sector {} match the file exactly.", r.firmware_bytes, r.partition_start), C_GRN));
            if r.cleared_arm {
                eprintln!("{}", color("  the disk-mode update that was armed is cleared (`flashpod bootrom arm` arms this firmware's)", C_YEL));
            }
        }
        Err(e) => {
            let what = if e.kind == flashpod_core::ErrorKind::VerifyFailed { "VERIFY FAILED \u{2014} do NOT use this drive: " } else { "" };
            eprintln!("{}", color(&format!("flashpod {}: {}{}: {}", cmd, what, t.path, e.message), C_RED));
            return 1;
        }
    }
    drop(dev);
    if repartition {
        after_table_write(&t);
    } else if !t.image {
        crate::platform::current().flush_buffers(&t.path);
    }
    eprintln!("{}", color(&format!("\nDone. {} has the firmware{}.", t.path,
                                   if repartition { " and a new, empty data partition" } else { "" }), C_GRN));
    0
}

/// The flags `firmware write` lost (#115): each names where it went.
fn moved_flash_flags(ns: &Ns) -> Option<i32> {
    let moved: [(&str, &str); 7] = [
        ("no_format", "--no-format: `firmware write` never touches the data partition now, so drop it."),
        ("arm", "--arm: arming is its own command now: `flashpod bootrom arm --target T`."),
        ("no_arm", "--no-arm: `firmware write` never arms now, so drop it."),
        ("lba48", "--lba48: `flashpod drive partition` uses the whole drive by default now (--128g caps it)."),
        ("max_data_gb_moved", "--max-data-gb: it moved to `flashpod drive partition`."),
        ("format_moved", "--format: it moved to `flashpod drive partition`."),
        ("firmware_flag", "--firmware: the firmware file is the argument now and the drive is --target: `flashpod firmware write --target T FIRMWARE`."),
    ];
    for (k, msg) in moved {
        let set = match ns.get(k) {
            Some(Val::Bool(v)) => *v,
            Some(Val::Str(_)) | Some(Val::Float(_)) => true,
            _ => false,
        };
        if set {
            eprintln!("flashpod firmware write: {}", msg);
            return Some(2);
        }
    }
    None
}

/// `bootrom arm [stock|PROGRAM] [--target T] [--yes] [--dry-run]` and
/// `bootrom disarm [--target T] [--dry-run]`, on the firmware already on
/// the drive.
pub fn cmd_bootrom(ns: &Ns, verb: &str) -> i32 {
    let cmd = format!("bootrom {}", verb);
    if let Some(p) = s(ns, "program") {
        eprintln!("flashpod {} {}: not built yet (coming in flashpod 1.0). `flashpod bootrom arm` arms the program in the firmware already on the drive.", cmd, p);
        return 2;
    }
    let t = match resolve_target(ns, &cmd) {
        Ok(t) => t,
        Err(rc) => return rc,
    };
    let mut argv = vec!["bootrom".to_string(), verb.into(), "--target".into(), t.path.clone()];
    for (flag, k) in [("--yes", "yes"), ("--dry-run", "dry_run")] {
        if b(ns, k) {
            argv.push(flag.into());
        }
    }
    if let Err(rc) = need_root(&t, &cmd, &argv) {
        return rc;
    }
    say_target(&t);
    let dry = b(ns, "dry_run");
    let mut dev = match open(&t, false) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    let now = match drive::armed(dev.as_mut()) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("flashpod {}: {}: {}", cmd, t.path, e.message);
            return 1;
        }
    };
    let want = verb == "arm";
    if !want && now.is_none() {
        println!("{}: nothing is armed (no disk-mode program in the firmware there); nothing written.", t.path);
        return 0;
    }
    if now == Some(want) {
        println!("{}: the disk-mode update is already {}; nothing written.", t.path, if want { "armed" } else { "disarmed" });
        return 0;
    }
    if dry {
        eprintln!("{}", color(&format!("  [dry-run] would {} the disk-mode update on {} (two directory sectors); nothing written", verb, t.path), C_DIM));
        return 0;
    }
    if want && !b(ns, "yes") {
        if !stdin_isatty() {
            eprintln!("flashpod {}: arming needs a yes; without a terminal, pass --yes.", cmd);
            return 1;
        }
        eprintln!("{}", color(ARM_WARNING, C_RED));
        if !yes_no(&format!("\n{}", ARM_QUESTION.replace("this card", "this drive"))) {
            eprintln!("Nothing written.");
            return 1;
        }
    }
    drop(dev);
    let mut dev = match open(&t, true) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod {}: can't open {}: {}", cmd, t.path, e);
            return 1;
        }
    };
    let r = if want { drive::arm(dev.as_mut()) } else { drive::disarm(dev.as_mut()) };
    match r {
        Ok(r) => {
            println!("{}: disk-mode update {} ({}; {} sectors written, read back OK).", t.path,
                     if want { "ARMED" } else { "disarmed" }, r.program.unwrap_or("program not identified"), r.sectors_written);
            if !t.image {
                crate::platform::current().flush_buffers(&t.path);
            }
            0
        }
        Err(e) => {
            eprintln!("{}", color(&format!("flashpod {}: {}: {}", cmd, t.path, e.message), C_RED));
            1
        }
    }
}
