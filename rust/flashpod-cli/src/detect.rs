//! Finding the iPod when neither --mount nor --raw names it: cli.py's
//! candidate_mounts, unmounted_candidates, detect_ls_source,
//! resolve_raw_target and their choosers, mount_device / _sudo_mount, and
//! the eject offer after a command.

use crate::argparse::{Ns, Val};
use crate::host::{self, capture, have, is_admin};
use crate::rawdev::{self, RawDevice};
use crate::term;
use flashpod_core::fat::{Fat32, ATTR_DIRECTORY};
use flashpod_core::itunesdb::Library;
use serde_json::Value;

pub enum Source {
    Mount(String),
    Raw(String),
    Lib(Library),
}

fn ns_s(ns: &Ns, k: &str) -> Option<String> {
    match ns.get(k) {
        Some(Val::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

fn ns_list(ns: &Ns, k: &str) -> Vec<String> {
    match ns.get(k) {
        Some(Val::List(v)) => v.clone(),
        _ => Vec::new(),
    }
}

/// _cmd_args for the data commands (ls/add/rm/init/rebuild).
pub fn cmd_args(ns: &Ns) -> Vec<String> {
    let c = ns_s(ns, "command").unwrap_or_default();
    let mut a = vec![c.clone()];
    match c.as_str() {
        "ls" | "list" => a.extend(ns_s(ns, "field").filter(|s| !s.is_empty())),
        "add" => a.extend(ns_list(ns, "files")),
        "rm" | "remove" | "delete" | "erase" => a.extend(ns_list(ns, "what")),
        "init" | "rebuild" => a.extend(ns_s(ns, "name").filter(|s| !s.is_empty())),
        _ => {}
    }
    a
}

fn with_desc(node: &str, desc: &str) -> String {
    if desc.is_empty() { node.to_string() } else { format!("{}  ({})", node, desc) }
}

/// Python's choice.isdigit() and int(choice) < n.
fn pick(choice: &str, n: usize) -> Option<usize> {
    if choice.is_empty() || !choice.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    choice.parse::<usize>().ok().filter(|&i| i < n)
}

/// input(prompt).strip(); None (after a newline) at end of input.
fn ask(prompt: &str) -> Option<String> {
    match term::input_line(prompt) {
        Some(a) => Some(a.trim().to_string()),
        None => {
            println!();
            None
        }
    }
}

// ------------------------------------------------------------------ mounts

/// candidate_mounts: (score, mountpoint), best first.
pub fn candidate_mounts() -> Vec<(i32, String)> {
    let mut cands = Vec::new();
    for (dev, mnt, fstype) in host::mounted_filesystems() {
        let ft = fstype.to_lowercase();
        if !(ft.contains("fat") || ft.contains("msdos") || ft.contains("hfs")) {
            continue;
        }
        if dev.starts_with("/dev/") && !std::path::Path::new(&dev).exists() {
            eprintln!("flashpod: ignoring {} \u{2014} stale mount, its device ({}) is gone (sudo umount -l {} to clean up).", mnt, dev, mnt);
            continue;
        }
        let mut score = 0;
        if std::path::Path::new(&crate::mount::join(&mnt, "iPod_Control")).is_dir() {
            score += 10;
        }
        let base = mnt.trim_end_matches(['/', '\\']);
        let base = base.rsplit('/').next().unwrap_or(base);
        if base.to_lowercase().contains("ipod") {
            score += 5;
        }
        if mnt.starts_with("/media/") || mnt.starts_with("/run/media/") || mnt.starts_with("/Volumes/") {
            score += 1;
        }
        if score > 0 {
            cands.push((score, mnt));
        }
    }
    cands.sort_by_key(|c| -c.0);
    cands
}

/// unmounted_candidates: (score, dev, label, tran), best first.
pub fn unmounted_candidates() -> Vec<(i32, String, String, String)> {
    let out = match capture(&["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,LABEL,TRAN,RM,HOTPLUG,MOUNTPOINT"]) {
        Some(o) if o.code == Some(0) => o.stdout,
        _ => return Vec::new(),
    };
    let v: Value = serde_json::from_str(&out).unwrap_or(Value::Null);
    let mut cands = Vec::new();
    fn walk(node: &Value, tran: &str, cands: &mut Vec<(i32, String, String, String)>) {
        let s = |k: &str| node[k].as_str().unwrap_or("").to_string();
        let truthy = |k: &str| match &node[k] {
            Value::Bool(b) => *b,
            Value::Number(x) => x.as_i64() != Some(0),
            Value::String(x) => !x.is_empty(),
            _ => false,
        };
        let tran = if s("tran").is_empty() { tran.to_string() } else { s("tran") };
        if node["type"].as_str() == Some("part") && s("mountpoint").is_empty()
            && matches!(s("fstype").as_str(), "vfat" | "exfat" | "hfsplus")
        {
            let label = s("label");
            let mut score = 0;
            if label.to_lowercase().contains("ipod") {
                score += 5;
            }
            if tran == "sbp" || tran == "ieee1394" {
                score += 5;
            }
            if truthy("rm") || truthy("hotplug") || tran == "usb" {
                score += 1;
            }
            if score > 0 {
                cands.push((score, format!("/dev/{}", s("name")), label, tran.clone()));
            }
        }
        for child in node["children"].as_array().into_iter().flatten() {
            walk(child, &tran, cands);
        }
    }
    for d in v["blockdevices"].as_array().into_iter().flatten() {
        walk(d, "", &mut cands);
    }
    cands.sort_by_key(|c| -c.0);
    cands
}

/// _unmounted_disks: the candidates not mounted (a whole disk counts as
/// mounted when one of its partitions is).
fn unmounted_disks(disks: &[(String, String)]) -> Vec<(String, String)> {
    let mounted: Vec<String> = host::mounted_filesystems().into_iter()
        .filter(|(d, _, _)| d.starts_with("/dev/")).map(|(d, _, _)| host::realpath(&d)).collect();
    let part_suffix = |t: &str| {
        let t = t.strip_prefix('p').unwrap_or(t);
        (!t.is_empty() && t.chars().all(|c| c.is_ascii_digit()))
            || {
                // (?:s\d+)+
                let mut rest = t;
                let mut ok = !rest.is_empty();
                while ok && !rest.is_empty() {
                    match rest.strip_prefix('s') {
                        Some(r) => {
                            let n = r.chars().take_while(|c| c.is_ascii_digit()).count();
                            ok = n > 0;
                            rest = &r[n..];
                        }
                        None => ok = false,
                    }
                }
                ok
            }
    };
    disks.iter().filter(|(node, _)| {
        let real = host::realpath(node);
        let is = mounted.contains(&real) || mounted.iter().any(|m| m.starts_with(&real) && {
            let tail = &m[real.len()..];
            !tail.is_empty() && part_suffix(tail)
        });
        !is
    }).cloned().collect()
}

/// _choose_mounted.
pub fn choose_mounted(cands: &[(i32, String)]) -> Option<String> {
    if !term::stdin_isatty() {
        eprintln!("flashpod: no --mount given and not a terminal, so the detected mount can't be confirmed; pass --mount. Candidates:");
        for (_, m) in cands {
            eprintln!("  {}", m);
        }
        return None;
    }
    if cands.len() == 1 {
        let mnt = &cands[0].1;
        let ans = ask(&format!("Using iPod mounted at {} \u{2014} continue? [Y/n] ", mnt))?;
        if matches!(ans.to_lowercase().as_str(), "" | "y" | "yes") {
            return Some(mnt.clone());
        }
        eprintln!("flashpod: aborted; pass --mount to pick a different one");
        return None;
    }
    println!("Possible iPod mountpoints:");
    for (i, (score, mnt)) in cands.iter().enumerate() {
        println!("  [{}] {}{}", i, mnt, if *score >= 10 { "  (has iPod_Control)" } else { "" });
    }
    let choice = ask("Select [0]: ")?;
    if choice.is_empty() {
        return Some(cands[0].1.clone());
    }
    match pick(&choice, cands.len()) {
        Some(i) => Some(cands[i].1.clone()),
        None => {
            eprintln!("flashpod: invalid selection");
            None
        }
    }
}

// ------------------------------------------------------------------ mounting

/// _run_quiet_mount: mount(8)'s stderr minus the fstab/daemon-reload nag.
fn run_quiet_mount(cmd: &[String]) -> i32 {
    let out = std::process::Command::new(&cmd[0]).args(&cmd[1..]).stderr(std::process::Stdio::piped()).output();
    match out {
        Ok(o) => {
            for line in String::from_utf8_lossy(&o.stderr).lines() {
                if !(line.contains("fstab has been modified") || line.contains("daemon-reload")) {
                    eprintln!("{}", line);
                }
            }
            o.status.code().unwrap_or(1)
        }
        Err(_) => 1,
    }
}

fn shell_quote(s: &str) -> String {
    if !s.is_empty() && s.chars().all(|c| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c)) {
        s.to_string()
    } else {
        format!("'{}'", s.replace('\'', "'\"'\"'"))
    }
}

/// _sudo_mount.
fn sudo_mount(dev: &str, label: &str) -> Option<String> {
    let user = std::env::var("SUDO_USER").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| {
        unsafe {
            let pw = libc::getpwuid(libc::getuid());
            if pw.is_null() { String::new() } else { std::ffi::CStr::from_ptr((*pw).pw_name).to_string_lossy().into_owned() }
        }
    });
    let name = if label.trim().is_empty() { "IPOD".to_string() } else { label.trim().to_string() };
    let mountpoint = format!("/media/{}/{}", user, name);
    let (uid, gid) = unsafe { (libc::getuid(), libc::getgid()) };
    eprintln!("flashpod: udisks unavailable; mounting {} via sudo (you may be prompted for your password)...", dev);
    let script = format!("mkdir -p {} && mount -o uid={},gid={} {} {}", shell_quote(&mountpoint), uid, gid, shell_quote(dev), shell_quote(&mountpoint));
    if run_quiet_mount(&["sudo".into(), "sh".into(), "-c".into(), script]) != 0 {
        eprintln!("flashpod: sudo mount of {} failed", dev);
        return None;
    }
    eprintln!("Mounted {} at {}", dev, mountpoint);
    Some(mountpoint)
}

/// mount_device: udisks, else (on a terminal) sudo mount.
pub fn mount_device(dev: &str, label: &str) -> Option<String> {
    if !host::ensure_firewire_disk_safe(dev) {
        let disk = host::disk_of_dev(dev);
        eprintln!("flashpod: {} is a FireWire iPod and its host I/O settings could not be made safe \u{2014} refusing to mount it (mounting reads big and can crash the bridge). Pin them and retry:\n  sudo sh -c 'echo 4 >/sys/block/{}/queue/max_sectors_kb; echo 0 >/sys/block/{}/queue/read_ahead_kb'", dev, disk, disk);
        return None;
    }
    let res = capture(&["udisksctl", "mount", "-b", dev]);
    if let Some(r) = &res {
        if r.code == Some(0) {
            // "Mounted /dev/sdb2 at /media/david/IPOD" (older udisks: a ".")
            let line = r.stdout.trim_end();
            if let Some(i) = line.rfind(" at ") {
                let m = line[i + 4..].trim_end();
                let m = m.strip_suffix('.').unwrap_or(m);
                if !m.is_empty() {
                    eprintln!("{}", r.stdout.trim());
                    return Some(m.to_string());
                }
            }
            eprintln!("flashpod: mounted {} but couldn't parse the mountpoint; pass --mount", dev);
            return None;
        }
        let why = if r.stderr.trim().is_empty() { r.stdout.trim() } else { r.stderr.trim() };
        eprintln!("flashpod: udisks mount failed: {}", why);
    }
    if !term::stdin_isatty() {
        eprintln!("flashpod: cannot prompt for a sudo password here; mount the partition manually and pass --mount.");
        return None;
    }
    sudo_mount(dev, label)
}

/// offer_mount.
pub fn offer_mount(announce_empty: bool) -> Option<String> {
    let cands = unmounted_candidates();
    if cands.is_empty() {
        if announce_empty {
            eprintln!("flashpod: no iPod-like mounts found (is it plugged in?), or pass --mount");
        }
        return None;
    }
    if !term::stdin_isatty() {
        eprintln!("flashpod: found unmounted iPod-like partitions but can't ask to mount them here; mount one (udisksctl mount -b <dev>) and pass --mount:");
        for (_, dev, label, tran) in &cands {
            eprintln!("  {}  label={} tran={}", dev, if label.is_empty() { "-" } else { label }, if tran.is_empty() { "-" } else { tran });
        }
        return None;
    }
    let describe = |c: &(i32, String, String, String)| {
        let bits: Vec<&str> = [c.2.as_str(), c.3.as_str()].into_iter().filter(|b| !b.is_empty()).collect();
        if bits.is_empty() { c.1.clone() } else { format!("{} ({})", c.1, bits.join(", ")) }
    };
    if cands.len() == 1 {
        let ans = ask(&format!("Found unmounted iPod partition {} \u{2014} mount it? [Y/n] ", describe(&cands[0])))?;
        if !matches!(ans.to_lowercase().as_str(), "" | "y" | "yes") {
            return None;
        }
        return mount_device(&cands[0].1, &cands[0].2);
    }
    println!("Unmounted iPod-like partitions:");
    for (i, c) in cands.iter().enumerate() {
        println!("  [{}] {}", i, describe(c));
    }
    let choice = ask("Mount which? [0] ")?;
    if choice.is_empty() {
        return mount_device(&cands[0].1, &cands[0].2);
    }
    match pick(&choice, cands.len()) {
        Some(i) => mount_device(&cands[i].1, &cands[i].2),
        None => {
            eprintln!("flashpod: invalid selection");
            None
        }
    }
}

/// detect_mount.
pub fn detect_mount() -> Option<String> {
    let cands = candidate_mounts();
    if cands.is_empty() {
        return offer_mount(true);
    }
    choose_mounted(&cands)
}

// ------------------------------------------------------------------ scanning

/// open_raw_fat over a device node, read-only, or why not.
fn probe_fat<T>(node: &str, f: impl FnOnce(&mut Fat32) -> Result<T, String>) -> Result<T, String> {
    let xfer = rawdev::effective_xfer();
    let mut dev = RawDevice::open(node, false, xfer).map_err(|e| rawdev::py_os(&e).to_string())?;
    let mut fs = rawdev::open_raw_fat(&mut dev, xfer)?;
    f(&mut fs)
}

/// scan_for_ipod: the candidates whose FAT holds iPod_Control/iTunes/iTunesDB.
pub fn scan_for_ipod(cands: &[(String, String)]) -> Vec<(String, String)> {
    cands.iter().filter(|(node, _)| {
        probe_fat(node, |fs| fs.exists("iPod_Control/iTunes/iTunesDB")).unwrap_or(false)
    }).cloned().collect()
}

/// _count_music_files.
fn count_music_files(fs: &mut Fat32) -> usize {
    let mut total = 0;
    if let Ok(Some(dirs)) = fs.listdir("iPod_Control/Music") {
        for d in dirs {
            if d.attr & ATTR_DIRECTORY == 0 || !d.name.to_uppercase().starts_with('F') {
                continue;
            }
            match fs.listdir(&format!("iPod_Control/Music/{}", d.name)) {
                Ok(Some(files)) => total += files.iter().filter(|f| f.attr & ATTR_DIRECTORY == 0).count(),
                Ok(None) => {}
                Err(_) => return total,
            }
        }
    }
    total
}

/// _ls_from_raw: open_raw_target(node, writable=False) and the library.
fn ls_from_raw(node: &str, desc: &str) -> Option<Source> {
    if !host::ensure_firewire_disk_safe(node) {
        eprintln!("flashpod: couldn't pin safe FireWire I/O settings for {}; the kernel may probe it unsafely after writes.", node);
    }
    let xfer = rawdev::effective_xfer();
    let mut dev = match RawDevice::open(node, false, xfer) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("flashpod: need root to write {}.\n  sudo {} ... --raw {}", node, host::self_cmd().join(" "), node);
            return None;
        }
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, rawdev::py_os(&e));
            return None;
        }
    };
    let mut fs = match rawdev::open_raw_fat(&mut dev, xfer) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, e);
            return None;
        }
    };
    let found = if desc.is_empty() { format!("Found iPod on {}", node) } else { format!("Found iPod on {} ({})", node, desc) };
    match flashpod_core::ops::load_library(&mut fs, &mut crate::entropy()) {
        Err(e) if e.kind == flashpod_core::error::ErrorKind::CorruptLibrary => {
            let n = count_music_files(&mut fs);
            eprintln!("{} \u{2014} {} music file(s) on the card, but its iTunesDB is corrupt ({}).\n  The library can't be listed until the database is rebuilt \u{2014} run `flashpod rebuild` (or `flashpod add`, which offers to rebuild first).", found, n, e.message);
            None
        }
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            None
        }
        Ok(None) => {
            eprintln!("flashpod: {} has an iPod directory structure but no iTunesDB \u{2014} run `flashpod rebuild` (or `flashpod init` for an empty library).", node);
            None
        }
        Ok(Some(lib)) => {
            eprintln!("{} \u{2014} {} tracks.", found, lib.tracks.len());
            Some(Source::Lib(lib))
        }
    }
}

