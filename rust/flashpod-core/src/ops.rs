//! The library calls that change a card's music: card.init, remove,
//! plan_add, apply_add, add, rebuild -- over an image's FAT, with
//! target.RawTarget's operations. Ids and "now" come from an [`Entropy`],
//! drawn where the Python library draws them.

use crate::block::BlockDevice;
use crate::entropy::Entropy;
use crate::error::{Error, ErrorKind};
use crate::fat::{self, Fat32, SECTOR};
use crate::itunesdb::{self, Library, Track};
use crate::library::{self, AudioInfo, Options, TagSource, ADD_HEADROOM};
use crate::text::py_repr;
use serde_json::{json, Value};

pub const DB: &str = "iPod_Control/iTunes/iTunesDB";

fn os_err(e: String) -> Error {
    Error::new(ErrorKind::Error, e)
}

/// _Image.target(): the image's FAT, NotACard when there isn't one.
pub fn open_fat(dev: &mut dyn BlockDevice) -> Result<Fat32<'_>, Error> {
    fat::open_image_fat(dev).map_err(|e| {
        Error::new(ErrorKind::NotACard, e.message).detail("reason", e.reason).detail("error", e.error)
    })
}

/// RawTarget.load_library: None without an iTunesDB, CorruptLibrary if it
/// won't parse.
pub fn load_library(fs: &mut Fat32, ent: &mut Entropy) -> Result<Option<Library>, Error> {
    match fs.read_file(DB).map_err(os_err)? {
        None => Ok(None),
        Some(data) => itunesdb::parse_bytes_with(&data, ent)
            .map(Some)
            .map_err(|e| Error::new(ErrorKind::CorruptLibrary, e)),
    }
}

/// card._load.
pub fn load(fs: &mut Fat32, ent: &mut Entropy) -> Result<Library, Error> {
    load_library(fs, ent)?
        .ok_or_else(|| Error::new(ErrorKind::NotInitialized, "no iTunesDB on the card (init it first)"))
}

pub fn save_library(fs: &mut Fat32, lib: &Library, ent: &mut Entropy) -> Result<(), Error> {
    let data = itunesdb::serialize(lib, ent);
    fs.write_file(DB, &data).map_err(os_err)?;
    fs.sync().map_err(os_err)
}

fn makedirs(fs: &mut Fat32, path: &str) -> Result<(), Error> {
    let mut cur = String::new();
    for part in path.split('/') {
        cur = if cur.is_empty() { part.to_string() } else { format!("{}/{}", cur, part) };
        if !fs.exists(&cur).map_err(os_err)? {
            fs.mkdir(&cur).map_err(os_err)?;
        }
    }
    Ok(())
}

fn ensure_dirs(fs: &mut Fat32) -> Result<(), Error> {
    let mut subs = vec!["iTunes".to_string(), "Device".to_string()];
    subs.extend((0..50).map(|i| format!("Music/F{:02}", i)));
    for s in subs {
        makedirs(fs, &format!("iPod_Control/{}", s))?;
    }
    Ok(())
}

/// card.init.
pub fn init(dev: &mut dyn BlockDevice, name: &str, overwrite: bool, ent: &mut Entropy) -> Result<Value, Error> {
    let mut fs = open_fat(dev)?;
    if !overwrite {
        let existing = match load_library(&mut fs, ent) {
            Err(e) if e.kind == ErrorKind::CorruptLibrary => true,
            Err(e) => return Err(e),
            Ok(x) => x.is_some(),
        };
        if existing {
            return Err(Error::new(ErrorKind::AlreadyInitialized, "the card already has an iTunesDB"));
        }
    }
    ensure_dirs(&mut fs)?;
    let lib = Library::new(name, ent);
    save_library(&mut fs, &lib, ent)?;
    fs.sync().map_err(os_err)?;
    Ok(json!({"name": name}))
}

/// RawTarget.remove_location.
fn remove_location(fs: &mut Fat32, location: &Option<String>) -> Result<(), Error> {
    let loc = match location.as_deref() {
        Some(l) if !l.is_empty() => l,
        _ => return Ok(()),
    };
    let path: String = loc.split(':').filter(|p| !p.is_empty()).collect::<Vec<_>>().join("/");
    if fs.exists(&path).map_err(os_err)? {
        fs.remove(&path).map_err(os_err)?;
    }
    Ok(())
}

