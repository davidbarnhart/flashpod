//! The harness's handle on the Rust library: one command per call, JSON on
//! stdout (an error prints the error's JSON and exits 1). Not the flashpod
//! CLI; scripts/rust_parity.py drives it.
//!
//!   fpcore inspect IMAGE
//!   fpcore tracks IMAGE
//!   fpcore min-card-bytes | create-blank PATH SIZE | format-data PATH
//!   fpcore flash PATH FW [--lba48] [--max-data-gb N] [--no-format] [--arm]
//!   fpcore flash-firmware PATH FW
//!   fpcore init PATH [--name N] [--overwrite] | remove PATH WHAT...
//!   fpcore plan-add|add PATH [--tags JSON] [--no-expand] [--no-assess] [--full fit] [--ceiling fit|force] FILES...
//!   fpcore rebuild PATH [--tags JSON] [--name N] [--no-save]
//!   fpcore tags FILE... | card-tags IMAGE   (what make_track reads, per file)
//! (tags come from the Rust reader; --tags JSON supplies Python's instead)
//! (FLASHPOD_SEED / FLASHPOD_NOW: the id seed and clock)
//!   fpcore splitmix SEED COUNT
//!   fpcore entropy SEED COUNT

use flashpod_core::block::FileDevice;
use flashpod_core::card;
use flashpod_core::entropy::SplitMix64;
use flashpod_core::library::{TagResult, TagSource};

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
    // parity harness only: a smaller stock limit without "the user set it"
    if let Some(n) = std::env::var("FLASHPOD_PARITY_STOCK_LIMIT").ok().and_then(|v| v.parse().ok()) {
        o.db_limit = n;
    }
    if let Ok(v) = std::env::var("FLASHPOD_DB_LIMIT") {
        o.db_limit_set = true;
        if let Ok(n) = v.trim().parse::<i64>() {
            o.db_limit = n;
        }
    }
    o
}

/// FLASHPOD_SEED and FLASHPOD_NOW (the parity harness sets both);
/// otherwise a seed and "now" from the clock.
fn entropy() -> flashpod_core::entropy::Entropy {
    let clock = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap();
    let seed = std::env::var("FLASHPOD_SEED").ok().and_then(|v| v.parse().ok()).unwrap_or(clock.as_nanos() as u64);
    let now = std::env::var("FLASHPOD_NOW").ok().and_then(|v| v.parse().ok()).unwrap_or(clock.as_secs() as i64);
    flashpod_core::entropy::Entropy::new(seed, now)
}

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1).cloned())
}

/// The arguments that aren't flags (or a flag's value).
fn positional(args: &[String], with_value: &[&str]) -> Vec<String> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if with_value.contains(&args[i].as_str()) {
            i += 2;
            continue;
        }
        if !args[i].starts_with("--") {
            out.push(args[i].clone());
        }
        i += 1;
    }
    out
}

/// Tags read by the Python library (mutagen), handed over as JSON:
/// {path: {"audio": {...}} | {"error": "..."} | {"unrecognized": true}}.
struct JsonTags(serde_json::Map<String, serde_json::Value>);

impl JsonTags {
    fn load(path: &str) -> Self {
        let v: serde_json::Value = serde_json::from_slice(&std::fs::read(path).expect("tags file")).expect("tags JSON");
        JsonTags(v.as_object().cloned().unwrap_or_default())
    }
}

impl flashpod_core::library::TagSource for JsonTags {
    fn read(&mut self, path: &str) -> flashpod_core::library::TagResult {
        use flashpod_core::library::TagResult;
        match self.0.get(path) {
            Some(v) if v.get("audio").is_some() => {
                TagResult::Audio(serde_json::from_value(v["audio"].clone()).expect("audio fields"))
            }
            Some(v) if v.get("error").is_some() => TagResult::Unreadable(v["error"].as_str().unwrap_or("").into()),
            Some(_) => TagResult::Unrecognized,
            None => TagResult::Unreadable(format!("no tags supplied for {}", path)),
        }
    }
}