/// _choose_source.
fn choose_source(sources: &[(bool, String, String)]) -> Option<Source> {
    println!("Multiple iPod disks found:");
    for (i, (_, r, label)) in sources.iter().enumerate() {
        println!("  [{}] {}{}", i, r, if label.is_empty() { String::new() } else { format!("  ({})", label) });
    }
    if !term::stdin_isatty() {
        eprintln!("flashpod: not a terminal, so can't ask which to use; pass --mount <path> or --raw <device>.");
        return None;
    }
    let choice = ask("Use which? [0] ")?;
    let idx = pick(&choice, sources.len()).unwrap_or(0);
    let (is_mount, r, _) = &sources[idx];
    Some(if *is_mount { Source::Mount(r.clone()) } else { Source::Raw(r.clone()) })
}

/// _pick_among_sources: a mounted iPod and other unmounted FAT disks.
fn pick_among_sources(ns: &Ns, mounts: &[(i32, String)], unmounted: &[(String, String)]) -> Option<Source> {
    if !is_admin() {
        if term::stdin_isatty() {
            eprintln!("flashpod: another disk is attached alongside the mounted iPod; checking whether it's a second iPod means reading it raw, which needs root \u{2014} elevating via sudo...");
            host::sudo_reexec(&cmd_args(ns));
        }
        eprintln!("flashpod: couldn't get root to check the other attached disk(s); using the mounted iPod. Pass --raw <device> to target another:");
        for (node, desc) in unmounted {
            eprintln!("  {}", with_desc(node, desc));
        }
        return choose_mounted(mounts).map(Source::Mount);
    }
    let found = scan_for_ipod(unmounted);
    if found.is_empty() {
        return choose_mounted(mounts).map(Source::Mount);
    }
    let mut sources: Vec<(bool, String, String)> = mounts.iter()
        .map(|(score, m)| (true, m.clone(), format!("mounted{}", if *score >= 10 { " \u{b7} has iPod_Control" } else { "" })))
        .collect();
    sources.extend(found.into_iter().map(|(n, d)| (false, n, if d.is_empty() { "raw device".to_string() } else { d })));
    choose_source(&sources)
}

