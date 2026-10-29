//! platform/linux.py: lsblk, sysfs, the mount table and sudo. The work is
//! done where it always was (devices.rs, host.rs, detect.rs); this only
//! names it as the Linux Platform.

use super::Platform;
use crate::flash::Exit;
use crate::{detect, devices, host};

pub struct Linux;

impl Platform for Linux {
    fn is_admin(&self) -> bool {
        host::is_admin()
    }
    fn mounted_filesystems(&self) -> Vec<(String, String, String)> {
        host::mounted_filesystems()
    }
    fn device_mountpoints(&self, dev: &str) -> Vec<(String, String)> {
        detect::device_mountpoints(dev)
    }
    fn choose_device(&self) -> Result<String, Exit> {
        devices::choose_device()
    }
    fn validate_target(&self, dev: &str) -> Result<(), Exit> {
        devices::validate_target(dev)
    }
    fn device_sectors(&self, dev: &str) -> u64 {
        devices::device_sectors(dev)
    }
    fn partition_node(&self, dev: &str, index: u32) -> String {
        devices::partition_node(dev, index)
    }
    fn unmount_all(&self, dev: &str, dry: bool) {
        devices::unmount_all(dev, dry)
    }
    fn wipe_signatures(&self, dev: &str) {
        devices::wipe_signatures(dev)
    }
    fn reread_partition_table(&self, dev: &str) {
        devices::reread_partition_table(dev)
    }
    fn flush_buffers(&self, dev: &str) {
        devices::flush_buffers(dev)
    }
    fn fat_mount_cmd(&self, part: &str, mnt: &str) -> Vec<String> {
        vec!["mount".into(), part.into(), mnt.into()]
    }
    fn eject(&self, dev: &str) {
        detect::eject(dev)
    }
    fn elevate(&self, extra: &[String]) {
        host::sudo_reexec(extra)
    }
}

#[cfg(test)]
mod tests {
    use super::{Linux, Platform};

    #[test]
    fn partition_nodes() {
        assert_eq!(Linux.partition_node("/dev/sdb", 2), "/dev/sdb2");
        assert_eq!(Linux.partition_node("/dev/mmcblk0", 2), "/dev/mmcblk0p2");
    }

    #[test]
    fn fat_mount_is_plain_mount() {
        assert_eq!(Linux.fat_mount_cmd("/dev/sdb2", "/mnt/x"), ["mount", "/dev/sdb2", "/mnt/x"]);
    }

    #[test]
    fn mount_table_is_read() {
        let dir = std::env::temp_dir().join(format!("fp-plat-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let table = dir.join("mounts");
        std::fs::write(&table, "/dev/sdb2 /media/u/IPOD\\040X vfat rw 0 0\n").unwrap();
        std::env::set_var("FLASHPOD_MOUNTS_FILE", &table);
        let got = Linux.mounted_filesystems();
        std::env::remove_var("FLASHPOD_MOUNTS_FILE");
        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(got, [("/dev/sdb2".to_string(), "/media/u/IPOD X".to_string(), "vfat".to_string())]);
    }
}
