//! The add batch, removal and the database ceiling: flashpod/library.py and
//! the tag-to-track half of tracks.py. Tags come from a [`TagSource`] (the
//! Python reference uses mutagen; until the port reads tags itself the
//! harness hands Rust what mutagen read).

use crate::entropy::Entropy;
use crate::error::{Error, ErrorKind};
use crate::itunesdb::{self, Library, Track, MARKER_FLAC, MARKER_MP3};
use crate::text::casefold;
use serde_json::json;
use std::collections::HashSet;

pub const ADD_HEADROOM: i64 = 2 << 20;
pub const AUDIO_EXTS: [&str; 8] = [".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff", ".flac"];

/// What tracks._track_from_audio takes from a file: the tags (as
/// tracks.first() returns them) and the stream info.
#[derive(Debug, Clone, Default, serde::Deserialize, serde::Serialize)]
pub struct AudioInfo {
    pub title: Option<String>,
    pub artist: Option<String>,
    pub album: Option<String>,
    pub genre: Option<String>,
    pub composer: Option<String>,
    pub tracknumber: Option<String>,
    pub date: Option<String>,
    pub length: f64,
    #[serde(default)]
    pub bitrate: i64,
    #[serde(default)]
    pub sample_rate: i64,
    pub size: u64,
}

pub enum TagResult {
    Audio(AudioInfo),
    /// reading raised; the exception's text
    Unreadable(String),
    /// mutagen returned None
    Unrecognized,
}

/// What a long call reports and asks while it runs, as card.py's
/// ``progress(stage, done, total, item)`` and ``cancelled()``: stages "tags"
/// (files read), "copy" (files copied), "write" (bytes of one file),
/// "rebuild" (files read back). Both are called on the caller's thread, at
/// the same points as in Python; ``cancelled`` is asked between files.
pub trait Hooks {
    fn progress(&mut self, _stage: &str, _done: u64, _total: u64, _item: &str) {}
    fn cancelled(&mut self) -> bool {
        false
    }
}

/// No progress, never cancelled.
pub struct NoHooks;
impl Hooks for NoHooks {}

pub trait TagSource {
    fn read(&mut self, path: &str) -> TagResult;
    /// A file on the card (a rebuild), read through `file`; by default
    /// looked up by its path like `read`.
    fn read_stream(&mut self, path: &str, file: &mut dyn crate::tags::ReadAt) -> TagResult {
        let _ = file;
        self.read(path)
    }
}

/// os.path.splitext: (root, ext); a leading dot doesn't start an extension.
pub fn splitext(path: &str) -> (&str, &str) {
    let base_start = path.rfind('/').map(|i| i + 1).unwrap_or(0);
    let base = &path[base_start..];
    match base.rfind('.') {
        Some(i) if base[..i].chars().any(|c| c != '.') => (&path[..base_start + i], &base[i..]),
        _ => (path, ""),
    }
}

pub fn basename(path: &str) -> &str {
    &path[path.rfind('/').map(|i| i + 1).unwrap_or(0)..]
}

fn py_isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

/// tracks._track_from_audio.
pub fn track_from_audio(a: &AudioInfo, fallback_title: &str, size: u64, ext: &str, ent: &mut Entropy) -> Track {
    let mut t = Track::new(ent);
    t.title = Some(a.title.clone().unwrap_or_else(|| fallback_title.to_string()));
    t.artist = a.artist.clone();
    t.album = a.album.clone();
    t.genre = a.genre.clone();
    t.composer = a.composer.clone();
    let (ft, marker) = if ext.to_lowercase() == ".flac" {
        ("FLAC audio file", MARKER_FLAC)
    } else {
        ("MPEG audio file", MARKER_MP3)
    };
    t.filetype = Some(ft.to_string());
    t.marker = marker;
    if let Some(n) = &a.tracknumber {
        let head = n.split('/').next().unwrap_or("");
        if py_isdigit(head) {
            t.track_nr = head.parse().unwrap_or(0);
        }
    }
    if let Some(d) = &a.date {
        let head: String = d.chars().take(4).collect();
        if py_isdigit(&head) {
            t.year = head.parse().unwrap_or(0);
        }
    }
    t.tracklen = (a.length * 1000.0) as i64 as u32;
    let mut bitrate = a.bitrate;
    if bitrate == 0 && a.length != 0.0 {
        bitrate = (size as f64 * 8.0 / a.length) as i64;
    }
    t.bitrate = bitrate.div_euclid(1000) as u32;
    t.samplerate = a.sample_rate as u32;
    t.size = size as u32;
    t
}

