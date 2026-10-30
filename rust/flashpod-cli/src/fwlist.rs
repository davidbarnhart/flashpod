//! `firmware list`, and the firmware `firmware write` takes by name: the
//! ipod-patches catalog (flashpod_core::patchlib) plus the user's own builds.
//! A build is `<data dir>/flashpod/builds/<id>.json` (a catalog entry and its
//! `patches`) beside `<id>.bin`. Downloads are cached as
//! `<cache dir>/<id>-<first 12 of sha256>.bin`, the raw firmware, and checked
//! on every use.

use crate::argparse::{Ns, Val};
use crate::catalog::{self, fmt_table};
use crate::patches;
use flashpod_core::patchlib::{self, FirmwareEntry, FirmwareUrl};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

const WHO: &str = "flashpod flash";

fn builds_dir() -> PathBuf {
    match std::env::var("XDG_DATA_HOME") {
        Ok(v) if !v.is_empty() => PathBuf::from(v).join("flashpod/builds"),
        _ => catalog::home().join(".local/share/flashpod/builds"),
    }
}

struct Build {
    entry: FirmwareEntry,
    bin: PathBuf,
}

/// One build's files, checked. `Err` says why it can't be used.
fn read_build(json_path: &Path, catalog: &[FirmwareEntry]) -> Result<Build, String> {
    let text = std::fs::read(json_path).map_err(|e| e.to_string())?;
    let v: Value = serde_json::from_slice(&text).map_err(|e| e.to_string())?;
    let mut entry = patchlib::parse_entry(&v)?;
    if json_path.file_stem().and_then(|s| s.to_str()) != Some(entry.id.as_str()) {
        return Err(format!("its id is {}, and the file must be named after it", entry.id));
    }
    if catalog.iter().any(|c| c.id == entry.id) {
        return Err(format!("the catalog already has a firmware called {}", entry.id));
    }
    let bin = json_path.with_extension("bin");
    if !bin.exists() {
        return Err(format!("{} is missing", bin.display()));
    }
    // a build counts as tested only when its entry says so
    entry.tested_on_hardware = Some(entry.tested_on_hardware.unwrap_or(false));
    Ok(Build { entry, bin })
}

/// The user's builds, sorted. A bad one is named and left out.
fn user_builds(catalog: &[FirmwareEntry]) -> Vec<Build> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(builds_dir()).into_iter().flatten().flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    paths.iter().filter_map(|p| match read_build(p, catalog) {
        Ok(b) => Some(b),
        Err(why) => {
            eprintln!("flashpod: skipping your build {}: {}", p.display(), why);
            None
        }
    }).collect()
}

fn cache_file(e: &FirmwareEntry) -> PathBuf {
    PathBuf::from(catalog::cache_dir()).join(format!("{}-{}.bin", e.id, &e.sha256[..12]))
}

fn is_cached(e: &FirmwareEntry) -> bool {
    std::fs::read(cache_file(e)).is_ok_and(|d| patchlib::firmware_matches(e, &d))
}

fn writable(e: &FirmwareEntry) -> bool {
    e.models.is_empty() || e.models.iter().any(|m| !catalog::read_only(m))
}

fn tested(e: &FirmwareEntry) -> &'static str {
    match e.tested_on_hardware {
        Some(true) => "yes",
        Some(false) => "no",
        None => "?",
    }
}

fn row(e: &FirmwareEntry) -> Vec<String> {
    let models: Vec<String> = e.models.iter()
        .map(|m| if catalog::read_only(m) { format!("{}*", m) } else { m.clone() }).collect();
    let base = e.base.clone().filter(|b| *b != e.id).unwrap_or_default();
    let patches = if e.patches.is_empty() { String::new() } else { e.patches.len().to_string() };
    vec![e.id.clone(), models.join(", "), base, patches, tested(e).to_string(), e.sha256[..12].to_string()]
}

