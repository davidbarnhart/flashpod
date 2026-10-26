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
use argparse::{Arg, Cli, Nargs, Ns, Val};

mod argparse;
mod data;
mod flash;
mod mount;
mod term;
mod window;
mod winnow;

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

/// `flashpod flash ...` on an image file. None: not something the Rust
/// build handles yet.
fn cmd_flash(ns: &Ns) -> Option<i32> {
    let device = s(ns, "device")?;
    // platform.use_image: only a regular file is an image
    if !std::fs::metadata(&device).map(|m| m.is_file()).unwrap_or(false) || b(ns, "self_test") {
        return None;
    }
    let mut o = flash::Opts {
        device,
        // without --firmware: the manifest chooser and download, not ported
        firmware: s(ns, "firmware")?,
        yes: b(ns, "yes"),
        dry_run: b(ns, "dry_run"),
        no_format: b(ns, "no_format"),
        arm: b(ns, "arm"),
        lba48: b(ns, "lba48"),
        max_data_gb: match ns.get("max_data_gb") {
            Some(Val::Float(f)) => Some(*f),
            _ => None,
        },
    };
    if !o.arm && !b(ns, "no_arm") {
        o.arm = flash::ask_arm(&o.firmware);
    }
    // offer init on the fresh card only when it will work: interactive, a
    // real write, a FAT32 data partition
    let offer = term::stdin_isatty() && !o.dry_run && !o.no_format;
    let mut hook = |dev: &str, f: &mut FileDevice, data_start: u64| -> Result<(), flash::Exit> {
        data::offer_init_after_flash_image(dev, f, data_start);
        Ok(())
    };
    Some(match flash::flash(&o, if offer { Some(&mut hook) } else { None }) {
        Ok(rc) => rc,
        Err(flash::Exit(msg)) => {
            eprintln!("{}", msg);
            1
        }
    })
}

/// cli.__doc__, the top-level help's description.
const DOC: &str = "flashpod - manage music on the iPod.

Subcommands:
  flashpod ls (or: list)          artist/album tree with track counts
  flashpod ls all                 same tree, with every track listed (ids shown)
  flashpod ls artist|album        flat per-artist or per-album track counts
  flashpod add [path1 path2 ...]  add audio files; directories are scanned
                                  recursively (tags read via mutagen);
                                  with no paths, opens a directory picker
  flashpod rm id [id ...]         remove tracks by id (see `flashpod ls`)
  flashpod rm artist|album <name> remove all tracks by an artist / in an album
  flashpod init [name]            create iPod_Control structure + empty DB
  flashpod flash [/dev/sdX]       write iPod firmware + partition layout to a
                                  CF/SD card (1G/2G iPod; needs sudo)
  flashpod burn IMAGE [/dev/sdX]  write a card image file onto a removable
                                  SD/CF card (needs sudo)

The mountpoint is auto-detected from mounted filesystems (FAT-family
mounts under /media, ranked by iPod_Control presence), and a detected
mount is always confirmed first: Y/n for a single candidate, a numbered
chooser for several. Non-interactive runs must pass --mount.
The iTunesDB is read/written natively (itunesdb.py) \u{2014} no libgpod.
";

