//! platform/windows.py: disks through PowerShell's Storage cmdlets, raw
//! \\.\PhysicalDriveN handles with their volumes locked, dismounted and
//! taken offline first, and the partition table re-read through
//! IOCTL_DISK_UPDATE_PROPERTIES. Not yet validated on real hardware
//! (CI drives it against a VHD through scripts/simulate_card.py).

use super::Platform;
use crate::flash::{fmt_size, Exit};
use crate::host::capture;
use crate::term::{color, input_line, C_CYN, C_RED, C_YEL};
use flashpod_core::block::BlockDevice;
use std::io;
use std::sync::Mutex;

const SECTOR: u64 = 512;
const IOCTL_DISK_UPDATE_PROPERTIES: u32 = 0x0007_0140;
const FSCTL_LOCK_VOLUME: u32 = 0x0009_0018;
const FSCTL_DISMOUNT_VOLUME: u32 = 0x0009_0020;
const IOCTL_VOLUME_OFFLINE: u32 = 0x0056_C00C;

pub struct Windows;

/// Volume handles held locked while flashpod writes the disk under them;
/// dropping one releases its lock.
static HELD: Mutex<Vec<(String, std::fs::File)>> = Mutex::new(Vec::new());

fn powershell(script: &str) -> String {
    capture(&["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        .filter(|o| o.code == Some(0)).map(|o| o.stdout).unwrap_or_default()
}

struct Disk {
    number: u32,
    size: u64,
    name: String,
    bus: String,
    system: bool,
}

/// Get-Disk: every disk with its number, size, name, bus and whether it
/// backs the running system.
fn disks() -> Vec<Disk> {
    powershell("Get-Disk | ForEach-Object { \"$($_.Number)|$($_.Size)|$($_.FriendlyName)|$($_.BusType)|$($_.IsSystem)|$($_.IsBoot)\" }")
        .lines().filter_map(|l| {
            let f: Vec<&str> = l.trim().split('|').collect();
            if f.len() < 6 || !f[0].chars().all(|c| c.is_ascii_digit()) || f[0].is_empty() {
                return None;
            }
            Some(Disk {
                number: f[0].parse().ok()?,
                size: f[1].parse().unwrap_or(0),
                name: f[2].trim().to_string(),
                bus: f[3].to_string(),
                system: f[4].trim().eq_ignore_ascii_case("true") || f[5].trim().eq_ignore_ascii_case("true"),
            })
        }).collect()
}

/// Removable/USB disks, else every non-system disk.
fn removable_disks() -> Vec<Disk> {
    let all = disks();
    let (cands, rest): (Vec<Disk>, Vec<Disk>) = all.into_iter().filter(|d| !d.system)
        .partition(|d| matches!(d.bus.as_str(), "USB" | "SD" | "MMC" | "1394"));
    if cands.is_empty() { rest } else { cands }
}

fn drive_number(dev: &str) -> Option<u32> {
    let digits: String = dev.chars().rev().take_while(|c| c.is_ascii_digit()).collect::<Vec<_>>().into_iter().rev().collect();
    digits.parse().ok()
}

fn is_physical_drive(dev: &str) -> bool {
    dev.strip_prefix(r"\\.\PhysicalDrive").is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()))
}

fn open_handle(path: &str, write: bool) -> io::Result<std::fs::File> {
    use std::os::windows::fs::OpenOptionsExt;
    std::fs::OpenOptions::new().read(true).write(write).share_mode(1 | 2).open(path)
        .map_err(|e| io::Error::new(e.kind(), format!("CreateFileW failed for {}: {}", path, e)))
}

fn render(cands: &[Disk]) {
    eprintln!("{}", color("\nAttached removable disks:\n", C_CYN));
    for (i, d) in cands.iter().enumerate() {
        eprintln!("  [{}] \\\\.\\PhysicalDrive{:<3} {:>10}  {} ({})", i, d.number, fmt_size(d.size), d.name, d.bus);
        if d.size == 0 {
            eprintln!("{}", color("        no card inserted \u{2014} empty reader slot", C_YEL));
        }
    }
    eprintln!();
}

