//! `flash` without --firmware: cli.choose_model, choose_firmware and
//! ensure_firmware over the firmware manifest built into the binary
//! (flashpod/firmware/firmware.json), with the download cache of
//! catalog._firmware_cache_dir.

use crate::argparse::{py_repr, wrap};
use crate::term;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{IsTerminal, Read, Write};

/// resources.firmware_manifest(), as shipped.
const MANIFEST: &str = include_str!("../../../flashpod/firmware/firmware.json");

/// _load_manifest.
pub fn load_manifest() -> Option<Value> {
    // FP_MANIFEST: scripts/test_cli_unchanged.py's served catalog, which it
    // also hands Python (cli.FIRMWARE_MANIFEST)
    let text = match std::env::var("FP_MANIFEST") {
        Ok(path) => match std::fs::read_to_string(&path) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("flashpod flash: firmware manifest unusable ({}); pass --firmware", flashpod_core::tags::os_error(&e, &path));
                return None;
            }
        },
        Err(_) => MANIFEST.to_string(),
    };
    match serde_json::from_str(&text) {
        Ok(v) => Some(v),
        Err(e) => {
            eprintln!("flashpod flash: firmware manifest unusable ({}); pass --firmware", e);
            None
        }
    }
}

fn st(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn model_name(m: &Value) -> String {
    let n = st(&m["name"]);
    if !n.is_empty() {
        return n;
    }
    let i = st(&m["id"]);
    if i.is_empty() { "?".into() } else { i }
}

fn ljust(s: &str, w: usize) -> String {
    let n = s.chars().count();
    if n >= w { s.to_string() } else { format!("{}{}", s, " ".repeat(w - n)) }
}

/// _fmt_table: every column sized to its content but `wrap_col`, which takes
/// the leftover width and wraps.
pub(crate) fn fmt_table(headers: &[&str], rows: &[Vec<String>], wrap_col: usize) -> Vec<String> {
    let (indent, max_width) = (2usize, 78usize);
    let ncol = headers.len();
    let len = |s: &str| s.chars().count();
    let mut widths: Vec<usize> = headers.iter().map(|h| len(h)).collect();
    for r in rows {
        for c in 0..ncol {
            if c != wrap_col {
                widths[c] = widths[c].max(len(&r[c]));
            }
        }
    }
    let fixed = indent + widths.iter().enumerate().filter(|(c, _)| *c != wrap_col).map(|(_, w)| w).sum::<usize>() + 2 * (ncol - 1);
    widths[wrap_col] = 16usize.max(max_width.saturating_sub(fixed));
    let line = |cells: &[String]| -> String {
        let s = format!("{}{}", " ".repeat(indent),
                        (0..ncol).map(|c| ljust(&cells[c], widths[c])).collect::<Vec<_>>().join("  "));
        s.trim_end().to_string()
    };
    let mut out = vec![line(&headers.iter().map(|h| h.to_string()).collect::<Vec<_>>()),
                       line(&widths.iter().map(|w| "-".repeat(*w)).collect::<Vec<_>>())];
    for r in rows {
        let collapsed = r[wrap_col].split_whitespace().collect::<Vec<_>>().join(" ");
        let mut pieces = wrap(&collapsed, widths[wrap_col]);
        if pieces.is_empty() {
            pieces.push(String::new());
        }
        for (i, piece) in pieces.iter().enumerate() {
            let mut cells: Vec<String> = (0..ncol).map(|c| if i > 0 { String::new() } else { r[c].clone() }).collect();
            cells[wrap_col] = piece.clone();
            out.push(line(&cells));
        }
    }
    out
}

fn pick(choice: &str, n: usize) -> Option<usize> {
    if choice.is_empty() || !choice.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    choice.parse::<usize>().ok().filter(|&i| i < n)
}

/// Models whose firmware 1.0 doesn't write (firmware::check_writable): the
/// choosers leave them out and --model names them as not supported yet.
const READ_ONLY_MODELS: &[&str] = &["4G", "photo"];

pub(crate) fn read_only(id: &str) -> bool {
    READ_ONLY_MODELS.iter().any(|m| m.eq_ignore_ascii_case(id))
}

pub enum Model {
    Any,
    Bail,
    One(Value),
}

/// choose_model: which iPod decides which firmware is offered.
pub fn choose_model(manifest: &Value, want: Option<&str>) -> Model {
    let models: Vec<Value> = manifest["models"].as_array().cloned().unwrap_or_default()
        .into_iter().filter(|m| !read_only(&st(&m["id"]))).collect();
    if models.is_empty() {
        return Model::Any;
    }
    if let Some(w) = want {
        if read_only(w) {
            eprintln!("flashpod flash: writing {} firmware isn't supported yet (untested on hardware); \
                       inspect and firmware extract still read it", w);
            return Model::Bail;
        }
        if let Some(m) = models.iter().find(|m| st(&m["id"]).to_lowercase() == w.to_lowercase()) {
            return Model::One(m.clone());
        }
        let known: Vec<String> = models.iter().map(|m| st(&m["id"])).collect();
        eprintln!("flashpod flash: unknown model {} (known: {})", py_repr(w), known.join(", "));
        return Model::Bail;
    }
    if !term::stdin_isatty() {
        return Model::Any;
    }
    println!("Which iPod are you flashing for?\n");
    let rows: Vec<Vec<String>> = models.iter().enumerate()
        .map(|(i, m)| vec![i.to_string(), st(&m["name"]), st(&m["years"]), st(&m["tell"])])
        .collect();
    for l in fmt_table(&["#", "iPod", "Years", "How to identify it"], &rows, 3) {
        println!("{}", l);
    }
    println!();
    let choice = match term::input_line("Select model: ") {
        Some(c) => c.trim().to_string(),
        None => {
            println!();
            return Model::Bail;
        }
    };
    match pick(&choice, models.len()) {
        Some(i) => Model::One(models[i].clone()),
        None => {
            eprintln!("flashpod flash: invalid selection");
            Model::Bail
        }
    }
}

/// choose_firmware: an entry, restricted to the model's.
pub fn choose_firmware(manifest: &Value, model: Option<&Value>) -> Option<Value> {
    let mut entries = manifest["firmwares"].as_array().cloned().unwrap_or_default();
    entries.retain(|e| !e["models"].as_array().is_some_and(|a| !a.is_empty() && a.iter().all(|x| read_only(&st(x)))));
    if entries.is_empty() {
        eprintln!("flashpod flash: no firmware listed in the manifest; pass --firmware");
        return None;
    }
    if let Some(m) = model {
        let id = st(&m["id"]);
        entries.retain(|e| e["models"].as_array().is_some_and(|a| a.iter().any(|x| st(x) == id)));
        if entries.is_empty() {
            eprintln!("flashpod flash: no firmware hosted for {}; pass --firmware", model_name(m));
            return None;
        }
    }
    let default = entries.iter().position(|e| e["default"].as_bool().unwrap_or(false) || e["default"].as_i64().is_some_and(|v| v != 0)).unwrap_or(0);
    if !term::stdin_isatty() {
        let e = &entries[default];
        eprintln!("flashpod flash: using default firmware {} ({}, {})", st(&e["file"]), st(&e["generation"]), st(&e["version"]));
        return Some(e.clone());
    }
    println!("Firmware for {} (newest first):\n", model.map(model_name).unwrap_or_else(|| "your iPod".into()));
    let rows: Vec<Vec<String>> = entries.iter().enumerate().map(|(i, e)| {
        let mut ver = st(&e["version"]);
        let maker = st(&e["maker"]);
        if !maker.is_empty() {
            ver += &format!(" ({})", maker);
        }
        if i == default {
            ver += " *";
        }
        let size = match e["size"].as_f64() {
            Some(s) if s != 0.0 => format!("{:.1}M", s / (1u64 << 20) as f64),
            _ => String::new(),
        };
        vec![i.to_string(), ver, st(&e["build_date"]), size, st(&e["description"])]
    }).collect();
    for l in fmt_table(&["#", "Version", "Built", "Size", "Notes"], &rows, 4) {
        println!("{}", l);
    }
    println!("\n  * recommended default\n");
    let mut choice = match term::input_line(&format!("Select firmware [{}]: ", default)) {
        Some(c) => c.trim().to_string(),
        None => {
            println!();
            return None;
        }
    };
    if choice.is_empty() {
        choice = default.to_string();
    }
    match pick(&choice, entries.len()) {
        Some(i) => Some(entries[i].clone()),
        None => {
            eprintln!("flashpod flash: invalid selection");
            None
        }
    }
}

/// The home folder: HOME, or USERPROFILE on Windows.
pub(crate) fn home() -> std::path::PathBuf {
    std::env::var("HOME").or_else(|_| std::env::var("USERPROFILE")).unwrap_or_else(|_| "~".into()).into()
}

/// catalog._firmware_cache_dir.
pub(crate) fn cache_dir() -> String {
    match std::env::var("XDG_CACHE_HOME") {
        Ok(v) if !v.is_empty() => format!("{}/flashpod/firmware", v),
        _ => format!("{}/.cache/flashpod/firmware", home().display()),
    }
}

fn sha256(path: &str) -> Option<String> {
    let mut f = std::fs::File::open(path).ok()?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Some(h.finalize().iter().map(|b| format!("{:02x}", b)).collect())
}

#[cfg(target_os = "linux")]
pub(crate) fn agent() -> Result<ureq::Agent, String> {
    Ok(ureq::agent())
}

/// The system TLS stack (Security.framework, SChannel) and its trust store.
#[cfg(not(target_os = "linux"))]
pub(crate) fn agent() -> Result<ureq::Agent, String> {
    let tls = native_tls::TlsConnector::new().map_err(|e| format!("<urlopen error {}>", e))?;
    Ok(ureq::AgentBuilder::new().tls_connector(std::sync::Arc::new(tls)).build())
}

/// _download: stream `url` to `dst`, a progress line on a terminal. Err is
/// worded like the urllib exception Python prints. `limit` stops a download
/// that runs past the size the catalog gives.
pub(crate) fn download(url: &str, dst: &str, total: Option<u64>, limit: Option<u64>) -> Result<(), String> {
    let resp = agent()?.get(url).set("User-Agent", "flashpod").call().map_err(|e| match e {
        ureq::Error::Status(code, r) => format!("HTTP Error {}: {}", code, r.status_text()),
        ureq::Error::Transport(t) => format!("<urlopen error {}>", t),
    })?;
    let total = total.filter(|&t| t != 0).or_else(|| resp.header("Content-Length").and_then(|v| v.parse().ok())).unwrap_or(0);
    let mut out = std::fs::File::create(dst).map_err(|e| flashpod_core::tags::os_error(&e, dst))?;
    let mut body = resp.into_reader();
    let tty = std::io::stderr().is_terminal();
    let mut buf = vec![0u8; 1 << 16];
    let (mut done, mut last) = (0u64, 0.0f64);
    loop {
        // resp.read(1 << 16): a full chunk unless the body ends first
        let mut n = 0;
        while n < buf.len() {
            let k = body.read(&mut buf[n..]).map_err(|e| e.to_string())?;
            if k == 0 {
                break;
            }
            n += k;
        }
        if n == 0 {
            break;
        }
        out.write_all(&buf[..n]).map_err(|e| flashpod_core::tags::os_error(&e, dst))?;
        done += n as u64;
        if limit.is_some_and(|l| done > l) {
            return Err(format!("longer than the {} bytes expected", limit.unwrap_or(0)));
        }
        if tty {
            let now = crate::window::monotonic();
            if now - last >= 0.2 {
                let pct = if total > 0 { format!("{}% ", done * 100 / total) } else { String::new() };
                let mib = (1u64 << 20) as f64;
                eprint!("\r  {}({:.1}/{:.1} MiB)   ", pct, done as f64 / mib, (if total > 0 { total } else { done }) as f64 / mib);
                last = now;
            }
        }
    }
    if tty {
        eprintln!();
    }
    Ok(())
}

/// ensure_firmware: the verified cache, else download + verify + cache.
/// (A Rust build bundles no firmware next to the manifest.)
pub fn ensure_firmware(entry: &Value, base_url: &str) -> Option<String> {
    let name = st(&entry["file"]);
    let want = entry["sha256"].as_str().map(String::from).filter(|s| !s.is_empty());
    let dir = cache_dir();
    let dst = format!("{}/{}", dir, name);
    let url = entry["url"].as_str().filter(|s| !s.is_empty()).map(String::from)
        .or_else(|| if base_url.is_empty() { None } else { Some(format!("{}/{}", base_url.trim_end_matches('/'), name)) });
    if std::path::Path::new(&dst).exists() {
        if want.is_none() || sha256(&dst) == want {
            return Some(dst);
        }
        eprintln!("flashpod flash: cached {} failed checksum; re-downloading", name);
        let _ = std::fs::remove_file(&dst);
    }
    let url = match url {
        Some(u) => u,
        None => {
            eprintln!("flashpod flash: no download URL for {}; pass --firmware", name);
            return None;
        }
    };
    if let Err(e) = std::fs::create_dir_all(&dir) {
        eprintln!("flashpod flash: cannot create cache dir: {}", flashpod_core::tags::os_error(&e, &dir));
        return None;
    }
    eprintln!("flashpod flash: downloading {}\n  from {}", name, url);
    let tmp = format!("{}.part", dst);
    if let Err(e) = download(&url, &tmp, entry["size"].as_u64(), None) {
        let _ = std::fs::remove_file(&tmp);
        eprintln!("flashpod flash: download failed ({}).", e);
        eprintln!("  Or download it yourself from {} and pass it with --firmware.", url);
        return None;
    }
    if want.is_some() && sha256(&tmp) != want {
        let _ = std::fs::remove_file(&tmp);
        eprintln!("flashpod flash: {} failed checksum verification \u{2014} refusing to use it.\n  Re-run to retry, or download from {} and pass --firmware.", name, url);
        return None;
    }
    if let Err(e) = std::fs::rename(&tmp, &dst) {
        eprintln!("flashpod flash: {}", flashpod_core::tags::os_error(&e, &tmp));
        return None;
    }
    eprintln!("flashpod flash: verified and cached {}", name);
    Some(dst)
}
