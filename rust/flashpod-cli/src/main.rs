//! The flashpod command line, in Rust. M6a: `--version` and `ls` on a disk
//! image (`--raw IMAGE`). Output is meant to match the Python flashpod
//! character for character; scripts/test_cli_unchanged.py (--rust) is the
//! check.

use flashpod_core::block::FileDevice;
use flashpod_core::entropy::Entropy;
use flashpod_core::inspect;
use flashpod_core::itunesdb::Track;
use flashpod_core::ops;
use flashpod_core::text::casefold;

mod data;
mod flash;
mod term;

const VERSION: &str = "0.5.2";

/// What Python's `orunknown` does.
fn orunknown(s: &Option<String>) -> String {
    match s {
        Some(s) if !s.is_empty() => s.clone(),
        _ => "(unknown)".to_string(),
    }
}

/// `{s:<36.36s}`: cut to 36 characters, pad to 36.
fn fit36(s: &str) -> String {
    let cut: String = s.chars().take(36).collect();
    let n = cut.chars().count();
    format!("{}{}", cut, " ".repeat(36 - n))
}

fn entropy() -> Entropy {
    let seed = std::env::var("FLASHPOD_SEED").ok().and_then(|v| v.parse().ok()).unwrap_or_else(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64 ^ (std::process::id() as u64) << 32)
            .unwrap_or(1)
    });
    let now = std::env::var("FLASHPOD_NOW").ok().and_then(|v| v.parse().ok()).unwrap_or_else(|| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0)
    });
    Entropy::new(seed, now)
}

fn cmd_ls(name: &str, tracks: &[Track], field: &str) -> i32 {
    let field = match field {
        "artists" => "artist",
        "albums" => "album",
        f => f,
    };
    if field == "artist" || field == "album" {
        // casefolded name -> (first-seen display name, count)
        let mut counts: Vec<(String, String, usize)> = Vec::new();
        for t in tracks {
            let n = orunknown(if field == "artist" { &t.artist } else { &t.album });
            let k = casefold(&n);
            match counts.iter_mut().find(|c| c.0 == k) {
                Some(c) => {
                    c.1 = n;
                    c.2 += 1;
                }
                None => counts.push((k, n, 1)),
            }
        }
        println!("iPod \"{}\": {} {}s ({} tracks)", name, counts.len(), field, tracks.len());
        counts.sort_by(|a, b| a.0.cmp(&b.0));
        for (_, n, c) in counts {
            println!("{:5}  {}", c, n);
        }
        return 0;
    }
    let show_tracks = field == "all";
    let key = |t: &Track| (casefold(&orunknown(&t.artist)), casefold(&orunknown(&t.album)), t.track_nr, casefold(&orunknown(&t.title)));
    let mut sorted: Vec<&Track> = tracks.iter().collect();
    sorted.sort_by_key(|t| key(t));
    let lib = flashpod_core::itunesdb::Library { name: name.to_string(), tracks: tracks.to_vec(), dbid: 0 };
    let (nt, na, nb) = inspect::counts(&lib);
    println!("iPod \"{}\": {} tracks, {} artists, {} albums", name, nt, na, nb);
    let mut prev_artist: Option<String> = None;
    let mut prev_album: Option<String> = None;
    for t in &sorted {
        let artist = orunknown(&t.artist);
        let album = orunknown(&t.album);
        if prev_artist.is_none() || Some(casefold(&artist)) != prev_artist {
            println!("{}", artist);
            prev_artist = Some(casefold(&artist));
            prev_album = None;
        }
        if prev_album.is_none() || Some(casefold(&album)) != prev_album {
            if show_tracks {
                println!("  {}", album);
            } else {
                let n = sorted
                    .iter()
                    .filter(|u| Some(casefold(&orunknown(&u.artist))) == prev_artist && casefold(&orunknown(&u.album)) == casefold(&album))
                    .count();
                println!("  {} ({} track{})", album, n, if n != 1 { "s" } else { "" });
            }
            prev_album = Some(casefold(&album));
        }
        if show_tracks {
            let nr = if t.track_nr != 0 { format!("{:2}.", t.track_nr) } else { "   ".to_string() };
            println!(
                "    {:6}  {} {} {:2}:{:02}",
                t.id,
                nr,
                fit36(&orunknown(&t.title)),
                t.tracklen / 60000,
                t.tracklen / 1000 % 60
            );
        }
    }
    0
}

/// load_library_raw: the library off an image, or why not (stderr, None).
fn load_library_raw(device: &str) -> Option<flashpod_core::itunesdb::Library> {
    let mut dev = match FileDevice::open_image(device, false) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod: couldn't read a FAT filesystem on {}: {}\n(pass the iPod's data partition, e.g. /dev/rdisk1s2, or its whole disk /dev/rdisk1)", device, e.message);
            return None;
        }
    };
    let mut ent = entropy();
    let mut fs = match ops::open_fat(&mut dev) {
        Ok(f) => f,
        Err(e) => {
            let msg = e.details.get("error").and_then(|v| v.as_str()).unwrap_or(&e.message).to_string();
            eprintln!("flashpod: couldn't read a FAT filesystem on {}: {}\n(pass the iPod's data partition, e.g. /dev/rdisk1s2, or its whole disk /dev/rdisk1)", device, msg);
            return None;
        }
    };
    match ops::load_library(&mut fs, &mut ent) {
        Err(e) => {
            eprintln!("flashpod: failed to parse iTunesDB from {}: {}", device, e.message);
            None
        }
        Ok(Some(lib)) => Some(lib),
        Ok(None) => {
            diagnose_missing_db(&mut fs, device);
            None
        }
    }
}

