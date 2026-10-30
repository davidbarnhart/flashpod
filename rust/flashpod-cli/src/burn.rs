//! `flashpod burn IMAGE [DEVICE]` (burn.py): a card image onto a removable
//! card, every check before a byte is written, read back afterwards.

use crate::devices;
use crate::flash::{fmt_size, Exit};
use crate::term::{self, color, C_DIM, C_GRN, C_RED, C_YEL};
use flashpod_core::block::FileDevice;
use serde_json::Value;
use std::io::{Read, Seek, SeekFrom, Write};

const CHUNK: u64 = 4 << 20;

fn say(msg: &str, c: Option<&str>) {
    match c {
        Some(c) => eprintln!("{}", color(msg, c)),
        None => eprintln!("{}", msg),
    }
}

fn fail(msg: &str) -> Exit {
    Exit(color(&format!("flashpod burn: {}", msg), C_RED))
}

fn is_block_device(dev: &str) -> bool {
    use std::os::unix::fs::FileTypeExt;
    std::fs::metadata(dev).map(|m| m.file_type().is_block_device()).unwrap_or(false)
}

/// burn.check_target: a removable whole disk at least as big as the image.
fn check_target(dev: &str, image_bytes: u64) -> Result<(u64, Value), Exit> {
    if !std::path::Path::new(dev).exists() {
        return Err(fail(&format!("no such device: {}", dev)));
    }
    if !is_block_device(dev) {
        return Err(fail(&format!("{} is not a block device. To copy an image to a file, use cp.", dev)));
    }
    devices::validate_target(dev)?;
    let real = crate::host::realpath(dev);
    let name = real.rsplit('/').next().unwrap_or(&real).to_string();
    let entry = devices::list_candidates()?
        .into_iter()
        .find(|d| d["name"].as_str() == Some(name.as_str()))
        .ok_or_else(|| fail(&format!("{} is not a removable disk (USB, MMC or hot-plug, per lsblk); flashpod burn only writes those. `lsblk -o NAME,SIZE,TRAN,RM` shows what is attached.", dev)))?;
    if matches!(entry["tran"].as_str(), Some("sbp" | "ieee1394")) {
        return Err(fail(&format!("{} is on FireWire; early iPod FireWire bridges crash on large writes. Put the card in a USB reader instead.", dev)));
    }
    let size = devices::device_sectors(dev) * 512;
    if size == 0 {
        return Err(fail(&format!("{} reports 0 bytes: is a card in the slot? (An empty slot of a multi-slot reader still shows up as a disk.)", dev)));
    }
    if size < image_bytes {
        return Err(fail(&format!("{} is {}; the image is {}. Use a card at least as big as the image.", dev, fmt_size(size), fmt_size(image_bytes))));
    }
    Ok((size, entry))
}

/// str() of a JSON value as Python prints the dict entry.
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        o => o.to_string(),
    }
}

pub fn burn(image: &str, device: Option<&str>, assume_yes: bool, dry_run: bool, verify: bool) -> Result<i32, Exit> {
    // card.inspect: the image under a shared lock, read-only
    let info = {
        let dev = FileDevice::open_image(image, false).and_then(|d| d.try_lock(false, image).map(|_| d));
        let mut dev = dev.map_err(|e| fail(&format!("{}: {}", image, e.message)))?;
        flashpod_core::inspect::inspect(&mut dev, image, crate::data::options()).map_err(|e| fail(&format!("{}: {}", image, e.message)))?
    };
    if info.partition_table.is_none() {
        return Err(fail(&format!("{} has no partition table: a blank card. Flash it first (flashpod flash {}).", image, image)));
    }
    let image_bytes = info.size_bytes;
    let dev = match device {
        Some(d) => d.to_string(),
        None => devices::choose_device()?,
    };
    let dev = dev.as_str();
    let (dev_bytes, entry) = check_target(dev, image_bytes)?;

    let fw = &info.firmware;
    let db = &info.itunesdb;
    say(&format!("\n  PLAN -- this will ERASE {} and write {} onto it", dev, image), Some(C_RED));
    say(&format!("    card        : {}  {} {}", fmt_size(dev_bytes), entry["vendor"].as_str().unwrap_or("").trim(), entry["model"].as_str().unwrap_or("").trim()), None);
    say(&format!("    image       : {}", fmt_size(image_bytes)), None);
    if dev_bytes > image_bytes {
        say(&format!("    (the last {} of the card stay unused)", fmt_size(dev_bytes - image_bytes)), None);
    }
    let fw_line = if fw["present"].as_bool() == Some(true) { py_str(&fw["build"]) } else { format!("NONE ({})", py_str(&fw["reason"])) };
    say(&format!("    firmware    : {}", fw_line), None);
    let db_line = if db["state"].as_str() == Some("ok") {
        format!("{} tracks, {} artists", db["tracks"], db["artists"])
    } else {
        py_str(&db["state"])
    };
    say(&format!("    iTunesDB    : {}", db_line), None);
    for p in &info.problems {
        say(&format!("    \u{26a0} {}", p), Some(C_YEL));
    }
    if !info.ready {
        say("    \u{26a0} the image isn't a ready-to-play card (see above); it is written as it is.", Some(C_YEL));
    }
    if info.past_128gib {
        say("    \u{26a0} the data partition reaches past 128 GiB: only firmware with the LBA48 fix can address it.", Some(C_YEL));
    }
    let mps = crate::detect::device_mountpoints(dev);
    if !mps.is_empty() {
        let m: Vec<String> = mps.iter().map(|(p, mp)| format!("{}@{}", p, mp)).collect();
        say(&format!("    mounted now : {}", m.join(", ")), Some(C_YEL));
    }
    if dry_run {
        say("  [dry-run] nothing written.", Some(C_DIM));
        return Ok(0);
    }
    if !assume_yes {
        let label = dev.trim_end_matches('/').rsplit('/').next().unwrap_or(dev);
        let want = format!("ERASE {}", label);
        // at end of input Python dies with EOFError; the plain mismatch
        // message instead, as for flash (David, 2026-10-25)
        let got = term::input_line(&color(&format!("\n  Type \"{}\" to proceed: ", want), C_RED)).unwrap_or_default();
        if got.trim() != want {
            return Err(Exit("Confirmation did not match. Aborted.".into()));
        }
    }
    {
        let mut src = FileDevice::open_image(image, false).map_err(|e| fail(&format!("{}: {}", image, e.message)))?;
        // hold the image still while we copy it
        src.try_lock(false, image).map_err(|e| Exit(format!("flashpod: {}", e.message)))?;
        devices::unmount_all(dev, false);
        copy(&mut src.file, dev, image_bytes)?;
        if verify {
            verify_card(&mut src.file, dev, image_bytes)?;
        }
    }
    devices::reread_partition_table(dev);
    crate::detect::eject(dev);
    say(&format!("\nDone. {} now holds {} -- put it in the iPod.", dev, image), Some(C_GRN));
    Ok(0)
}

