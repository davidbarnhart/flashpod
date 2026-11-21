//! `flashpod eject [TARGET]` (#123): make sure an iPod or card is safe to
//! unplug. Each step is checked, not assumed:
//!
//! 1. TARGET (a mountpoint, a partition or a disk; none = the chooser) is
//!    resolved to its whole disk, and that disk is shown.
//! 2. Open files: any process holding a file, a working directory, a memory
//!    map or the device node itself on that disk is named, and eject stops.
//!    Nothing is killed and nothing is unmounted.
//! 3. sync, then unmount every mount of every partition of the disk.
//! 4. Flush the disk's buffers, then re-check: nothing of it mounted (in any
//!    mount namespace), the kernel's in-flight counters 0, no holders.
//! 5. Stop it: SCSI STOP UNIT (a FireWire iPod then shows "OK to
//!    disconnect"), USB port power-off, or loop detach; then confirm the
//!    stop took (the disk reports not ready, or is gone).
//! 6. "Safe to unplug" only when 2-5 all passed; otherwise the failed step.
//!
//! macOS does the same through diskutil and lsof. Windows: not built.

use crate::argparse::{Ns, Val};

/// The verdict lines. Tests match these exactly: keep "Safe" and "NOT safe"
/// distinct at the start of the line.
#[cfg_attr(windows, allow(dead_code))]
fn safe(node: &str) -> i32 {
    println!("Safe to unplug {}.", node);
    0
}

#[cfg_attr(windows, allow(dead_code))]
fn step_ok(n: u32, name: &str, msg: &str) {
    println!("  ok    {} {}: {}", n, name, msg);
}

#[cfg_attr(windows, allow(dead_code))]
fn step_fail(node: &str, n: u32, name: &str, why: &str, advice: &str) -> i32 {
    println!("  FAIL  {} {}: {}", n, name, why);
    println!("NOT safe to unplug {}: step {} ({}) failed. {}", node, n, name, advice);
    1
}

pub fn cmd_eject(ns: &Ns) -> i32 {
    let target = match ns.get("target") {
        Some(Val::Str(s)) if !s.is_empty() => Some(s.clone()),
        _ => None,
    };
    imp::run(target.as_deref())
}

/// Built only with `--features eject-faults`, for scripts/test_eject.py
/// --mutants: FLASHPOD_EJECT_FAULT names one check to skip, so the test can
/// show it would catch that check missing. Release builds have no hook.
#[cfg(feature = "eject-faults")]
#[allow(dead_code)]
fn fault(name: &str) -> bool {
    std::env::var("FLASHPOD_EJECT_FAULT").map(|v| v == name).unwrap_or(false)
}

#[cfg(not(feature = "eject-faults"))]
#[allow(dead_code)]
fn fault(_name: &str) -> bool {
    false
}

/// Ask for root by re-running `flashpod eject NODE` under sudo. Returns only
/// when that can't happen.
#[cfg(unix)]
fn need_root(node: &str) -> i32 {
    let argv = vec!["eject".to_string(), node.to_string()];
    if crate::term::stdin_isatty() {
        eprintln!("flashpod eject: seeing every program's open files needs root \u{2014} elevating via sudo...");
        crate::platform::current().elevate(&argv);
    }
    eprintln!("flashpod eject: seeing every program's open files needs root.\n  sudo {}",
              crate::host::self_cmd().into_iter().chain(argv).collect::<Vec<_>>().join(" "));
    1
}

// ====================================================================== Linux

