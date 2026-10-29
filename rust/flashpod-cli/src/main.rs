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
use argparse::{Arg, Cli, Nargs, Ns, Parser, Val};

mod argparse;
mod burn;
mod catalog;
mod data;
mod export;
mod detect;
mod devices;
mod flash;
mod host;
mod rawdev;
mod mount;
mod pathpicker;
mod platform;
mod sys;
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
    let lib = flashpod_core::itunesdb::Library { name: name.to_string(), tracks: tracks.to_vec(), dbid: 0, playlists: Vec::new() };
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
    if !std::path::Path::new(device).is_file() {
        return load_library_device(device);
    }
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

/// load_library_raw over a device node.
fn load_library_device(device: &str) -> Option<flashpod_core::itunesdb::Library> {
    let say = |e: &str| eprintln!("flashpod: couldn't read a FAT filesystem on {}: {}\n(pass the iPod's data partition, e.g. /dev/rdisk1s2, or its whole disk /dev/rdisk1)", device, e);
    let xfer = rawdev::effective_xfer();
    let mut dev = match rawdev::RawDevice::open(device, false, xfer) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("flashpod: need root to read {}.\n  sudo {} ls --raw {}", device, host::self_cmd().join(" "), device);
            return None;
        }
        Err(e) => {
            say(&rawdev::py_os(&e).to_string());
            return None;
        }
    };
    let mut fs = match rawdev::open_raw_fat(&mut dev, xfer) {
        Ok(f) => f,
        Err(e) => {
            say(&e);
            return None;
        }
    };
    match ops::load_library(&mut fs, &mut entropy()) {
        Err(e) if e.kind == flashpod_core::error::ErrorKind::CorruptLibrary => {
            eprintln!("flashpod: failed to parse iTunesDB from {}: {}", device, e.message);
            None
        }
        Err(e) => {
            say(&e.message);
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

/// The `flash` branch of cli.main: the firmware is picked (and fetched)
/// first, as the invoking user; then, for a device, root via sudo, the
/// resolved choices carried across as flags.
fn cmd_flash(ns: &mut Ns) -> i32 {
    if b(ns, "self_test") {
        return flash::self_test();
    }
    let device = s(ns, "device").filter(|d| !d.is_empty());
    let image = device.as_deref().map(flash::is_image).unwrap_or(false);
    let mut max_data_gb = None;
    let firmware = match s(ns, "firmware").filter(|f| !f.is_empty()) {
        Some(f) => f,
        None => {
            let manifest = match catalog::load_manifest() {
                Some(m) => m,
                None => return 1,
            };
            let model = match catalog::choose_model(&manifest, s(ns, "model").as_deref().filter(|m| !m.is_empty())) {
                catalog::Model::Bail => return 1,
                catalog::Model::Any => None,
                catalog::Model::One(m) => Some(m),
            };
            let entry = match catalog::choose_firmware(&manifest, model.as_ref()) {
                Some(e) => e,
                None => return 1,
            };
            max_data_gb = entry["max_data_gb"].as_f64();
            match catalog::ensure_firmware(&entry, manifest["base_url"].as_str().unwrap_or("")) {
                Some(f) => f,
                None => return 1,
            }
        }
    };
    if let Some(Val::Float(f)) = ns.get("max_data_gb") {
        max_data_gb = Some(*f);
    }
    // carry both resolved choices across the sudo boundary
    ns.insert("firmware".into(), Val::Str(firmware.clone()));
    ns.insert("max_data_gb".into(), max_data_gb.map(Val::Float).unwrap_or(Val::None));
    if !b(ns, "arm") && !b(ns, "no_arm") {
        let armed = flash::ask_arm(&firmware);
        ns.insert("arm".into(), Val::Bool(armed));
        ns.insert("no_arm".into(), Val::Bool(!armed));
    }
    if !image && !b(ns, "dry_run") && !crate::platform::current().is_admin() {
        let args = detect::cmd_args(ns);
        if term::stdin_isatty() {
            eprintln!("flashpod flash: writing to a disk needs root \u{2014} elevating via sudo...");
            crate::platform::current().elevate(&args);
        }
        eprintln!("flashpod flash: Run as root (sudo) to write to a block device.\n  sudo {}",
                  host::self_cmd().into_iter().chain(args).collect::<Vec<_>>().join(" "));
        return 1;
    }
    let o = flash::Opts {
        device,
        firmware,
        yes: b(ns, "yes"),
        dry_run: b(ns, "dry_run"),
        no_format: b(ns, "no_format"),
        arm: b(ns, "arm"),
        lba48: b(ns, "lba48"),
        max_data_gb,
    };
    // offer init on the fresh card only when it will work: interactive, a
    // real write, a FAT32 data partition
    let offer = term::stdin_isatty() && !o.dry_run && !o.no_format;
    let mut hook = |dev: &str, open: Option<(&mut dyn flashpod_core::block::BlockDevice, u64)>| match open {
        Some((f, data_start)) => data::offer_init_after_flash_image(dev, f, data_start),
        None => data::offer_init_after_flash_unix(dev),
    };
    match flash::flash(&o, if offer { Some(&mut hook) } else { None }) {
        Ok(rc) => rc,
        Err(flash::Exit(msg)) => {
            eprintln!("{}", msg);
            1
        }
    }
}

/// The `burn` branch of cli.main: root via sudo unless it's a dry run.
fn cmd_burn(ns: &Ns) -> i32 {
    if !b(ns, "dry_run") && !crate::platform::current().is_admin() {
        let args = detect::cmd_args(ns);
        if term::stdin_isatty() {
            eprintln!("flashpod burn: writing to a disk needs root \u{2014} elevating via sudo...");
            crate::platform::current().elevate(&args);
        }
        eprintln!("flashpod burn: needs root.\n  sudo {}", host::self_cmd().into_iter().chain(args).collect::<Vec<_>>().join(" "));
        return 1;
    }
    let image = s(ns, "image").unwrap_or_default();
    let device = s(ns, "device").filter(|d| !d.is_empty());
    match burn::burn(&image, device.as_deref(), b(ns, "yes"), b(ns, "dry_run"), !b(ns, "no_verify")) {
        Ok(rc) => rc,
        Err(flash::Exit(msg)) => {
            eprintln!("{}", msg);
            1
        }
    }
}

/// cli.__doc__, the top-level help's description.
/// The top-level description: the 1.0 command layout (#97), shared with
/// scripts/layout1_parser.py, the argparse reference test_cli_args checks
/// the Rust parser against.
const DOC: &str = include_str!("doc1.txt");

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

    // 1.0 layout (#97): commands grouped by what they act on. `flash` stays
    // as a top-level alias of `firmware write`.
    let inspect = cli.add_parser_at(0, "inspect", &[], "report everything about a card, iPod or image", &[]);
    cli.parser(inspect).add(Arg::pos("args", Nargs::Star).metavar("TARGET"));

    let fw = cli.add_parser_at(0, "firmware", &[], "inspect, extract, patch and write firmware", &[]);
    cli.add_subparsers_at(fw, "firmware_command");
    cli.add_parser_at(fw, "list", &[], "the firmware flashpod can write", &[]);
    let ex = cli.add_parser_at(fw, "extract", &[], "copy the firmware partition (or one image) off a card", &[]);
    let exp = cli.parser(ex);
    exp.add(Arg::pos("target", Nargs::One).help("card image file, or a card or iPod disk (needs root)"));
    exp.add(Arg::opt(&["-o", "--output"]).required().help("file to write; a FILE.json note is written beside it"));
    exp.add(Arg::opt(&["--image"]).choices(&["osos", "aupd", "rsrc"]).help("one image from the firmware directory, not the whole partition"));
    let w = cli.add_parser_at(fw, "write", &[], "write iPod firmware to a CF/SD card (erases it)", &[]);
    flash_args(cli.parser(w));
    for (name, help) in [("patch", "build a patched firmware"), ("patches", "list the patch library"),
                         ("arm", "arm a disk-mode (boot ROM) update"), ("recover", "the boot ROM recovery routine")] {
        let i = cli.add_parser_at(fw, name, &[], help, &[]);
        cli.parser(i).add(Arg::pos("args", Nargs::Star).metavar("ARG"));
    }

    let card = cli.add_parser_at(0, "card", &[], "card image files", &[]);
    cli.add_subparsers_at(card, "card_command");
    let new = cli.add_parser_at(card, "new", &[], "make a blank card image", &[]);
    cli.parser(new).add(Arg::pos("args", Nargs::Star).metavar("ARG"));
    let burn = cli.add_parser_at(card, "burn", &[], "write a card image file to an SD/CF card (erases it)", &[]);
    burn_args(cli.parser(burn));

    let music = cli.add_parser_at(0, "music", &[], "the music library on an iPod, card or image", &[]);
    cli.add_subparsers_at(music, "music_command");
    let i = cli.add_parser_at(music, "ls", &["list"], "list library", &common());
    cli.parser(i).add(Arg::pos("field", Nargs::Optional).choices(&["all", "artist", "artists", "album", "albums"])
        .help("'all' adds tracks to the tree; 'artist'/'album' print flat counts"));
    let i = cli.add_parser_at(music, "add", &[], "add audio files", &common());
    cli.parser(i).add(Arg::pos("files", Nargs::Star).metavar("path")
        .help("audio files or directories (scanned recursively); prompts if omitted"));
    let i = cli.add_parser_at(music, "rm", &["remove", "delete", "erase"], "remove tracks", &common());
    cli.parser(i).add(Arg::pos("what", Nargs::Plus).metavar("id|artist|album")
        .help("track ids, or `artist <name>` / `album <name>` to remove every matching track"));
    let i = cli.add_parser_at(music, "init", &[], "create directory structure + empty DB", &common());
    cli.parser(i).add(Arg::pos("name", Nargs::Optional).help("iPod name (default: iPod)"));
    let i = cli.add_parser_at(music, "rebuild", &[], "rebuild the iTunesDB from the music files already on the iPod", &common());
    cli.parser(i).add(Arg::pos("name", Nargs::Optional).help("iPod name (default: iPod)"));
    let i = cli.add_parser_at(music, "export", &[], "copy music off with tags and readable names", &common());
    let p = cli.parser(i);
    p.add(Arg::opt(&["--to"]).metavar("DIR").help("where to put it (default: ./<the iPod's name>)"));
    p.add_mutex(vec![
        Arg::flag(&["--other-files"]).help("also export the files the database doesn't list, without asking"),
        Arg::flag(&["--no-other-files"]).help("don't export them, and don't ask"),
    ]);
    let pl = cli.add_parser_at(music, "playlist", &[], "make and edit playlists", &common());
    cli.add_subparsers_at(pl, "playlist_command");
    let i = cli.add_parser_at(pl, "ls", &[], "the playlists, or one playlist's tracks", &common());
    cli.parser(i).add(Arg::pos("name", Nargs::Optional).help("a playlist to list the tracks of"));
    let i = cli.add_parser_at(pl, "new", &[], "make a playlist", &common());
    cli.parser(i).add(Arg::pos("name", Nargs::One).help("its name"));
    cli.parser(i).add(Arg::pos("ids", Nargs::Star).metavar("id").help("tracks to put in it, in order"));
    for (name, help) in [("add", "add tracks to a playlist"), ("rm", "take tracks out of a playlist (they stay on the iPod)")] {
        let i = cli.add_parser_at(pl, name, &[], help, &common());
        cli.parser(i).add(Arg::pos("name", Nargs::One).help("the playlist"));
        cli.parser(i).add(Arg::pos("ids", Nargs::Plus).metavar("id").help("track ids (from `flashpod music ls all`)"));
    }
    let i = cli.add_parser_at(pl, "delete", &[], "delete a playlist (its tracks stay on the iPod)", &common());
    cli.parser(i).add(Arg::pos("name", Nargs::One).help("the playlist"));

    let em = cli.add_parser_at(0, "emulate", &[], "boot a firmware or card image in emupod", &[]);
    cli.parser(em).add(Arg::pos("args", Nargs::Star).metavar("ARG"));
    let fl = cli.add_parser_at(0, "flash", &[], "write iPod firmware to a CF/SD card (alias of `firmware write`)", &[]);
    flash_args(cli.parser(fl));
    cli
}

/// burn's arguments (card burn).
fn burn_args(burn: &mut Parser) {
    burn.add(Arg::pos("image", Nargs::One).help("the card image file"));
    burn.add(Arg::pos("device", Nargs::Optional).help("target disk, e.g. /dev/sdc (else a chooser of removable disks)"));
    burn.add(Arg::flag(&["--yes"]).help("skip the typed ERASE confirmation"));
    burn.add(Arg::flag(&["--dry-run"]).help("check everything and show the plan; write nothing"));
    burn.add(Arg::flag(&["--no-verify"]).help("don't read the card back afterwards"));
}

/// flash's arguments (firmware write, and its alias flash).
fn flash_args(fl: &mut Parser) {
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
}

/// 0.x command names and their 1.0 spelling.
const RENAMED: [(&str, &str); 10] = [
    ("ls", "music ls"), ("list", "music ls"), ("add", "music add"), ("rm", "music rm"), ("remove", "music rm"),
    ("delete", "music rm"), ("erase", "music rm"), ("init", "music init"), ("rebuild", "music rebuild"),
    ("burn", "card burn"),
];

/// A 0.x command name in place of the command: say its new name. The
/// top-level options before it are skipped (--mount and --raw take a value).
fn renamed_command(args: &[String]) -> Option<&'static str> {
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        if a == "--mount" || a == "--raw" {
            i += 2;
            continue;
        }
        if a.starts_with('-') {
            i += 1;
            continue;
        }
        return RENAMED.iter().find(|(old, _)| *old == a).map(|(_, new)| *new);
    }
    None
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


fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Some(new) = renamed_command(&args) {
        eprintln!("flashpod: that command is now `flashpod {}` (flashpod 1.0 groups commands: run `flashpod --help`).", new);
        std::process::exit(2);
    }
    let mut ns = match build_cli().parse_args(&args) {
        Ok(ns) => ns,
        Err(code) => std::process::exit(code),
    };
    // the 1.0 names run the handlers the 0.x names did: `command` becomes
    // that handler's name, `group` remembers the spelling (for sudo)
    let group = s(&ns, "command").unwrap_or_default();
    let sub = match group.as_str() {
        "music" => s(&ns, "music_command"),
        "firmware" => s(&ns, "firmware_command"),
        "card" => s(&ns, "card_command"),
        _ => None,
    };
    let handler = match (group.as_str(), sub.as_deref()) {
        ("music", Some(m)) => m.to_string(),
        ("firmware", Some("write")) | ("flash", None) => "flash".to_string(),
        ("card", Some("burn")) => "burn".to_string(),
        (g, Some(m)) => format!("{} {}", g, m),
        (g, None) => g.to_string(),
    };
    ns.insert("command".into(), Val::Str(handler.clone()));
    ns.insert("group".into(), Val::Str(group.clone()));
    let command = handler;
    let code = match command.as_str() {
        "flash" => cmd_flash(&mut ns),
        "firmware extract" => cmd_extract(&ns),
        "burn" => cmd_burn(&ns),
        "ls" | "list" | "add" | "rm" | "remove" | "delete" | "erase" | "init" | "rebuild" | "export" | "playlist" => data_command(&ns, &command),
        other => {
            eprintln!("flashpod {}: not built yet (flashpod 1.0, epic #88).", other);
            2
        }
    };
    std::process::exit(code);
}