/// card.remove: the removed tracks as dicts.
pub fn remove(dev: &mut dyn BlockDevice, what: &[String], ent: &mut Entropy) -> Result<Value, Error> {
    let mut fs = open_fat(dev)?;
    let mut lib = load(&mut fs, ent)?;
    let idx = library::select_for_removal(&lib, what)?;
    let victims: Vec<Track> = idx.iter().map(|&i| lib.tracks[i].clone()).collect();
    let mut taken = vec![false; lib.tracks.len()];
    for &i in &idx {
        remove_location(&mut fs, &lib.tracks[i].location)?;
        taken[i] = true;
    }
    let mut k = 0;
    lib.tracks.retain(|_| {
        let keep = !taken[k];
        k += 1;
        keep
    });
    save_library(&mut fs, &lib, ent)?;
    Ok(json!(victims.iter().map(|t| t.to_dict()).collect::<Vec<_>>()))
}

/// RawTarget.copy: a collision-free fp###### name in a random F## folder.
fn copy(fs: &mut Fat32, src: &str, read_source: &mut dyn FnMut(&str) -> std::io::Result<Vec<u8>>,
        ent: &mut Entropy) -> Result<String, String> {
    let music = "iPod_Control/Music";
    let mut fdirs: Vec<String> = fs
        .listdir(music)?
        .unwrap_or_default()
        .into_iter()
        .map(|e| e.name)
        .filter(|n| n.to_uppercase().starts_with('F'))
        .collect();
    fdirs.sort();
    if fdirs.is_empty() {
        return Err("no Music/F## directories (run init first?)".into());
    }
    let fdir = fdirs[ent.rng.choice_index(fdirs.len())].clone();
    let (_, ext) = library::splitext(src);
    let ext = if ext.is_empty() { ".mp3".to_string() } else { ext.to_lowercase() };
    let (name, dst) = loop {
        let name = format!("fp{:06}{}", ent.rng.randrange(1_000_000), ext);
        let dst = format!("{}/{}/{}", music, fdir, name);
        if !fs.exists(&dst)? {
            break (name, dst);
        }
    };
    // the source is read after the name is drawn, as RawTarget.copy does
    let data = read_source(src)
        .map_err(|e| format!("[Errno {}] {}: {}", e.raw_os_error().unwrap_or(0), e, py_repr(src)))?;
    fs.write_file(&dst, &data)?;
    Ok(format!(":iPod_Control:Music:{}:{}", fdir, name))
}

/// A plan, as card.plan_add returns it: the JSON fields and what apply
/// needs (the library and the pending (path, track) pairs).
pub struct Plan {
    pub json: Value,
    pub lib: Library,
    pub items: Vec<(String, Track)>,
    pub fixed: Option<Option<bool>>,
}

/// The library-fix probe, memoized, over the FAT's block device (the disk's
/// sector 0 is -part_start, as RawTarget.firmware_read reads it).
fn probe(fs: &mut Fat32, memo: &mut Option<Option<bool>>) -> Option<bool> {
    if let Some(v) = memo {
        return *v;
    }
    let part = fs.sec.part_start;
    let sec = &mut fs.sec;
    let mut sectors = |lba: u64, count: u64| sec.read(lba.wrapping_sub(part), count).ok();
    let mbr = sectors(0, 1);
    let v = mbr.and_then(|mbr| {
        if mbr[510] != 0x55 || mbr[511] != 0xaa {
            return None;
        }
        let mut start = None;
        for off in [446usize, 462, 478, 494] {
            let lba = u32::from_le_bytes(mbr[off + 8..off + 12].try_into().unwrap()) as u64;
            let cnt = u32::from_le_bytes(mbr[off + 12..off + 16].try_into().unwrap());
            if mbr[off + 4] == 0 && lba != 0 && cnt != 0 {
                start = Some(lba);
                break;
            }
        }
        let start = start?;
        let mut read = |offset: u64, n: usize| -> Option<Vec<u8>> {
            if n == 0 {
                return Some(Vec::new());
            }
            let first = offset / SECTOR;
            let last = (offset + n as u64 - 1) / SECTOR;
            let data = sectors(start + first, last - first + 1)?;
            let skip = (offset - first * SECTOR) as usize;
            Some(data[skip..skip + n].to_vec())
        };
        crate::firmware::fix_on_card(&mut read, crate::library::LIBRARY_FIX_NAME)
    });
    *memo = Some(v);
    v
}

