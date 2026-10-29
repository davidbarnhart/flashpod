//! `init`, `add`, `rm` and `rebuild` over `--raw IMAGE`: cli.run_raw's image
//! branch and the commands it dispatches to (cmd_init_raw, cmd_add_raw /
//! _cmd_add_core, cmd_rm_raw / _cmd_rm_core, cmd_rebuild_raw), message for
//! message, on a terminal or off one.

use crate::entropy;
use crate::term;
use crate::window::{self, EtaEstimator, LineWindow};
use crate::winnow;
use std::cell::RefCell;
use flashpod_core::block::FileDevice;
use flashpod_core::error::{Error, ErrorKind};
use flashpod_core::inspect::Options;
use flashpod_core::library::{self, Hooks};
use flashpod_core::ops::{self, Target};
use crate::mount::{self, MountTarget};
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

/// library._db_size_limit and whether FLASHPOD_DB_LIMIT is set at all.
pub fn options() -> Options {
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

/// Which target a command runs over, for the messages that differ.
pub struct Ctx {
    pub node: String,
    pub mount: bool,
}

impl Ctx {
    /// An error from the target: over a mount, main's `except OSError`
    /// (_report_io_error); over the raw FAT, the message.
    fn fail(&self, e: &Error) -> i32 {
        if self.mount {
            report_io_error(&self.node, e);
        } else {
            eprintln!("flashpod: {}", e.message);
        }
        1
    }
}

/// _report_io_error.
pub fn report_io_error(mount: &str, e: &Error) {
    if mount::errno(e) == Some(libc::EIO) {
        eprintln!("flashpod: I/O error talking to the iPod at {} \u{2014} it likely disconnected, or the cable/connector is flaky. Reconnect it, remount, and retry. If it persists the filesystem may be damaged (reformat with `flashpod flash`).", mount);
    } else {
        eprintln!("flashpod: cannot access {}: {}", mount, e.message);
    }
}

/// _say_mount_load_error.
pub fn say_mount_load_error(mount: &str, e: &Error) {
    if e.kind == ErrorKind::NotInitialized {
        if !std::path::Path::new(mount).is_dir() {
            eprintln!("flashpod: {} does not exist \u{2014} is the iPod mounted?", mount);
        } else {
            eprintln!("flashpod: no iTunesDB on {} (run `flashpod init` first?)", mount);
        }
    } else {
        eprintln!("flashpod: failed to read iTunesDB: {}", e.message);
    }
}

/// The --mount branch of cli.main for a directory: the command over
/// target.MountTarget.
pub fn run_mount(mount: &str, cmd: Cmd) -> i32 {
    let mut t = MountTarget::new(mount);
    let cx = Ctx { node: mount.to_string(), mount: true };
    let mut ent = entropy();
    match cmd {
        Cmd::Init(name) => match t.init_structure(name, &mut ent) {
            Ok(()) => {
                println!("Initialized iPod directory structure at {}", mount);
                0
            }
            Err(e) => cx.fail(&e),
        },
        Cmd::Rebuild(name) => {
            if !t.music_dir_exists() {
                eprintln!("flashpod: no iPod_Control/Music on {} \u{2014} is this an iPod?", mount);
                return 1;
            }
            rebuild(&mut t, &cx, name.unwrap_or("iPod"), &mut ent)
        }
        Cmd::Rm(what) => rm(&mut t, &cx, what, &mut ent),
        Cmd::Add(paths) => add(&mut t, &cx, paths, &mut ent, false),
    }
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
    let cx = Ctx { node: node.to_string(), mount: false };
    match cmd {
        Cmd::Init(_) => unreachable!(),
        Cmd::Rebuild(name) => rebuild(&mut fs, &cx, name.unwrap_or("iPod"), &mut ent),
        Cmd::Rm(what) => rm(&mut fs, &cx, what, &mut ent),
        Cmd::Add(paths) => add(&mut fs, &cx, paths, &mut ent, false),
    }
}

/// _raw_rebuild's window: one "recovering" line updated in place.
struct RebuildUi<'a>(&'a RefCell<LineWindow>);

impl Hooks for RebuildUi<'_> {
    fn progress(&mut self, _stage: &str, nr: u64, total: u64, item: &str) {
        self.0.borrow_mut().update(format!("[{}/{}] recovering {}...", nr, total, library::basename(item)));
    }
}

