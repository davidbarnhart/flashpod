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

    /// The catalog's entries, each build joined to builds/<build>.json for
    /// its base and patches.
    pub fn firmwares(&self) -> Result<Vec<FirmwareEntry>, Error> {
        let catalog = self.catalog()?;
        let list = catalog["firmwares"].as_array()
            .ok_or_else(|| unavailable("catalog/firmware.json: no firmware list", "json"))?;
        let mut out = Vec::new();
        for v in list {
            let mut e = parse_entry(v)
                .map_err(|why| unavailable(format!("catalog/firmware.json: {}", why), "json"))?;
            if let Some(build) = e.build.clone() {
                let b = self.json(&format!("builds/{}.json", build))?;
                e.base = b["base"].as_str().map(str::to_string);
                e.patches = strings(&b["patches"]).unwrap_or_default();
            }
            out.push(e);
        }
        Ok(out)
    }
}

/// One firmware file the catalog lists: `bytes` and `sha256` are the
/// firmware file's own, whatever container a URL holds it in.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FirmwareEntry {
    pub id: String,
    pub title: String,
    pub maker: String,
    pub version: String,
    pub models: Vec<String>,
    pub bytes: u64,
    pub sha256: String,
    pub base: Option<String>,
    pub build: Option<String>,
    pub patches: Vec<String>,
    pub urls: Vec<FirmwareUrl>,
    /// None: no hardware test recorded, which is not the same as false.
    pub tested_on_hardware: Option<bool>,
}

/// Where to download a firmware; `bytes` and `sha256` are the downloaded
/// file's.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct FirmwareUrl {
    pub url: String,
    pub role: String,
    pub container: String,
    pub bytes: u64,
    pub sha256: String,
}

fn is_sha256(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// The schema's id: lower-case letters, digits, dots and dashes. An id names
/// a cache file, so this is also what keeps it inside the cache.
pub fn is_id(s: &str) -> bool {
    s.bytes().next().is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && s.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'.' || c == b'-')
}

fn strings(v: &Value) -> Option<Vec<String>> {
    v.as_array()?.iter().map(|x| x.as_str().map(str::to_string)).collect()
}

/// A catalog entry, checked: a bad one is an error, never a guess.
pub fn parse_entry(v: &Value) -> Result<FirmwareEntry, String> {
    let id = v["id"].as_str().filter(|s| is_id(s)).ok_or_else(|| format!("id {}", q(&v["id"])))?.to_string();
    let bad = |what: &str| format!("{}: {} {}", id, what, q(&v[what]));
    let text = |k: &str| v[k].as_str().map(str::to_string).ok_or_else(|| bad(k));
    let opt_id = |k: &str| match &v[k] {
        Value::Null => Ok(None),
        x => x.as_str().filter(|s| is_id(s)).map(|s| Some(s.to_string())).ok_or_else(|| bad(k)),
    };
    let sha = v["sha256"].as_str().filter(|s| is_sha256(s)).ok_or_else(|| bad("sha256"))?.to_string();
    let mut urls = Vec::new();
    for u in v["urls"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        let field = |k: &str| u[k].as_str().map(str::to_string).ok_or_else(|| format!("{}: url {} {}", id, k, q(&u[k])));
        let url = FirmwareUrl {
            url: field("url")?,
            role: field("role")?,
            container: field("container")?,
            bytes: u["bytes"].as_u64().ok_or_else(|| format!("{}: url bytes {}", id, q(&u["bytes"])))?,
            sha256: field("sha256").and_then(|s| if is_sha256(&s) { Ok(s) } else { Err(format!("{}: url sha256 {}", id, s)) })?,
        };
        urls.push(url);
    }
    Ok(FirmwareEntry {
        title: v["title"].as_str().unwrap_or("").to_string(),
        maker: text("maker")?,
        version: text("version")?,
        models: strings(&v["models"]).ok_or_else(|| bad("models"))?,
        bytes: v["bytes"].as_u64().ok_or_else(|| bad("bytes"))?,
        sha256: sha,
        base: opt_id("base")?,
        build: opt_id("build")?,
        patches: match &v["patches"] {
            Value::Null => Vec::new(),
            x => strings(x).ok_or_else(|| bad("patches"))?,
        },
        urls,
        tested_on_hardware: match &v["tested_on_hardware"] {
            Value::Null => None,
            x => Some(x.as_bool().ok_or_else(|| bad("tested_on_hardware"))?),
        },
        id,
    })
}