/// firmware extract TARGET -o FILE [--image osos|aupd|rsrc] (#99): the
/// firmware partition, or one image, off a card image (no root) or a disk
/// (root, through the platform's raw open), to FILE, with FILE.json
/// beside it. Never overwrites; reads only.
fn cmd_extract(ns: &Ns) -> i32 {
    let target = s(ns, "target").unwrap_or_default();
    let out = s(ns, "output").unwrap_or_default();
    let note_path = format!("{}.json", out);
    for p in [&out, &note_path] {
        if std::path::Path::new(p).exists() {
            eprintln!("flashpod firmware extract: {} already exists; pick another name.", p);
            return 1;
        }
    }
    let opened: std::io::Result<Box<dyn flashpod_core::block::BlockDevice>> = if flash::is_image(&target) {
        FileDevice::open(std::path::Path::new(&target), false).map(|d| Box::new(d) as Box<dyn flashpod_core::block::BlockDevice>)
    } else if !std::path::Path::new(&target).exists() {
        eprintln!("flashpod firmware extract: no such file or disk: {}", target);
        return 1;
    } else {
        crate::platform::current().open_raw(&target, false)
    };
    let mut dev = match opened {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("flashpod firmware extract: reading {} needs root (sudo).", target);
            return 1;
        }
        Err(e) => {
            eprintln!("flashpod firmware extract: can't open {}: {}", target, e);
            return 1;
        }
    };
    let image = s(ns, "image").filter(|i| !i.is_empty());
    let r = match flashpod_core::extract::extract_firmware(dev.as_mut(), image.as_deref()) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("flashpod firmware extract: {}: {}", target, e.message);
            return 1;
        }
    };
    let mut note = r.note.clone();
    note["source"] = serde_json::json!(target);
    let written = std::fs::write(&out, &r.bytes)
        .and_then(|_| std::fs::write(&note_path, serde_json::to_string_pretty(&note).unwrap_or_default() + "\n"));
    if let Err(e) = written {
        eprintln!("flashpod firmware extract: can't write {}: {}", out, e);
        return 1;
    }
    let build = note["installed"]["build"].as_str().unwrap_or("unknown build");
    println!("Extracted {} ({} bytes, sha256 {}) from {} to {}; note in {}.",
             image.as_deref().unwrap_or("the firmware partition"), r.bytes.len(), r.sha256, target, out, note_path);
    println!("Installed firmware: {}", build);
    0
}

