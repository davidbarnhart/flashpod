//! platform/base.py's Platform: everything flashpod asks of the host OS to
//! find, guard and write a card. One implementation per OS, picked at
//! compile time; callers go through current() and never name an OS.
//!
//! The FireWire workarounds (queue pinning, firewire_sbp2, raw scanning)
//! are not here: they are Linux-only, stay in host.rs, and go in 1.0 (#98).

use crate::flash::Exit;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;

pub trait Platform: Sync {
    /// Root (or Administrator): needed to write a disk.
    fn is_admin(&self) -> bool;
    /// (device, mountpoint, fstype) for every mounted filesystem.
    fn mounted_filesystems(&self) -> Vec<(String, String, String)>;
    /// (partition node, mountpoint) for each mounted partition of `dev`.
    fn device_mountpoints(&self, dev: &str) -> Vec<(String, String)>;
    /// The interactive chooser over flashable disks: a whole-disk path.
    fn choose_device(&self) -> Result<String, Exit>;
    /// Refuse a missing disk, a partition node, or the system disk.
    fn validate_target(&self, dev: &str) -> Result<(), Exit>;
    /// The disk's size in 512-byte sectors, 0 when it can't be read.
    fn device_sectors(&self, dev: &str) -> u64;
    /// Partition `index` of whole disk `dev`.
    fn partition_node(&self, dev: &str, index: u32) -> String;
    /// Before the raw write: unmount every partition of `dev`.
    fn unmount_all(&self, dev: &str, dry: bool);
    /// Before the raw write: clear old filesystem signatures (may be a no-op).
    fn wipe_signatures(&self, dev: &str);
    /// After the raw write: make the OS see the new partition table.
    fn reread_partition_table(&self, dev: &str);
    /// After the raw write: push everything to the card.
    fn flush_buffers(&self, dev: &str);
    /// The command that mounts FAT partition `part` at `mnt`.
    fn fat_mount_cmd(&self, part: &str, mnt: &str) -> Vec<String>;
    /// Flush, unmount and power off / eject `dev` (prints its own progress).
    fn eject(&self, dev: &str);
    /// Replace this process with flashpod run as root, `extra` as its
    /// arguments. Returns only when it can't.
    fn elevate(&self, extra: &[String]);
}

#[cfg(target_os = "linux")]
pub fn current() -> &'static dyn Platform {
    &linux::Linux
}

#[cfg(target_os = "macos")]
pub fn current() -> &'static dyn Platform {
    &macos::MacOS
}