/// detect_ls_source.
pub fn detect_ls_source(ns: &Ns) -> Option<Source> {
    let mounts = candidate_mounts();
    let disks = host::fat_disk_candidates();
    if !mounts.is_empty() {
        let unmounted = unmounted_disks(&disks);
        if !unmounted.is_empty() {
            return match pick_among_sources(ns, &mounts, &unmounted)? {
                Source::Raw(node) => ls_from_raw(&node, ""),
                other => Some(other),
            };
        }
        return choose_mounted(&mounts).map(Source::Mount);
    }
    if disks.is_empty() {
        if let Some(m) = offer_mount(false) {
            return Some(Source::Mount(m));
        }
        eprintln!("flashpod: no iPod found \u{2014} nothing mounted and no attached disk to scan. Plug it in, or pass --mount/--raw.");
        return None;
    }
    if !is_admin() {
        eprintln!("flashpod: looking for an iPod means reading attached disks, which needs root \u{2014} elevating via sudo...");
        host::sudo_reexec(&cmd_args(ns));
        eprintln!("flashpod: couldn't get root to scan. Re-run with sudo, or pass --raw <device>. Candidate disk(s):");
        for (node, desc) in &disks {
            eprintln!("  {}", with_desc(node, desc));
        }
        return None;
    }
    let found = scan_for_ipod(&disks);
    if found.is_empty() {
        eprintln!("flashpod: scanned {} disk(s); none held an iPod database (iPod_Control/iTunes/iTunesDB).", disks.len());
        for (node, desc) in &disks {
            eprintln!("  checked {}", with_desc(node, desc));
        }
        host::hint_if_bridge_collapsed();
        return None;
    }
    let (node, desc) = if found.len() == 1 {
        found[0].clone()
    } else {
        println!("Multiple iPod disks found:");
        for (i, (n, d)) in found.iter().enumerate() {
            println!("  [{}] {}", i, with_desc(n, d));
        }
        let choice = ask("Read which? [0] ")?;
        found[pick(&choice, found.len()).unwrap_or(0)].clone()
    };
    ls_from_raw(&node, &desc)
}