/// cli.main's ArgumentParser, argument for argument.
fn build_cli() -> Cli {
    let mut cli = Cli::new("flashpod", DOC);
    let top = cli.top();
    top.add_version(&format!("flashpod {}", VERSION));
    top.add(Arg::opt(&["--mount"]).help("iPod mountpoint (default: auto-detect from mounted filesystems)"));
    top.add(Arg::opt(&["--raw"]).metavar("DEVICE").help(
        "read the iPod directly from a raw device (e.g. /dev/rdisk1s2), no OS mount \u{2014} for iPods the OS \
         can't mount, like a flaky FireWire bridge on macOS. Needs root. Currently `ls` only."));
    top.add(Arg::flag(&["--unsafe-queue"]).help(
        "proceed even if the FireWire host queue settings are known-broken for the iPod"));
    cli.add_subparsers("command");
    let common = || vec![
        Arg::opt(&["--mount"]).no_default().hidden(),
        Arg::opt(&["--raw"]).no_default().hidden(),
        Arg::flag(&["--unsafe-queue"]).no_default().hidden(),
    ];

    cli.add_parser("ls", &["list"], "list library", &common())
        .add(Arg::pos("field", Nargs::Optional).choices(&["all", "artist", "artists", "album", "albums"])
            .help("'all' adds tracks to the tree; 'artist'/'album' print flat counts"));
    cli.add_parser("add", &[], "add audio files", &common())
        .add(Arg::pos("files", Nargs::Star).metavar("path")
            .help("audio files or directories (scanned recursively); prompts if omitted"));
    cli.add_parser("rm", &["remove", "delete", "erase"], "remove tracks", &common())
        .add(Arg::pos("what", Nargs::Plus).metavar("id|artist|album")
            .help("track ids, or `artist <name>` / `album <name>` to remove every matching track"));
    cli.add_parser("init", &[], "create directory structure + empty DB", &common())
        .add(Arg::pos("name", Nargs::Optional).help("iPod name (default: iPod)"));
    cli.add_parser("rebuild", &[], "rebuild the iTunesDB from the music files already on the iPod", &common())
        .add(Arg::pos("name", Nargs::Optional).help("iPod name (default: iPod)"));

    let burn = cli.add_parser("burn", &[], "write a card image file to an SD/CF card (erases it)", &[]);
    burn.add(Arg::pos("image", Nargs::One).help("the card image file"));
    burn.add(Arg::pos("device", Nargs::Optional).help("target disk, e.g. /dev/sdc (else a chooser of removable disks)"));
    burn.add(Arg::flag(&["--yes"]).help("skip the typed ERASE confirmation"));
    burn.add(Arg::flag(&["--dry-run"]).help("check everything and show the plan; write nothing"));
    burn.add(Arg::flag(&["--no-verify"]).help("don't read the card back afterwards"));

    let fl = cli.add_parser("flash", &[], "write iPod firmware to a CF/SD card (erases it)", &[]);
    fl.add(Arg::pos("device", Nargs::Optional).help("target disk, e.g. /dev/sdb (else interactive chooser)"));
    fl.add(Arg::opt(&["--firmware"]).help(
        "firmware image: raw Firmware-*, .bin.gz or .ipsw (default: choose from firmware/firmware.json)"));
    fl.add(Arg::opt(&["--model"]).help(
        "iPod model to flash for, e.g. 1G, 2G, 3G (default: ask; decides which firmware is offered)"));
    fl.add(Arg::flag(&["--yes"]).help("skip the typed ERASE confirmation"));
    fl.add(Arg::flag(&["--dry-run"]).help("show the plan, write nothing"));
    fl.add(Arg::flag(&["--no-format"]).help(
        "keep the data partition and its music: rewrite only the firmware and partition table (refused \
         unless the card's data partition matches)"));
    fl.add_mutex(vec![
        Arg::flag(&["--arm"]).help(
            "arm the card for a disk-mode update: on its next start on FireWire power, the iPod reprograms its \
             flash chip from the card (small risk; see README)"),
        Arg::flag(&["--no-arm"]).help("don't arm, and don't ask"),
    ]);
    fl.add(Arg::flag(&["--lba48"]).help(
        "EXPERIMENTAL: use the whole card for data, past the 128 GiB LBA28 cap (only works on an LBA48-patched iPod)"));
    fl.add(Arg::opt(&["--max-data-gb"]).float().help(
        "cap the data partition to N decimal GB, overriding the firmware's manifest limit (use to probe an \
         early firmware's real disk-size ceiling)"));
    fl.add(Arg::flag(&["--self-test"]).help("validate layout logic and exit (no hardware)"));
    cli
}

fn s(ns: &Ns, k: &str) -> Option<String> {
    match ns.get(k) {
        Some(Val::Str(v)) => Some(v.clone()),
        _ => None,
    }
}

fn b(ns: &Ns, k: &str) -> bool {
    matches!(ns.get(k), Some(Val::Bool(true)))
}