/// The command's file-like list: add's paths, or playlist's track ids.
fn data_files(ns: &Ns) -> Vec<String> {
    let ids = list(ns, "ids");
    if ids.is_empty() { list(ns, "files") } else { ids }
}

/// music export's --to and --other-files / --no-other-files.
fn export_opts(ns: &Ns) -> (Option<String>, Option<bool>) {
    let others = if b(ns, "other_files") { Some(true) } else if b(ns, "no_other_files") { Some(false) } else { None };
    (s(ns, "to").filter(|t| !t.is_empty()), others)
}

/// music export over --raw: a card image, or a device (root already).
fn export_raw(ns: &Ns, node: &str, image: bool) -> i32 {
    let (dest, others) = export_opts(ns);
    let opened: Result<Box<dyn flashpod_core::block::BlockDevice>, String> = if image {
        FileDevice::open_image(node, false).map(|d| Box::new(d) as Box<dyn flashpod_core::block::BlockDevice>).map_err(|e| e.message)
    } else {
        let xfer = rawdev::effective_xfer();
        rawdev::RawDevice::open(node, false, xfer).map(|d| Box::new(d) as Box<dyn flashpod_core::block::BlockDevice>)
            .map_err(|e| rawdev::py_os(&e).to_string())
    };
    let mut dev = match opened {
        Ok(d) => d,
        Err(e) => {
            eprintln!("flashpod music export: couldn't open {}: {}", node, e);
            return 1;
        }
    };
    let fs = match ops::open_fat(dev.as_mut()) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("flashpod music export: couldn't read a FAT filesystem on {}: {}", node, e.message);
            return 1;
        }
    };
    export::export(&mut export::FatSource(fs), node, dest.as_deref(), others)
}

