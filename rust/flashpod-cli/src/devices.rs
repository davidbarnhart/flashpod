//! The device half of ipod_flash and platform/linux.py: which disks may be
//! flashed (the chooser), the safety gate, the size, and the housekeeping
//! around the raw write (unmount, wipe, re-read, flush).

use crate::flash::{fmt_size, Exit};
use crate::host::{capture, have};
use crate::term::{color, input_line, C_CYN, C_DIM, C_RED, C_YEL};
use flashpod_core::layout::{LBA28_MAX, SECTOR};
use serde_json::Value;

/// A JSON value lsblk may give as a number, a string or a bool, as Python's
/// int(x or 0).
fn int(v: &Value) -> u64 {
    match v {
        Value::Number(n) => n.as_u64().unwrap_or(0),
        Value::String(s) => s.trim().parse().unwrap_or(0),
        Value::Bool(b) => *b as u64,
        _ => 0,
    }
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or("")
}

fn truthy(v: &Value) -> bool {
    matches!(v, Value::Bool(true)) || v.as_str() == Some("1") || v.as_u64() == Some(1)
}

fn lsblk_json(cols: &[&str]) -> Result<Value, Exit> {
    let mut cmd = vec!["lsblk", "-J"];
    cmd.extend_from_slice(cols);
    // ipod_flash.run(check=True): a failure is a CalledProcessError traceback
    // in Python; here a plain message
    let o = capture(&cmd).filter(|o| o.code == Some(0)).ok_or_else(|| Exit("flashpod: lsblk failed".into()))?;
    serde_json::from_str(&o.stdout).map_err(|e| Exit(format!("flashpod: lsblk: {}", e)))
}

/// ipod_flash.root_disk_names: the disks backing / and /boot, never touched.
pub fn root_disk_names() -> Result<Vec<String>, Exit> {
    let v = lsblk_json(&["-o", "NAME,MOUNTPOINT,PKNAME,TYPE"])?;
    let mut names = Vec::new();
    fn walk(n: &Value, top: &str, names: &mut Vec<String>) {
        if matches!(n["mountpoint"].as_str(), Some("/" | "/boot" | "/boot/efi" | "[SWAP]" | "/home")) {
            names.push(top.to_string());
        }
        for c in n["children"].as_array().into_iter().flatten() {
            walk(c, top, names);
        }
    }
    for d in v["blockdevices"].as_array().into_iter().flatten() {
        walk(d, s(d, "name"), &mut names);
    }
    Ok(names)
}

/// ipod_flash.list_candidates: removable / USB whole disks.
pub fn list_candidates() -> Result<Vec<Value>, Exit> {
    let v = lsblk_json(&["-b", "-o", "NAME,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINT,FSTYPE,LABEL,PTTYPE"])?;
    let protected = root_disk_names()?;
    let mut out = Vec::new();
    for d in v["blockdevices"].as_array().into_iter().flatten() {
        if s(d, "type") != "disk" || protected.iter().any(|p| p == s(d, "name")) {
            continue;
        }
        if truthy(&d["rm"]) || truthy(&d["hotplug"]) || matches!(s(d, "tran"), "usb" | "mmc" | "ieee1394") {
            out.push(d.clone());
        }
    }
    Ok(out)
}

/// ipod_flash.usb_device_dir.
fn usb_device_dir(name: &str) -> Option<String> {
    let mut p = crate::host::realpath(&format!("/sys/block/{}/device", name));
    while p.len() > 1 {
        if std::path::Path::new(&p).join("idVendor").exists() {
            return Some(p);
        }
        p = match p.rfind('/') {
            Some(0) => "/".into(),
            Some(i) => p[..i].to_string(),
            None => return None,
        };
    }
    None
}

fn sysfs_read(d: &str, attr: &str) -> String {
    std::fs::read_to_string(std::path::Path::new(d).join(attr)).map(|t| t.trim().to_string()).unwrap_or_default()
}

