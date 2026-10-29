//! Print every file on a card image's FAT volume: fat_files IMAGE
fn main() {
    let path = std::env::args().nth(1).unwrap();
    let mut dev = flashpod_core::block::FileDevice::open(std::path::Path::new(&path), false).unwrap();
    let mut fs = flashpod_core::ops::open_fat(&mut dev).unwrap();
    for (p, n) in flashpod_core::export::fat_files(&mut fs).unwrap() {
        println!("{}\t{}", p, n);
    }
}