#[cfg(target_os = "linux")]
mod imp {
    use super::{fault, need_root, safe, step_fail, step_ok};
    use crate::host::{self, capture, capture_timeout, have};
    use crate::term;
    use std::collections::BTreeSet;
    use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt};
    use std::path::Path;
    use std::time::{Duration, Instant};

    type Dev = (u32, u32);

    /// glibc's major()/minor() over a dev_t as std reports it.
    fn dev_of(d: u64) -> Dev {
        let major = ((d >> 32) & 0xffff_f000) | ((d >> 8) & 0xfff);
        let minor = ((d >> 12) & 0xffff_ff00) | (d & 0xff);
        (major as u32, minor as u32)
    }

    fn parse_dev(s: &str, radix: u32) -> Option<Dev> {
        let (a, b) = s.trim().split_once(':')?;
        Some((u32::from_str_radix(a, radix).ok()?, u32::from_str_radix(b, radix).ok()?))
    }

    fn sysfs(path: &str) -> String {
        std::fs::read_to_string(path).map(|s| s.trim().to_string()).unwrap_or_default()
    }

    /// The \NNN octal escapes of /proc/*/mountinfo.
    fn unoctal(s: &str) -> String {
        let b = s.as_bytes();
        let mut out = Vec::with_capacity(b.len());
        let mut i = 0;
        while i < b.len() {
            if b[i] == b'\\' && i + 3 < b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
                out.push(((b[i + 1] - b'0') << 6) | ((b[i + 2] - b'0') << 3) | (b[i + 3] - b'0'));
                i += 4;
            } else {
                out.push(b[i]);
                i += 1;
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    }

    /// The block device name (sdb2) behind major:minor, via /sys/dev/block.
    fn name_of(d: Dev) -> Option<String> {
        let p = std::fs::canonicalize(format!("/sys/dev/block/{}:{}", d.0, d.1)).ok()?;
        p.file_name().map(|n| n.to_string_lossy().into_owned())
    }

    /// The whole disk a block device belongs to (itself when it is one).
    fn whole_disk(name: &str) -> String {
        let p = format!("/sys/class/block/{}", name);
        if Path::new(&format!("{}/partition", p)).exists() {
            if let Some(parent) = std::fs::canonicalize(&p).ok().and_then(|r| r.parent().map(|x| x.to_path_buf())) {
                if let Some(n) = parent.file_name() {
                    return n.to_string_lossy().into_owned();
                }
            }
        }
        name.to_string()
    }

    #[derive(Clone, Copy, PartialEq, Debug)]
    enum Kind {
        Loop,
        Usb,
        Scsi,
        Slot,
    }

    struct Disk {
        name: String,
        node: String,
        /// The disk and each partition: (name, major:minor).
        devs: Vec<(String, Dev)>,
        kind: Kind,
    }

    impl Disk {
        fn set(&self) -> BTreeSet<Dev> {
            self.devs.iter().map(|(_, d)| *d).collect()
        }
    }

    pub(super) struct Mount {
        dev: Dev,
        point: String,
    }

    pub(super) fn parse_mountinfo(text: &str) -> Vec<Mount> {
        text.lines()
            .filter_map(|l| {
                let f: Vec<&str> = l.split(' ').collect();
                Some(Mount { dev: parse_dev(f.get(2)?, 10)?, point: unoctal(f.get(4)?) })
            })
            .collect()
    }

    fn own_mounts() -> Vec<Mount> {
        parse_mountinfo(&std::fs::read_to_string("/proc/self/mountinfo").unwrap_or_default())
    }

    fn pids() -> Vec<i32> {
        let me = std::process::id() as i32;
        let mut v: Vec<i32> = std::fs::read_dir("/proc")
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| e.file_name().to_str().and_then(|n| n.parse().ok()))
            .filter(|&p| p != me)
            .collect();
        v.sort();
        v
    }

    fn prog(pid: i32) -> String {
        sysfs(&format!("/proc/{}/comm", pid))
    }

    /// Mounts of the disk in OTHER mount namespaces (a container or a
    /// service with private mounts keeps the filesystem alive after our
    /// unmount): (mountpoint, pid, program).
    fn foreign_mounts(set: &BTreeSet<Dev>) -> Vec<(String, i32, String)> {
        let own = std::fs::read_link("/proc/self/ns/mnt").ok();
        let mut seen = BTreeSet::new();
        let mut out = Vec::new();
        for pid in pids() {
            let ns = match std::fs::read_link(format!("/proc/{}/ns/mnt", pid)) {
                Ok(n) => n,
                Err(_) => continue,
            };
            if Some(&ns) == own.as_ref() || !seen.insert(ns) {
                continue;
            }
            let text = std::fs::read_to_string(format!("/proc/{}/mountinfo", pid)).unwrap_or_default();
            for m in parse_mountinfo(&text) {
                if set.contains(&m.dev) {
                    out.push((m.point, pid, prog(pid)));
                }
            }
        }
        out
    }

    /// Every mount of the disk anywhere: ours, then other namespaces'.
    fn mounts_left(set: &BTreeSet<Dev>) -> Vec<String> {
        let mut v: Vec<String> = own_mounts().into_iter().filter(|m| set.contains(&m.dev)).map(|m| m.point).collect();
        for (p, pid, prog) in foreign_mounts(set) {
            v.push(format!("{} (in another mount namespace: pid {} {})", p, pid, prog));
        }
        v
    }

    pub(super) struct Holder {
        pid: i32,
        prog: String,
        how: &'static str,
        path: String,
    }

    impl std::fmt::Display for Holder {
        fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            if self.pid == 0 {
                write!(f, "{}: {} {}", self.prog, self.how, self.path)
            } else {
                write!(f, "pid {} ({}): {} {}", self.pid, self.prog, self.how, self.path)
            }
        }
    }

    /// One /proc/PID/maps line's (major:minor, path) when it maps a file.
    pub(super) fn map_line(line: &str) -> Option<(Dev, String)> {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 6 || f[4] == "0" {
            return None;
        }
        Some((parse_dev(f[3], 16)?, f[5..].join(" ")))
    }

    /// Who holds anything on the disk: processes (open files, working or
    /// root directories, program files, memory maps, the device nodes), swap,
    /// kernel stacking (dm/md) and loop devices backed by a file on it.
    /// The second value lists processes we weren't allowed to look into.
    fn holders(disk: &Disk) -> (Vec<Holder>, Vec<i32>) {
        let set = disk.set();
        let mut out: Vec<Holder> = Vec::new();
        let mut denied = Vec::new();
        if fault("no-holder-check") {
            return (out, denied);
        }
        let denied_err = |e: &std::io::Error| e.kind() == std::io::ErrorKind::PermissionDenied;
        for pid in pids() {
            let mut blind = false;
            let mut seen = BTreeSet::new();
            let mut push = |out: &mut Vec<Holder>, how: &'static str, path: String| {
                if seen.insert((how, path.clone())) {
                    out.push(Holder { pid, prog: prog(pid), how, path });
                }
            };
            for (link, how) in [("cwd", "working directory"), ("root", "root directory"), ("exe", "program file")] {
                let p = format!("/proc/{}/{}", pid, link);
                match std::fs::metadata(&p) {
                    Ok(m) if set.contains(&dev_of(m.dev())) => {
                        let t = std::fs::read_link(&p).map(|t| t.to_string_lossy().into_owned()).unwrap_or_default();
                        push(&mut out, how, t);
                    }
                    Err(e) if denied_err(&e) => blind = true,
                    _ => {}
                }
            }
            match std::fs::read_dir(format!("/proc/{}/fd", pid)) {
                Ok(rd) => {
                    for e in rd.flatten() {
                        let p = e.path();
                        let m = match std::fs::metadata(&p) {
                            Ok(m) => m,
                            Err(_) => continue,
                        };
                        let t = || std::fs::read_link(&p).map(|t| t.to_string_lossy().into_owned()).unwrap_or_default();
                        if m.file_type().is_block_device() && set.contains(&dev_of(m.rdev())) {
                            push(&mut out, "has the device open:", t());
                        } else if set.contains(&dev_of(m.dev())) {
                            push(&mut out, "open file", t());
                        }
                    }
                }
                Err(e) if denied_err(&e) => blind = true,
                Err(_) => {}
            }
            match std::fs::read_to_string(format!("/proc/{}/maps", pid)) {
                Ok(text) => {
                    for (d, path) in text.lines().filter_map(map_line) {
                        if set.contains(&d) {
                            push(&mut out, "memory-mapped file", path);
                        }
                    }
                }
                Err(e) if denied_err(&e) => blind = true,
                Err(_) => {}
            }
            if blind {
                denied.push(pid);
            }
        }
        // swap on a partition, or a swap file on it
        for line in std::fs::read_to_string("/proc/swaps").unwrap_or_default().lines().skip(1) {
            if let Some(p) = line.split_whitespace().next() {
                let p = unoctal(p);
                if let Ok(m) = std::fs::metadata(&p) {
                    if (m.file_type().is_block_device() && set.contains(&dev_of(m.rdev()))) || set.contains(&dev_of(m.dev())) {
                        out.push(Holder { pid: 0, prog: "kernel".into(), how: "swap in use on", path: p });
                    }
                }
            }
        }
        // dm/md/lvm stacked on the disk or a partition
        for (name, _) in &disk.devs {
            let dir = if *name == disk.name { format!("/sys/block/{}/holders", name) } else { format!("/sys/block/{}/{}/holders", disk.name, name) };
            for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
                out.push(Holder { pid: 0, prog: "kernel".into(), how: "device stacked on it:", path: e.file_name().to_string_lossy().into_owned() });
            }
        }
        // a loop device whose backing file lives on this disk
        for e in std::fs::read_dir("/sys/block").into_iter().flatten().flatten() {
            let n = e.file_name().to_string_lossy().into_owned();
            if !n.starts_with("loop") || n == disk.name {
                continue;
            }
            let bf = sysfs(&format!("/sys/block/{}/loop/backing_file", n));
            if bf.is_empty() {
                continue;
            }
            if let Ok(m) = std::fs::metadata(bf.trim_end_matches(" (deleted)")) {
                if set.contains(&dev_of(m.dev())) {
                    out.push(Holder { pid: 0, prog: "kernel".into(), how: "loop device backed by a file on it:", path: format!("/dev/{} <- {}", n, bf) });
                }
            }
        }
        (out, denied)
    }

    fn inflight(disk: &Disk) -> u64 {
        disk.devs.iter().map(|(n, _)| {
            sysfs(&format!("/sys/class/block/{}/inflight", n)).split_whitespace().filter_map(|x| x.parse::<u64>().ok()).sum::<u64>()
        }).sum()
    }

    // ------------------------------------------------------------ SCSI (SG_IO)

    #[repr(C)]
    struct SgIoHdr {
        interface_id: libc::c_int,
        dxfer_direction: libc::c_int,
        cmd_len: libc::c_uchar,
        mx_sb_len: libc::c_uchar,
        iovec_count: libc::c_ushort,
        dxfer_len: libc::c_uint,
        dxferp: *mut libc::c_void,
        cmdp: *const libc::c_uchar,
        sbp: *mut libc::c_uchar,
        timeout: libc::c_uint,
        flags: libc::c_uint,
        pack_id: libc::c_int,
        usr_ptr: *mut libc::c_void,
        status: libc::c_uchar,
        masked_status: libc::c_uchar,
        msg_status: libc::c_uchar,
        sb_len_wr: libc::c_uchar,
        host_status: libc::c_ushort,
        driver_status: libc::c_ushort,
        resid: libc::c_int,
        duration: libc::c_uint,
        info: libc::c_uint,
    }

    const SG_IO: libc::c_ulong = 0x2285;
    const SG_DXFER_NONE: libc::c_int = -1;
    const BLKFLSBUF: libc::c_ulong = 0x1261;

    /// A no-data SCSI command's outcome.
    pub(super) struct Scsi {
        status: u8,
        host: u16,
        driver: u16,
        sense: Vec<u8>,
    }

    impl Scsi {
        fn good(&self) -> bool {
            self.status == 0 && self.host == 0 && (self.driver & 0x0f) == 0
        }
        /// (sense key, ASC, ASCQ) from fixed or descriptor sense data.
        pub(super) fn sense(&self) -> Option<(u8, u8, u8)> {
            let s = &self.sense;
            match s.first().map(|b| b & 0x7f) {
                Some(0x70) | Some(0x71) if s.len() >= 14 => Some((s[2] & 0x0f, s[12], s[13])),
                Some(0x72) | Some(0x73) if s.len() >= 4 => Some((s[1] & 0x0f, s[2], s[3])),
                _ => None,
            }
        }
        fn describe(&self) -> String {
            match self.sense() {
                Some((k, a, q)) => format!("sense key {:#x}, ASC/ASCQ {:02x}/{:02x}", k, a, q),
                None => format!("status {:#x}, host {:#x}, driver {:#x}", self.status, self.host, self.driver),
            }
        }
    }

    fn sg(f: &std::fs::File, cdb: &[u8], timeout_ms: u32) -> std::io::Result<Scsi> {
        use std::os::unix::io::AsRawFd;
        let mut sense = [0u8; 32];
        let mut h = SgIoHdr {
            interface_id: b'S' as libc::c_int,
            dxfer_direction: SG_DXFER_NONE,
            cmd_len: cdb.len() as u8,
            mx_sb_len: sense.len() as u8,
            iovec_count: 0,
            dxfer_len: 0,
            dxferp: std::ptr::null_mut(),
            cmdp: cdb.as_ptr(),
            sbp: sense.as_mut_ptr(),
            timeout: timeout_ms,
            flags: 0,
            pack_id: 0,
            usr_ptr: std::ptr::null_mut(),
            status: 0,
            masked_status: 0,
            msg_status: 0,
            sb_len_wr: 0,
            host_status: 0,
            driver_status: 0,
            resid: 0,
            duration: 0,
            info: 0,
        };
        if unsafe { libc::ioctl(f.as_raw_fd(), SG_IO as _, &mut h as *mut SgIoHdr) } < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Scsi { status: h.status, host: h.host_status, driver: h.driver_status, sense: sense[..h.sb_len_wr as usize].to_vec() })
    }

    /// Open a block node read-only without waiting for media: a read-only
    /// close doesn't make udev re-probe the disk.
    fn open_node(node: &str) -> std::io::Result<std::fs::File> {
        std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK).open(node)
    }

    /// SYNCHRONIZE CACHE(10): the disk's own write cache to the medium.
    fn sync_cache(node: &str) -> Result<&'static str, String> {
        let f = open_node(node).map_err(|e| format!("couldn't open {}: {}", node, e))?;
        let r = sg(&f, &[0x35, 0, 0, 0, 0, 0, 0, 0, 0, 0], 60_000).map_err(|e| format!("SYNCHRONIZE CACHE: {}", e))?;
        if r.good() {
            return Ok("its write cache synchronized");
        }
        if matches!(r.sense(), Some((0x05, _, _))) {
            return Ok("it has no write cache to synchronize");
        }
        Err(format!("SYNCHRONIZE CACHE failed ({})", r.describe()))
    }

    /// START STOP UNIT (stop; eject where the medium is removable), then
    /// TEST UNIT READY must say NOT READY.
    fn scsi_stop(node: &str) -> Result<String, String> {
        let f = open_node(node).map_err(|e| format!("couldn't open {}: {}", node, e))?;
        let _ = sg(&f, &[0x1e, 0, 0, 0, 0, 0], 10_000); // ALLOW MEDIUM REMOVAL; not every disk knows it
        let mut r = sg(&f, &[0x1b, 0, 0, 0, 0x02, 0], 30_000).map_err(|e| format!("STOP UNIT: {}", e))?;
        if !r.good() && matches!(r.sense(), Some((0x05, _, _))) {
            // LoEj is only for removable media; some disks refuse it
            r = sg(&f, &[0x1b, 0, 0, 0, 0x00, 0], 30_000).map_err(|e| format!("STOP UNIT: {}", e))?;
        }
        if !r.good() {
            return Err(format!("the disk refused STOP UNIT ({})", r.describe()));
        }
        let t = sg(&f, &[0, 0, 0, 0, 0, 0], 10_000).map_err(|e| format!("TEST UNIT READY: {}", e))?;
        if t.good() {
            return Err("the disk accepted STOP UNIT but still reports ready".into());
        }
        match t.sense() {
            Some((0x02, a, q)) => Ok(format!("SCSI STOP UNIT sent; the disk now reports not ready (ASC/ASCQ {:02x}/{:02x})", a, q)),
            _ => Err(format!("after STOP UNIT, TEST UNIT READY gave {} instead of NOT READY", t.describe())),
        }
    }

    fn poll(secs: f64, mut done: impl FnMut() -> bool) -> bool {
        let end = Instant::now() + Duration::from_secs_f64(secs);
        loop {
            if done() {
                return true;
            }
            if Instant::now() >= end {
                return false;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
    }

    // ------------------------------------------------------------ step 1

    fn usage_error() -> i32 {
        eprintln!("flashpod eject: name the iPod or card to eject: a mountpoint, a partition or a disk (no terminal to choose one on).");
        2
    }

    /// TARGET -> whole-disk name, or an exit code (the message printed).
    fn resolve(arg: Option<&str>) -> Result<String, i32> {
        let arg = match arg {
            Some(a) => a,
            None => {
                if !term::stdin_isatty() {
                    return Err(usage_error());
                }
                return match crate::devices::choose_eject() {
                    Ok(d) => Ok(whole_disk(d.trim_start_matches("/dev/"))),
                    Err(crate::flash::Exit(m)) => {
                        eprintln!("{}", m);
                        Err(1)
                    }
                };
            }
        };
        let m = match std::fs::metadata(arg) {
            Ok(m) => m,
            Err(_) => {
                eprintln!("flashpod eject: no such mountpoint or disk: {}", arg);
                return Err(1);
            }
        };
        let ft = m.file_type();
        let dev = if ft.is_block_device() {
            dev_of(m.rdev())
        } else if ft.is_dir() {
            dev_of(m.dev())
        } else if ft.is_file() {
            eprintln!("Target: {} (image file)", arg);
            eprintln!("flashpod eject: {} is an image file; it isn't attached, so there is nothing to eject.", arg);
            return Err(1);
        } else {
            eprintln!("flashpod eject: {} is neither a mountpoint nor a disk.", arg);
            return Err(1);
        };
        match name_of(dev) {
            Some(n) => Ok(whole_disk(&n)),
            None => {
                eprintln!("flashpod eject: {} isn't on a disk (nothing to eject).", arg);
                Err(1)
            }
        }
    }

    /// Show the disk; refuse what eject can't or mustn't stop.
    fn describe(name: &str) -> Result<Disk, i32> {
        let node = format!("/dev/{}", name);
        eprintln!("Target: {} (drive)", node);
        let sys = format!("/sys/block/{}", name);
        if !Path::new(&sys).is_dir() {
            eprintln!("flashpod eject: {} isn't a disk this computer knows.", node);
            return Err(1);
        }
        if let Ok(roots) = crate::devices::root_disk_names() {
            if roots.iter().any(|r| r == name) {
                eprintln!("flashpod eject: refusing: {} backs the running system.", node);
                return Err(1);
            }
        }
        if sysfs(&format!("{}/size", sys)) == "0" {
            eprintln!("flashpod eject: {} reports 0 bytes (an empty reader slot, a collapsed bridge, or the idle lead of a FireWire+USB cable). There's nothing to flush; unplug it. Stopping it can hang.", node);
            return Err(1);
        }
        let kind = if name.starts_with("loop") {
            if sysfs(&format!("{}/loop/backing_file", sys)).is_empty() {
                eprintln!("flashpod eject: {} isn't attached to anything.", node);
                return Err(1);
            }
            Kind::Loop
        } else if name.starts_with("mmcblk") {
            Kind::Slot
        } else if Path::new(&format!("{}/device/scsi_level", sys)).exists() {
            let path = std::fs::canonicalize(format!("{}/device", sys)).map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
            if path.contains("/usb") { Kind::Usb } else { Kind::Scsi }
        } else {
            eprintln!("flashpod eject: flashpod can't stop a disk like {} (it ejects SCSI, USB and FireWire disks, card slots and loop devices).", node);
            return Err(1);
        };
        let mut devs = vec![(name.to_string(), parse_dev(&sysfs(&format!("{}/dev", sys)), 10).unwrap_or((0, 0)))];
        let mut parts: Vec<String> = std::fs::read_dir(&sys).into_iter().flatten().flatten()
            .filter(|e| e.path().join("partition").exists())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .collect();
        parts.sort();
        for p in &parts {
            devs.push((p.clone(), parse_dev(&sysfs(&format!("{}/{}/dev", sys, p)), 10).unwrap_or((0, 0))));
        }
        let what = match kind {
            Kind::Loop => format!("loop device ({})", sysfs(&format!("{}/loop/backing_file", sys))),
            _ => {
                let model = sysfs(&format!("{}/device/model", sys));
                let vendor = sysfs(&format!("{}/device/vendor", sys));
                let id = format!("{} {}", vendor, model).trim().to_string();
                let via = match kind {
                    Kind::Usb => "USB",
                    Kind::Slot => "card slot",
                    _ if host::is_firewire_disk(&node) => "FireWire",
                    _ => "SCSI",
                };
                if id.is_empty() { via.to_string() } else { format!("{}, {}", id, via) }
            }
        };
        let sectors: u64 = sysfs(&format!("{}/size", sys)).parse().unwrap_or(0);
        eprintln!("  {}, {}; {}", what, crate::flash::fmt_size(sectors * 512),
                  if parts.is_empty() { "no partitions".to_string() } else { format!("partitions {}", parts.join(", ")) });
        Ok(Disk { name: name.to_string(), node, devs, kind })
    }

    // ------------------------------------------------------------ run

    pub fn run(arg: Option<&str>) -> i32 {
        let name = match resolve(arg) {
            Ok(n) => n,
            Err(rc) => return rc,
        };
        let disk = match describe(&name) {
            Ok(d) => d,
            Err(rc) => return rc,
        };
        if !host::is_admin() {
            return need_root(&disk.node);
        }
        // our own working directory mustn't hold the disk
        let _ = std::env::set_current_dir("/");
        let node = disk.node.clone();
        let set = disk.set();

        // 2. open files
        let (held, blind) = holders(&disk);
        if !held.is_empty() {
            for h in &held {
                println!("        {}", h);
            }
            return step_fail(&node, 2, "open files", &format!("{} hold(s) on {}", held.len(), node),
                             "Close those (flashpod killed nothing and unmounted nothing), then run flashpod eject again.");
        }
        if !blind.is_empty() {
            return step_fail(&node, 2, "open files", &format!("couldn't look into {} process(es)", blind.len()),
                             "Run it as root so every process can be checked.");
        }
        step_ok(2, "open files", "nothing on it is open");

        // 3. sync, then unmount every mount of every partition
        crate::sys::sync_all();
        if host::is_firewire_disk(&node) && !host::ensure_firewire_disk_safe(&node) {
            return step_fail(&node, 3, "unmount", "couldn't set the safe FireWire queue settings",
                             "Unmounting makes udev re-read the disk, which at default settings can crash the iPod's bridge. Nothing was unmounted.");
        }
        let mut mounts: Vec<Mount> = own_mounts().into_iter().filter(|m| set.contains(&m.dev)).collect();
        // innermost first
        mounts.sort_by_key(|m| std::cmp::Reverse(m.point.matches('/').count()));
        let mut failed = Vec::new();
        let mut done = Vec::new();
        for m in &mounts {
            match capture(&["umount", &m.point]) {
                Some(o) if o.code == Some(0) => done.push(m.point.clone()),
                Some(o) => failed.push(format!("{}: {}", m.point, o.stderr.trim())),
                None => failed.push(format!("{}: couldn't run umount", m.point)),
            }
        }
        if !failed.is_empty() {
            return step_fail(&node, 3, "unmount", &failed.join("; "), "");
        }
        step_ok(3, "unmount", &if done.is_empty() { "nothing was mounted".to_string() } else { format!("unmounted {}", done.join(", ")) });

        // 4. flush, then re-check: no mounts, nothing in flight, no holders
        crate::sys::sync_all();
        let mut flushed = Vec::new();
        for (n, _) in &disk.devs {
            let p = format!("/dev/{}", n);
            let f = match open_node(&p) {
                Ok(f) => f,
                Err(e) => return step_fail(&node, 4, "flush", &format!("couldn't open {}: {}", p, e), ""),
            };
            if let Err(e) = f.sync_all() {
                return step_fail(&node, 4, "flush", &format!("fsync {}: {}", p, e), "");
            }
            use std::os::unix::io::AsRawFd;
            if unsafe { libc::ioctl(f.as_raw_fd(), BLKFLSBUF as _, 0) } < 0 {
                return step_fail(&node, 4, "flush", &format!("flushing {}: {}", p, std::io::Error::last_os_error()), "");
            }
            flushed.push(n.clone());
        }
        let mut cache = String::new();
        if matches!(disk.kind, Kind::Scsi | Kind::Usb) {
            match sync_cache(&node) {
                Ok(m) => cache = format!(", {}", m),
                Err(e) => return step_fail(&node, 4, "flush", &e, ""),
            }
        }
        if have("udevadm") {
            let _ = capture_timeout(&["udevadm", "settle", "--timeout=10"], 15.0);
        }
        let mut left = Vec::new();
        let mut busy = 0;
        let mut late: Vec<String> = Vec::new();
        let clean = poll(5.0, || {
            left = if fault("no-mount-recheck") { Vec::new() } else { mounts_left(&set) };
            busy = inflight(&disk);
            late = holders(&disk).0.iter().map(|h| h.to_string()).collect();
            left.is_empty() && busy == 0 && late.is_empty()
        });
        if !clean {
            let mut why = Vec::new();
            if !left.is_empty() {
                why.push(format!("still mounted: {}", left.join(", ")));
            }
            if busy != 0 {
                why.push(format!("{} request(s) still in flight", busy));
            }
            if !late.is_empty() {
                why.push(format!("held again: {}", late.join("; ")));
            }
            return step_fail(&node, 4, "flush", &why.join("; "), "");
        }
        step_ok(4, "flush", &format!("buffers flushed ({}){}; nothing mounted, 0 in flight", flushed.join(", "), cache));

        // 5. stop
        let stopped = match disk.kind {
            Kind::Loop => {
                let r = capture(&["losetup", "-d", &node]);
                let bf = format!("/sys/block/{}/loop/backing_file", disk.name);
                if poll(5.0, || sysfs(&bf).is_empty()) {
                    Ok("loop device detached".to_string())
                } else {
                    Err(format!("still attached after losetup -d{}",
                                r.filter(|o| o.code != Some(0)).map(|o| format!(" ({})", o.stderr.trim())).unwrap_or_default()))
                }
            }
            Kind::Usb => {
                let gone = || !Path::new(&format!("/sys/block/{}", disk.name)).exists();
                let off = have("udisksctl")
                    && capture(&["udisksctl", "power-off", "--no-user-interaction", "-b", &node]).and_then(|o| o.code) == Some(0);
                if off && poll(10.0, gone) {
                    Ok("USB port powered off; the disk is gone from this computer".to_string())
                } else {
                    scsi_stop(&node).map(|m| format!("{} (the USB port couldn't be powered off; the reader stays visible)", m))
                }
            }
            Kind::Scsi => scsi_stop(&node),
            Kind::Slot => Ok("a card slot has no stop; nothing is mounted or in flight".to_string()),
        };
        match stopped {
            Ok(m) => step_ok(5, "stop", &m),
            Err(e) => return step_fail(&node, 5, "stop", &e, "Everything is unmounted and flushed, but the disk wasn't confirmed stopped."),
        }
        safe(&node)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn dev_numbers_round_trip() {
            // glibc makedev(8, 18) and makedev(259, 1048577)
            assert_eq!(dev_of(0x812), (8, 18));
            let d: u64 = ((259u64 & 0xfff) << 8) | ((1_048_577u64 & 0xff) as u64) | ((1_048_577u64 & 0xffff_ff00) << 12);
            assert_eq!(dev_of(d), (259, 1_048_577));
        }

        #[test]
        fn mountinfo_lines() {
            let t = "36 35 8:18 / /media/u/IPOD\\040X rw,nosuid - vfat /dev/sdb2 rw\n37 35 0:5 / /dev rw - devtmpfs udev rw\n";
            let m = parse_mountinfo(t);
            assert_eq!(m.len(), 2);
            assert_eq!(m[0].dev, (8, 18));
            assert_eq!(m[0].point, "/media/u/IPOD X");
        }

        #[test]
        fn maps_lines() {
            assert_eq!(map_line("7f00-7f10 r--p 00000000 08:12 1234 /media/u/IPOD/a b.so"), Some(((8, 0x12), "/media/u/IPOD/a b.so".into())));
            assert_eq!(map_line("7f00-7f10 rw-p 00000000 00:00 0 "), None);
        }

        #[test]
        fn sense_formats() {
            let mut fixed = vec![0u8; 18];
            fixed[0] = 0x70;
            fixed[2] = 0x02;
            fixed[12] = 0x04;
            fixed[13] = 0x02;
            let s = Scsi { status: 2, host: 0, driver: 0, sense: fixed };
            assert_eq!(s.sense(), Some((2, 4, 2)));
            let d = Scsi { status: 2, host: 0, driver: 0, sense: vec![0x72, 0x05, 0x20, 0x00] };
            assert_eq!(d.sense(), Some((5, 0x20, 0)));
        }

        #[test]
        fn octal_escapes() {
            assert_eq!(unoctal("/a\\040b\\134c"), "/a b\\c");
            assert_eq!(unoctal("/plain"), "/plain");
        }
    }
}

// ====================================================================== macOS

#[cfg(target_os = "macos")]
mod imp {
    use super::{need_root, safe, step_fail, step_ok};
    use crate::host::{self, capture};
    use crate::platform::macos as mac;
    use crate::term;
    use std::path::Path;
    use std::time::{Duration, Instant};

    /// TARGET -> whole disk (disk2), or an exit code (message printed).
    fn resolve(arg: Option<&str>) -> Result<String, i32> {
        let arg = match arg {
            Some(a) => a,
            None => {
                if !term::stdin_isatty() {
                    eprintln!("flashpod eject: name the iPod or card to eject: a mountpoint, a partition or a disk (no terminal to choose one on).");
                    return Err(2);
                }
                return match crate::platform::current().choose_device() {
                    Ok(d) => Ok(mac::whole_disk(&d)),
                    Err(crate::flash::Exit(m)) => {
                        eprintln!("{}", m);
                        Err(1)
                    }
                };
            }
        };
        let p = Path::new(arg);
        if p.is_file() {
            eprintln!("Target: {} (image file)", arg);
            eprintln!("flashpod eject: {} is an image file; it isn't attached, so there is nothing to eject.", arg);
            return Err(1);
        }
        if arg.starts_with("/dev/") || arg.starts_with("disk") || arg.starts_with("rdisk") {
            return Ok(mac::whole_disk(arg));
        }
        if p.is_dir() {
            if let Some(info) = mac::diskutil_info(arg) {
                let parent = mac::text(&info, "ParentWholeDisk");
                if !parent.is_empty() {
                    return Ok(parent);
                }
            }
            eprintln!("flashpod eject: {} isn't on a disk diskutil knows (nothing to eject).", arg);
            return Err(1);
        }
        eprintln!("flashpod eject: no such mountpoint or disk: {}", arg);
        Err(1)
    }

    fn slices(disk: &str) -> Vec<String> {
        let mut v: Vec<String> = std::fs::read_dir("/dev").into_iter().flatten().flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n == disk || n.starts_with(&format!("{}s", disk)))
            .collect();
        v.sort();
        v
    }

    fn mounts_of(disk: &str) -> Vec<String> {
        crate::platform::current().device_mountpoints(&format!("/dev/{}", disk)).into_iter().map(|(_, mp)| mp).collect()
    }

    /// lsof over the disk's mountpoints and its device nodes: "pid N (prog):
    /// path" lines, our own process left out.
    fn holders(disk: &str) -> Vec<String> {
        let me = std::process::id().to_string();
        let mut args: Vec<String> = vec!["lsof".into(), "-F".into(), "pcn".into(), "-w".into(), "--".into()];
        args.extend(mounts_of(disk));
        for s in slices(disk) {
            args.push(format!("/dev/{}", s));
            if Path::new(&format!("/dev/r{}", s)).exists() {
                args.push(format!("/dev/r{}", s));
            }
        }
        let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        let out = match capture(&refs) {
            Some(o) => o.stdout,
            None => return vec!["couldn't run lsof".into()],
        };
        let (mut pid, mut prog) = (String::new(), String::new());
        let mut v = Vec::new();
        for line in out.lines() {
            let (tag, rest) = line.split_at(1.min(line.len()));
            match tag {
                "p" => pid = rest.to_string(),
                "c" => prog = rest.to_string(),
                "n" if pid != me => v.push(format!("pid {} ({}): {}", pid, prog, rest)),
                _ => {}
            }
        }
        v.sort();
        v.dedup();
        v
    }

    pub fn run(arg: Option<&str>) -> i32 {
        let disk = match resolve(arg) {
            Ok(d) => d,
            Err(rc) => return rc,
        };
        let node = format!("/dev/{}", disk);
        eprintln!("Target: {} (drive)", node);
        if let Err(crate::flash::Exit(m)) = crate::platform::current().validate_target(&node) {
            eprintln!("flashpod eject: {}", m);
            return 1;
        }
        match mac::diskutil_info(&node) {
            Some(info) => {
                if mac::size(&info) == 0 {
                    eprintln!("flashpod eject: {} reports 0 bytes (an empty reader slot). There's nothing to flush; unplug it.", node);
                    return 1;
                }
                eprintln!("  {}, {}, {}", mac::text(&info, "MediaName"), mac::text(&info, "BusProtocol"), crate::flash::fmt_size(mac::size(&info)));
            }
            None => {
                eprintln!("flashpod eject: diskutil doesn't know {}.", node);
                return 1;
            }
        }
        if !host::is_admin() {
            return need_root(&node);
        }
        let _ = std::env::set_current_dir("/");

        let held = holders(&disk);
        if !held.is_empty() {
            for h in &held {
                println!("        {}", h);
            }
            return step_fail(&node, 2, "open files", &format!("{} hold(s) on {}", held.len(), node),
                             "Close those (flashpod killed nothing and unmounted nothing), then run flashpod eject again.");
        }
        step_ok(2, "open files", "nothing on it is open");

        crate::sys::sync_all();
        let before = mounts_of(&disk);
        if !before.is_empty() {
            match capture(&["diskutil", "unmountDisk", &node]) {
                Some(o) if o.code == Some(0) => {}
                Some(o) => return step_fail(&node, 3, "unmount", o.stderr.trim().lines().last().unwrap_or("diskutil unmountDisk failed"), ""),
                None => return step_fail(&node, 3, "unmount", "couldn't run diskutil", ""),
            }
        }
        step_ok(3, "unmount", &if before.is_empty() { "nothing was mounted".to_string() } else { format!("unmounted {}", before.join(", ")) });

        crate::sys::sync_all();
        let left = mounts_of(&disk);
        let late = holders(&disk);
        if !left.is_empty() || !late.is_empty() {
            let mut why = Vec::new();
            if !left.is_empty() {
                why.push(format!("still mounted: {}", left.join(", ")));
            }
            if !late.is_empty() {
                why.push(format!("held again: {}", late.join("; ")));
            }
            return step_fail(&node, 4, "flush", &why.join("; "), "");
        }
        step_ok(4, "flush", "synced; nothing mounted (macOS doesn't report in-flight I/O per disk, so that isn't checked)");

        let r = capture(&["diskutil", "eject", &node]);
        let end = Instant::now() + Duration::from_secs(10);
        while Path::new(&node).exists() && Instant::now() < end {
            std::thread::sleep(Duration::from_millis(200));
        }
        if Path::new(&node).exists() {
            let why = r.filter(|o| o.code != Some(0)).map(|o| o.stderr.trim().to_string()).unwrap_or_default();
            return step_fail(&node, 5, "stop", &format!("{} is still there after diskutil eject{}", node,
                             if why.is_empty() { String::new() } else { format!(" ({})", why) }),
                             "Everything is unmounted, but the disk wasn't confirmed ejected.");
        }
        step_ok(5, "stop", "ejected; the disk is gone from this computer");
        safe(&node)
    }
}

// ====================================================================== Windows

#[cfg(windows)]
mod imp {
    pub fn run(_arg: Option<&str>) -> i32 {
        eprintln!("flashpod eject: not built on Windows yet (#123). Use \"Safely Remove Hardware\" in the taskbar.");
        1
    }
}