/// Lock, dismount and take offline every volume on `dev`, holding the
/// handles so Windows doesn't bring them back mid-write. A refused lock
/// gets a forced dismount (invalidating whoever holds it) and a retry.
fn lock_volumes(dev: &str) -> Vec<(String, Option<String>)> {
    let mut held = HELD.lock().unwrap_or_else(|p| p.into_inner());
    let mut out = Vec::new();
    for (vol, _) in Windows.device_mountpoints(dev) {
        let path = format!(r"\\.\{}", vol.trim_end_matches('\\'));
        if held.iter().any(|(p, _)| *p == path) {
            out.push((path, None));
            continue;
        }
        let h = match open_handle(&path, true) {
            Ok(h) => h,
            Err(e) => {
                out.push((path, Some(format!("open failed: {}", e))));
                continue;
            }
        };
        let locked = crate::sys::ioctl(&h, FSCTL_LOCK_VOLUME, 0).or_else(|_| {
            crate::sys::ioctl(&h, FSCTL_DISMOUNT_VOLUME, 0)?;
            crate::sys::ioctl(&h, FSCTL_LOCK_VOLUME, 0)
        }).and_then(|_| crate::sys::ioctl(&h, FSCTL_DISMOUNT_VOLUME, 0));
        match locked {
            Ok(_) => {
                let _ = crate::sys::ioctl(&h, IOCTL_VOLUME_OFFLINE, 0);
                held.push((path.clone(), h));
                out.push((path, None));
            }
            Err(e) => out.push((path, Some(e.to_string()))),
        }
    }
    out
}

fn release_locks() {
    HELD.lock().unwrap_or_else(|p| p.into_inner()).clear();
}

impl Platform for Windows {
    fn is_admin(&self) -> bool {
        crate::sys::is_root()
    }

    /// Get-Volume's lettered volumes: ("D:", "D:\", filesystem).
    fn mounted_filesystems(&self) -> Vec<(String, String, String)> {
        powershell("Get-Volume | Where-Object DriveLetter | ForEach-Object { \"$($_.DriveLetter)|$($_.FileSystem)\" }")
            .lines().filter_map(|l| {
                let f: Vec<&str> = l.trim().split('|').collect();
                let letter = f.first().filter(|s| !s.is_empty())?;
                Some((format!("{}:", letter), format!("{}:\\", letter), f.get(1).unwrap_or(&"").to_string()))
            }).collect()
    }

    /// Every access path of the disk's partitions: drive letters (D:\) and
    /// letterless volume GUID paths (\\?\Volume{...}\), so both get locked.
    fn device_mountpoints(&self, dev: &str) -> Vec<(String, String)> {
        let num = match drive_number(dev) {
            Some(n) if is_physical_drive(dev) => n,
            _ => return Vec::new(),
        };
        let txt = powershell(&format!("Get-Partition -DiskNumber {} -ErrorAction SilentlyContinue | ForEach-Object {{ foreach ($a in $_.AccessPaths) {{ $a }} }}", num));
        let mut seen = std::collections::BTreeSet::new();
        let mut out = Vec::new();
        for p in txt.lines().map(str::trim).filter(|p| !p.is_empty()) {
            if !seen.insert(p.to_string()) {
                continue;
            }
            if p.len() >= 2 && p.as_bytes()[1] == b':' {
                out.push((p[..2].to_string(), p.to_string()));
            } else if let Some(rest) = p.strip_prefix(r"\\?\") {
                out.push((rest.trim_end_matches('\\').to_string(), p.to_string()));
            }
        }
        out
    }