fn list(ns: &Ns, k: &str) -> Vec<String> {
    match ns.get(k) {
        Some(Val::List(v)) => v.clone(),
        _ => Vec::new(),
    }
}

/// Whether `path` is a mountpoint in the OS mount table (cli._device_for_mount
/// finds a device for it). Those take the device-side steps -- the stale-mount
/// and FireWire-queue checks, the eject offer -- which this build doesn't
/// have yet; a plain directory has none of them.
fn is_mountpoint(path: &str) -> bool {
    let table = std::env::var("FLASHPOD_MOUNTS_FILE").unwrap_or_else(|_| "/proc/mounts".into());
    let want = match std::fs::canonicalize(path) {
        Ok(p) => p,
        Err(_) => std::path::PathBuf::from(path),
    };
    let unescape = |s: &str| -> String {
        let b = s.as_bytes();
        let mut out = Vec::new();
        let mut i = 0;
        while i < b.len() {
            if b[i] == b'\\' && i + 3 < b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
                out.push(u8::from_str_radix(std::str::from_utf8(&b[i + 1..i + 4]).unwrap(), 8).unwrap_or(0));
                i += 4;
            } else {
                out.push(b[i]);
                i += 1;
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    };
    std::fs::read_to_string(table).unwrap_or_default().lines().any(|l| {
        let mut it = l.split_whitespace();
        let (_dev, mnt) = (it.next(), it.next());
        match mnt {
            Some(m) => {
                let m = unescape(m);
                std::fs::canonicalize(&m).unwrap_or_else(|_| m.clone().into()) == want
            }
            None => false,
        }
    })
}

fn not_implemented() -> i32 {
    eprintln!("flashpod: not implemented in the Rust build yet");
    2
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let ns = match build_cli().parse_args(&args) {
        Ok(ns) => ns,
        Err(code) => std::process::exit(code),
    };
    let command = s(&ns, "command").unwrap_or_default();
    let raw = s(&ns, "raw");
    let mount = s(&ns, "mount").filter(|m| !m.is_empty());
    let code = match command.as_str() {
        "flash" => cmd_flash(&ns).unwrap_or_else(not_implemented),
        "ls" | "list" => match (raw.as_deref().filter(|r| !r.is_empty()), mount.as_deref()) {
            (Some(r), _) => match load_library_raw(r) {
                Some(lib) => cmd_ls(&lib.name, &lib.tracks, &s(&ns, "field").unwrap_or_default()),
                None => 1,
            },
            (None, Some(m)) if !is_mountpoint(m) => {
                // cli.load_library: card.library over a MountTarget
                let mut t = mount::MountTarget { mount: m.to_string() };
                match ops::load(&mut t, &mut entropy()) {
                    Ok(lib) => cmd_ls(&lib.name, &lib.tracks, &s(&ns, "field").unwrap_or_default()),
                    Err(e) => {
                        data::say_mount_load_error(m, &e);
                        1
                    }
                }
            }
            _ => not_implemented(),
        },
        cmd @ ("init" | "rebuild" | "rm" | "remove" | "delete" | "erase" | "add") => {
            let image = raw.as_deref().filter(|r| std::fs::metadata(r).map(|m| m.is_file()).unwrap_or(false));
            let files = list(&ns, "files");
            let what = list(&ns, "what");
            let name = s(&ns, "name");
            let c = match cmd {
                "init" => data::Cmd::Init(name.as_deref().unwrap_or("iPod")),
                "rebuild" => data::Cmd::Rebuild(name.as_deref()),
                "add" => data::Cmd::Add(&files),
                _ => data::Cmd::Rm(&what),
            };
            match (image, raw.as_deref().filter(|r| !r.is_empty()), mount.as_deref()) {
                (Some(node), _, _) => data::run_raw_image(node, c),
                (None, None, Some(m)) if !is_mountpoint(m) => data::run_mount(m, c),
                _ => not_implemented(),
            }
        }
        _ => not_implemented(),
    };
    std::process::exit(code);
}
