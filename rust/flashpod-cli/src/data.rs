//! `init`, `add`, `rm` and `rebuild` over `--raw IMAGE`: cli.run_raw's image
//! branch and the commands it dispatches to (cmd_init_raw, cmd_add_raw /
//! _cmd_add_core, cmd_rm_raw / _cmd_rm_core, cmd_rebuild_raw), message for
//! message, on a terminal-less run. On a terminal the progress window and
//! the trimming questions aren't ported yet; those hand back to the caller.

use crate::entropy;
use flashpod_core::block::FileDevice;
use flashpod_core::error::{Error, ErrorKind};
use flashpod_core::fat::Fat32;
use flashpod_core::inspect::Options;
use flashpod_core::library::{self, Hooks};
use flashpod_core::ops;
use flashpod_core::tags::FileTags;
use serde_json::Value;

/// cli.fmt_size: MiB with one decimal, GiB with two past 1024 MiB.
pub fn fmt_size(n: i64) -> String {
    let mib = n as f64 / (1u64 << 20) as f64;
    if mib >= 1024.0 {
        format!("{:.2} GiB", mib / 1024.0)
    } else {
        format!("{:.1} MiB", mib)
    }
}

/// cli.fmt_duration.
fn fmt_duration(secs: f64) -> String {
    let s = secs as u64;
    let (m, s) = (s / 60, s % 60);
    if m > 0 {
        format!("{}m{:02}s", m, s)
    } else {
        format!("{}s", s)
    }
}

/// time.monotonic, frozen when FLASHPOD_NOW pins the clock (the test
/// harness freezes Python's the same way).
struct Clock(Option<std::time::Instant>);

impl Clock {
    fn start() -> Self {
        Clock(if std::env::var_os("FLASHPOD_NOW").is_some() { None } else { Some(std::time::Instant::now()) })
    }
    fn secs(&self) -> f64 {
        self.0.map_or(0.0, |t| t.elapsed().as_secs_f64())
    }
}

/// library._db_size_limit and whether FLASHPOD_DB_LIMIT is set at all.
fn options() -> Options {
    let mut o = Options::default();
    if let Ok(v) = std::env::var("FLASHPOD_DB_LIMIT") {
        o.db_limit_set = true;
        if let Ok(n) = v.trim().parse::<i64>() {
            o.db_limit = n;
        }
    }
    o
}

fn orunknown(s: Option<&str>) -> String {
    match s {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => "(unknown)".to_string(),
    }
}

fn error_text(e: &Error) -> String {
    e.details.get("error").and_then(|v| v.as_str()).unwrap_or(&e.message).to_string()
}

pub enum Cmd<'a> {
    Init(&'a str),
    Rebuild(Option<&'a str>),
    Rm(&'a [String]),
    Add(&'a [String]),
}

/// run_raw for an image: card.open_target(node, write=True) (the flock),
/// then the command. offer_eject skips image files.
pub fn run_raw_image(node: &str, cmd: Cmd) -> i32 {
    let dev = FileDevice::open_image(node, true).and_then(|d| d.try_lock(true, node).map(|_| d));
    let mut dev = match dev {
        Ok(d) => d,
        Err(e) if e.kind == ErrorKind::CardBusy => {
            eprintln!("flashpod: {} is in use by another program (an emulator, the web app, another flashpod); try again when it's done.", node);
            return 1;
        }
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, error_text(&e));
            return 1;
        }
    };
    let mut ent = entropy();
    if let Cmd::Init(name) = cmd {
        return match ops::init(&mut dev, name, true, &mut ent) {
            Ok(_) => {
                println!("Initialized iPod directory structure on {}", node);
                0
            }
            Err(e) => {
                eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, error_text(&e));
                1
            }
        };
    }
    let mut fs = match ops::open_fat(&mut dev) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, error_text(&e));
            return 1;
        }
    };
    match cmd {
        Cmd::Init(_) => unreachable!(),
        Cmd::Rebuild(name) => rebuild(&mut fs, node, name.unwrap_or("iPod"), &mut ent),
        Cmd::Rm(what) => rm(&mut fs, node, what, &mut ent),
        Cmd::Add(paths) => add(&mut fs, node, paths, &mut ent),
    }
}

/// cmd_rebuild_raw: rebuild the iTunesDB from the music on the card.
fn rebuild(fs: &mut Fat32, node: &str, name: &str, ent: &mut flashpod_core::entropy::Entropy) -> i32 {
    let lib = match ops::rebuild_fs(fs, name, true, &mut FileTags, ent, &mut library::NoHooks, &mut |m| eprintln!("{}", m)) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            return 1;
        }
    };
    let n = lib.tracks.len();
    println!("Rebuilt the iTunesDB on {}: {} track{} recovered from the card.", node, n, if n != 1 { "s" } else { "" });
    0
}

