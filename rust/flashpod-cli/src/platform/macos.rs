//! platform/macos.py: diskutil (its -plist output) to find, guard, unmount
//! and eject disks, the BSD mount table, and sudo. Raw I/O goes through the
//! unbuffered /dev/rdiskN node (rawdev.rs).

use super::Platform;
use crate::flash::{fmt_size, Exit};
use crate::term::{color, input_line, C_CYN, C_RED, C_YEL};
use plist::{Dictionary, Value};
use std::collections::BTreeSet;
use std::process::{Command, Stdio};

pub struct MacOS;

/// `diskutil <verb> -plist [operands]`. -plist must follow the verb: `list`
/// and `info` read a trailing argument as the disk.
fn diskutil_plist(args: &[&str]) -> Option<Dictionary> {
    let out = Command::new("diskutil").arg(args[0]).arg("-plist").args(&args[1..])
        .stdin(Stdio::null()).stderr(Stdio::null()).output().ok()?;
    if !out.status.success() || out.stdout.is_empty() {
        return None;
    }
    Value::from_reader(std::io::Cursor::new(out.stdout)).ok()?.into_dictionary()
}

fn diskutil_info(dev: &str) -> Option<Dictionary> {
    diskutil_plist(&["info", dev])
}

fn text(d: &Dictionary, k: &str) -> String {
    d.get(k).and_then(Value::as_string).unwrap_or("").trim().to_string()
}

fn size(d: &Dictionary) -> u64 {
    ["TotalSize", "Size"].iter().filter_map(|k| d.get(k).and_then(Value::as_unsigned_integer)).find(|&n| n != 0).unwrap_or(0)
}

/// /dev/disk2s1 -> disk2; /dev/rdisk2 -> disk2; disk2 -> disk2.
fn whole_disk(dev: &str) -> String {
    let name = dev.rsplit('/').next().unwrap_or(dev);
    let name = name.strip_prefix('r').filter(|n| n.starts_with("disk")).unwrap_or(name);
    match name.strip_prefix("disk") {
        Some(rest) => format!("disk{}", rest.split('s').next().unwrap_or(rest)),
        None => name.to_string(),
    }
}

fn apfs_stores(info: &Dictionary) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for st in info.get("APFSPhysicalStores").and_then(Value::as_array).into_iter().flatten() {
        let node = match st {
            Value::Dictionary(d) => d.get("APFSPhysicalStore").and_then(Value::as_string),
            v => v.as_string(),
        };
        if let Some(n) = node.filter(|n| !n.is_empty()) {
            out.insert(whole_disk(n));
        }
    }
    out
}

/// Every whole disk backing the running system. `diskutil info /` names the
/// synthesized APFS container as ParentWholeDisk, not the physical drive
/// under it, so the container's physical stores are added too.
fn system_whole_disks() -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let info = match diskutil_info("/") {
        Some(i) => i,
        None => return out,
    };
    let parent = text(&info, "ParentWholeDisk");
    if !parent.is_empty() {
        out.insert(whole_disk(&parent));
    }
    out.extend(apfs_stores(&info));
    for disk in out.clone() {
        if let Some(i) = diskutil_info(&format!("/dev/{}", disk)) {
            out.extend(apfs_stores(&i));
        }
    }
    out
}

/// External whole disks, with their diskutil info.
fn external_disks() -> Vec<(String, Dictionary)> {
    let ids = match diskutil_plist(&["list", "physical"]) {
        Some(pl) => pl.get("WholeDisks").and_then(Value::as_array).cloned().unwrap_or_default(),
        None => return Vec::new(),
    };
    let mut out = Vec::new();
    for d in ids.iter().filter_map(Value::as_string) {
        let info = match diskutil_info(&format!("/dev/{}", d)) {
            Some(i) => i,
            None => continue,
        };
        let internal = info.get("Internal").or_else(|| info.get("DeviceInternal")).and_then(Value::as_boolean).unwrap_or(true);
        if !internal {
            out.push((d.to_string(), info));
        }
    }
    out
}

fn render(disks: &[(String, Dictionary)]) {
    eprintln!("{}", color("\nAttached external disks:\n", C_CYN));
    for (i, (d, info)) in disks.iter().enumerate() {
        let mut name = text(info, "MediaName");
        if name.is_empty() {
            name = text(info, "IORegistryEntryName");
        }
        if name.is_empty() {
            name = "disk".into();
        }
        eprintln!("  [{}] /dev/{:<7}  {:>10}  {}", i, d, fmt_size(size(info)), name);
    }
    eprintln!();
}

