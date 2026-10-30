//! The ipod-patches library: the port of flashpod/patchlib.py.
//!
//! Sources in order: network (the index, then only files the cache doesn't
//! hold by sha256), cache, bundled. The caller supplies all three through
//! [`Store`], so this module does no I/O and builds for wasm; the CLI's
//! store downloads with ureq, caches under ~/.cache/flashpod/patches and
//! embeds the snapshot. Unlike Python's Library, which re-reads and
//! re-checks a file on every read, this one holds the checked bytes.

use crate::error::{Error, ErrorKind};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub const INDEX_URL: &str = "https://raw.githubusercontent.com/davidbarnhart/ipod-patches/main/";
pub const FORMAT: u64 = 3;
pub const TOP_DIRS: &[&str] = &["bases", "builds", "catalog", "patches", "schema"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Network,
    Cache,
    Bundled,
}

impl Source {
    pub fn name(self) -> &'static str {
        match self {
            Source::Network => "network",
            Source::Cache => "cache",
            Source::Bundled => "bundled",
        }
    }
}

/// Where the library's bytes come from. `read` serves the cache and the
/// bundled snapshot; `get` fetches a URL.
pub trait Store {
    fn get(&mut self, url: &str) -> Result<Vec<u8>, String>;
    fn read(&self, source: Source, path: &str) -> Option<Vec<u8>>;
    fn write_cache(&mut self, path: &str, data: &[u8]) -> Result<(), String>;
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct IndexEntry {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

fn unavailable(message: impl Into<String>, reason: &str) -> Error {
    Error::new(ErrorKind::PatchLibraryUnavailable, message).detail("reason", reason)
}

/// patchlib._q: a value quoted as JSON.
fn q(v: &Value) -> String {
    serde_json::to_string(v).unwrap_or_default()
}

fn plain_path(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    parts.len() >= 2
        && TOP_DIRS.contains(&parts[0])
        && parts.iter().all(|p| {
            !p.is_empty() && *p != "." && *p != ".." && !p.contains('\\') && !p.contains('\0') && !p.contains(':')
        })
}

/// patchlib.parse_index: the index's file list, checked.
pub fn parse_index(data: &[u8]) -> Result<Vec<IndexEntry>, Error> {
    let bad = |why: String| unavailable(format!("patch index unusable: {}", why), "index");
    let index: Value = std::str::from_utf8(data).ok()
        .and_then(|t| serde_json::from_str(t).ok())
        .ok_or_else(|| bad("not JSON".into()))?;
    let obj = index.as_object().ok_or_else(|| bad("not an object".into()))?;
    let format = obj.get("format").cloned().unwrap_or(Value::Null);
    if !(format.is_u64() && format.as_u64() == Some(FORMAT)) {
        return Err(bad(format!("format is {}, flashpod reads {}", q(&format), FORMAT)));
    }
    let files = obj.get("files").and_then(Value::as_array).ok_or_else(|| bad("no file list".into()))?;
    let mut out = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for f in files {
        let f = f.as_object().ok_or_else(|| bad("file entry is not an object".into()))?;
        let get = |k: &str| f.get(k).cloned().unwrap_or(Value::Null);
        let (path, size, sha) = (get("path"), get("bytes"), get("sha256"));
        let p = match path.as_str() {
            Some(p) if plain_path(p) => p.to_string(),
            _ => return Err(bad(format!("path {}", q(&path)))),
        };
        if !seen.insert(p.clone()) {
            return Err(bad(format!("{} listed twice", q(&path))));
        }
        let n = size.as_u64().ok_or_else(|| bad(format!("{}: bytes is not a size", q(&path))))?;
        match sha.as_str() {
            Some(s) if s.len() == 64 && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) => {
                out.push(IndexEntry { path: p, bytes: n, sha256: s.to_string() })
            }
            _ => return Err(bad(format!("{}: sha256 {}", q(&path), q(&sha)))),
        }
    }
    Ok(out)
}

fn sha256_hex(b: &[u8]) -> String {
    Sha256::digest(b).iter().map(|x| format!("{:02x}", x)).collect()
}

fn matches(entry: &IndexEntry, data: &[u8]) -> bool {
    data.len() as u64 == entry.bytes && sha256_hex(data) == entry.sha256
}

fn read_checked(store: &dyn Store, source: Source, entry: &IndexEntry) -> Option<Vec<u8>> {
    store.read(source, &entry.path).filter(|d| matches(entry, d))
}

/// A checked copy of the patch library.
#[derive(Debug, Clone)]
pub struct Library {
    pub files: Vec<IndexEntry>,
    data: BTreeMap<String, Vec<u8>>,
    pub source: Source,
    pub fallback_reason: Option<String>,
}

impl Library {
    pub fn read(&self, path: &str) -> Result<&[u8], Error> {
        self.data.get(path).map(Vec::as_slice).ok_or_else(|| {
            unavailable(format!("{} is not in the patch library", path), "missing").detail("path", path)
        })
    }