/// cmd_rm_raw / _cmd_rm_core / _say_no_such_track.
fn rm(fs: &mut Fat32, node: &str, what: &[String], ent: &mut flashpod_core::entropy::Entropy) -> i32 {
    let victims = match ops::remove_fs(fs, what, ent) {
        Ok(v) => v,
        Err(e) => match e.kind {
            ErrorKind::CorruptLibrary => {
                eprintln!("flashpod: the iTunesDB on {} is corrupt ({}) \u{2014} run `flashpod rebuild` to rebuild it from the files on the card.", node, e.message);
                return 1;
            }
            ErrorKind::NotInitialized => {
                eprintln!("flashpod: no iTunesDB on {} (run `flashpod init` first?)", node);
                return 1;
            }
            ErrorKind::NoSuchTrack => {
                let why = e.details.get("reason").and_then(|v| v.as_str()).unwrap_or("");
                return match why {
                    "name_required" => {
                        eprintln!("flashpod rm {}: name required", what[0]);
                        2
                    }
                    "no_match" => {
                        eprintln!("flashpod rm: no tracks match that name (see `flashpod ls`)");
                        1
                    }
                    "bad_ids" => {
                        eprintln!("flashpod rm: expected track ids, or `artist <name>` / `album <name>`");
                        2
                    }
                    _ => {
                        let missing: Vec<String> = e.details.get("missing").and_then(|v| v.as_array())
                            .map(|a| a.iter().map(|i| i.to_string()).collect()).unwrap_or_default();
                        eprintln!("flashpod rm: no track with id {} (see `flashpod ls all`)", missing.join(", "));
                        1
                    }
                };
            }
            _ => {
                eprintln!("flashpod: {}", e.message);
                return 1;
            }
        },
    };
    for t in &victims {
        println!("Removed: {} - {}", orunknown(t.artist.as_deref()), orunknown(t.title.as_deref()));
    }
    if victims.len() > 1 {
        println!("Removed {} tracks", victims.len());
    }
    0
}

/// apply_add's hooks, worded as _cmd_add_core's non-terminal LineWindow:
/// each file's "Adding:" line on stdout, nothing for progress ticks.
struct AddHooks<'a> {
    labels: &'a [String],
}

impl Hooks for AddHooks<'_> {
    fn progress(&mut self, stage: &str, done: u64, total: u64, _item: &str) {
        if stage == "copy" {
            println!("[{}/{}] Adding: {}...", done, total, self.labels[done as usize - 1]);
        }
    }
}