fn data_cmd<'a>(cmd: &str, name: &'a Option<String>, files: &'a [String], what: &'a [String], action: &'a str) -> data::Cmd<'a> {
    match cmd {
        "init" => data::Cmd::Init(name.as_deref().unwrap_or("iPod")),
        "rebuild" => data::Cmd::Rebuild(name.as_deref()),
        "add" => data::Cmd::Add(files),
        "playlist" => data::Cmd::Playlist(action, name.as_deref(), files),
        _ => data::Cmd::Rm(what),
    }
}

/// run_raw: ls/add/rm/init/rebuild over the raw device or image `node`,
/// elevating with sudo first when we lack root. `image`: platform.use_image
/// took --raw for a card image (decided once, from the argument).
fn run_raw(ns: &Ns, command: &str, node: &str, image: bool) -> i32 {
    if !image && !crate::platform::current().is_admin() {
        eprintln!("flashpod: {} over the raw device {} needs root \u{2014} elevating via sudo...", command, node);
        let mut extra = vec!["--raw".to_string(), node.to_string()];
        extra.extend(detect::cmd_args(ns));
        crate::platform::current().elevate(&extra);
        eprintln!("flashpod: couldn't get root for raw access to {}.", node);
        return 1;
    }
    if command == "export" {
        return export_raw(ns, node, image);
    }
    if matches!(command, "ls" | "list") {
        return match load_library_raw(node) {
            Some(lib) => cmd_ls(&lib.name, &lib.tracks, &s(ns, "field").unwrap_or_default()),
            None => 1,
        };
    }
    let (name, files, what) = (s(ns, "name").filter(|n| !n.is_empty()), data_files(ns), list(ns, "what"));
    let action = s(ns, "playlist_command").unwrap_or_default();
    let c = data_cmd(command, &name, &files, &what, &action);
    data::run(if image { data::TargetSpec::Image(node) } else { data::TargetSpec::Device(node) }, c)
}