/// _choose_init_disk.
fn choose_init_disk(disks: &[(String, String)]) -> Option<String> {
    let labelled: Vec<(String, String, String)> = disks.iter().map(|(node, desc)| {
        let status = match probe_fat(node, |fs| {
            if fs.read_file("iPod_Control/iTunes/iTunesDB")?.is_some() {
                Ok("ALREADY an iPod \u{2014} init resets its database")
            } else if fs.exists("iPod_Control")? {
                Ok("has iPod_Control but no database")
            } else {
                Ok("empty/freshly-flashed")
            }
        }) {
            Ok(s) => s.to_string(),
            Err(_) => "unreadable FAT".to_string(),
        };
        (node.clone(), desc.clone(), status)
    }).collect();
    if labelled.len() == 1 {
        let (node, desc, status) = &labelled[0];
        let what = if desc.is_empty() { node.clone() } else { format!("{} ({})", node, desc) };
        if !term::ask_yes(&format!("Initialize {} \u{2014} {}? [y/N] ", what, status), false) {
            return None;
        }
        return Some(node.clone());
    }
    println!("Disks that could be initialized:");
    for (i, (node, desc, status)) in labelled.iter().enumerate() {
        println!("  [{}] {}  \u{2014} {}", i, with_desc(node, desc), status);
    }
    let choice = ask("Initialize which? [none] ")?;
    pick(&choice, labelled.len()).map(|i| labelled[i].0.clone())
}