/// _raw_rebuild: the library rebuilt from the files on the card, with a
/// progress line; saved only with `save`.
fn raw_rebuild(fs: &mut dyn Target, name: &str, save: bool, ent: &mut flashpod_core::entropy::Entropy)
    -> Result<flashpod_core::itunesdb::Library, Error>
{
    let win = RefCell::new(LineWindow::new());
    let lib = ops::rebuild_fs(fs, name, save, &mut FileTags, ent, &mut RebuildUi(&win),
                              &mut |m| win.borrow_mut().note(&m))?;
    win.borrow_mut().clear();
    Ok(lib)
}

/// cmd_rebuild_raw / cmd_rebuild: rebuild the iTunesDB from the music on
/// the card.
fn rebuild(fs: &mut dyn Target, cx: &Ctx, name: &str, ent: &mut flashpod_core::entropy::Entropy) -> i32 {
    let lib = match raw_rebuild(fs, name, true, ent) {
        Ok(l) => l,
        Err(e) => return cx.fail(&e),
    };
    let n = lib.tracks.len();
    println!("Rebuilt the iTunesDB on {}: {} track{} recovered from the {}.", cx.node, n, if n != 1 { "s" } else { "" },
             if cx.mount { "iPod" } else { "card" });
    0
}

/// cmd_rm_raw / _cmd_rm_core / _say_no_such_track.
fn rm(fs: &mut dyn Target, cx: &Ctx, what: &[String], ent: &mut flashpod_core::entropy::Entropy) -> i32 {
    let node = cx.node.as_str();
    let victims = match ops::remove_fs(fs, what, ent) {
        Ok(v) => v,
        Err(e) => match e.kind {
            ErrorKind::CorruptLibrary | ErrorKind::NotInitialized if cx.mount => {
                say_mount_load_error(node, &e);
                return 1;
            }
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
            _ => return cx.fail(&e),
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

/// _cmd_add_core's callbacks for both passes, drawn in the LineWindow.
struct AddUi {
    win: LineWindow,
    labels: Vec<String>,
    eta: Option<EtaEstimator>,
    batch_done: i64,
    cur: usize,
    npend: u64,
    last: f64,
}

impl Hooks for AddUi {
    fn progress(&mut self, stage: &str, done: u64, total: u64, item: &str) {
        match stage {
            "tags" => self.win.add(format!("[{}/{}] Reading tags: {}...", done, total, library::basename(item)), true),
            "copy" => {
                self.cur = done as usize;
                self.npend = total;
                self.last = 0.0;
                self.win.add(format!("[{}/{}] Adding: {}...", done, total, self.labels[self.cur - 1]), false);
            }
            "write" => {
                let now = window::monotonic();
                // throttle to ~8/sec; always let 100% through
                if done < total && now - self.last < 0.125 {
                    return;
                }
                self.last = now;
                let eta = self.eta.as_mut().unwrap();
                eta.update(self.batch_done + done.min(total) as i64);
                let mib = (1u64 << 20) as f64;
                let pct = if total > 0 { done * 100 / total } else { 100 };
                self.win.update(format!("[{}/{}] Adding: {}... {}% ({:.1}/{:.1} MiB)", self.cur, self.npend,
                                        self.labels[self.cur - 1], pct, done as f64 / mib, total as f64 / mib));
                let status = eta.remaining_text();
                self.win.set_status(status);
            }
            _ => {}
        }
    }
    fn report(&mut self, msg: &str) {
        self.win.note(msg);
    }
    fn duplicate(&mut self, nr: u64, total: u64, path: &str) {
        self.win.update(format!("[{}/{}] skipping {}: already on iPod", nr, total, library::basename(path)));
    }
    fn copied(&mut self, _nr: u64, _total: u64, size: u64) {
        self.batch_done += size as i64;
    }
    fn failed(&mut self, nr: u64, total: u64, path: &str, size: u64, error: &str) {
        self.win.note(&format!("[{}/{}] FAILED {}: {}", nr, total, path, error));
        if let Some(e) = self.eta.as_mut() {
            e.exclude(size as i64);
        }
    }
}

/// cmd_add_raw / _cmd_add_core.
/// `fresh`: the card was inited a moment ago (the post-flash offer), so
/// the picker is told (0, None) instead of reading the database.
fn add(fs: &mut dyn Target, cx: &Ctx, paths: &[String], ent: &mut flashpod_core::entropy::Entropy, fresh: bool) -> i32 {
    let prompted;
    let paths = if paths.is_empty() {
        let (existing, db_bytes) = if fresh { (Some(0), None) } else { db_facts(fs, ent) };
        match prompt_for_paths(existing, db_bytes) {
            Some(p) => {
                prompted = p;
                &prompted[..]
            }
            None => return 1,
        }
    } else {
        paths
    };
    if paths[0].is_empty() {
        return 1;
    }
    let files = library::expand(paths, &mut |m| eprintln!("{}", m));
    if files.is_empty() {
        eprintln!("nothing to add");
        return 1;
    }
    let node = cx.node.as_str();
    let mut rebuilt = false;
    let lib = match ops::load(fs, ent) {
        Ok(l) => l,
        // no rebuild offer over a mount: a corrupt DB is reported, as always
        Err(e) if cx.mount => {
            say_mount_load_error(node, &e);
            return 1;
        }
        Err(e) if e.kind == ErrorKind::NotInitialized => {
            eprintln!("flashpod: no iTunesDB on {} (run `flashpod init` first?)", node);
            return 1;
        }
        Err(e) if e.kind == ErrorKind::CorruptLibrary => {
            if !term::stdin_isatty() {
                eprintln!("flashpod: the iTunesDB is corrupt and can't be read ({}). Run `flashpod rebuild` to rebuild it from the files on the card.", e.message);
                return 1;
            }
            println!("Arrr! Blow me down \u{2014} yer iPod's song-ledger be scuttled, naught but bilgewater where the music log oughta be. We'll not be stowin' fresh booty aboard till she's careened an' a new log writ proper.");
            if !term::ask_yes("Shall I scrape 'er hull an' rebuild, ye salty sea-dog? [Aye/Nay] ", true) {
                println!("flashpod: belay that \u{2014} nothin' added.");
                return 0;
            }
            rebuilt = true;
            match raw_rebuild(fs, "iPod", false, ent) {
                Ok(l) => l,
                Err(e) => {
                    eprintln!("flashpod: {}", e.message);
                    return 1;
                }
            }
        }
        Err(e) => {
            eprintln!("flashpod: {}", e.message);
            return 1;
        }
    };
    let opts = options();
    let mut ui = AddUi { win: LineWindow::new(), labels: Vec::new(), eta: None, batch_done: 0, cur: 0, npend: 0, last: 0.0 };
    let mut plan = match ops::plan_add_fs(fs, &files, &mut FileTags, ent, opts, false, false, Some(lib), &mut ui) {
        Ok(p) => p,
        Err(e) => return cx.fail(&e),
    };
    let count = |v: &Value| v.as_array().map_or(0, |a| a.len());
    let mut failures = count(&plan.json["failed"]);
    let skipped = count(&plan.json["duplicates"]);
    ui.win.clear();
    let (mut dropped, mut db_dropped) = (0, 0);

    if !plan.items.is_empty() {
        if let Some(err) = plan.json["free_error"].as_str() {
            ui.win.note(&format!("flashpod add: couldn't check free space ({}); proceeding anyway", err));
        }
        if let Some(budget) = plan.json["budget"].as_i64() {
            let batch: i64 = plan.items.iter().map(|(_, t)| t.size as i64).sum();
            if batch <= budget {
                println!("flashpod add: this batch is {}. You've got {} more than you need, Dude. That's gnarly!",
                         fmt_size(batch), fmt_size(budget - batch));
            } else if !term::stdin_isatty() {
                eprintln!("flashpod add: batch is {} but only {} free; trim the selection or free space (can't prompt here).",
                          fmt_size(batch), fmt_size(budget));
                return 1;
            } else {
                let kept = match winnow::winnow(&plan.items, budget) {
                    Some(k) => k,
                    None => {
                        println!("flashpod add: nothing added.");
                        return 0;
                    }
                };
                dropped = plan.items.len() - kept.len();
                plan.items = kept.iter().map(|&i| plan.items[i].clone()).collect();
                if plan.items.is_empty() {
                    println!("flashpod add: nothing selected; nothing added.");
                    return 0;
                }
            }
        }
    }

    // will the grown iTunesDB still load on the iPod?
    let mut keep = plan.items.len();
    if keep > 0 && opts.db_limit != 0 {
        let new: Vec<&flashpod_core::itunesdb::Track> = plan.items.iter().map(|(_, t)| t).collect();
        let mut memo = None;
        let c = library::ceiling(&plan.lib, &new, opts, &mut || fs.probe(&mut memo), ent);
        let have = plan.lib.tracks.len();
        let (past, extra) = if c.kind == Some("tracks") {
            if c.fit == keep {
                println!("flashpod add: the iPod's database will be {} bytes \u{2014} more than stock firmware loads, but this card's firmware has the large-library fix ({} of {} tracks).",
                         commas(c.projected.unwrap_or(0)), commas((have + keep) as i64), commas(c.limit));
                (false, "")
            } else {
                eprintln!("flashpod add: that is past the {} tracks the firmware lists; the rest would be on the card but never shown.", commas(c.limit));
                if c.fit == 0 {
                    eprintln!("  The library already holds {}; remove tracks (`flashpod rm`) or set FLASHPOD_DB_LIMIT=0 to override.", commas(have as i64));
                    return 1;
                }
                (true, "cap")
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
            (true, "ceiling")
        } else {
            (false, "")
        };
        if past {
            if !term::stdin_isatty() {
                eprintln!("  {} of the {} new tracks would fit; trim the batch, or set FLASHPOD_DB_LIMIT=0 to override (can't prompt here).", c.fit, keep);
                return 1;
            }
            let all = if extra == "cap" {
                "  Add ALL anyway \u{2014} the iPod won't list the extra tracks? [y/N] "
            } else {
                "  Add ALL anyway \u{2014} the iPod will likely show an empty library? [y/N] "
            };
            if term::ask_yes(&format!("  {} of {} new tracks fit under the {}. Add just those? [Y/n] ", c.fit, keep, extra), true) {
                db_dropped = keep - c.fit;
                keep = c.fit;
            } else if !term::ask_yes(all, false) {
                println!("flashpod add: nothing added.");
                return 0;
            }
        }
    }

    // pass 2: copy what survived, then the one save
    let start = window::monotonic();
    ui.eta = Some(EtaEstimator::new(plan.items[..keep].iter().map(|(_, t)| t.size as i64).sum()));
    ui.labels = plan.items[..keep]
        .iter()
        .map(|(_, t)| {
            let title = t.title.clone().unwrap_or_default();
            match t.artist.as_deref() {
                Some(a) if !a.is_empty() => format!("{} \u{2014} {}", title, a),
                _ => title,
            }
        })
        .collect();
    let rep = match ops::apply_add_fs(fs, &mut plan, Some(keep), rebuilt, &mut |p| std::fs::read(p), ent, &mut ui) {
        Ok(r) => r,
        Err(e) => return cx.fail(&e),
    };
    failures += count(&rep["failed"]);
    let added = count(&rep["added"]);
    let added_bytes = rep["bytes"].as_i64().unwrap_or(0);
    ui.win.set_status(None);
    let secs = window::monotonic() - start;
    let mut parts = vec![format!("{} track{} added", added, if added != 1 { "s" } else { "" })];
    if dropped > 0 {
        parts.push(format!("{} dropped to fit free space", dropped));
    }
    if db_dropped > 0 {
        parts.push(format!("{} dropped to fit the database ceiling", db_dropped));
    }
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

/// os.path.expanduser for "~" and "~/...".
pub fn expanduser(p: &str) -> String {
    match (p.strip_prefix('~'), std::env::var("HOME")) {
        (Some(rest), Ok(home)) if rest.is_empty() || rest.starts_with('/') => {
            let home = home.trim_end_matches('/');
            format!("{}{}", if home.is_empty() { "/" } else { home }, rest)
        }
        _ => p.to_string(),
    }
}

/// cli._raw_db_facts / _mounted_db_facts: best-effort (track count, DB
/// file bytes) for the picker's status bar.
fn db_facts(fs: &mut dyn Target, ent: &mut flashpod_core::entropy::Entropy) -> (Option<i64>, Option<i64>) {
    match fs.db_bytes() {
        Some(raw) if !raw.is_empty() => match flashpod_core::itunesdb::parse_bytes_with(&raw, ent) {
            Ok(lib) => (Some(lib.tracks.len() as i64), Some(raw.len() as i64)),
            Err(_) => (None, None),
        },
        _ => (None, None),
    }
}

/// cli.prompt_for_paths: on a terminal, a typed path or (Enter alone) the
/// directory picker, starting at the user's home; else prompt_for_path.
pub fn prompt_for_paths(existing: Option<i64>, db_bytes: Option<i64>) -> Option<Vec<String>> {
    if term::stdin_isatty() && term::stdout_isatty() {
        // The picker takes over the whole screen; pause first so what was
        // just printed can be read before it disappears.
        let typed = match term::input_line("\nPress ENTER to select content to add to the iPod (or type a path): ") {
            Some(t) => t.trim().to_string(),
            None => {
                println!();
                eprintln!("flashpod add: cancelled");
                return None;
            }
        };
        if !typed.is_empty() {
            let path = expanduser(&typed);
            if !std::path::Path::new(&path).exists() {
                eprintln!("flashpod add: no such file or directory: {}", path);
                return None;
            }
            return Some(vec![path]);
        }
        let picked = crate::pathpicker::DirectoryPicker::new(
            &crate::pathpicker::invoking_home(),
            Some(Box::new(crate::pathpicker::count_audio)),
            Some(crate::pathpicker::picker_status(existing, db_bytes)),
        ).run();
        let picked = match picked {
            Ok(p) => p,
            Err(e) => {
                eprintln!("flashpod: directory picker failed ({}); type a path instead.", e);
                return prompt_for_path().map(|p| vec![p]);
            }
        };
        let picked = match picked {
            None => {
                eprintln!("flashpod add: cancelled");
                return None;
            }
            Some(p) if p.is_empty() => {
                eprintln!("flashpod add: nothing selected");
                return None;
            }
            Some(p) => p,
        };
        println!("Adding {} selected path{}:", picked.len(), if picked.len() != 1 { "s" } else { "" });
        for p in &picked {
            println!("  {}", p);
        }
        return Some(picked);
    }
    prompt_for_path().map(|p| vec![p])
}

/// cli.prompt_for_path (no tab completion).
fn prompt_for_path() -> Option<String> {
    if !term::stdin_isatty() {
        eprintln!("flashpod add: no paths given and stdin is not a terminal");
        return None;
    }
    let path = match term::input_line("File or directory to add (TAB to complete): ") {
        Some(p) => expanduser(p.trim()),
        None => {
            println!();
            return None;
        }
    };
    if path.is_empty() {
        eprintln!("flashpod add: nothing entered");
        return None;
    }
    if !std::path::Path::new(&path).exists() {
        eprintln!("flashpod add: no such file or directory: {}", path);
        return None;
    }
    Some(path)
}

/// _offer_init_after_flash_win, which images take (platform.image): init
/// and then add through the flash's still-open handle, the partition table
/// not yet written.
pub fn offer_init_after_flash_image(dev: &str, file: &mut FileDevice, data_start: u64) {
    let max_xfer = std::env::var("FLASHPOD_RAW_MAX_XFER").ok().and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(flashpod_core::fat::IMAGE_MAX_XFER).max(1);
    let sec = flashpod_core::fat::Sectors { dev: file, part_start: data_start, max_xfer };
    let mut fs = match flashpod_core::fat::Fat32::open(sec) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", dev, e);
            return;
        }
    };
    if !term::ask_yes(&format!("\nThe card still needs the iPod database before it can take music (\"flashpod init\").\nRun init on {} now? [Y/n] ", dev), true) {
        eprintln!("Skipped. Later: run `flashpod init --raw {}`.", dev);
        return;
    }
    let mut ent = entropy();
    if let Err(e) = fs.init_structure("iPod", &mut ent) {
        eprintln!("flashpod: {}", e.message);
        return;
    }
    println!("Initialized iPod directory structure on {}", dev);
    if term::ask_yes("\nMusic can be loaded onto the card now, or later when it is in the iPod.\nLoad music onto the card now? [Y/n] ", true) {
        let cx = Ctx { node: dev.to_string(), mount: false };
        add(&mut fs, &cx, &[], &mut ent, true);
    }
}

/// run_raw for a device node (root already): open_raw_target, the command,
/// then the eject offer.
pub fn run_raw_device(node: &str, cmd: Cmd) -> i32 {
    if !crate::host::ensure_firewire_disk_safe(node) {
        eprintln!("flashpod: couldn't pin safe FireWire I/O settings for {}; the kernel may probe it unsafely after writes.", node);
    }
    let xfer = crate::rawdev::effective_xfer();
    let mut dev = match crate::rawdev::RawDevice::open(node, true, xfer) {
        Ok(d) => d,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            eprintln!("flashpod: need root to write {}.\n  sudo {} ... --raw {}", node, crate::host::self_cmd().join(" "), node);
            return 1;
        }
        Err(e) => {
            eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, crate::rawdev::py_os(&e));
            return 1;
        }
    };
    let rc = {
        let mut fs = match crate::rawdev::open_raw_fat(&mut dev, xfer) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("flashpod: couldn't open a FAT filesystem on {}: {}", node, e);
                return 1;
            }
        };
        let cx = Ctx { node: node.to_string(), mount: false };
        let mut ent = entropy();
        match cmd {
            Cmd::Init(name) => match fs.init_structure(name, &mut ent) {
                Ok(()) => {
                    println!("Initialized iPod directory structure on {}", node);
                    0
                }
                Err(e) => cx.fail(&e),
            },
            Cmd::Rebuild(name) => rebuild(&mut fs, &cx, name.unwrap_or("iPod"), &mut ent),
            Cmd::Rm(what) => rm(&mut fs, &cx, what, &mut ent),
            Cmd::Add(paths) => add(&mut fs, &cx, paths, &mut ent, false),
        }
    };
    drop(dev);
    crate::detect::offer_eject(Some(node), rc);
    rc
}