    fn choose_device(&self) -> Result<String, Exit> {
        let empty = "No removable disks found. Plug in the card and retry.";
        let mut items = removable_disks();
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
                items = removable_disks();
                if items.is_empty() {
                    eprintln!("{}", color(&format!("  {}", empty), C_YEL));
                } else {
                    render(&items);
                }
                continue;
            }
            if !sel.is_empty() && sel.chars().all(|c| c.is_ascii_digit()) {
                let i = sel.parse::<usize>().unwrap_or(usize::MAX);
                if i < items.len() {
                    if items[i].size != 0 {
                        return Ok(format!(r"\\.\PhysicalDrive{}", items[i].number));
                    }
                    eprintln!("{}", color("  that slot is empty \u{2014} insert a card and press 'r' to rescan, or pick another device", C_YEL));
                    continue;
                }
            }
            eprintln!("{}", color("  invalid selection", C_RED));
        }
    }

    fn validate_target(&self, dev: &str) -> Result<(), Exit> {
        if !is_physical_drive(dev) {
            return Err(Exit(color(&format!("expected a \\\\.\\PhysicalDriveN path; got {}", dev), C_RED)));
        }
        let num = drive_number(dev).unwrap_or(u32::MAX);
        if disks().iter().any(|d| d.number == num && d.system) {
            return Err(Exit(color(&format!("refusing: PhysicalDrive{} backs the running system.", num), C_RED)));
        }
        Ok(())
    }

    /// IOCTL_DISK_GET_LENGTH_INFO, else Get-Disk's size (which needs no
    /// Administrator, so `flash --dry-run` works unelevated).
    fn device_sectors(&self, dev: &str) -> u64 {
        if let Ok(n) = open_handle(dev, false).and_then(|h| crate::sys::disk_length(&h)) {
            return n / SECTOR;
        }
        let num = match drive_number(dev) {
            Some(n) => n,
            None => return 0,
        };
        disks().into_iter().find(|d| d.number == num).map(|d| d.size / SECTOR).unwrap_or(0)
    }

    /// Windows has no partition node; the init offer runs before the MBR
    /// through the raw handle instead (init_before_mbr).
    fn partition_node(&self, _dev: &str, _index: u32) -> String {
        String::new()
    }

    fn unmount_all(&self, dev: &str, dry: bool) {
        if !dry {
            lock_volumes(dev);
        }
    }

    /// The raw zeroing in the layout write is enough.
    fn wipe_signatures(&self, _dev: &str) {}

    fn reread_partition_table(&self, dev: &str) {
        release_locks();
        if let Some(n) = drive_number(dev) {
            powershell(&format!("Update-Disk -Number {}", n));
        }
    }

    fn flush_buffers(&self, dev: &str) {
        if let Ok(h) = open_handle(dev, true) {
            let _ = h.sync_all();
        }
    }

    /// Volumes are mounted by the OS; there is no mount command.
    fn fat_mount_cmd(&self, _part: &str, _mnt: &str) -> Vec<String> {
        Vec::new()
    }

    /// Windows' eject only flushes: the volume manager remounts the card
    /// on its own once the table is re-read.
    fn eject(&self, dev: &str) {
        eprintln!("{}", color(&format!("  flushing + ejecting {}", dev), crate::term::C_DIM));
        release_locks();
        self.flush_buffers(dev);
    }

    fn elevate(&self, extra: &[String]) {
        crate::host::sudo_reexec(extra)
    }

    fn init_before_mbr(&self) -> bool {
        true
    }

    /// Zero sector 0, drop the volume locks and re-read the table, so the
    /// volume manager destroys the old volumes and a fresh handle may write
    /// anywhere on the disk.
    fn invalidate_cached_partitions(&self, dev: &str) {
        if let Ok(h) = open_handle(dev, true) {
            let _ = crate::sys::write_all_at(&h, &[0u8; SECTOR as usize], 0);
            let _ = h.sync_all();
        }
        release_locks();
        if let Ok(h) = open_handle(dev, true) {
            let _ = crate::sys::ioctl(&h, IOCTL_DISK_UPDATE_PROPERTIES, 0);
        }
    }

    fn open_raw(&self, dev: &str, write: bool) -> io::Result<Box<dyn BlockDevice>> {
        if write {
            lock_volumes(dev);
        }
        Ok(Box::new(Aligned { file: open_handle(dev, write)?, len: None }))
    }
}

/// base.AlignedRawIO: a physical drive takes only whole, sector-aligned
/// transfers, so unaligned edges are read, patched and written back.
struct Aligned {
    file: std::fs::File,
    len: Option<u64>,
}

impl Aligned {
    fn sectors(&self, first: u64, count: u64) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; (count * SECTOR) as usize];
        let mut got = 0;
        while got < buf.len() {
            let n = crate::sys::read_at(&self.file, &mut buf[got..], first * SECTOR + got as u64)?;
            if n == 0 {
                break;
            }
            got += n;
        }
        Ok(buf)
    }
}

impl BlockDevice for Aligned {
    fn len(&self) -> io::Result<u64> {
        match self.len {
            Some(n) => Ok(n),
            None => crate::sys::disk_length(&self.file),
        }
    }

    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        let first = offset / SECTOR;
        let last = (offset + buf.len() as u64).div_ceil(SECTOR);
        let data = self.sectors(first, last - first)?;
        let skip = (offset - first * SECTOR) as usize;
        let n = buf.len().min(data.len().saturating_sub(skip));
        buf[..n].copy_from_slice(&data[skip..skip + n]);
        Ok(n)
    }

    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()> {
        if buf.is_empty() {
            return Ok(());
        }
        let end = offset + buf.len() as u64;
        if offset % SECTOR == 0 && end % SECTOR == 0 {
            return crate::sys::write_all_at(&self.file, buf, offset);
        }
        let first = offset / SECTOR;
        let last = end.div_ceil(SECTOR);
        let mut data = self.sectors(first, last - first)?;
        data.resize(((last - first) * SECTOR) as usize, 0);
        let skip = (offset - first * SECTOR) as usize;
        data[skip..skip + buf.len()].copy_from_slice(buf);
        crate::sys::write_all_at(&self.file, &data, first * SECTOR)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.sync_all()
    }

    fn set_len(&mut self, _len: u64) -> io::Result<()> {
        Err(io::Error::other("a device can't be resized"))
    }
}