const RAW_FALLBACK_MSG: &str = "iPod partition could not be successfully mounted by the operating system. flashpod will use the partition in raw mode via its internal FAT driver (slower).";

/// _raw_or_mounted (Linux): FireWire stays raw; anything else is mounted.
fn raw_or_mounted(node: &str) -> Source {
    if host::is_firewire_disk(node) {
        return Source::Raw(node.to_string());
    }
    eprintln!("flashpod: {} isn't FireWire \u{2014} mounting it to use the kernel's (much faster) FAT driver...", node);
    match mount_device(node, "") {
        Some(m) => Source::Mount(m),
        None => {
            eprintln!("{}", RAW_FALLBACK_MSG);
            Source::Raw(node.to_string())
        }
    }
}

/// resolve_raw_target: a target for add/rm/init/rebuild.
pub fn resolve_raw_target(ns: &Ns) -> Option<Source> {
    let command = ns_s(ns, "command").unwrap_or_default();
    let mounts = candidate_mounts();
    let disks = host::fat_disk_candidates();
    if !mounts.is_empty() {
        let unmounted = if command != "init" { unmounted_disks(&disks) } else { Vec::new() };
        if !unmounted.is_empty() {
            return pick_among_sources(ns, &mounts, &unmounted);
        }
        return choose_mounted(&mounts).map(Source::Mount);
    }
    if disks.is_empty() {
        if let Some(m) = offer_mount(false) {
            return Some(Source::Mount(m));
        }
        eprintln!("flashpod: no iPod found \u{2014} nothing mounted and no attached disk. Plug it in, or pass --mount/--raw.");
        return None;
    }
    if !is_admin() {
        eprintln!("flashpod: {} over the iPod's raw device needs root \u{2014} elevating via sudo...", command);
        host::sudo_reexec(&cmd_args(ns));
        eprintln!("flashpod: couldn't get root. Re-run with sudo, or pass --raw <device>. Candidate disk(s):");
        for (node, desc) in &disks {
            eprintln!("  {}", with_desc(node, desc));
        }
        return None;
    }
    if command == "init" {
        return choose_init_disk(&disks).map(Source::Raw);
    }
    let found = scan_for_ipod(&disks);
    if found.is_empty() {
        eprintln!("flashpod: scanned {} disk(s); none held an iPod database. For a fresh card, run `flashpod init` first.", disks.len());
        host::hint_if_bridge_collapsed();
        return None;
    }
    if found.len() == 1 {
        let (node, desc) = &found[0];
        eprintln!("Found iPod on {}{}.", node, if desc.is_empty() { String::new() } else { format!(" ({})", desc) });
        return Some(raw_or_mounted(node));
    }
    println!("Multiple iPod disks found:");
    for (i, (n, d)) in found.iter().enumerate() {
        println!("  [{}] {}", i, with_desc(n, d));
    }
    let choice = ask("Use which? [0] ")?;
    Some(raw_or_mounted(&found[pick(&choice, found.len()).unwrap_or(0)].0))
}

