//! The Linux host as cli.py and platform/linux.py see it: the mount table,
//! lsblk's disks, sysfs queue settings, the FireWire stack, and re-running
//! flashpod under sudo.

use crate::term;
use crate::window;
use serde_json::Value;
use std::process::{Command, Stdio};

/// platform.linux.MOUNTS_FILE: FLASHPOD_MOUNTS_FILE points it at a fake table.
pub fn mounts_file() -> String {
    std::env::var("FLASHPOD_MOUNTS_FILE").unwrap_or_else(|_| "/proc/mounts".into())
}

/// linux._unescape: the four codes /proc/mounts uses.
fn unescape(field: &str) -> String {
    field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
}

/// LinuxPlatform.mounted_filesystems: (device, mountpoint, fstype).
pub fn mounted_filesystems() -> Vec<(String, String, String)> {
    let text = match std::fs::read(mounts_file()) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(_) => return Vec::new(),
    };
    text.lines()
        .filter_map(|l| {
            let p: Vec<&str> = l.split_whitespace().collect();
            if p.len() < 3 {
                return None;
            }
            Some((unescape(p[0]), unescape(p[1]), p[2].to_string()))
        })
        .collect()
}

pub fn is_admin() -> bool {
    unsafe { libc::geteuid() == 0 }
}

/// shutil.which over PATH.
pub fn have(tool: &str) -> bool {
    let path = std::env::var("PATH").unwrap_or_default();
    path.split(':').any(|d| {
        let p = std::path::Path::new(if d.is_empty() { "." } else { d }).join(tool);
        std::fs::metadata(&p).map(|m| m.is_file() && std::os::unix::fs::PermissionsExt::mode(&m.permissions()) & 0o111 != 0)
            .unwrap_or(false)
    })
}

pub struct Out {
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
}

/// subprocess.run with captured output; None when the tool can't be run
/// (OSError).
pub fn capture(cmd: &[&str]) -> Option<Out> {
    let out = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::null()).output().ok()?;
    Some(Out {
        code: out.status.code(),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    })
}

