//! A whole Mac card image through card::flash (APM, firmware, wrapped
//! HFS+), for fsck and for macOS to mount:  mac_card PATH BYTES FIRMWARE
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (path, bytes, fw) = (&a[1], a[2].parse::<u64>().expect("BYTES"), std::fs::read(&a[3]).expect("FIRMWARE"));
    let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(true).open(path).unwrap();
    f.set_len(bytes).unwrap();
    drop(f);
    let mut dev = flashpod_core::block::FileDevice::open(std::path::Path::new(path), true).unwrap();
    let o = flashpod_core::card::FlashOptions { format: true, mac: true, hfs_time: 3_000_000_000, ..Default::default() };
    let r = flashpod_core::card::flash(&mut dev, fw, o).expect("flash");
    println!("data_start={} data_sectors={}", r.data_start, r.data_sectors);
}
