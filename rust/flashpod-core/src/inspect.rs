//! card.inspect and card.tracks.

use crate::block::BlockDevice;
use crate::error::{Error, ErrorKind};
use crate::fat;
use crate::firmware;
use crate::itunesdb;
use crate::layout::{self, Partition, LBA28_MAX, SECTOR};
use crate::text::{casefold, latin1, py_rstrip};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashSet};

pub const DB_PATH: &str = "iPod_Control/iTunes/iTunesDB";
/// library.IPOD_DB_SIZE_LIMIT
pub const IPOD_DB_SIZE_LIMIT: i64 = 3_600_000;
/// library.IPOD_PATCHED_TRACK_LIMIT
pub const IPOD_PATCHED_TRACK_LIMIT: i64 = 32_000;
pub const LIBRARY_FIX: &str = "library-leak";

/// What the Python library reads from FLASHPOD_DB_LIMIT (no environment
/// in wasm: the caller passes it).
#[derive(Debug, Clone, Copy)]
pub struct Options {
    /// The byte limit; 0 disables it (library._db_size_limit).
    pub db_limit: i64,
    /// FLASHPOD_DB_LIMIT was set: taken at its word (no track cap).
    pub db_limit_set: bool,
}

impl Default for Options {
    fn default() -> Self {
        Options { db_limit: IPOD_DB_SIZE_LIMIT, db_limit_set: false }
    }
}

#[derive(Debug, Serialize)]
pub struct CardInfo {
    pub path: String,
    pub size_bytes: u64,
    pub partition_table: Option<&'static str>,
    pub partitions: Vec<Partition>,
    pub layout_ok: bool,
    pub problems: Vec<String>,
    pub firmware: Value,
    pub filesystem: Option<Value>,
    pub itunesdb: Value,
    pub music_files: usize,
    pub orphans: Vec<String>,
    pub missing: Vec<String>,
    pub past_128gib: bool,
    pub ipod_compatible: bool,
    pub ready: bool,
}

/// library.counts: (tracks, artists, albums), names case-folded.
pub fn counts(lib: &itunesdb::Library) -> (usize, usize, usize) {
    let unk = |s: &Option<String>| match s {
        Some(x) if !x.is_empty() => x.clone(),
        _ => "(unknown)".to_string(),
    };
    let artists: HashSet<String> = lib.tracks.iter().map(|t| casefold(&unk(&t.artist))).collect();
    let albums: HashSet<(String, String)> =
        lib.tracks.iter().map(|t| (casefold(&unk(&t.artist)), casefold(&unk(&t.album)))).collect();
    (lib.tracks.len(), artists.len(), albums.len())
}

pub fn inspect(dev: &mut dyn BlockDevice, path: &str, opts: Options) -> Result<CardInfo, Error> {
    let size = dev.len()?;
    let mut info = CardInfo {
        path: path.to_string(),
        size_bytes: size,
        partition_table: None,
        partitions: Vec::new(),
        layout_ok: false,
        problems: Vec::new(),
        firmware: json!({"present": false, "reason": "no layout"}),
        filesystem: None,
        itunesdb: json!({"state": "absent"}),
        music_files: 0,
        orphans: Vec::new(),
        missing: Vec::new(),
        past_128gib: false,
        ipod_compatible: false,
        ready: false,
    };
    if size < SECTOR {
        info.problems.push("empty image".to_string());
        return Ok(info);
    }
    let mbr = dev.read_vec(0, SECTOR as usize)?;
    let parts = match layout::partitions(&mbr) {
        None => {
            info.problems.push("no partition table (blank card?)".to_string());
            return Ok(info);
        }
        Some(p) => p,
    };
    info.partition_table = Some("mbr");
    let (fw, data, problems) = layout::layout_facts(&parts, size);
    info.partitions = parts;
    info.layout_ok = problems.is_empty();
    info.problems = problems;
    if let Some(f) = &fw {
        let base = f.start * SECTOR;
        let limit = f.sectors * SECTOR;
        let mut read = |off: i64, n: usize| -> Vec<u8> {
            if off < 0 || off as u64 >= limit {
                return Vec::new();
            }
            let n = (n as u64).min(limit - off as u64) as usize;
            dev.read_vec(base + off as u64, n).unwrap_or_default()
        };
        info.firmware = firmware::describe_installed(&mut read, &firmware::catalog());
    }
    if let Some(d) = &data {
        info.past_128gib = d.start + d.sectors > LBA28_MAX;
        let boot = dev.read_vec(d.start * SECTOR, SECTOR as usize)?;
        let label = {
            let s = latin1(crate::text::slice(&boot, 71, 82));
            let t = py_rstrip(&s).to_string();
            if t.is_empty() { Value::Null } else { json!(t) }
        };
        let fixes: Vec<String> = info
            .firmware
            .get("fixes")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
            .unwrap_or_default();
        match fat::open_image_fat(dev) {
            Err(e) => info.filesystem = Some(json!({"error": e.message})),
            Ok(mut fs) => {
                let cb = fs.sectors_per_cluster as u64 * SECTOR;
                let free = fs.free_bytes();
                info.filesystem = Some(json!({"type": "FAT32", "label": label,
                                              "total_bytes": fs.clusters * cb,
                                              "free_bytes": free, "cluster_bytes": cb}));
                inspect_library(&mut info, &mut fs, opts, &fixes);
            }
        }
    }
    info.ipod_compatible = info.layout_ok
        && info.filesystem.as_ref().map_or(false, |f| f.get("error").is_none());
    info.ready = info.ipod_compatible
        && info.firmware.get("present").and_then(Value::as_bool).unwrap_or(false)
        && info.firmware.pointer("/osos/checksum_ok").and_then(Value::as_bool).unwrap_or(false)
        && info.itunesdb.get("state").and_then(Value::as_str) == Some("ok");
    Ok(info)
}