/// ipod_flash.usbids_vendor.
fn usbids_vendor(vid: &str) -> String {
    for path in ["/usr/share/misc/usb.ids", "/usr/share/hwdata/usb.ids"] {
        let Ok(bytes) = std::fs::read(path) else { continue };
        let text: String = bytes.iter().map(|&b| b as char).collect(); // latin1
        for line in text.split_inclusive('\n') {
            let c: Vec<char> = line.chars().collect();
            let head: String = c.iter().take(4).collect();
            if head.to_lowercase() == vid.to_lowercase() && matches!(c.get(4), Some(' ' | '\t')) {
                return c[4..].iter().collect::<String>().trim().to_string();
            }
        }
    }
    String::new()
}

/// ipod_flash.device_identity.
fn device_identity(d: &Value) -> String {
    let mut name = s(d, "model").trim().to_string();
    if name.is_empty() {
        name = s(d, "vendor").trim().to_string();
    }
    if let Some(usb) = usb_device_dir(s(d, "name")) {
        let product = sysfs_read(&usb, "product");
        if !product.is_empty() && (name.is_empty() || name.to_lowercase().contains("class")) {
            name = product;
        }
        let brand = usbids_vendor(&sysfs_read(&usb, "idVendor"));
        let brand = brand.split(", Inc").next().unwrap_or("");
        let brand = brand.split(", Ltd").next().unwrap_or("").trim().to_string();
        if !brand.is_empty() && brand.to_lowercase() != "generic" && !name.to_lowercase().contains(&brand.to_lowercase()) {
            name += &format!(" ({})", brand);
        }
    }
    match s(d, "tran") {
        "ieee1394" => name += ", FireWire",
        "mmc" => name += ", SD slot",
        _ => {}
    }
    name
}

fn pttype_name(p: &str) -> String {
    match p {
        "dos" => "MBR".into(),
        "gpt" => "GPT".into(),
        "mac" => "APM".into(),
        o => o.into(),
    }
}

/// ipod_flash.describe_contents.
fn describe_contents(d: &Value) -> String {
    if int(&d["size"]) == 0 {
        return "no card inserted".into();
    }
    let parts: Vec<&Value> = d["children"].as_array().map(|a| a.iter().collect()).unwrap_or_default();
    if parts.is_empty() {
        if !s(d, "fstype").is_empty() {
            let lbl = if s(d, "label").is_empty() { String::new() } else { format!(" '{}'", s(d, "label")) };
            return format!("{}{}, no partition table", s(d, "fstype"), lbl);
        }
        if !s(d, "pttype").is_empty() {
            return format!("{}, no partitions", pttype_name(s(d, "pttype")));
        }
        return "blank \u{2014} no partition table".into();
    }
    let bits: Vec<String> = parts
        .iter()
        .map(|p| {
            let fs = if s(p, "fstype").is_empty() { "unformatted" } else { s(p, "fstype") };
            let mut b = format!("{} {}", fs, fmt_size(int(&p["size"])));
            if !s(p, "label").is_empty() {
                b += &format!(" '{}'", s(p, "label"));
            }
            if !s(p, "mountpoint").is_empty() {
                b += &format!(" @ {}", s(p, "mountpoint"));
            }
            b
        })
        .collect();
    let pt = if s(d, "pttype").is_empty() { "?".to_string() } else { pttype_name(s(d, "pttype")) };
    let mut out = format!("{}: {}", pt, bits.join(", "));
    if parts.iter().any(|p| s(p, "label").to_uppercase() == "IPOD") {
        out += "  <- looks like an iPod card";
    }
    out
}

/// ipod_flash.capacity_warning.
pub fn capacity_warning(total: u64) -> Option<String> {
    if total <= LBA28_MAX {
        return None;
    }
    Some(format!(
        "reports {} \u{2014} past the 128 GiB (LBA28) limit these early iPods can address. An oversized card (256 GB+) is the usual cause: the iPod can't reach beyond 128 GiB, so it misreports capacity (often a wrapped, much-larger number) and reads back an unreadable filesystem \u{2014} even though flashpod caps the data partition at 128 GiB. Use a 128 GB-or-smaller card. (A loose IDE/CF connector can produce similar bogus readings, so reseat if the card really is small enough.)",
        fmt_size(total * SECTOR)
    ))
}