/// subprocess.run(cmd, timeout=secs) with captured output: None when the
/// tool can't be run, Some(None) when it ran past the timeout (killed, as
/// subprocess does), else Some(exit code).
pub fn capture_timeout(cmd: &[&str], secs: f64) -> Option<Option<i32>> {
    let mut child = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::null())
        .stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().ok()?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(secs);
    loop {
        if let Ok(Some(st)) = child.try_wait() {
            return Some(Some(st.code().unwrap_or(1)));
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Some(None);
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}

/// subprocess.run(cmd) with the terminal inherited; the exit code, None
/// when it couldn't be started.
pub fn call(cmd: &[String]) -> Option<i32> {
    let _ = std::io::Write::flush(&mut std::io::stdout());
    Command::new(&cmd[0]).args(&cmd[1..]).status().ok().map(|s| s.code().unwrap_or(1))
}

/// os.path.realpath, for paths that may not exist (resolved as far as they do).
pub fn realpath(p: &str) -> String {
    if let Ok(r) = std::fs::canonicalize(p) {
        return r.to_string_lossy().into_owned();
    }
    let path = std::path::Path::new(p);
    match (path.parent(), path.file_name()) {
        (Some(dir), Some(name)) if !dir.as_os_str().is_empty() => {
            let d = realpath(&dir.to_string_lossy());
            format!("{}/{}", d.trim_end_matches('/'), name.to_string_lossy())
        }
        _ => p.to_string(),
    }
}

/// LinuxPlatform.fat_disk_candidates: (node, description) for every
/// external disk worth probing.
pub fn fat_disk_candidates() -> Vec<(String, String)> {
    let mut out = None;
    for attempt in 0..2 {
        match capture(&["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,LABEL,TRAN,RM,HOTPLUG,SIZE"]) {
            Some(o) if o.code == Some(0) => {
                out = Some(o.stdout);
                break;
            }
            _ => {
                if attempt == 1 {
                    return Vec::new();
                }
                window::sleep(0.5);
            }
        }
    }
    let v: Value = match serde_json::from_str(&out.unwrap_or_default()) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let s = |n: &Value, k: &str| n[k].as_str().unwrap_or("").to_string();
    let truthy = |n: &Value, k: &str| match &n[k] {
        Value::Bool(b) => *b,
        Value::Number(x) => x.as_i64() != Some(0),
        Value::String(x) => !x.is_empty(),
        _ => false,
    };
    let describe = |node: &Value, tran: &str| {
        let label = if tran == "sbp" || tran == "ieee1394" { "FireWire".to_string() } else { tran.to_string() };
        [s(node, "label"), label, s(node, "size")].into_iter().filter(|b| !b.is_empty()).collect::<Vec<_>>().join(" ")
    };
    fn partitions(node: &Value, out: &mut Vec<Value>) {
        for child in node["children"].as_array().into_iter().flatten() {
            if child["type"].as_str() == Some("part") {
                out.push(child.clone());
            }
            partitions(child, out);
        }
    }
    let fat = |t: &str| t == "vfat" || t == "exfat";
    let mut cands = Vec::new();
    for disk in v["blockdevices"].as_array().into_iter().flatten() {
        if !matches!(disk["type"].as_str(), Some("disk") | Some("rbc")) {
            continue;
        }
        if matches!(s(disk, "size").trim(), "0" | "0B") {
            continue;
        }
        let tran = s(disk, "tran");
        let removable = truthy(disk, "rm") || truthy(disk, "hotplug");
        if !(removable || matches!(tran.as_str(), "usb" | "sbp" | "ieee1394")) {
            continue;
        }
        let mut parts = Vec::new();
        partitions(disk, &mut parts);
        let fat_parts: Vec<&Value> = parts.iter().filter(|p| fat(&s(p, "fstype"))).collect();
        if !fat_parts.is_empty() {
            for p in fat_parts {
                cands.push((format!("/dev/{}", s(p, "name")), describe(p, &tran)));
            }
            continue;
        }
        if fat(&s(disk, "fstype")) {
            cands.push((format!("/dev/{}", s(disk, "name")), describe(disk, &tran)));
            continue;
        }
        let fstypes: Vec<String> = if parts.is_empty() { vec![s(disk, "fstype")] } else { parts.iter().map(|p| s(p, "fstype")).collect() };
        if fstypes.iter().all(|t| !t.is_empty()) {
            continue;
        }
        cands.push((format!("/dev/{}", s(disk, "name")), describe(disk, &tran)));
    }
    cands
}

/// cli._disk_of_dev: /dev/sdb2 -> sdb when sysfs knows sdb.
pub fn disk_of_dev(dev: &str) -> String {
    let name = dev.rsplit('/').next().unwrap_or(dev).to_string();
    // re.sub(r"p?\d+$", "", name)
    let b = name.as_bytes();
    let mut end = b.len();
    while end > 0 && b[end - 1].is_ascii_digit() {
        end -= 1;
    }
    let stripped = if end < b.len() {
        if end > 0 && b[end - 1] == b'p' { &name[..end - 1] } else { &name[..end] }
    } else {
        &name[..]
    };
    if !stripped.is_empty() && std::path::Path::new(&format!("/sys/block/{}", stripped)).is_dir() {
        return stripped.to_string();
    }
    name
}

fn lsblk_tran(disk: &str) -> Option<String> {
    let o = capture(&["lsblk", "-dno", "TRAN", &format!("/dev/{}", disk)])?;
    if o.code != Some(0) {
        return Some(String::new());
    }
    Some(o.stdout.trim().to_string())
}

fn read_int(path: &str) -> Option<i64> {
    std::fs::read_to_string(path).ok()?.trim().parse().ok()
}

/// firewire_disk_problem: (disk, problems) for a FireWire disk whose queue
/// settings are unsafe.
pub fn firewire_disk_problem(disk: &str) -> Option<(String, Vec<String>)> {
    let tran = lsblk_tran(disk)?;
    if tran != "sbp" && tran != "ieee1394" {
        return None;
    }
    let msk = read_int(&format!("/sys/block/{}/queue/max_sectors_kb", disk))?;
    let rak = read_int(&format!("/sys/block/{}/queue/read_ahead_kb", disk))?;
    let mut bad = Vec::new();
    if msk > 4 {
        bad.push(format!("max_sectors_kb={} (need 4)", msk));
    }
    if rak != 0 {
        bad.push(format!("read_ahead_kb={} (need 0)", rak));
    }
    if let Some(qd) = read_int(&format!("/sys/block/{}/device/queue_depth", disk)) {
        if qd != 1 {
            bad.push(format!("queue_depth={} (need 1)", qd));
        }
    }
    if bad.is_empty() { None } else { Some((disk.to_string(), bad)) }
}

/// pin_firewire_queue: write the safe settings (root, or through sudo).
pub fn pin_firewire_queue(disk: &str) -> bool {
    let qd = format!("/sys/block/{}/device/queue_depth", disk);
    let script = format!(
        "echo 4 >/sys/block/{d}/queue/max_sectors_kb && echo 0 >/sys/block/{d}/queue/read_ahead_kb && {{ [ \"$(cat {qd} 2>/dev/null)\" = \"1\" ] || echo 1 >{qd}; }}",
        d = disk, qd = qd);
    let mut cmd: Vec<String> = vec!["sh".into(), "-c".into(), script];
    if !is_admin() {
        eprintln!("flashpod: pinning safe FireWire I/O settings on {} (needs sudo)...", disk);
        let mut pre: Vec<String> = if term::stdin_isatty() { vec!["sudo".into()] } else { vec!["sudo".into(), "-n".into()] };
        pre.extend(cmd);
        cmd = pre;
    }
    call(&cmd) == Some(0)
}

/// ensure_firewire_disk_safe: True when `dev`'s disk isn't FireWire, or is
/// pinned now.
pub fn ensure_firewire_disk_safe(dev: &str) -> bool {
    let disk = disk_of_dev(dev);
    if firewire_disk_problem(&disk).is_none() {
        return true;
    }
    pin_firewire_queue(&disk);
    firewire_disk_problem(&disk).is_none()
}

/// firewire_queue_problem: firewire_disk_problem for the disk behind `mount`.
pub fn firewire_queue_problem(mount: &str) -> Option<(String, Vec<String>)> {
    let text = std::fs::read(mounts_file()).ok()?;
    let mut dev = None;
    for line in String::from_utf8_lossy(&text).lines() {
        let p: Vec<&str> = line.split_whitespace().collect();
        if p.len() >= 2 {
            // re.sub(r"\\([0-7]{3})", chr(octal), ...)
            let mnt = unoctal(p[1]);
            if mnt == mount {
                dev = Some(p[0].to_string());
            }
        }
    }
    let dev = dev?;
    if !dev.starts_with("/dev/") {
        return None;
    }
    firewire_disk_problem(&disk_of_dev(&dev))
}

fn unoctal(s: &str) -> String {
    let c: Vec<char> = s.chars().collect();
    let mut out = String::new();
    let mut i = 0;
    while i < c.len() {
        if c[i] == '\\' && i + 4 <= c.len() && c[i + 1..i + 4].iter().all(|x| ('0'..='7').contains(x)) {
            let v = u32::from_str_radix(&c[i + 1..i + 4].iter().collect::<String>(), 8).unwrap();
            out.push(char::from_u32(v).unwrap_or('?'));
            i += 4;
        } else {
            out.push(c[i]);
            i += 1;
        }
    }
    out
}

pub fn is_firewire_disk(dev: &str) -> bool {
    matches!(lsblk_tran(&disk_of_dev(dev)).as_deref(), Some("sbp") | Some("ieee1394"))
}

/// firewire_sbp2_needed: a remote FireWire device on the bus and no driver.
pub fn firewire_sbp2_needed() -> bool {
    if std::path::Path::new("/sys/module/firewire_sbp2").is_dir() {
        return false;
    }
    let dir = "/sys/bus/firewire/devices";
    let names = match std::fs::read_dir(dir) {
        Ok(r) => r.filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned())).collect::<Vec<_>>(),
        Err(_) => return false,
    };
    names.iter().filter(|n| !n.contains('.')).any(|n| {
        std::fs::read_to_string(format!("{}/{}/is_local", dir, n)).map(|s| s.trim() == "0").unwrap_or(false)
    })
}