fn diagnose_missing_db(fs: &mut flashpod_core::fat::Fat32, device: &str) {
    let mut names = |path: &str| -> Option<Vec<String>> {
        match fs.listdir(path) {
            Ok(Some(v)) => Some(v.into_iter().map(|e| e.name).collect()),
            _ => None,
        }
    };
    let root = names("");
    if let Some(root) = &root {
        if !root.iter().any(|n| casefold(n) == "ipod_control") {
            eprintln!("flashpod: {} has no iPod_Control directory \u{2014} it looks freshly flashed/formatted, not yet set up.", device);
            eprintln!("  root contains: {}", if root.is_empty() { "(empty)".to_string() } else { root.join(", ") });
            eprintln!("  Initialize it (on Linux: `flashpod init`), then add music.");
            return;
        }
    }
    for path in ["iPod_Control", "iPod_Control/iTunes"] {
        match names(path) {
            None => {
                eprintln!("flashpod: {}: '{}' didn't resolve as a directory \u{2014} unexpected; this may be a read bug. Please report.", device, path);
                return;
            }
            Some(l) => eprintln!("  {}/ contains: {}", path, if l.is_empty() { "(empty)".to_string() } else { l.join(", ") }),
        }
    }
    eprintln!("flashpod: no iTunesDB on {} (iPod_Control is present but iPod_Control/iTunes/iTunesDB is missing). If this iPod should have music, that's unexpected \u{2014} otherwise run `flashpod init`.", device);
}

/// `flashpod flash ...` on an image file (argparse's p_fl, for the options
/// an image takes). None: not something the Rust build handles yet.
fn cmd_flash(args: &[String]) -> Option<i32> {
    let mut o = flash::Opts {
        device: String::new(),
        firmware: String::new(),
        yes: false,
        dry_run: false,
        no_format: false,
        arm: false,
        lba48: false,
        max_data_gb: None,
    };
    let mut no_arm = false;
    let mut device: Option<String> = None;
    let mut firmware: Option<String> = None;
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        let (key, inline) = match a.split_once('=') {
            Some((k, v)) if k.starts_with("--") => (k, Some(v.to_string())),
            _ => (a, None),
        };
        let mut value = || -> Option<String> {
            if inline.is_some() {
                return inline.clone();
            }
            i += 1;
            args.get(i).cloned()
        };
        match key {
            "--firmware" => firmware = Some(value()?),
            "--max-data-gb" => o.max_data_gb = Some(value()?.parse().ok()?),
            "--yes" => o.yes = true,
            "--dry-run" => o.dry_run = true,
            "--no-format" => o.no_format = true,
            "--arm" => o.arm = true,
            "--no-arm" => no_arm = true,
            "--lba48" => o.lba48 = true,
            _ if a.starts_with('-') => return None,
            _ if device.is_none() => device = Some(a.to_string()),
            _ => return None,
        }
        i += 1;
    }
    let device = device?;
    // platform.use_image: only a regular file is an image
    if !std::fs::metadata(&device).map(|m| m.is_file()).unwrap_or(false) {
        return None;
    }
    if o.arm && no_arm {
        return None;
    }
    o.device = device;
    o.firmware = firmware?;
    if !o.arm && !no_arm {
        o.arm = flash::ask_arm(&o.firmware);
    }
    // offer_init_after_flash needs add; on a terminal that's still Python's
    if term::stdin_isatty() && !o.dry_run && !o.no_format {
        return None;
    }
    Some(match flash::flash(&o, None) {
        Ok(rc) => rc,
        Err(flash::Exit(msg)) => {
            eprintln!("{}", msg);
            1
        }
    })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(|s| s.as_str()) == Some("flash") {
        let code = cmd_flash(&args[1..]).unwrap_or_else(|| {
            eprintln!("flashpod: not implemented in the Rust build yet");
            2
        });
        std::process::exit(code);
    }
    if args.iter().any(|a| a == "--version") {
        println!("flashpod {}", VERSION);
        return;
    }
    let mut raw: Option<String> = None;
    let mut rest: Vec<String> = Vec::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--raw" if i + 1 < args.len() => {
                raw = Some(args[i + 1].clone());
                i += 2;
            }
            a => {
                rest.push(a.to_string());
                i += 1;
            }
        }
    }
    let code = match rest.first().map(|s| s.as_str()) {
        Some("ls") | Some("list") => {
            let field = rest.get(1).map(|s| s.as_str()).unwrap_or("");
            match raw.as_deref().and_then(load_library_raw) {
                Some(lib) => cmd_ls(&lib.name, &lib.tracks, field),
                None => 1,
            }
        }
        Some(cmd @ ("init" | "rebuild" | "rm" | "remove" | "delete" | "erase" | "add")) => {
            let image = raw.as_deref().filter(|r| std::fs::metadata(r).map(|m| m.is_file()).unwrap_or(false));
            // the progress window and the add/rebuild questions on a terminal
            // aren't ported yet
            let tty = term::stdin_isatty() || std::io::IsTerminal::is_terminal(&std::io::stdout());
            let args = &rest[1..];
            match image {
                Some(node) if !(tty && matches!(cmd, "add" | "rebuild")) => data::run_raw_image(node, match cmd {
                    "init" => data::Cmd::Init(args.first().map(|s| s.as_str()).unwrap_or("iPod")),
                    "rebuild" => data::Cmd::Rebuild(args.first().map(|s| s.as_str())),
                    "add" => data::Cmd::Add(args),
                    _ => data::Cmd::Rm(args),
                }),
                _ => {
                    eprintln!("flashpod: not implemented in the Rust build yet");
                    2
                }
            }
        }
        _ => {
            eprintln!("flashpod: not implemented in the Rust build yet");
            2
        }
    };
    std::process::exit(code);
}