/// ipod_flash._render_candidates.
fn render(cands: &[Value]) {
    eprintln!("{}", color("\nAttached removable storage:\n", C_CYN));
    let usb_dirs: Vec<Option<String>> = cands.iter().map(|d| usb_device_dir(s(d, "name"))).collect();
    struct Row {
        n: String,
        dev: String,
        size: String,
        reader: String,
        contents: String,
        warn: Option<String>,
    }
    let mut rows = Vec::new();
    for (i, d) in cands.iter().enumerate() {
        let mut ident = device_identity(d);
        if let Some(u) = &usb_dirs[i] {
            if usb_dirs.iter().filter(|x| x.as_ref() == Some(u)).count() > 1 {
                let slot = usb_dirs[..=i].iter().filter(|x| x.as_ref() == Some(u)).count();
                ident += &format!(", slot {}", slot);
            }
        }
        let mounted = d["children"].as_array().into_iter().flatten().any(|p| !s(p, "mountpoint").is_empty())
            || !s(d, "mountpoint").is_empty();
        let mut contents = describe_contents(d);
        if mounted {
            contents += " (mounted)";
        }
        rows.push(Row {
            n: i.to_string(),
            dev: format!("/dev/{}", s(d, "name")),
            size: fmt_size(int(&d["size"])),
            reader: ident,
            contents,
            warn: capacity_warning(int(&d["size"]) / SECTOR),
        });
    }
    let len = |x: &str| x.chars().count();
    let nw = (rows.len() - 1).to_string().len().max(1);
    let dw = rows.iter().map(|r| len(&r.dev)).max().unwrap_or(0).max(len("Device"));
    let sw = rows.iter().map(|r| len(&r.size)).max().unwrap_or(0).max(len("Size"));
    let rw = rows.iter().map(|r| len(&r.reader)).max().unwrap_or(0).max(len("Reader"));
    let pad = |x: &str, w: usize, right: bool| {
        let fill = " ".repeat(w.saturating_sub(len(x)));
        if right { format!("{}{}", fill, x) } else { format!("{}{}", x, fill) }
    };
    let prow = |n: &str, dev: &str, size: &str, reader: &str| {
        format!("  {}  {}  {}  {}", pad(n, nw, true), pad(dev, dw, false), pad(size, sw, true), pad(reader, rw, false))
            .trim_end()
            .to_string()
    };
    let sub = 2 + nw + 2 + dw + 2 + sw + 2;
    eprintln!("{}", color(&prow("#", "Device", "Size", "Reader"), C_CYN));
    eprintln!("{}", color(&prow(&"-".repeat(nw), &"-".repeat(dw), &"-".repeat(sw), &"-".repeat(rw)), C_DIM));
    for r in &rows {
        eprintln!("{}", prow(&r.n, &r.dev, &r.size, &r.reader));
        let mut pieces = crate::argparse::wrap(&r.contents, 24.max(80usize.saturating_sub(sub)));
        if pieces.is_empty() {
            pieces.push(String::new());
        }
        for piece in pieces {
            eprintln!("{}", color(&format!("{}{}", " ".repeat(sub), piece), C_DIM));
        }
        if let Some(w) = &r.warn {
            eprintln!("{}", color(&format!("{}\u{26a0} {}", " ".repeat(sub), w), C_YEL));
        }
    }
    eprintln!();
}

/// ipod_flash.choose_device (pick_device over list_candidates).
pub fn choose_device() -> Result<String, Exit> {
    let empty = "No removable/USB disks found. Plug in the card and retry.";
    let mut items = list_candidates()?;
    if items.is_empty() {
        return Err(Exit(color(empty, C_RED)));
    }
    render(&items);
    loop {
        let sel = match input_line(&color("Select device number ('r' to rescan, 'q' to quit): ", C_CYN)) {
            Some(l) => l.trim().to_lowercase(),
            None => {
                eprintln!();
                return Err(Exit("Aborted.".into()));
            }
        };
        if matches!(sel.as_str(), "q" | "quit" | "") {
            return Err(Exit("Aborted.".into()));
        }
        if matches!(sel.as_str(), "r" | "refresh" | "rescan") {
            items = list_candidates()?;
            if items.is_empty() {
                eprintln!("{}", color(&format!("  {}", empty), C_YEL));
            } else {
                render(&items);
            }
            continue;
        }
        // str.isdigit() and int(sel) < len(items)
        if !sel.is_empty() && sel.chars().all(|c| c.is_ascii_digit()) {
            let i = sel.parse::<usize>().unwrap_or(usize::MAX);
            if i < items.len() {
                if int(&items[i]["size"]) != 0 {
                    return Ok(format!("/dev/{}", s(&items[i], "name")));
                }
                eprintln!("{}", color("  that slot is empty \u{2014} insert a card and press 'r' to rescan, or pick another device", C_YEL));
                continue;
            }
        }
        eprintln!("{}", color("  invalid selection", C_RED));
    }
}