/// _wait_for_firewire_disk.
fn wait_for_firewire_disk(timeout: f64) -> Option<String> {
    let deadline = window::monotonic() + timeout;
    loop {
        if let Some(o) = Command::new("lsblk").args(["-J", "-o", "NAME,TYPE,TRAN"]).stdin(Stdio::null())
            .stderr(Stdio::null()).output().ok()
        {
            if let Ok(v) = serde_json::from_slice::<Value>(&o.stdout) {
                for d in v["blockdevices"].as_array().into_iter().flatten() {
                    if matches!(d["type"].as_str(), Some("disk") | Some("rbc"))
                        && matches!(d["tran"].as_str(), Some("sbp") | Some("ieee1394"))
                    {
                        window::sleep(1.0);
                        return d["name"].as_str().map(String::from);
                    }
                }
            }
        }
        if window::monotonic() >= deadline {
            return None;
        }
        window::sleep(0.4);
    }
}

/// load_firewire_sbp2.
pub fn load_firewire_sbp2() -> bool {
    let mut cmd: Vec<String> = vec!["modprobe".into(), "firewire-sbp2".into()];
    if !is_admin() {
        if !term::stdin_isatty() {
            eprintln!("flashpod: a FireWire device is attached but firewire_sbp2 isn't loaded, so it can't appear as a disk. Load it with:\n  sudo modprobe firewire-sbp2");
            return false;
        }
        eprintln!("flashpod: a FireWire device is attached but firewire_sbp2 isn't loaded, so it can't appear as a disk \u{2014} loading it (you may be prompted for your password)...");
        cmd.insert(0, "sudo".into());
    } else {
        eprintln!("flashpod: loading firewire_sbp2 so the attached FireWire device appears as a disk...");
    }
    match call(&cmd) {
        Some(0) => {}
        Some(_) => {
            eprintln!("flashpod: failed to load firewire_sbp2.");
            return false;
        }
        None => {
            eprintln!("flashpod: couldn't load firewire_sbp2 ([Errno 2] No such file or directory: '{}').", cmd[0]);
            return false;
        }
    }
    if let Some(disk) = wait_for_firewire_disk(8.0) {
        ensure_firewire_disk_safe(&disk);
    }
    std::path::Path::new("/sys/module/firewire_sbp2").is_dir()
}

