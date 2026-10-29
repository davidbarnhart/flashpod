//! Write an empty wrapped HFS+ volume over a whole file (no partition map),
//! for checking hfsplus::format_wrapped against emupod's generator and
//! fsck.hfsplus:  hfs_image PATH BYTES [MAC_TIME]
use flashpod_core::block::FileDevice;
use flashpod_core::hfsplus::format_wrapped;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (path, bytes) = (&a[1], a[2].parse::<u64>().expect("BYTES"));
    let now = a.get(3).map(|t| t.parse().expect("MAC_TIME")).unwrap_or(3_000_000_000u32);
    let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(true).open(path).unwrap();
    f.set_len(bytes).unwrap();
    drop(f);
    let mut dev = FileDevice::open(std::path::Path::new(path), true).unwrap();
    let g = format_wrapped(&mut dev, 0, bytes / 512, "iPod", now).unwrap();
    println!("{:?}", g);
}