fn section(title: &str, entries: &[&FirmwareEntry]) {
    if entries.is_empty() {
        return;
    }
    println!("\n{}:", title);
    let rows: Vec<Vec<String>> = entries.iter().map(|e| row(e)).collect();
    for l in fmt_table(&["Name", "For", "Base", "Patches", "Tested", "sha256"], &rows, 2) {
        println!("{}", l);
    }
}

fn json_entry(e: &FirmwareEntry, kind: &str) -> Value {
    let mut v = serde_json::to_value(e).unwrap_or(Value::Null);
    v["kind"] = json!(kind);
    v["cached"] = json!(is_cached(e));
    v["writable"] = json!(writable(e));
    v
}

pub fn cmd_list(ns: &Ns) -> i32 {
    let flag = |k: &str| matches!(ns.get(k), Some(Val::Bool(true)));
    let online = !flag("offline");
    let listing = patches::load(online).and_then(|lib| lib.firmwares().map(|f| (lib, f)));
    let (lib, firmwares) = match listing {
        Ok(x) => x,
        Err(e) => {
            eprintln!("flashpod firmware list: {}", e.message);
            return 1;
        }
    };
    let builds = user_builds(&firmwares);
    let reason = if online { lib.fallback_reason.clone() } else { None };
    if flag("json") {
        let mut all: Vec<Value> = firmwares.iter().map(|e| json_entry(e, "catalog")).collect();
        all.extend(builds.iter().map(|b| {
            let mut v = json_entry(&b.entry, "build");
            v["cached"] = json!(true);
            v["path"] = json!(b.bin.display().to_string());
            v
        }));
        let out = json!({"source": lib.source.name(), "fallback_reason": reason, "firmwares": all});
        println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
        return 0;
    }
    println!("Firmware catalog: {} ({} firmwares)", patches::source_words(lib.source), firmwares.len());
    if let Some(why) = reason {
        println!("  (couldn't refresh it: {})", why);
    }
    let (apple, ours): (Vec<&FirmwareEntry>, Vec<&FirmwareEntry>) = firmwares.iter().partition(|e| e.maker == "apple");
    section("Preserved Apple releases", &apple);
    section("flashpod releases", &ours);
    section("Your builds", &builds.iter().map(|b| &b.entry).collect::<Vec<_>>());
    println!();
    if firmwares.iter().any(|e| e.models.iter().any(|m| catalog::read_only(m))) {
        println!("* flashpod can read this firmware but won't write it yet (untested on hardware)");
    }
    println!("? = no hardware test recorded");
    let missing: Vec<&str> = firmwares.iter().filter(|e| e.urls.is_empty()).map(|e| e.id.as_str()).collect();
    if !missing.is_empty() {
        println!("No download listed: {}", missing.join(", "));
    }
    0
}

/// A name that can only be a file: nothing in the catalog looks like this,
/// so no catalog and no network are involved.
fn looks_like_path(name: &str) -> bool {
    name.contains('/') || name.contains(std::path::MAIN_SEPARATOR)
        || [".bin", ".gz", ".ipsw", ".zip"].iter().any(|x| name.ends_with(x))
}

/// Whether this firmware may be written for `model`; said before any download.
fn check_writable(e: &FirmwareEntry, model: Option<&str>) -> Result<(), String> {
    if !writable(e) {
        return Err(format!("writing {} firmware isn't supported yet (untested on hardware); \
                            inspect and firmware extract still read it", e.models.join("/")));
    }
    if let Some(m) = model {
        if !e.models.iter().any(|x| x.eq_ignore_ascii_case(m)) {
            return Err(format!("{} is for {}, not {}", e.id, e.models.join(", "), m));
        }
    }
    Ok(())
}

/// The verified cache copy, if there is one; a bad one is thrown away.
fn cached_path(e: &FirmwareEntry) -> Option<String> {
    let path = cache_file(e);
    let data = std::fs::read(&path).ok()?;
    if patchlib::firmware_matches(e, &data) {
        return Some(path.display().to_string());
    }
    eprintln!("{}: cached {} fails its sha256; downloading it again", WHO, e.id);
    let _ = std::fs::remove_file(&path);
    None
}