/// card.plan_add. `files` expanded unless `expand_dirs`.
#[allow(clippy::too_many_arguments)]
pub fn plan_add_fs(fs: &mut Fat32, files: &[String], src: &mut dyn TagSource, ent: &mut Entropy, opts: Options,
                   expand_dirs: bool, assess: bool, lib: Option<Library>) -> Result<Plan, Error> {
    let mut warnings = Vec::new();
    let paths: Vec<String> = if expand_dirs {
        library::expand(files, &mut |m| warnings.push(m))
    } else {
        files.to_vec()
    };
    if paths.is_empty() {
        return Err(Error::new(ErrorKind::NothingToAdd, "no audio files in that selection")
            .detail("warnings", json!(warnings)));
    }
    let mut unreadable = Vec::new();
    let lib = match lib {
        Some(l) => l,
        None => load(fs, ent)?,
    };
    let b = library::read_batch(&lib, &paths, src, ent, &mut |m| unreadable.push(m));
    let (mut free, mut free_error) = (None, None);
    if !b.pending.is_empty() {
        match fs.free_bytes() {
            Some(v) => free = Some(v as i64),
            None => free_error = Some("free-cluster count unavailable (FSInfo has none)".to_string()),
        }
    }
    let mut memo = None;
    let new: Vec<&Track> = b.pending.iter().map(|(_, t)| t).collect();
    let db = if assess && !b.pending.is_empty() {
        Some(library::ceiling(&lib, &new, opts, &mut || probe(fs, &mut memo), ent))
    } else {
        None
    };
    let budget = free.map(|f| f - ADD_HEADROOM);
    let batch: i64 = b.pending.iter().map(|(_, t)| t.size as i64).sum();
    let newj: Vec<Value> = b
        .pending
        .iter()
        .map(|(p, t)| {
            let mut v = serde_json::to_value(t.to_dict()).unwrap();
            v["path"] = json!(p);
            v
        })
        .collect();
    let mut j = json!({
        "files": paths.len(), "new": newj, "duplicates": b.skipped, "unreadable": unreadable,
        "failed": b.failed, "warnings": warnings, "batch_bytes": batch, "free_bytes": free,
        "free_error": free_error, "budget": budget, "cancelled": false,
    });
    if assess {
        let space_fit = match budget {
            Some(bu) if batch > bu => {
                let (mut n, mut used) = (0usize, 0i64);
                for (_, t) in &b.pending {
                    used += t.size as i64;
                    if used > bu {
                        break;
                    }
                    n += 1;
                }
                n
            }
            _ => b.pending.len(),
        };
        let (limit, projected, db_fit, kind, fix) = match &db {
            Some(c) => (c.limit, c.projected, c.fit, c.kind.unwrap_or("bytes"), c.firmware),
            None => (opts.db_limit, None, 0, "bytes", None),
        };
        let kind = if db.as_ref().map_or(false, |c| c.kind.is_none()) { Value::Null } else { json!(kind) };
        j["space_fit"] = json!(space_fit);
        j["db"] = json!({"limit": if limit != 0 { json!(limit) } else { Value::Null },
                         "projected": projected, "fit": db_fit, "kind": kind, "library_fix": fix});
        let fits = space_fit.min(db_fit);
        j["fits"] = json!(fits);
        j["fits_all"] = json!(fits == b.pending.len());
        j["fingerprint"] = json!(library::fingerprint(&lib, &new));
    }
    Ok(Plan { json: j, lib, items: b.pending, fixed: memo })
}

pub fn plan_add(dev: &mut dyn BlockDevice, files: &[String], src: &mut dyn TagSource, ent: &mut Entropy,
                opts: Options, expand_dirs: bool, assess: bool) -> Result<Plan, Error> {
    let mut fs = open_fat(dev)?;
    plan_add_fs(&mut fs, files, src, ent, opts, expand_dirs, assess, None)
}

/// card.apply_add: copy `keep` (default: the plan's tracks), save once.
pub fn apply_add_fs(fs: &mut Fat32, plan: &mut Plan, keep: Option<usize>, save_anyway: bool,
                    read_source: &mut dyn FnMut(&str) -> std::io::Result<Vec<u8>>, ent: &mut Entropy)
    -> Result<Value, Error>
{
    let n = keep.unwrap_or(plan.items.len()).min(plan.items.len());
    let items: Vec<(String, Track)> = plan.items[..n].to_vec();
    let mut added: Vec<Track> = Vec::new();
    let mut failed: Vec<Value> = Vec::new();
    for (path, mut track) in items {
        let located = copy(fs, &path, read_source, ent);
        match located {
            Err(e) => failed.push(json!({"path": path, "error": e})),
            Ok(loc) => {
                track.location = Some(loc);
                track.id = plan.lib.next_track_id();
                plan.lib.tracks.push(track.clone());
                added.push(track);
            }
        }
    }
    if !added.is_empty() || save_anyway {
        save_library(fs, &plan.lib, ent)?;
    }
    Ok(json!({
        "added": added.iter().map(|t| t.to_dict()).collect::<Vec<_>>(),
        "duplicates": plan.json["duplicates"], "unreadable": plan.json["unreadable"],
        "failed": failed, "dropped_space": 0, "dropped_ceiling": 0, "cancelled": false,
        "bytes": added.iter().map(|t| t.size as i64).sum::<i64>(),
    }))
}