    pub fn json(&self, path: &str) -> Result<Value, Error> {
        serde_json::from_slice(self.read(path)?)
            .map_err(|e| unavailable(format!("{}: {}", path, e), "json").detail("path", path))
    }

    pub fn paths(&self, top: &str) -> Vec<&str> {
        let prefix = format!("{}/", top);
        self.data.keys().filter(|p| p.starts_with(&prefix)).map(String::as_str).collect()
    }

    pub fn bases(&self) -> Result<Vec<Value>, Error> {
        self.paths("bases").into_iter().filter(|p| p.ends_with(".json")).map(|p| self.json(p)).collect()
    }

    pub fn builds(&self) -> Result<Vec<Value>, Error> {
        self.paths("builds").into_iter().filter(|p| p.ends_with(".json")).map(|p| self.json(p)).collect()
    }

    pub fn patches(&self, base: Option<&str>) -> Result<Vec<Value>, Error> {
        self.paths("patches").into_iter()
            .filter(|p| p.ends_with("/patch.json") && base.is_none_or(|b| p.split('/').nth(1) == Some(b)))
            .map(|p| self.json(p)).collect()
    }

    pub fn catalog(&self) -> Result<Value, Error> {
        self.json("catalog/firmware.json")
    }
}

/// patchlib.open_dir: the library a source holds, if its index parses and
/// every file checks.
pub fn open_source(store: &dyn Store, source: Source) -> Result<Library, Error> {
    let raw = store.read(source, "index.json")
        .ok_or_else(|| unavailable(format!("no {} patch library", source.name()), "absent"))?;
    let files = parse_index(&raw)?;
    let mut data = BTreeMap::new();
    for e in &files {
        let d = read_checked(store, source, e).ok_or_else(|| {
            unavailable(format!("{} patch library: {} is missing or fails its sha256", source.name(), e.path), "checksum")
                .detail("path", e.path.as_str())
        })?;
        data.insert(e.path.clone(), d);
    }
    Ok(Library { files, data, source, fallback_reason: None })
}

/// patchlib.refresh: bring the cache up to date from `base_url`.
pub fn refresh(store: &mut dyn Store, base_url: &str) -> Result<Library, Error> {
    let base_url = format!("{}/", base_url.trim_end_matches('/'));
    let raw_index = store.get(&format!("{}index.json", base_url))
        .map_err(|e| unavailable(format!("cannot fetch the patch index: {}", e), "download"))?;
    let files = parse_index(&raw_index)?;
    let mut data = BTreeMap::new();
    for e in &files {
        if let Some(d) = read_checked(store, Source::Cache, e) {
            data.insert(e.path.clone(), d);
            continue;
        }
        let d = match read_checked(store, Source::Bundled, e) {
            Some(d) => d,
            None => {
                let url = format!("{}{}", base_url, e.path);
                let d = store.get(&url).map_err(|err| {
                    unavailable(format!("cannot fetch {}: {}", e.path, err), "download").detail("url", url.as_str())
                })?;
                if !matches(e, &d) {
                    return Err(unavailable(format!("{} does not match the patch index", e.path), "checksum")
                        .detail("url", url.as_str()));
                }
                d
            }
        };
        write(store, &e.path, &d)?;
        data.insert(e.path.clone(), d);
    }
    write(store, "index.json", &raw_index)?;
    Ok(Library { files, data, source: Source::Network, fallback_reason: None })
}

fn write(store: &mut dyn Store, path: &str, data: &[u8]) -> Result<(), Error> {
    store.write_cache(path, data)
        .map_err(|e| unavailable(format!("cannot write the patch cache: {}", e), "cache_dir"))
}

/// patchlib.load: the library from the best source available.
pub fn load(store: &mut dyn Store, online: bool, base_url: &str) -> Result<Library, Error> {
    let mut reasons = Vec::new();
    if online {
        match refresh(store, base_url) {
            Ok(lib) => return Ok(lib),
            Err(e) => reasons.push(e.message),
        }
    }
    for source in [Source::Cache, Source::Bundled] {
        match open_source(store, source) {
            Ok(mut lib) => {
                lib.fallback_reason = if reasons.is_empty() { None } else { Some(reasons.join("; ")) };
                return Ok(lib);
            }
            Err(e) => reasons.push(e.message),
        }
    }
    Err(unavailable(format!("no usable patch library: {}", reasons.join("; ")), "none"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// A store over in-memory maps: the server, the cache, the snapshot.
    #[derive(Default)]
    struct Mem {
        server: HashMap<String, Vec<u8>>,
        cache: HashMap<String, Vec<u8>>,
        bundled: HashMap<String, Vec<u8>>,
        got: Vec<String>,
    }

    const BASE: &str = "https://example.invalid/p/";

    impl Store for Mem {
        fn get(&mut self, url: &str) -> Result<Vec<u8>, String> {
            let path = url.trim_start_matches(BASE).to_string();
            self.got.push(path.clone());
            self.server.get(&path).cloned().ok_or_else(|| format!("404 {}", url))
        }
        fn read(&self, source: Source, path: &str) -> Option<Vec<u8>> {
            match source {
                Source::Cache => self.cache.get(path).cloned(),
                _ => self.bundled.get(path).cloned(),
            }
        }
        fn write_cache(&mut self, path: &str, data: &[u8]) -> Result<(), String> {
            self.cache.insert(path.to_string(), data.to_vec());
            Ok(())
        }
    }

    fn library(files: &[(&str, &[u8])]) -> HashMap<String, Vec<u8>> {
        let mut m: HashMap<String, Vec<u8>> = files.iter().map(|(p, d)| (p.to_string(), d.to_vec())).collect();
        let entries: Vec<Value> = files.iter()
            .map(|(p, d)| serde_json::json!({"path": p, "bytes": d.len(), "sha256": sha256_hex(d)})).collect();
        m.insert("index.json".into(), serde_json::to_vec(&serde_json::json!({"format": 3, "files": entries})).unwrap());
        m
    }

    #[test]
    fn network_cache_bundled() {
        let old: &[(&str, &[u8])] = &[("bases/a.json", b"{}"), ("patches/a/x/patch.json", b"{\"id\":\"x\"}")];
        let new: &[(&str, &[u8])] = &[("bases/a.json", b"{}"), ("patches/a/x/patch.json", b"{\"id\":\"x2\"}")];
        let mut s = Mem { server: library(new), bundled: library(old), ..Default::default() };
        let lib = load(&mut s, true, BASE).unwrap();
        assert_eq!(lib.source, Source::Network);
        assert_eq!(s.got, ["index.json", "patches/a/x/patch.json"]);
        assert_eq!(lib.patches(Some("a")).unwrap()[0]["id"], "x2");
        s.got.clear();
        load(&mut s, true, BASE).unwrap();
        assert_eq!(s.got, ["index.json"]);

        // same length, wrong bytes: refused, cache kept
        s.cache.remove("patches/a/x/patch.json");
        let good_index = s.cache["index.json"].clone();
        s.server.insert("patches/a/x/patch.json".into(), b"{\"id\":\"x3\"}".to_vec());
        let lib = load(&mut s, true, BASE).unwrap();
        assert_eq!(lib.source, Source::Bundled);
        assert!(lib.fallback_reason.unwrap().contains("does not match the patch index"));
        assert_eq!(s.cache["index.json"], good_index);

        s.server.clear();
        s.cache = library(new);
        let lib = load(&mut s, true, BASE).unwrap();
        assert_eq!(lib.source, Source::Cache);
        assert!(lib.fallback_reason.unwrap().starts_with("cannot fetch the patch index"));
    }

    #[test]
    fn hostile_indexes_refused() {
        for path in ["../x", "/etc/x", "patches/../../x", "elsewhere/x", "patches", "patches//x", "patches/a\\..\\x", "patches/C:x"] {
            let idx = serde_json::json!({"format": 3, "files": [{"path": path, "bytes": 1, "sha256": "0".repeat(64)}]});
            let e = parse_index(&serde_json::to_vec(&idx).unwrap()).unwrap_err();
            assert_eq!(e.details["reason"], "index", "{}", path);
        }
        for fmt in [serde_json::json!(2), serde_json::json!(4), serde_json::json!("3"), Value::Null, serde_json::json!(3.0)] {
            let idx = serde_json::json!({"format": fmt, "files": []});
            assert!(parse_index(&serde_json::to_vec(&idx).unwrap()).is_err(), "{}", fmt);
        }
        assert!(parse_index(b"{\"format\": NaN}").is_err());
    }
}