fn download(u: &FirmwareUrl, e: &FirmwareEntry) -> Result<Vec<u8>, String> {
    let dir = catalog::cache_dir();
    std::fs::create_dir_all(&dir).map_err(|err| flashpod_core::tags::os_error(&err, &dir))?;
    let tmp = format!("{}.download.part", cache_file(e).display());
    eprintln!("{}: downloading {}\n  from {}", WHO, e.id, u.url);
    let got = catalog::download(&u.url, &tmp, Some(u.bytes), Some(u.bytes)).and_then(|()| std::fs::read(&tmp).map_err(|err| err.to_string()));
    let _ = std::fs::remove_file(&tmp);
    got
}

/// Download, check and cache a catalog firmware.
fn fetch(e: &FirmwareEntry) -> Result<String, String> {
    let f = patchlib::fetch_firmware(&mut |u| download(u, e), e).map_err(|err| {
        if err.details.get("reason").and_then(Value::as_str) == Some("no_urls") {
            format!("{}; pass a file with --firmware", err.message)
        } else {
            err.message
        }
    })?;
    for why in &f.skipped {
        eprintln!("{}: skipped {}", WHO, why);
    }
    let dst = cache_file(e);
    let tmp = format!("{}.part", dst.display());
    std::fs::write(&tmp, &f.data).map_err(|err| flashpod_core::tags::os_error(&err, &tmp))?;
    std::fs::rename(&tmp, &dst).map_err(|err| flashpod_core::tags::os_error(&err, &tmp))?;
    eprintln!("{}: verified and cached {}", WHO, e.id);
    Ok(dst.display().to_string())
}

fn use_build(b: &Build, model: Option<&str>) -> Result<String, String> {
    check_writable(&b.entry, model)?;
    let data = std::fs::read(&b.bin).map_err(|err| flashpod_core::tags::os_error(&err, &b.bin.display().to_string()))?;
    if !patchlib::firmware_matches(&b.entry, &data) {
        return Err(format!("your build {} is damaged: {} doesn't match its sha256", b.entry.id, b.bin.display()));
    }
    Ok(b.bin.display().to_string())
}

/// The file `firmware write` should use for `name`: a path as given, or a
/// catalog or build name resolved, downloading only when it must.
pub fn resolve(name: &str, model: Option<&str>) -> Result<String, String> {
    if looks_like_path(name) {
        return Ok(name.to_string());
    }
    let exists = Path::new(name).exists();
    let known = patches::load(false).and_then(|l| l.firmwares()).map_err(|e| e.message);
    if exists {
        // a broken catalog can't make this name ambiguous
        let catalog = known.unwrap_or_default();
        if catalog.iter().any(|e| e.id == name) || user_builds(&catalog).iter().any(|b| b.entry.id == name) {
            return Err(format!("{} is both a file here and a firmware in `flashpod firmware list`. \
                                Say ./{} for the file, or run this from another folder for the firmware.", name, name));
        }
        return Ok(name.to_string());
    }
    let catalog = known?;
    let builds = user_builds(&catalog);
    if let Some(b) = builds.iter().find(|b| b.entry.id == name) {
        return use_build(b, model);
    }
    if let Some(e) = catalog.iter().find(|e| e.id == name) {
        check_writable(e, model)?;
        if let Some(p) = cached_path(e) {
            return Ok(p);
        }
    }
    let fresh = patches::load(true).and_then(|l| l.firmwares()).map_err(|e| e.message)?;
    match fresh.iter().find(|e| e.id == name) {
        Some(e) => {
            check_writable(e, model)?;
            match cached_path(e) {
                Some(p) => Ok(p),
                None => fetch(e),
            }
        }
        None => Err(format!("{} is not a file, and not in the catalog or your builds (see flashpod firmware list)", name)),
    }
}