/// _hint_if_bridge_collapsed.
pub fn hint_if_bridge_collapsed() {
    let names = match std::fs::read_dir("/sys/block") {
        Ok(r) => r.filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned())).collect::<Vec<_>>(),
        Err(_) => return,
    };
    for name in names {
        if read_int(&format!("/sys/block/{}/size", name)) != Some(0) {
            continue;
        }
        if matches!(lsblk_tran(&name).as_deref(), Some("sbp") | Some("ieee1394")) {
            eprintln!("flashpod: /dev/{} is a FireWire device reporting 0 bytes \u{2014} its bridge has collapsed (this iPod's bridge does that after large/queued I/O). Unplug the iPod, plug it back in, and retry.", name);
        }
    }
}

/// _self_cmd: a compiled flashpod re-runs its own binary, as a frozen
/// (PyInstaller) build does with sys.executable.
pub fn self_cmd() -> Vec<String> {
    vec![std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| "flashpod".into())]
}

/// _sudo_reexec: replace this process with flashpod under sudo, the
/// FLASHPOD_* settings carried through `env`. Returns only when it can't.
pub fn sudo_reexec(extra: &[String]) {
    if !term::stdin_isatty() {
        return;
    }
    let mut pass: Vec<(String, String)> = std::env::vars().filter(|(k, _)| k.starts_with("FLASHPOD_")).collect();
    pass.sort();
    let mut cmd: Vec<String> = vec!["sudo".into()];
    if !pass.is_empty() {
        cmd.push("env".into());
        cmd.extend(pass.into_iter().map(|(k, v)| format!("{}={}", k, v)));
    }
    cmd.extend(self_cmd());
    cmd.extend(extra.iter().cloned());
    let _ = std::io::Write::flush(&mut std::io::stdout());
    let err = std::os::unix::process::CommandExt::exec(Command::new("sudo").args(&cmd[1..]));
    let text = match err.raw_os_error() {
        Some(n) => {
            let m = err.to_string();
            format!("[Errno {}] {}: 'sudo'", n, m.split(" (os error").next().unwrap_or(&m))
        }
        None => err.to_string(),
    };
    eprintln!("flashpod: couldn't run sudo ({}); re-run it yourself:\n  {}", text, cmd.join(" "));
}