/// tempfile.mkdtemp(prefix): a fresh 0700 directory under $TMPDIR.
fn mkdtemp(prefix: &str) -> std::io::Result<String> {
    use std::os::unix::fs::DirBuilderExt;
    let tmp = std::env::temp_dir();
    let mut seed = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0)
        ^ (std::process::id() as u128) << 64;
    let chars = b"abcdefghijklmnopqrstuvwxyz0123456789_";
    loop {
        let mut name = String::from(prefix);
        for _ in 0..8 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            name.push(chars[((seed >> 64) % chars.len() as u128) as usize] as char);
        }
        let path = tmp.join(&name);
        match std::fs::DirBuilder::new().mode(0o700).create(&path) {
            Ok(()) => return Ok(path.to_string_lossy().into_owned()),
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
}

/// _offer_init_after_flash_unix: mount the fresh data partition at a temp
/// dir (root here), init, offer add, unmount; the eject follows.
pub fn offer_init_after_flash_unix(dev: &str) {
    let part = crate::platform::current().partition_node(dev, 2);
    // the node appears once udev has re-probed the table just committed
    let mut found = false;
    for _ in 0..50 {
        if std::path::Path::new(&part).exists() {
            found = true;
            break;
        }
        window::sleep(0.1);
    }
    if !found {
        eprintln!("init skipped: {} never appeared after the partition table was re-read.\n  Mount the card and run `flashpod init` instead.", part);
        return;
    }
    if !term::ask_yes(&format!("\nThe card still needs the iPod database before it can take music (\"flashpod init\").\nRun init on {} now? [Y/n] ", part), true) {
        eprintln!("Skipped. Later: mount the card and run `flashpod init`.");
        return;
    }
    let base = |p: &str| p.rsplit('/').next().unwrap_or(p).to_string();
    let existing: Vec<String> = crate::platform::current().device_mountpoints(&part)
        .into_iter()
        .filter(|(p, _)| base(p) == base(&part))
        .map(|(_, mp)| mp)
        .collect();
    let ours = existing.is_empty();
    let mnt = match existing.into_iter().next() {
        Some(m) => m,
        None => match mkdtemp("flashpod-init-") {
            Ok(m) => m,
            Err(e) => {
                eprintln!("flashpod: {}", e);
                return;
            }
        },
    };
    let mounted = if ours {
        let cmd: Vec<String> = crate::platform::current().fat_mount_cmd(&part, &mnt);
        let rc = crate::detect::run_quiet_mount(&cmd);
        if rc != 0 {
            let argv: Vec<String> = cmd.iter().map(|a| crate::argparse::py_repr(a)).collect();
            eprintln!("init skipped: mounting {} failed (Command '[{}]' returned non-zero exit status {}.); mount the card and run `flashpod init` instead.", part, argv.join(", "), rc);
            false
        } else {
            true
        }
    } else {
        true
    };
    if mounted {
        let mut t = MountTarget::new(&mnt);
        let mut ent = entropy();
        match t.init_structure("iPod", &mut ent) {
            Ok(()) => {
                println!("Initialized iPod directory structure on {}", part);
                if term::ask_yes("\nMusic can be loaded onto the card now, or later when it is in the iPod.\nLoad music onto the card now? [Y/n] ", true) {
                    let cx = Ctx { node: mnt.clone(), mount: true };
                    add(&mut t, &cx, &[], &mut ent, true);
                }
                let _ = crate::host::call(&["sync".to_string()]);
            }
            Err(e) => eprintln!("flashpod: {}", e.message),
        }
        if ours {
            let _ = crate::host::call(&["umount".to_string(), mnt.clone()]);
        }
    }
    if ours {
        let _ = std::fs::remove_dir(&mnt);
    }
}