/// tracks.make_track: None (with the reason reported) for unusable files.
pub fn make_track(path: &str, nr: usize, total: usize, src: &mut dyn TagSource, ent: &mut Entropy,
                  report: &mut dyn FnMut(String)) -> Option<Track> {
    match src.read(path) {
        TagResult::Unreadable(e) => {
            report(format!("[{}/{}] skipping {}: unreadable ({})", nr, total, path, e));
            None
        }
        TagResult::Unrecognized => {
            report(format!("[{}/{}] skipping {}: not a recognized audio file", nr, total, path));
            None
        }
        TagResult::Audio(a) => {
            let (root, ext) = splitext(path);
            let _ = root;
            let fallback = splitext(basename(path)).0.to_string();
            Some(track_from_audio(&a, &fallback, a.size, ext, ent))
        }
    }
}

/// str.strip() with Python's whitespace set.
fn py_strip(s: &str) -> &str {
    let ws = |c: char| matches!(c, '\t' | '\n' | '\u{b}' | '\u{c}' | '\r' | '\u{1c}'..='\u{1f}' | ' ' | '\u{85}' | '\u{a0}')
        || (c as u32 > 0xff && c.is_whitespace());
    s.trim_matches(ws)
}

/// tracks.track_key.
pub fn track_key(t: &Track) -> (u32, u32, String) {
    (t.size, t.tracklen, casefold(py_strip(t.title.as_deref().unwrap_or(""))))
}

fn is_rebuilt_title(title: &str) -> bool {
    // re.compile(r"fp\d{6}$").match
    let b = title.as_bytes();
    b.len() == 8 && title.starts_with("fp") && title[2..].chars().all(|c| c.is_ascii_digit())
}

pub struct Batch {
    pub pending: Vec<(String, Track)>,
    pub failed: Vec<String>,
    pub skipped: Vec<String>,
}

/// library.read_batch.
pub fn read_batch(lib: &Library, files: &[String], src: &mut dyn TagSource, ent: &mut Entropy,
                  report: &mut dyn FnMut(String), hooks: &mut dyn Hooks) -> Batch {
    let mut seen: HashSet<(u32, u32, String)> = lib.tracks.iter().map(track_key).collect();
    let rebuilt: HashSet<(u32, u32)> = lib
        .tracks
        .iter()
        .filter(|t| is_rebuilt_title(t.title.as_deref().unwrap_or("")))
        .map(|t| (t.size, t.tracklen))
        .collect();
    let n = files.len();
    let mut b = Batch { pending: Vec::new(), failed: Vec::new(), skipped: Vec::new() };
    for (i, path) in files.iter().enumerate() {
        if hooks.cancelled() {
            break;
        }
        hooks.progress("tags", i as u64 + 1, n as u64, path);
        let track = match make_track(path, i + 1, n, src, ent, report) {
            None => {
                b.failed.push(path.clone());
                continue;
            }
            Some(t) => t,
        };
        let key = track_key(&track);
        if seen.contains(&key) || rebuilt.contains(&(track.size, track.tracklen)) {
            b.skipped.push(path.clone());
            continue;
        }
        seen.insert(key);
        b.pending.push((path.clone(), track));
    }
    b
}

/// library._db_size_limit's result, and whether FLASHPOD_DB_LIMIT was set.
pub use crate::inspect::Options;

fn projected(lib: &Library, new: &[&Track], ent: &mut Entropy) -> i64 {
    let mut tmp = Library::new(&lib.name, ent);
    tmp.tracks = lib.tracks.iter().cloned().chain(new.iter().map(|t| (*t).clone())).collect();
    itunesdb::serialize(&tmp, ent).len() as i64
}