// ------------------------------------------------------------------ eject

/// The device mounted at `mount` (realpaths compared), or None.
pub fn device_for_mount(mount: &str) -> Option<String> {
    if mount.is_empty() {
        return None;
    }
    let want = host::realpath(mount);
    host::mounted_filesystems().into_iter().find(|(_, m, _)| host::realpath(m) == want).map(|(d, _, _)| d)
}

/// ipod_flash.device_mountpoints.
pub fn device_mountpoints(dev: &str) -> Vec<(String, String)> {
    let o = match capture(&["lsblk", "-J", "-o", "NAME,MOUNTPOINT", dev]) {
        Some(o) if o.code == Some(0) && !o.stdout.trim().is_empty() => o,
        _ => return Vec::new(),
    };
    let v: Value = serde_json::from_str(&o.stdout).unwrap_or(Value::Null);
    let mut mps = Vec::new();
    fn walk(n: &Value, mps: &mut Vec<(String, String)>) {
        if let Some(m) = n["mountpoint"].as_str().filter(|m| !m.is_empty()) {
            mps.push((format!("/dev/{}", n["name"].as_str().unwrap_or("")), m.to_string()));
        }
        for c in n["children"].as_array().into_iter().flatten() {
            walk(c, mps);
        }
    }
    for d in v["blockdevices"].as_array().into_iter().flatten() {
        walk(d, &mut mps);
    }
    mps
}

