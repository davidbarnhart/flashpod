//! `firmware patches`: the ipod-patches library through
//! flashpod_core::patchlib, with this binary's store -- downloads by ureq,
//! the cache under ~/.cache/flashpod/patches (patchlib.cache_dir), and the
//! snapshot embedded at build time (build.rs).

use crate::argparse::{Ns, Val};
use crate::catalog::{agent, fmt_table};
use flashpod_core::patchlib::{self, Library, Source, Store};
use serde_json::Value;
use std::path::PathBuf;

include!(concat!(env!("OUT_DIR"), "/bundled_patches.rs"));

/// patchlib.cache_dir.
fn cache_dir() -> PathBuf {
    match std::env::var("XDG_CACHE_HOME") {
        Ok(v) if !v.is_empty() => PathBuf::from(v).join("flashpod/patches"),
        _ => crate::catalog::home().join(".cache/flashpod/patches"),
    }
}

struct CliStore {
    cache: PathBuf,
}

impl Store for CliStore {
    fn get(&mut self, url: &str) -> Result<Vec<u8>, String> {
        let resp = agent()?.get(url).set("User-Agent", "flashpod")
            .timeout(std::time::Duration::from_secs(30)).call().map_err(|e| match e {
                ureq::Error::Status(code, r) => format!("HTTP Error {}: {}", code, r.status_text()),
                ureq::Error::Transport(t) => format!("<urlopen error {}>", t),
            })?;
        let mut body = Vec::new();
        std::io::Read::read_to_end(&mut resp.into_reader(), &mut body).map_err(|e| e.to_string())?;
        Ok(body)
    }

    fn read(&self, source: Source, path: &str) -> Option<Vec<u8>> {
        match source {
            Source::Bundled => BUNDLED.iter().find(|(p, _)| *p == path).map(|(_, d)| d.to_vec()),
            _ => std::fs::read(self.cache.join(path)).ok(),
        }
    }

    fn write_cache(&mut self, path: &str, data: &[u8]) -> Result<(), String> {
        let dst = self.cache.join(path);
        let tmp = PathBuf::from(format!("{}.part", dst.display()));
        std::fs::create_dir_all(dst.parent().unwrap()).map_err(|e| e.to_string())?;
        std::fs::write(&tmp, data).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, &dst).map_err(|e| e.to_string())
    }
}

/// The library from the best source; FLASHPOD_PATCHES_URL replaces the
/// GitHub index (tests serve their own).
pub fn load(online: bool) -> Result<Library, flashpod_core::error::Error> {
    let url = std::env::var("FLASHPOD_PATCHES_URL").unwrap_or_else(|_| patchlib::INDEX_URL.to_string());
    patchlib::load(&mut CliStore { cache: cache_dir() }, online, &url)
}

fn st(v: &Value) -> String {
    v.as_str().map(str::to_string).unwrap_or_default()
}

/// Where the library came from, for the first line of a listing.
pub fn source_words(source: Source) -> &'static str {
    match source {
        Source::Network => "downloaded from github.com/davidbarnhart/ipod-patches",
        Source::Cache => "the copy flashpod last downloaded",
        Source::Bundled => "the copy built into flashpod",
    }
}

pub fn cmd_patches(ns: &Ns) -> i32 {
    let online = !matches!(ns.get("offline"), Some(Val::Bool(true)));
    let lib = match load(online) {
        Ok(lib) => lib,
        Err(e) => {
            eprintln!("flashpod firmware patches: {}", e.message);
            return 1;
        }
    };
    println!("Patch library: {} ({} files)", source_words(lib.source), lib.files.len());
    if let (true, Some(why)) = (online, &lib.fallback_reason) {
        println!("  (couldn't refresh it: {})", why);
    }
    let listing = || -> Result<(), flashpod_core::error::Error> {
        for base in lib.bases()? {
            let id = st(&base["id"]);
            println!("\n{} [{}]", st(&base["title"]), id);
            let rows: Vec<Vec<String>> = lib.patches(Some(&id))?.iter()
                .map(|p| vec![st(&p["id"]), st(&p["status"]), st(&p["title"])]).collect();
            for l in fmt_table(&["Patch", "Status", "What it does"], &rows, 2) {
                println!("{}", l);
            }
        }
        let builds = lib.builds()?;
        if !builds.is_empty() {
            println!("\nBuilds:");
            let rows: Vec<Vec<String>> = builds.iter()
                .map(|b| vec![st(&b["id"]), st(&b["base"]), st(&b["title"])]).collect();
            for l in fmt_table(&["Build", "Base", "What it is"], &rows, 2) {
                println!("{}", l);
            }
        }
        Ok(())
    };
    match listing() {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("flashpod firmware patches: {}", e.message);
            1
        }
    }
}
