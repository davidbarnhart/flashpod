//! For each firmware file given: load it as flash does, flash it onto a
//! temporary 300 MB card (MBR, then Mac), extract osos, print its sha256.
//!   extract_check SCRATCH_DIR FIRMWARE...
use flashpod_core::card::{flash, FlashOptions};
use flashpod_core::extract::extract_firmware;

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let img = format!("{}/extract-check.img", a[1]);
    for path in &a[2..] {
        let fw = flashpod_core::firmware::load_firmware(&std::fs::read(path).unwrap()).expect("load");
        for mac in [false, true] {
            let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(true).open(&img).unwrap();
            f.set_len(300 << 20).unwrap();
            drop(f);
            let mut dev = flashpod_core::block::FileDevice::open(std::path::Path::new(&img), true).unwrap();
            flash(&mut dev, fw.clone(), FlashOptions { format: true, mac, ..Default::default() }).unwrap();
            let r = extract_firmware(&mut dev, Some("osos")).unwrap();
            println!("{} {} {} {}", path.rsplit('/').next().unwrap(), if mac { "apm" } else { "mbr" }, r.sha256, r.note["installed"]["format"]);
        }
    }
}