/// --tags JSON (Python's tags) or the Rust reader
fn tag_source(args: &[String]) -> Box<dyn TagSource> {
    match flag_value(args, "--tags") {
        Some(p) => Box::new(JsonTags::load(&p)),
        None => Box::new(flashpod_core::tags::FileTags),
    }
}

/// the harness's per-file form: {"audio": {...}} | {"error": ...} | {"unrecognized": true}
fn tag_json(r: TagResult) -> serde_json::Value {
    match r {
        TagResult::Audio(a) => serde_json::json!({"audio": a}),
        TagResult::Unreadable(e) => serde_json::json!({"error": e}),
        TagResult::Unrecognized => serde_json::json!({"unrecognized": true}),
    }
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
        Some("init") if args.len() >= 2 => {
            let name = flag_value(&args, "--name").unwrap_or_else(|| "iPod".into());
            let overwrite = args.iter().any(|a| a == "--overwrite");
            FileDevice::open_image(&args[1], true)
                .and_then(|mut dev| flashpod_core::ops::init(&mut dev, &name, overwrite, &mut entropy()))
        }
        Some("remove") if args.len() >= 2 => FileDevice::open_image(&args[1], true)
            .and_then(|mut dev| flashpod_core::ops::remove(&mut dev, &args[2..], &mut entropy())),
        Some("plan-add") | Some("add") if args.len() >= 2 => {
            let files = positional(&args[2..], &["--tags", "--full", "--ceiling"]);
            let mut src = tag_source(&args);
            let src = src.as_mut();
            let mut ent = entropy();
            let expand = !args.iter().any(|a| a == "--no-expand");
            let assess = !args.iter().any(|a| a == "--no-assess");
            if args[0] == "plan-add" {
                FileDevice::open_image(&args[1], false).and_then(|mut dev| {
                    flashpod_core::ops::plan_add(&mut dev, &files, src, &mut ent, options(), expand, assess)
                        .map(|p| p.json)
                })
            } else {
                let full = flag_value(&args, "--full").unwrap_or_else(|| "stop".into());
                let ceil = flag_value(&args, "--ceiling").unwrap_or_else(|| "refuse".into());
                let mut read = |p: &str| std::fs::read(p);
                FileDevice::open_image(&args[1], true).and_then(|mut dev| {
                    flashpod_core::ops::add(&mut dev, &files, &full, &ceil, src, &mut read, &mut ent, options())
                })
            }
        }
        Some("rebuild") if args.len() >= 2 => {
            let mut src = tag_source(&args);
            let name = flag_value(&args, "--name").unwrap_or_else(|| "iPod".into());
            let save = !args.iter().any(|a| a == "--no-save");
            FileDevice::open_image(&args[1], true)
                .and_then(|mut dev| flashpod_core::ops::rebuild(&mut dev, &name, save, src.as_mut(), &mut entropy()))
        }
        Some("tags") => {
            let mut out = serde_json::Map::new();
            for p in &args[1..] {
                out.insert(p.clone(), tag_json(flashpod_core::tags::FileTags.read(p)));
            }
            Ok(serde_json::Value::Object(out))
        }
        Some("card-tags") if args.len() == 2 => FileDevice::open_image(&args[1], false).and_then(|mut dev| {
            let mut fs = flashpod_core::ops::open_fat(&mut dev)?;
            let mut out = serde_json::Map::new();
            for (relpath, _) in fs.music_files().map_err(|e| flashpod_core::Error::new(flashpod_core::ErrorKind::NotACard, e))? {
                let r = match fs.resolve(&relpath) {
                    Ok(Some(e)) => match flashpod_core::tags::FatFile::open(&mut fs, e.first_cluster, e.size as u64) {
                        Ok(mut f) => flashpod_core::tags::audio_info(&relpath, &mut f),
                        Err(e) => TagResult::Unreadable(e),
                    },
                    Ok(None) => continue,
                    Err(e) => TagResult::Unreadable(e),
                };
                out.insert(relpath, tag_json(r));
            }
            Ok(serde_json::Value::Object(out))
        }),
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