/// LinuxPlatform.validate_target.
pub fn validate_target(dev: &str) -> Result<(), Exit> {
    if !std::path::Path::new(dev).exists() {
        return Err(Exit(color(&format!("no such device: {}", dev), C_RED)));
    }
    if dev.trim_end_matches(|c: char| c.is_ascii_digit()) != dev
        && !dev.starts_with("/dev/mmcblk")
        && !dev.starts_with("/dev/loop")
        && !dev.starts_with("/dev/nvme")
    {
        return Err(Exit(color(&format!("refusing a partition node ({}); pass the whole disk.", dev), C_RED)));
    }
    let base = dev.rsplit('/').next().unwrap_or(dev);
    if root_disk_names()?.iter().any(|n| n == base) {
        return Err(Exit(color(&format!("refusing: {} backs the running system.", dev), C_RED)));
    }
    Ok(())
}

/// ipod_flash.device_sectors: /sys/block, a seek to the end, blockdev, the
/// file size.
pub fn device_sectors(dev: &str) -> u64 {
    let base = dev.rsplit('/').next().unwrap_or(dev);
    let sysf = format!("/sys/block/{}/size", base);
    if std::path::Path::new(&sysf).exists() {
        if let Some(n) = std::fs::read_to_string(&sysf).ok().and_then(|t| t.trim().parse::<u64>().ok()) {
            return n;
        }
    }
    if let Ok(mut f) = std::fs::File::open(dev) {
        if let Ok(end) = std::io::Seek::seek(&mut f, std::io::SeekFrom::End(0)) {
            return end / SECTOR;
        }
    }
    if have("blockdev") {
        if let Some(n) = capture(&["blockdev", "--getsz", dev]).filter(|o| o.code == Some(0)).and_then(|o| o.stdout.trim().parse::<u64>().ok()) {
            return n;
        }
    }
    std::fs::metadata(dev).map(|m| m.len() / SECTOR).unwrap_or(0)
}

/// LinuxPlatform.partition_node: sdb -> sdb2, mmcblk0 -> mmcblk0p2.
pub fn partition_node(dev: &str, index: u32) -> String {
    let sep = if dev.ends_with(|c: char| c.is_ascii_digit()) { "p" } else { "" };
    format!("{}{}{}", dev, sep, index)
}

/// ipod_flash.unmount_all.
pub fn unmount_all(dev: &str, dry: bool) {
    for (part, mp) in crate::detect::device_mountpoints(dev) {
        eprintln!("{}", color(&format!("  unmounting {} ({})", part, mp), C_DIM));
        if !dry {
            let _ = capture(&["umount", &part]);
        }
    }
}

/// LinuxPlatform.wipe_signatures (not dry).
pub fn wipe_signatures(dev: &str) {
    if have("wipefs") {
        let _ = capture(&["wipefs", "-a", dev]);
    }
}

/// LinuxPlatform.reread_partition_table: partprobe, capped at 15 s, else
/// blockdev --rereadpt.
pub fn reread_partition_table(dev: &str) {
    let mut settled = false;
    if have("partprobe") {
        match crate::host::capture_timeout(&["partprobe", dev], 15.0) {
            Some(None) => eprintln!("{}", color("  partprobe stalled on udev settle; the partition map is already in place, continuing.", C_YEL)),
            _ => settled = true,
        }
    }
    if !settled && have("blockdev") {
        let _ = capture(&["blockdev", "--rereadpt", dev]);
    }
}

/// LinuxPlatform.flush_buffers.
pub fn flush_buffers(dev: &str) {
    let _ = capture(&["sync"]);
    if have("blockdev") {
        let _ = capture(&["blockdev", "--flushbufs", dev]);
    }
}
