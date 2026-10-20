//! The harness's handle on the Rust library: one command per call, JSON on
//! stdout (an error prints the error's JSON and exits 1). Not the flashpod
//! CLI; scripts/rust_parity.py drives it.
//!
//!   fpcore inspect IMAGE
//!   fpcore tracks IMAGE
//!   fpcore min-card-bytes | create-blank PATH SIZE | format-data PATH
//!   fpcore flash PATH FW [--lba48] [--max-data-gb N] [--no-format] [--arm]
//!   fpcore flash-firmware PATH FW
//!   fpcore splitmix SEED COUNT
//!   fpcore entropy SEED COUNT

use flashpod_core::block::FileDevice;
use flashpod_core::card;
use flashpod_core::entropy::SplitMix64;

/// card._firmware_bytes(path): load, unwrap, fix up, validate.
fn read_firmware(path: &str) -> Result<Vec<u8>, flashpod_core::Error> {
    let data = std::fs::read(path).map_err(|e| {
        flashpod_core::Error::new(flashpod_core::ErrorKind::FirmwareInvalid, format!("firmware error: {}", e))
            .detail("reason", e.to_string())
    })?;
    card::firmware_bytes(&data, false)
}

/// card.create_blank: CardTooSmall, CardExists, then a sparse file of the
/// rounded size, and its inspect().
fn create_blank(path: &str, size: u64) -> Result<serde_json::Value, flashpod_core::Error> {
    let size = card::blank_size(size)?;
    let f = std::fs::OpenOptions::new().write(true).create_new(true).open(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::AlreadyExists {
            flashpod_core::Error::new(flashpod_core::ErrorKind::CardExists, format!("{} already exists", path))
        } else {
            flashpod_core::Error::from(e)
        }
    })?;
    f.set_len(size)?;
    drop(f);
    let mut dev = FileDevice::open_image(path, false)?;
    flashpod_core::inspect::inspect(&mut dev, path, options()).map(|i| serde_json::to_value(i).unwrap())
}

/// FLASHPOD_DB_LIMIT, read as library._db_size_limit does.
fn options() -> flashpod_core::inspect::Options {
    let mut o = flashpod_core::inspect::Options::default();
    if let Ok(v) = std::env::var("FLASHPOD_DB_LIMIT") {
        o.db_limit_set = true;
        if let Ok(n) = v.trim().parse::<i64>() {
            o.db_limit = n;
        }
    }
    o
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = match args.first().map(String::as_str) {
        Some("inspect") if args.len() == 2 => {
            let path = &args[1];
            FileDevice::open_image(path, false)
                .and_then(|mut dev| flashpod_core::inspect::inspect(&mut dev, path, options()))
                .map(|info| serde_json::to_value(info).unwrap())
        }
        Some("tracks") if args.len() == 2 => {
            FileDevice::open_image(&args[1], false).and_then(|mut dev| flashpod_core::inspect::tracks(&mut dev))
        }
        Some("min-card-bytes") => Ok(serde_json::json!(card::min_card_bytes())),
        Some("create-blank") if args.len() == 3 => create_blank(&args[1], args[2].parse().expect("size")),
        Some("flash") if args.len() >= 3 => {
            let mut o = card::FlashOptions { format: true, ..Default::default() };
            let mut i = 3;
            while i < args.len() {
                match args[i].as_str() {
                    "--lba48" => o.lba48 = true,
                    "--no-format" => o.format = false,
                    "--arm" => o.arm = true,
                    "--max-data-gb" => {
                        i += 1;
                        o.max_data_gb = Some(args[i].parse().expect("gb"));
                    }
                    other => panic!("unknown flag {}", other),
                }
                i += 1;
            }
            read_firmware(&args[2]).and_then(|fw| {
                // arm is checked before the image is opened, as in Python
                if o.arm {
                    flashpod_core::firmware::arm_firmware(&fw).map_err(|e| {
                        flashpod_core::Error::new(flashpod_core::ErrorKind::FirmwareInvalid, e)
                            .detail("reason", "cannot_arm")
                    })?;
                }
                let mut dev = FileDevice::open_image(&args[1], true)?;
                card::flash(&mut dev, fw, o).map(|r| serde_json::to_value(r).unwrap())
            })
        }
        Some("flash-firmware") if args.len() == 3 => read_firmware(&args[2]).and_then(|fw| {
            let mut dev = FileDevice::open_image(&args[1], true)?;
            card::flash_firmware(&mut dev, fw).map(|r| serde_json::to_value(r).unwrap())
        }),
        Some("format-data") if args.len() == 2 => FileDevice::open_image(&args[1], true)
            .and_then(|mut dev| card::format_data(&mut dev))
            .map(|r| serde_json::to_value(r).unwrap()),
        Some("entropy") if args.len() == 3 => {
            // the calls flashpod makes, in a fixed pattern: getrandbits(64),
            // randrange(10**6), choice over 7 items
            let mut r = SplitMix64::new(args[1].parse().expect("seed"));
            let n: usize = args[2].parse().expect("count");
            Ok(serde_json::json!((0..n)
                .map(|_| vec![r.getrandbits(64).to_string(), r.randrange(1_000_000).to_string(),
                              r.choice_index(7).to_string()])
                .collect::<Vec<_>>()))
        }
        Some("splitmix") if args.len() == 3 => {
            let mut r = SplitMix64::new(args[1].parse().expect("seed"));
            let n: usize = args[2].parse().expect("count");
            Ok(serde_json::json!((0..n).map(|_| r.next_u64().to_string()).collect::<Vec<_>>()))
        }
        _ => {
            eprintln!("usage: fpcore inspect IMAGE | tracks IMAGE | splitmix SEED COUNT | entropy SEED COUNT");
            std::process::exit(2);
        }
    };
    match out {
        Ok(v) => println!("{}", v),
        Err(e) => {
            println!("{}", e.to_json());
            std::process::exit(1);
        }
    }
}