/// ipod_flash._run_privileged: root, or sudo in the terminal (sudo -n off one).
fn run_privileged(cmd: &[&str]) -> Option<i32> {
    let mut c: Vec<String> = cmd.iter().map(|s| s.to_string()).collect();
    if !is_admin() && have("sudo") {
        if term::stdin_isatty() {
            eprintln!("{}", term::color(&format!("  {} needs root \u{2014} elevating via sudo...", cmd[0]), term::C_DIM));
            c.insert(0, "sudo".into());
        } else {
            c.insert(0, "-n".into());
            c.insert(0, "sudo".into());
        }
    }
    if term::stdin_isatty() {
        host::call(&c)
    } else {
        let refs: Vec<&str> = c.iter().map(|s| s.as_str()).collect();
        capture(&refs).and_then(|o| o.code)
    }
}

/// ipod_flash.eject (not dry).
pub fn eject(dev: &str) {
    eprintln!("{}", term::color(&format!("  flushing + ejecting {}", dev), term::C_DIM));
    let _ = capture(&["sync"]);
    for (part, mp) in device_mountpoints(dev) {
        eprintln!("{}", term::color(&format!("  unmounting {} ({})", part, mp), term::C_DIM));
        if have("udisksctl") && capture(&["udisksctl", "unmount", "--no-user-interaction", "-b", &part]).and_then(|o| o.code) == Some(0) {
            continue;
        }
        run_privileged(&["umount", &part]);
    }
    if have("udisksctl") && capture(&["udisksctl", "power-off", "--no-user-interaction", "-b", dev]).and_then(|o| o.code) == Some(0) {
        return;
    }
    if have("eject") {
        run_privileged(&["eject", dev]);
    }
}