/// Whether `data` is this firmware file, by size and sha256.
pub fn firmware_matches(entry: &FirmwareEntry, data: &[u8]) -> bool {
    data.len() as u64 == entry.bytes && sha256_hex(data) == entry.sha256
}

/// A decompressed read that stops one byte past what the firmware can be, so
/// a hostile archive can't fill memory.
fn read_capped(r: impl std::io::Read, cap: u64) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    std::io::Read::read_to_end(&mut std::io::Read::take(r, cap + 1), &mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

/// The firmware file inside a download. An ipsw can hold several `Firmware*`
/// members, one for each kind of iPod it serves, so the member is chosen by
/// sha256, not by position.
pub fn unpack(container: &str, data: &[u8], entry: &FirmwareEntry) -> Result<Vec<u8>, String> {
    match container {
        "bin" => Ok(data.to_vec()),
        "bin.gz" => read_capped(flate2::read::MultiGzDecoder::new(data), entry.bytes).map_err(|e| format!("not a gzip file ({})", e)),
        "ipsw" => {
            let mut zf = zip::ZipArchive::new(std::io::Cursor::new(data)).map_err(|e| format!("not a zip file ({})", e))?;
            let names: Vec<String> = zf.file_names()
                .filter(|n| n.rsplit('/').next().unwrap_or("").starts_with("Firmware"))
                .map(String::from).collect();
            for name in &names {
                let member = zf.by_name(name).map_err(|e| e.to_string())?;
                let out = read_capped(member, entry.bytes)?;
                if firmware_matches(entry, &out) {
                    return Ok(out);
                }
            }
            Err(format!("none of its {} Firmware file(s) is this firmware", names.len()))
        }
        other => Err(format!("unknown container {}", q(&Value::String(other.to_string())))),
    }
}

/// A firmware file, checked, and how it was got.
#[derive(Debug)]
pub struct Fetched {
    pub data: Vec<u8>,
    pub url: String,
    /// "url: why", for each URL tried before this one.
    pub skipped: Vec<String>,
}

/// The catalog's download order: each URL in turn, its download checked
/// against the URL's sha256, unpacked, and the firmware checked against the
/// entry's. Any failure moves on to the next URL; the checks are what's
/// trusted, not where the bytes came from.
pub fn fetch_firmware(
    get: &mut dyn FnMut(&FirmwareUrl) -> Result<Vec<u8>, String>,
    entry: &FirmwareEntry,
) -> Result<Fetched, Error> {
    if entry.urls.is_empty() {
        return Err(Error::new(ErrorKind::FirmwareUnavailable, format!("{} has no download listed", entry.id))
            .detail("reason", "no_urls"));
    }
    let mut skipped = Vec::new();
    for u in &entry.urls {
        let tried = get(u).and_then(|d| {
            if d.len() as u64 != u.bytes || sha256_hex(&d) != u.sha256 {
                return Err(format!("the download doesn't match its sha256 ({} bytes, {} expected)", d.len(), u.bytes));
            }
            let fw = unpack(&u.container, &d, entry)?;
            if !firmware_matches(entry, &fw) {
                return Err("the firmware inside doesn't match its sha256".to_string());
            }
            Ok(fw)
        });
        match tried {
            Ok(data) => return Ok(Fetched { data, url: u.url.clone(), skipped }),
            Err(why) => skipped.push(format!("{}: {}", u.url, why)),
        }
    }
    Err(Error::new(ErrorKind::FirmwareUnavailable,
                   format!("no download of {} worked:\n  {}", entry.id, skipped.join("\n  ")))
        .detail("reason", "download"))
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

    fn entry(fw: &[u8], urls: Vec<FirmwareUrl>) -> FirmwareEntry {
        FirmwareEntry {
            id: "fw".into(), title: String::new(), maker: "apple".into(), version: "1".into(), models: vec!["1G".into()],
            bytes: fw.len() as u64, sha256: sha256_hex(fw), base: None, build: None, patches: vec![], urls,
            tested_on_hardware: None,
        }
    }

    fn url(name: &str, container: &str, d: &[u8]) -> FirmwareUrl {
        FirmwareUrl { url: name.into(), role: "fallback".into(), container: container.into(), bytes: d.len() as u64, sha256: sha256_hex(d) }
    }

    fn gz(d: &[u8]) -> Vec<u8> {
        let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        std::io::Write::write_all(&mut e, d).unwrap();
        e.finish().unwrap()
    }

    fn ipsw(members: &[(&str, &[u8])]) -> Vec<u8> {
        let mut z = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        for (n, d) in members {
            z.start_file(*n, zip::write::SimpleFileOptions::default()).unwrap();
            std::io::Write::write_all(&mut z, d).unwrap();
        }
        z.finish().unwrap().into_inner()
    }

    fn fetch(e: &FirmwareEntry, server: &[(&str, Vec<u8>)]) -> (Result<Fetched, Error>, Vec<String>) {
        let mut got = Vec::new();
        let r = fetch_firmware(&mut |u: &FirmwareUrl| {
            got.push(u.url.clone());
            server.iter().find(|(n, _)| *n == u.url).map(|(_, d)| d.clone()).ok_or_else(|| "404".to_string())
        }, e);
        (r, got)
    }

    #[test]
    fn fetch_in_order_each_check_moves_on() {
        let fw = b"the firmware".to_vec();
        let g = gz(&fw);
        let wrong = gz(b"other bytes!");
        let e = entry(&fw, vec![url("missing", "bin", &fw), url("bad-sha", "bin", &fw), url("wrong-inner", "bin.gz", &wrong), url("good", "bin.gz", &g)]);
        let (r, got) = fetch(&e, &[("bad-sha", b"the firmwarX".to_vec()), ("wrong-inner", wrong.clone()), ("good", g.clone())]);
        let f = r.unwrap();
        assert_eq!(f.data, fw);
        assert_eq!(f.url, "good");
        assert_eq!(got, ["missing", "bad-sha", "wrong-inner", "good"]);
        assert_eq!(f.skipped.len(), 3);
        assert!(f.skipped[1].contains("doesn't match its sha256"));
        assert!(f.skipped[2].contains("firmware inside"));

        // none works: every URL's reason is named
        let (r, _) = fetch(&e, &[]);
        let err = r.unwrap_err();
        assert_eq!(err.kind.code(), "firmware_unavailable");
        assert!(["missing", "bad-sha", "wrong-inner", "good"].iter().all(|n| err.message.contains(n)));

        let (r, got) = fetch(&entry(&fw, vec![]), &[]);
        assert_eq!(r.unwrap_err().details["reason"], "no_urls");
        assert!(got.is_empty());
    }

    #[test]
    fn unpack_finds_the_firmware() {
        let fw = b"the firmware".to_vec();
        let e = entry(&fw, vec![]);
        assert_eq!(unpack("bin", &fw, &e).unwrap(), fw);
        assert_eq!(unpack("bin.gz", &gz(&fw), &e).unwrap(), fw);
        // the second Firmware member is the one; other members are ignored
        let two = ipsw(&[("Firmware-1", b"another iPod"), ("x/Firmware-2", &fw), ("Manifest.plist", &fw)]);
        assert_eq!(unpack("ipsw", &two, &e).unwrap(), fw);
        let none = ipsw(&[("Firmware-1", b"another iPod"), ("Manifest.plist", &fw)]);
        assert!(unpack("ipsw", &none, &e).unwrap_err().contains("none of its 1"));
        assert!(unpack("bin.gz", b"not gzip", &e).is_err());
        assert!(unpack("ipsw", b"not zip", &e).is_err());
        assert!(unpack("tar", &fw, &e).is_err());
        // a bomb stops one byte past the size
        let big = gz(&vec![0u8; 1 << 20]);
        assert_eq!(unpack("bin.gz", &big, &e).unwrap().len() as u64, e.bytes + 1);
    }

    #[test]
    fn entries_parse_strictly() {
        let sha = "0".repeat(64);
        let good = serde_json::json!({"id": "a-1.5", "maker": "apple", "version": "1", "models": ["1G"], "bytes": 3, "sha256": sha,
            "urls": [{"url": "https://x/y", "role": "primary", "container": "bin", "bytes": 3, "sha256": sha, "note": "ignored"}]});
        let e = parse_entry(&good).unwrap();
        assert_eq!(e.tested_on_hardware, None);
        assert_eq!(e.urls[0].container, "bin");
        for (k, v) in [("id", serde_json::json!("../x")), ("id", serde_json::json!("A")), ("id", serde_json::json!(".a")),
                       ("sha256", serde_json::json!("abc")), ("bytes", serde_json::json!("3")), ("models", serde_json::json!("1G")),
                       ("tested_on_hardware", serde_json::json!("yes"))] {
            let mut bad = good.clone();
            bad[k] = v;
            assert!(parse_entry(&bad).is_err(), "{}", k);
        }
        let mut bad = good.clone();
        bad["urls"][0]["sha256"] = serde_json::json!("abc");
        assert!(parse_entry(&bad).is_err());
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