/// cmd_add_raw / _cmd_add_core, off a terminal.
fn add(fs: &mut Fat32, node: &str, paths: &[String], ent: &mut flashpod_core::entropy::Entropy) -> i32 {
    if paths.is_empty() {
        // prompt_for_paths -> prompt_for_path, stdin not a terminal
        eprintln!("flashpod add: no paths given and stdin is not a terminal");
        return 1;
    }
    if paths[0].is_empty() {
        return 1;
    }
    let files = library::expand(paths, &mut |m| eprintln!("{}", m));
    if files.is_empty() {
        eprintln!("nothing to add");
        return 1;
    }
    let lib = match ops::load(fs, ent) {
        Ok(l) => l,
        Err(e) if e.kind == ErrorKind::NotInitialized => {
            eprintln!("flashpod: no iTunesDB on {} (run `flashpod init` first?)", node);
            return 1;
        }
        Err(e) if e.kind == ErrorKind::CorruptLibrary => {
            eprintln!("flashpod: the iTunesDB is corrupt and can't be read ({}). Run `flashpod rebuild` to rebuild it from the files on the card.", e.message);
            return 1;
        }
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            return 1;
        }
    };
    let opts = options();
    let mut plan = match ops::plan_add_fs(fs, &files, &mut FileTags, ent, opts, false, false, Some(lib), &mut library::NoHooks) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            return 1;
        }
    };
    let strs = |v: &Value| -> Vec<String> {
        v.as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default()
    };
    for m in strs(&plan.json["unreadable"]) {
        eprintln!("{}", m);
    }
    let mut failures = strs(&plan.json["failed"]).len();
    let skipped = strs(&plan.json["duplicates"]).len();
    let mut keep = plan.items.len();

    if keep > 0 {
        if let Some(err) = plan.json["free_error"].as_str() {
            eprintln!("flashpod add: couldn't check free space ({}); proceeding anyway", err);
        }
        if let Some(budget) = plan.json["budget"].as_i64() {
            let batch: i64 = plan.items.iter().map(|(_, t)| t.size as i64).sum();
            if batch <= budget {
                println!("flashpod add: this batch is {}. You've got {} more than you need, Dude. That's gnarly!",
                         fmt_size(batch), fmt_size(budget - batch));
            } else {
                eprintln!("flashpod add: batch is {} but only {} free; trim the selection or free space (can't prompt here).",
                          fmt_size(batch), fmt_size(budget));
                return 1;
            }
        }
    }

    if keep > 0 && opts.db_limit != 0 {
        let new: Vec<&flashpod_core::itunesdb::Track> = plan.items.iter().map(|(_, t)| t).collect();
        let mut memo = None;
        let c = library::ceiling(&plan.lib, &new, opts, &mut || ops::probe(fs, &mut memo), ent);
        let have = plan.lib.tracks.len();
        if c.kind == Some("tracks") {
            if c.fit == keep {
                println!("flashpod add: the iPod's database will be {} bytes \u{2014} more than stock firmware loads, but this card's firmware has the large-library fix ({} of {} tracks).",
                         commas(c.projected.unwrap_or(0)), commas((have + keep) as i64), commas(c.limit));
            } else {
                eprintln!("flashpod add: that is past the {} tracks the firmware lists; the rest would be on the card but never shown.", commas(c.limit));
                if c.fit == 0 {
                    eprintln!("  The library already holds {}; remove tracks (`flashpod rm`) or set FLASHPOD_DB_LIMIT=0 to override.", commas(have as i64));
                    return 1;
                }
                eprintln!("  {} of the {} new tracks would fit; trim the batch, or set FLASHPOD_DB_LIMIT=0 to override (can't prompt here).", c.fit, keep);
                return 1;
            }
        } else if c.projected.unwrap_or(0) > c.limit {
            eprintln!("flashpod add: this batch would grow the iPod's database to {} bytes \u{2014} past the ~{}-byte ceiling early iPod firmware can load (the iPod would then show ZERO songs).",
                      commas(c.projected.unwrap_or(0)), commas(c.limit));
            if c.firmware.is_none() && !opts.db_limit_set {
                eprintln!("  (Couldn't read the card's firmware to check for the large-library fix that flashpod 1.6 has; assuming stock.)");
            }
            if c.fit == 0 {
                eprintln!("  The library alone already exceeds the ceiling; remove tracks (`flashpod rm`) or set FLASHPOD_DB_LIMIT=0 to override.");
                return 1;
            }
            eprintln!("  {} of the {} new tracks would fit; trim the batch, or set FLASHPOD_DB_LIMIT=0 to override (can't prompt here).", c.fit, keep);
            return 1;
        }
    }
    keep = keep.min(plan.items.len());

    let clock = Clock::start();
    let labels: Vec<String> = plan.items[..keep]
        .iter()
        .map(|(_, t)| {
            let title = t.title.clone().unwrap_or_default();
            match t.artist.as_deref() {
                Some(a) if !a.is_empty() => format!("{} \u{2014} {}", title, a),
                _ => title,
            }
        })
        .collect();
    let paths_kept: Vec<String> = plan.items[..keep].iter().map(|(p, _)| p.clone()).collect();
    let mut hooks = AddHooks { labels: &labels };
    let rep = match ops::apply_add_fs(fs, &mut plan, Some(keep), false, &mut |p| std::fs::read(p), ent, &mut hooks) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            return 1;
        }
    };
    if let Some(failed) = rep["failed"].as_array() {
        for f in failed {
            let path = f["path"].as_str().unwrap_or("");
            let nr = paths_kept.iter().position(|p| p == path).map_or(0, |i| i + 1);
            eprintln!("[{}/{}] FAILED {}: {}", nr, keep, path, f["error"].as_str().unwrap_or(""));
        }
        failures += failed.len();
    }
    let added = rep["added"].as_array().map_or(0, |a| a.len());
    let added_bytes = rep["bytes"].as_i64().unwrap_or(0);
    let secs = clock.secs();
    let mut parts = vec![format!("{} track{} added", added, if added != 1 { "s" } else { "" })];
    if skipped > 0 {
        parts.push(format!("{} skipped (already on iPod)", skipped));
    }
    if failures > 0 {
        parts.push(format!("{} failed", failures));
    }
    let mut summary = format!("{} in {}", parts.join(", "), fmt_duration(secs));
    if added_bytes > 0 && secs > 0.0 {
        let rate = added_bytes as f64 / secs;
        let r = if rate < (1u64 << 20) as f64 {
            format!("{:.0} KiB", rate / 1024.0)
        } else {
            format!("{:.1} MiB", rate / (1u64 << 20) as f64)
        };
        summary += &format!(" ({:.1} MiB at {}/s)", added_bytes as f64 / (1u64 << 20) as f64, r);
    }
    if failures > 0 {
        eprintln!("{}", summary);
        return 1;
    }
    println!("{}", summary);
    0
}

/// Python's f"{n:,}".
fn commas(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 {
        format!("-{}", out)
    } else {
        out
    }
}