fn inspect_library(info: &mut CardInfo, fs: &mut fat::Fat32, opts: Options, fixes: &[String]) {
    let step = (|| -> Result<Option<(Option<Vec<u8>>, BTreeSet<String>)>, String> {
        if !fs.exists("iPod_Control")? {
            return Ok(None);
        }
        let raw = fs.read_file(DB_PATH)?;
        let on_card: BTreeSet<String> = fs.music_files()?.into_iter().map(|(_r, loc)| loc).collect();
        Ok(Some((raw, on_card)))
    })();
    let (raw, on_card) = match step {
        Err(e) => {
            info.itunesdb = json!({"state": "unreadable", "error": e});
            return;
        }
        Ok(None) => {
            info.itunesdb = json!({"state": "absent", "reason": "no iPod_Control"});
            return;
        }
        Ok(Some(x)) => x,
    };
    info.music_files = on_card.len();
    let raw = match raw {
        None => {
            info.itunesdb = json!({"state": "absent", "reason": "no iTunesDB"});
            info.orphans = on_card.into_iter().collect();
            return;
        }
        Some(r) => r,
    };
    let lib = match itunesdb::parse_bytes(&raw) {
        Err(e) => {
            info.itunesdb = json!({"state": "corrupt", "error": e, "db_bytes": raw.len()});
            return;
        }
        Ok(l) => l,
    };
    let (ntracks, nartists, nalbums) = counts(&lib);
    let limit = if opts.db_limit != 0 { Some(opts.db_limit) } else { None };
    let mut ceiling = json!({"kind": "bytes", "limit": limit,
                             "headroom": limit.map(|l| l - raw.len() as i64)});
    if fixes.iter().any(|f| f == LIBRARY_FIX) && !opts.db_limit_set {
        let cap = IPOD_PATCHED_TRACK_LIMIT;
        ceiling = json!({"kind": "tracks", "limit": cap, "headroom": cap - ntracks as i64});
    }
    info.itunesdb = json!({"state": "ok", "name": lib.name, "tracks": ntracks, "artists": nartists,
                           "albums": nalbums, "db_bytes": raw.len(), "ceiling": ceiling});
    let in_db: BTreeSet<String> = lib.tracks.iter().filter_map(|t| t.location.clone()).collect();
    info.orphans = on_card.difference(&in_db).cloned().collect();
    info.missing = in_db.iter().filter(|l| !l.is_empty() && !on_card.contains(*l)).cloned().collect();
}

/// card.tracks: {"name", "tracks": [track_dict...]}; NotInitialized /
/// CorruptLibrary as card.library raises them.
pub fn tracks(dev: &mut dyn BlockDevice) -> Result<Value, Error> {
    let mut fs = fat::open_image_fat(dev).map_err(|e| {
        Error::new(ErrorKind::NotACard, e.message).detail("reason", e.reason).detail("error", e.error)
    })?;
    let raw = fs.read_file(DB_PATH).map_err(|e| Error::new(ErrorKind::Error, e))?;
    let raw = raw.ok_or_else(|| Error::new(ErrorKind::NotInitialized, "no iTunesDB on the card (init it first)"))?;
    let lib = itunesdb::parse_bytes(&raw).map_err(|e| Error::new(ErrorKind::CorruptLibrary, e))?;
    let list: Vec<_> = lib.tracks.iter().map(|t| t.to_dict()).collect();
    Ok(json!({"name": lib.name, "tracks": list}))
}