/// offer_eject: after a successful add/rm on a terminal, eject and wait.
pub fn offer_eject(dev: Option<&str>, rc: i32) {
    let dev = match dev {
        Some(d) if rc == 0 && term::stdin_isatty() => d,
        _ => return,
    };
    if std::path::Path::new(dev).is_file() {
        return;                         // an image: nothing to eject
    }
    let ans = match term::input_line("\nEject the iPod now? [Y/n] ") {
        Some(a) => a.trim().to_lowercase(),
        None => {
            eprintln!();
            return;
        }
    };
    if ans.starts_with('n') {
        println!("Left attached \u{2014} eject it before you unplug, or the OS may still have writes in flight.");
        return;
    }
    for attempt in 0..3 {
        eject(dev);
        if device_mountpoints(dev).is_empty() {
            println!("Ejected \u{2014} safe to disconnect.");
            return;
        }
        if attempt < 2 {
            println!("  still busy \u{2014} retrying...");
            crate::window::sleep(2.0);
        }
    }
    eprintln!("flashpod: couldn't eject {}. Close anything using the volume, then eject it yourself before unplugging.", dev);
}

// ------------------------------------------------------------------ remount

/// remount_as_user: a mount we can't read (left by root).
pub fn remount_as_user(mount: &str) -> Option<String> {
    let dev = device_for_mount(mount);
    if !term::stdin_isatty() {
        eprintln!("flashpod: {} is mounted by another user and not readable here; unmount it (sudo umount {}) and re-run, or pass --mount.", mount, mount);
        return None;
    }
    let dev = match dev {
        Some(d) => d,
        None => {
            eprintln!("flashpod: {} isn't readable and its device couldn't be determined; unmount it and re-run.", mount);
            return None;
        }
    };
    let ans = match term::input_line(&format!("{} is mounted by another user and not readable. Remount {} as you? [Y/n] ", mount, dev)) {
        Some(a) => a,
        None => {
            println!();
            return None;
        }
    };
    if !matches!(ans.trim().to_lowercase().as_str(), "" | "y" | "yes") {
        return None;
    }
    let base = mount.trim_end_matches('/');
    let label = base.rsplit('/').next().filter(|s| !s.is_empty()).unwrap_or("IPOD").to_string();
    eprintln!("flashpod: unmounting {} (sudo)...", mount);
    if host::call(&["sudo".into(), "umount".into(), mount.to_string()]) != Some(0) {
        eprintln!("flashpod: could not unmount {}.", mount);
        return None;
    }
    sudo_mount(&dev, &label)
}