fn quiet(cmd: &[&str]) {
    let _ = Command::new(cmd[0]).args(&cmd[1..]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status();
}

impl Platform for MacOS {
    fn is_admin(&self) -> bool {
        crate::host::is_admin()
    }

    /// `mount`: /dev/disk2s2 on /Volumes/IPOD (msdos, local, ...).
    fn mounted_filesystems(&self) -> Vec<(String, String, String)> {
        let out = match Command::new("mount").stdin(Stdio::null()).stderr(Stdio::null()).output() {
            Ok(o) if o.status.success() => o,
            _ => return Vec::new(),
        };
        String::from_utf8_lossy(&out.stdout).lines().filter_map(|line| {
            let (dev, rest) = line.split_once(" on ")?;
            let (mp, tail) = rest.rsplit_once(" (").unwrap_or((rest, ""));
            let fstype = tail.split(',').next().unwrap_or("").trim_matches(|c| c == ')' || c == ' ');
            Some((dev.trim().to_string(), mp.trim().to_string(), fstype.to_string()))
        }).collect()
    }

    fn device_mountpoints(&self, dev: &str) -> Vec<(String, String)> {
        let disk = whole_disk(dev);
        self.mounted_filesystems().into_iter().filter(|(d, _, _)| {
            let base = d.rsplit('/').next().unwrap_or(d);
            base == disk || base.starts_with(&format!("{}s", disk))
        }).map(|(d, mp, _)| (d, mp)).collect()
    }

    fn choose_device(&self) -> Result<String, Exit> {
        let empty = "No external disks found. Plug in the card reader and retry.";
        let mut items = external_disks();
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
                items = external_disks();
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
                    if size(&items[i].1) != 0 {
                        return Ok(format!("/dev/{}", items[i].0));
                    }
                    eprintln!("{}", color("  that slot is empty \u{2014} insert a card and press 'r' to rescan, or pick another device", C_YEL));
                    continue;
                }
            }
            eprintln!("{}", color("  invalid selection", C_RED));
        }
    }

    fn validate_target(&self, dev: &str) -> Result<(), Exit> {
        let base = dev.rsplit('/').next().unwrap_or(dev);
        if !std::path::Path::new(dev).exists() && !std::path::Path::new(&format!("/dev/r{}", base)).exists() {
            return Err(Exit(color(&format!("no such device: {}", dev), C_RED)));
        }
        let name = base.strip_prefix('r').filter(|n| n.starts_with("disk")).unwrap_or(base);
        if name.strip_prefix("disk").is_some_and(|rest| rest.contains('s')) {
            return Err(Exit(color(&format!("refusing a partition ({}); pass the whole disk (/dev/{}).", dev, whole_disk(dev)), C_RED)));
        }
        if system_whole_disks().contains(&whole_disk(dev)) {
            return Err(Exit(color(&format!("refusing: {} backs the running system.", dev), C_RED)));
        }
        Ok(())
    }

    fn device_sectors(&self, dev: &str) -> u64 {
        if std::fs::metadata(dev).map(|m| m.is_file()).unwrap_or(false) {
            return std::fs::metadata(dev).map(|m| m.len() / 512).unwrap_or(0);
        }
        if let Some(n) = diskutil_info(dev).map(|i| size(&i)).filter(|&n| n != 0) {
            return n / 512;
        }
        std::fs::File::open(dev).and_then(|mut f| std::io::Seek::seek(&mut f, std::io::SeekFrom::End(0))).map(|n| n / 512).unwrap_or(0)
    }

    /// Always sN: macOS has no pN nodes.
    fn partition_node(&self, dev: &str, index: u32) -> String {
        format!("{}s{}", dev, index)
    }

    fn unmount_all(&self, dev: &str, dry: bool) {
        if !dry {
            quiet(&["diskutil", "unmountDisk", &format!("/dev/{}", whole_disk(dev))]);
        }
    }

    /// The raw zeroing in the layout write is enough on macOS.
    fn wipe_signatures(&self, _dev: &str) {}

    /// macOS re-probes on its own; a `diskutil list` nudges /Volumes.
    fn reread_partition_table(&self, dev: &str) {
        quiet(&["diskutil", "list", &format!("/dev/{}", whole_disk(dev))]);
    }

    fn flush_buffers(&self, _dev: &str) {
        quiet(&["sync"]);
    }

    /// mount(8) won't probe an unmounted FAT slice; name the filesystem.
    fn fat_mount_cmd(&self, part: &str, mnt: &str) -> Vec<String> {
        vec!["mount".into(), "-t".into(), "msdos".into(), part.into(), mnt.into()]
    }

    fn eject(&self, dev: &str) {
        eprintln!("{}", color(&format!("  flushing + ejecting {}", dev), crate::term::C_DIM));
        quiet(&["sync"]);
        quiet(&["diskutil", "eject", &format!("/dev/{}", whole_disk(dev))]);
    }

    fn elevate(&self, extra: &[String]) {
        crate::host::sudo_reexec(extra)
    }
}

#[cfg(test)]
mod tests {
    use super::whole_disk;

    #[test]
    fn whole_disk_names() {
        assert_eq!(whole_disk("/dev/disk2s1"), "disk2");
        assert_eq!(whole_disk("/dev/rdisk2"), "disk2");
        assert_eq!(whole_disk("/dev/rdisk12s3"), "disk12");
        assert_eq!(whole_disk("disk4"), "disk4");
    }
}