fn fit_count(lib: &Library, new: &[&Track], limit: i64, ent: &mut Entropy) -> usize {
    if projected(lib, &[], ent) > limit {
        return 0;
    }
    let (mut lo, mut hi) = (0usize, new.len());
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if projected(lib, &new[..mid], ent) <= limit {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    lo
}

pub const IPOD_PATCHED_TRACK_LIMIT: i64 = 32_000;
pub const LIBRARY_FIX_NAME: &str = "library-leak";

#[derive(Debug, Clone)]
pub struct Ceiling {
    pub kind: Option<&'static str>,
    pub limit: i64,
    pub projected: Option<i64>,
    pub fit: usize,
    pub firmware: Option<bool>,
}

/// library.ceiling. `fixed()` reads the card's firmware for the
/// library-leak fix; called only past the byte limit, never when
/// FLASHPOD_DB_LIMIT was set.
pub fn ceiling(lib: &Library, new: &[&Track], opts: Options, fixed: &mut dyn FnMut() -> Option<bool>,
               ent: &mut Entropy) -> Ceiling {
    let limit = opts.db_limit;
    let n = new.len();
    if limit == 0 {
        return Ceiling { kind: None, limit, projected: None, fit: n, firmware: None };
    }
    let p = projected(lib, new, ent);
    if p <= limit {
        return Ceiling { kind: Some("bytes"), limit, projected: Some(p), fit: n, firmware: None };
    }
    let firmware = if !opts.db_limit_set { fixed() } else { None };
    if firmware == Some(true) {
        let cap = IPOD_PATCHED_TRACK_LIMIT;
        let have = lib.tracks.len() as i64;
        let fit = if have + n as i64 <= cap { n } else { (cap - have).max(0) as usize };
        return Ceiling { kind: Some("tracks"), limit: cap, projected: Some(p), fit, firmware: Some(true) };
    }
    Ceiling { kind: Some("bytes"), limit, projected: Some(p), fit: fit_count(lib, new, limit, ent), firmware }
}

/// library.select_for_removal: indices into lib.tracks, in Python's order.
pub fn select_for_removal(lib: &Library, what: &[String]) -> Result<Vec<usize>, Error> {
    let nst = |msg: String| Error::new(ErrorKind::NoSuchTrack, msg);
    if let Some(f) = what.first().filter(|f| *f == "artist" || *f == "album") {
        if what.len() < 2 {
            return Err(nst("name required".into()).detail("reason", "name_required").detail("field", f.as_str()));
        }
        let joined = what[1..].join(" ");
        let name = casefold(&joined);
        let v: Vec<usize> = lib
            .tracks
            .iter()
            .enumerate()
            .filter(|(_, t)| {
                let val = if f == "artist" { &t.artist } else { &t.album };
                casefold(val.as_deref().unwrap_or("")) == name
            })
            .map(|(i, _)| i)
            .collect();
        if v.is_empty() {
            return Err(nst("no tracks match that name".into())
                .detail("reason", "no_match").detail("field", f.as_str()).detail("name", joined));
        }
        return Ok(v);
    }
    let mut ids = Vec::new();
    for w in what {
        match w.trim().parse::<i64>() {
            Ok(i) => ids.push(i),
            Err(_) => return Err(nst("expected track ids".into()).detail("reason", "bad_ids")),
        }
    }
    let missing: Vec<i64> = ids.iter().copied().filter(|&i| !lib.tracks.iter().any(|t| t.id as i64 == i)).collect();
    if !missing.is_empty() {
        let list: Vec<String> = missing.iter().map(|i| i.to_string()).collect();
        return Err(nst(format!("no track with id {}", list.join(", ")))
            .detail("reason", "missing").detail("missing", json!(missing)));
    }
    // by_id keeps the last track with each id, as Python's dict does
    Ok(ids
        .iter()
        .map(|&i| lib.tracks.iter().rposition(|t| t.id as i64 == i).unwrap())
        .collect())
}

/// tracks.expand: directories become their audio files, sorted, recursing
/// in os.walk's order (a directory's own files, then its subdirectories,
/// both sorted; AppleDouble "._" files skipped). Plain paths pass through.
pub fn expand(paths: &[String], report: &mut dyn FnMut(String)) -> Vec<String> {
    fn walk(root: &str, found: &mut Vec<String>) {
        let rd = match std::fs::read_dir(root) {
            Ok(r) => r,
            Err(_) => return,
        };
        let mut dirs = Vec::new();
        let mut files = Vec::new();
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            let is_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false)
                || (e.file_type().map(|t| t.is_symlink()).unwrap_or(false) && e.path().is_dir());
            if is_dir {
                let real_dir = e.file_type().map(|t| t.is_dir()).unwrap_or(false);
                dirs.push((name, real_dir));
            } else {
                files.push(name);
            }
        }
        dirs.sort();
        files.sort();
        for f in files {
            if f.starts_with("._") {
                continue;
            }
            if AUDIO_EXTS.contains(&splitext(&f).1.to_lowercase().as_str()) {
                found.push(join(root, &f));
            }
        }
        for (d, real) in dirs {
            if real {
                // os.walk doesn't follow symlinked directories
                walk(&join(root, &d), found);
            }
        }
    }
    fn join(a: &str, b: &str) -> String {
        if a.ends_with('/') { format!("{}{}", a, b) } else { format!("{}/{}", a, b) }
    }
    let mut out = Vec::new();
    for p in paths {
        if !std::path::Path::new(p).is_dir() {
            out.push(p.clone());
            continue;
        }
        let mut found = Vec::new();
        walk(p, &mut found);
        if found.is_empty() {
            report(format!("warning: no audio files found under {}", p));
        }
        out.extend(found);
    }
    out
}

/// card._fingerprint: sha256 of the Python repr of
/// (len(tracks), sorted(keys of the library), [keys of the new tracks]).
pub fn fingerprint(lib: &Library, new: &[&Track]) -> String {
    let key_repr = |k: &(u32, u32, String)| format!("({}, {}, {})", k.0, k.1, crate::text::py_repr(&k.2));
    let mut have: Vec<(u32, u32, String)> = lib.tracks.iter().map(track_key).collect();
    have.sort();
    let new_keys: Vec<(u32, u32, String)> = new.iter().map(|t| track_key(t)).collect();
    let r = format!(
        "({}, [{}], [{}])",
        lib.tracks.len(),
        have.iter().map(key_repr).collect::<Vec<_>>().join(", "),
        new_keys.iter().map(key_repr).collect::<Vec<_>>().join(", ")
    );
    crate::firmware::sha256_hex(r.as_bytes())
}