/// cli.main from the --raw dispatch on: find the target (scan, mount
/// table), check the mount, run the command, offer the eject.
fn data_command(ns: &Ns, command: &str) -> i32 {
    if let Some(raw) = s(ns, "raw").filter(|r| !r.is_empty()) {
        let image = std::path::Path::new(&raw).is_file();
        return run_raw(ns, command, &raw, image);
    }
    let mut mount = s(ns, "mount").filter(|m| !m.is_empty());
    let data = matches!(command, "ls" | "list" | "add" | "rm" | "remove" | "delete" | "erase" | "init" | "rebuild" | "export" | "playlist");
    if data && mount.is_none() && host::firewire_sbp2_needed() {
        host::load_firewire_sbp2();
    }
    if matches!(command, "ls" | "list") && mount.is_none() {
        match detect::detect_ls_source(ns) {
            None => return 1,
            Some(detect::Source::Lib(lib)) => return cmd_ls(&lib.name, &lib.tracks, &s(ns, "field").unwrap_or_default()),
            Some(detect::Source::Mount(m)) => mount = Some(m),
            Some(detect::Source::Raw(node)) => return run_raw(ns, command, &node, false),
        }
    }
    if matches!(command, "add" | "rm" | "remove" | "delete" | "erase" | "init" | "rebuild" | "playlist") && mount.is_none() {
        match detect::resolve_raw_target(ns) {
            None => return 1,
            Some(detect::Source::Raw(node)) => return run_raw(ns, command, &node, false),
            Some(detect::Source::Mount(m)) => mount = Some(m),
            Some(detect::Source::Lib(_)) => return 1,
        }
    }
    let mount = match mount.or_else(detect::detect_mount) {
        Some(m) => m,
        None => return 1,
    };
    // the backing device vanished: a stale handle that EIOs on touch
    if let Some(dev) = detect::device_for_mount(&mount) {
        if !std::path::Path::new(&dev).exists() {
            eprintln!("flashpod: {} is a stale mount \u{2014} its device ({}) is gone (did the iPod disconnect?). Unmount it (sudo umount -l {}), reconnect the iPod, and retry.", mount, dev, mount);
            return 1;
        }
    }
    let mut mount = mount;
    let readable = crate::sys::can_enter(&mount);
    if !readable && detect::device_for_mount(&mount).is_some() {
        match detect::remount_as_user(&mount) {
            Some(m) => mount = m,
            None => return 1,
        }
    }
    let unsafe_queue = b(ns, "unsafe_queue");
    let mut problem = host::firewire_queue_problem(&mount);
    if let (Some((disk, _)), false) = (&problem, unsafe_queue) {
        host::pin_firewire_queue(disk);
        problem = host::firewire_queue_problem(&mount);
    }
    if let (Some((disk, bad)), false) = (&problem, unsafe_queue) {
        eprintln!("flashpod: {} is a FireWire iPod and the host I/O settings are UNSAFE for it:\n  {}\nLarge/queued reads can crash early iPod FireWire bridges and corrupt the filesystem.\nFix for this attach:\n  sudo sh -c 'echo 4 >/sys/block/{d}/queue/max_sectors_kb; echo 0 >/sys/block/{d}/queue/read_ahead_kb; echo 1 >/sys/block/{d}/device/queue_depth'\nFix permanently (settings reset on every re-attach):\n  sudo cp {} /etc/udev/rules.d/ && sudo udevadm control --reload\n(--unsafe-queue overrides this check)",
                  mount, bad.join(", "), UDEV_RULE, d = disk);
        return 1;
    }
    if command == "export" {
        let (dest, others) = export_opts(ns);
        return export::export(&mut export::MountSource(mount.clone()), &mount, dest.as_deref(), others);
    }
    if matches!(command, "ls" | "list") {
        let mut t = mount::MountTarget::new(&mount);
        return match ops::load(&mut t, &mut entropy()) {
            Ok(lib) => cmd_ls(&lib.name, &lib.tracks, &s(ns, "field").unwrap_or_default()),
            Err(e) => {
                data::say_mount_load_error(&mount, &e);
                1
            }
        };
    }
    let (name, files, what) = (s(ns, "name").filter(|n| !n.is_empty()), data_files(ns), list(ns, "what"));
    let action = s(ns, "playlist_command").unwrap_or_default();
    let rc = data::run(data::TargetSpec::Mount(&mount), data_cmd(command, &name, &files, &what, &action));
    if matches!(command, "add" | "rm" | "remove" | "delete" | "erase") {
        detect::offer_eject(detect::device_for_mount(&mount).as_deref(), rc);
    }
    rc
}

/// resources.udev_rule(): where a pip install keeps the rule.
const UDEV_RULE: &str = "contrib/99-flashpod-firewire-ipod.rules";
