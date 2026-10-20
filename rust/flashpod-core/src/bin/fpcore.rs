//! The harness's handle on the Rust library: one command per call, JSON on
//! stdout (an error prints the error's JSON and exits 1). Not the flashpod
//! CLI; scripts/rust_parity.py drives it.
//!
//!   fpcore inspect IMAGE
//!   fpcore splitmix SEED COUNT
//!   fpcore entropy SEED COUNT

use flashpod_core::block::FileDevice;
use flashpod_core::entropy::SplitMix64;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = match args.first().map(String::as_str) {
        Some("inspect") if args.len() == 2 => {
            let path = &args[1];
            FileDevice::open_image(path, false)
                .and_then(|mut dev| flashpod_core::inspect::inspect(&mut dev, path))
                .map(|info| serde_json::to_value(info).unwrap())
        }
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
            eprintln!("usage: fpcore inspect IMAGE | fpcore splitmix SEED COUNT");
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