fn progress(label: &str, done: u64, total: u64, t0: f64) {
    if !term::stderr_isatty() {
        return;
    }
    let rate = done as f64 / (crate::window::monotonic() - t0).max(1e-6);
    eprint!("\r  {} {:>3}%  {} of {}  ({:.1} MiB/s)   ", label, done * 100 / total.max(1), fmt_size(done), fmt_size(total), rate / (1u64 << 20) as f64);
    let _ = std::io::stderr().flush();
}

/// file.read(n) on a regular file: up to n bytes, short only at the end.
fn read_full(f: &mut std::fs::File, n: usize) -> std::io::Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    let mut got = 0;
    while got < n {
        let k = f.read(&mut buf[got..])?;
        if k == 0 {
            break;
        }
        got += k;
    }
    buf.truncate(got);
    Ok(buf)
}

fn copy(src: &mut std::fs::File, dev: &str, total: u64) -> Result<(), Exit> {
    say(&format!("  writing {} ...", fmt_size(total)), Some(C_DIM));
    let t0 = crate::window::monotonic();
    let mut done = 0u64;
    let io = |e: std::io::Error| Exit(e.to_string());
    src.seek(SeekFrom::Start(0)).map_err(io)?;
    let mut out = std::fs::OpenOptions::new().read(true).write(true).open(dev).map_err(|e| Exit(crate::flash::os_error_text(&e, dev)))?;
    while done < total {
        let chunk = read_full(src, CHUNK.min(total - done) as usize).map_err(io)?;
        if chunk.is_empty() {
            return Err(fail(&format!("short read from the image at {} of {} bytes", done, total)));
        }
        out.write_all(&chunk).map_err(io)?;
        done += chunk.len() as u64;
        progress("writing", done, total, t0);
    }
    out.flush().map_err(io)?;
    out.sync_all().map_err(io)?;
    drop(out);
    if term::stderr_isatty() {
        eprintln!();
    }
    say(&format!("  wrote {} in {:.0}s", fmt_size(total), crate::window::monotonic() - t0), Some(C_GRN));
    Ok(())
}

fn verify_card(src: &mut std::fs::File, dev: &str, total: u64) -> Result<(), Exit> {
    say("  verifying: reading the card back ...", Some(C_DIM));
    devices::flush_buffers(dev);
    let t0 = crate::window::monotonic();
    let mut done = 0u64;
    let io = |e: std::io::Error| Exit(e.to_string());
    src.seek(SeekFrom::Start(0)).map_err(io)?;
    let mut card = std::fs::File::open(dev).map_err(|e| Exit(crate::flash::os_error_text(&e, dev)))?;
    while done < total {
        let want = read_full(src, CHUNK.min(total - done) as usize).map_err(io)?;
        let got = read_full(&mut card, want.len()).map_err(io)?;
        if got != want {
            let n = got.len().min(want.len());
            let bad = (0..n).find(|&i| got[i] != want[i]).unwrap_or(n) as u64;
            return Err(fail(&format!("VERIFY FAILED at byte {} (sector {}): the card does not hold what was written. Do NOT use it; re-seat it or try another card.", done + bad, (done + bad) / 512)));
        }
        done += want.len() as u64;
        progress("verifying", done, total, t0);
    }
    if term::stderr_isatty() {
        eprintln!();
    }
    say(&format!("  verify OK: all {} on the card match the image.", fmt_size(total)), Some(C_GRN));
    Ok(())
}