/// card.add with a policy: full "stop"|"fit", ceiling "refuse"|"fit"|"force".
#[allow(clippy::too_many_arguments)]
pub fn add(dev: &mut dyn BlockDevice, files: &[String], full: &str, ceiling_pol: &str, src: &mut dyn TagSource,
           read_source: &mut dyn FnMut(&str) -> std::io::Result<Vec<u8>>, ent: &mut Entropy, opts: Options)
    -> Result<Value, Error>
{
    let mut fs = open_fat(dev)?;
    let mut now = plan_add_fs(&mut fs, files, src, ent, opts, true, true, None)?;
    let mut keep = now.items.len();
    let (mut dropped_space, mut dropped_ceiling) = (0, 0);
    let space_fit = now.json["space_fit"].as_u64().unwrap_or(0) as usize;
    if space_fit < keep {
        if full != "fit" {
            let budget = now.json["budget"].clone();
            return Err(Error::new(ErrorKind::NoSpace, format!(
                "the batch needs {} bytes; {} free", now.json["batch_bytes"], budget.as_i64().unwrap_or(0)))
                .detail("batch_bytes", now.json["batch_bytes"].clone()).detail("budget", budget)
                .detail("fit", space_fit));
        }
        dropped_space = keep - space_fit;
        keep = space_fit;
    }
    if keep > 0 {
        let new: Vec<&Track> = now.items[..keep].iter().map(|(_, t)| t).collect();
        let mut memo = now.fixed;
        let c = library::ceiling(&now.lib, &new, opts, &mut || probe(&mut fs, &mut memo), ent);
        if c.fit < keep {
            if ceiling_pol == "refuse" || (ceiling_pol == "fit" && c.fit == 0) {
                let msg = if c.kind == Some("tracks") {
                    format!("the library would hold {} tracks, past the {} the firmware lists",
                            now.lib.tracks.len() + keep, c.limit)
                } else {
                    format!("the iTunesDB would be {} bytes, past the {}-byte limit", c.projected.unwrap_or(0), c.limit)
                };
                return Err(Error::new(ErrorKind::DbCeiling, msg).detail("limit", c.limit)
                    .detail("projected", c.projected).detail("fit", c.fit).detail("kind", c.kind));
            }
            if ceiling_pol == "fit" {
                dropped_ceiling = keep - c.fit;
                keep = c.fit;
            }
        }
    }
    let mut rep = apply_add_fs(&mut fs, &mut now, Some(keep), false, read_source, ent)?;
    rep["dropped_space"] = json!(dropped_space);
    rep["dropped_ceiling"] = json!(dropped_ceiling);
    Ok(rep)
}

/// card.rebuild: the library rebuilt from the music files on the card (tags
/// read by `src` from each file, read lazily off the card), saved unless
/// !save. A file that can't be opened or read is skipped with a message.
pub fn rebuild(dev: &mut dyn BlockDevice, name: &str, save: bool, src: &mut dyn TagSource, ent: &mut Entropy)
    -> Result<Value, Error>
{
    let mut fs = open_fat(dev)?;
    ensure_dirs(&mut fs)?;
    let files = fs.music_files().map_err(os_err)?;
    let mut skipped = Vec::new();
    let mut located: Vec<(String, Track)> = Vec::new();
    for (relpath, location) in files {
        // target.read_track_file: open_file (None for a missing file or a
        // directory), then the tags through the lazy file
        let entry = match fs.resolve(&relpath) {
            Ok(Some(e)) if e.attr & crate::fat::ATTR_DIRECTORY == 0 => e,
            Ok(_) => continue,
            Err(e) => {
                skipped.push(format!("skipping {}: {}", relpath, e));
                continue;
            }
        };
        let size = entry.size as u64;
        let result = match crate::tags::FatFile::open(&mut fs, entry.first_cluster, size) {
            Ok(mut file) => src.read_stream(&relpath, &mut file),
            Err(e) => library::TagResult::Unreadable(e),
        };
        match result {
            library::TagResult::Unreadable(e) => skipped.push(format!("skipping {}: {}", relpath, e)),
            library::TagResult::Unrecognized => {}
            library::TagResult::Audio(a) => {
                let (_, ext) = library::splitext(&relpath);
                let fallback = library::splitext(library::basename(&relpath)).0.to_string();
                let info = AudioInfo { size, ..a };
                located.push((location, library::track_from_audio(&info, &fallback, size, ext, ent)));
            }
        }
    }
    let mut lib = Library::new(name, ent);
    for (loc, mut t) in located {
        t.location = Some(loc);
        t.id = lib.next_track_id();
        lib.tracks.push(t);
    }
    if save {
        save_library(&mut fs, &lib, ent)?;
    }
    Ok(json!({"tracks": lib.tracks.len(), "skipped": skipped}))
}
