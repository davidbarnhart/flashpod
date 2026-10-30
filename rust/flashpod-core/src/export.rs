//! Planning `music export` (#105, design in #69): where each track on the
//! card goes in an Artist/Album/NN Title.ext tree, and which files on the
//! card no track points at. Pure: the caller lists the card's files and
//! does the copying (with the host-side collision and resume rules).

use crate::itunesdb::{Library, Track};
use serde::Serialize;

/// One track to copy: its file on the card (a '/' path from the volume
/// root) and where it goes under the destination.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ExportItem {
    pub id: u32,
    pub source: String,
    pub dest: String,
    pub size: u64,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct ExportPlan {
    pub tracks: Vec<ExportItem>,
    /// Tracks whose file is not on the card (id, the location they name).
    pub missing: Vec<(u32, String)>,
    /// Files no track points at, minus iPod and OS machinery: (path, size).
    pub others: Vec<(String, u64)>,
}

/// A path component safe on Linux, macOS and Windows: no / \ : * ? " < > |
/// or control characters, no trailing dots or spaces, at most 255 bytes,
/// never empty, "." or "..".
pub fn sanitize(name: &str, fallback: &str) -> String {
    let mut s: String = name.chars()
        .filter(|c| !matches!(c, '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|') && !c.is_control())
        .collect();
    while s.len() > 255 {
        s.pop();
    }
    let s = s.trim().trim_end_matches(['.', ' ']).to_string();
    if s.is_empty() || s == "." || s == ".." {
        fallback.to_string()
    } else {
        s
    }
}

/// ":iPod_Control:Music:F00:x.mp3" -> "iPod_Control/Music/F00/x.mp3".
pub fn location_path(location: &str) -> String {
    location.trim_start_matches(':').replace(':', "/")
}

/// The file's extension: the one on the card when it has one (the DB's
/// filetype is unreliable for files `add` mislabelled, #33), else from the
/// filetype mhod, else "mp3".
fn extension(source: &str, filetype: Option<&str>) -> String {
    let base = source.rsplit('/').next().unwrap_or(source);
    if let Some((stem, ext)) = base.rsplit_once('.') {
        if !stem.is_empty() && !ext.is_empty() && ext.len() <= 5 {
            return ext.to_ascii_lowercase();
        }
    }
    match filetype.unwrap_or("") {
        "FLAC audio file" => "flac",
        "AAC audio file" => "m4a",
        "WAV audio file" => "wav",
        "AIFF audio file" => "aif",
        _ => "mp3",
    }.to_string()
}

fn present(s: &Option<String>) -> Option<&str> {
    s.as_deref().map(str::trim).filter(|t| !t.is_empty())
}

/// Artist/Album/NN Title.ext for a track whose file is `source`.
pub fn dest_of(t: &Track, source: &str) -> String {
    let artist = sanitize(present(&t.artist).unwrap_or("Unknown Artist"), "Unknown Artist");
    let album = sanitize(present(&t.album).unwrap_or("Unknown Album"), "Unknown Album");
    let base = source.rsplit('/').next().unwrap_or(source);
    let stem = base.rsplit_once('.').map(|(s, _)| s).unwrap_or(base);
    let title = present(&t.title).unwrap_or(stem);
    let ext = extension(source, t.filetype.as_deref());
    let name = if t.track_nr > 0 { format!("{:02} {}", t.track_nr, title) } else { title.to_string() };
    // leave room for " (NN)" and the extension inside 255 bytes
    let mut name = sanitize(&name, stem);
    while name.len() > 240 {
        name.pop();
    }
    format!("{}/{}/{}.{}", artist, album, name.trim_end_matches(['.', ' ']), ext)
}

/// Machinery and OS litter that is never offered as an "other file".
fn excluded(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    let first = lower.split('/').next().unwrap_or("");
    lower.starts_with("ipod_control/itunes/") || lower.starts_with("ipod_control/device/")
        || path.rsplit('/').next().is_some_and(|b| b.starts_with("._"))
        || matches!(first, ".trashes" | ".spotlight-v100" | ".fseventsd" | "system volume information"
                           | "$recycle.bin" | "recycled")
}

/// The export plan for `lib` given the card's files (every regular file,
/// '/' paths from the volume root, with sizes). Destinations that would
/// collide inside the plan get " (2)", " (3)", ... in track order.
pub fn plan_export(lib: &Library, files: &[(String, u64)]) -> ExportPlan {
    let by_lower: std::collections::HashMap<String, (&String, u64)> =
        files.iter().map(|(p, n)| (p.to_ascii_lowercase(), (p, *n))).collect();
    let mut plan = ExportPlan::default();
    let mut claimed = std::collections::HashSet::new();
    let mut taken = std::collections::HashSet::new();
    for t in &lib.tracks {
        let loc = match t.location.as_deref() {
            Some(l) if !l.is_empty() => l,
            _ => {
                plan.missing.push((t.id, String::new()));
                continue;
            }
        };
        let want = location_path(loc);
        let (source, size) = match by_lower.get(&want.to_ascii_lowercase()) {
            Some((p, n)) => ((*p).clone(), *n),
            None => {
                plan.missing.push((t.id, loc.to_string()));
                continue;
            }
        };
        claimed.insert(source.to_ascii_lowercase());
        let first = dest_of(t, &source);
        let mut dest = first.clone();
        let mut k = 2;
        while !taken.insert(dest.to_ascii_lowercase()) {
            let (stem, ext) = first.rsplit_once('.').unwrap_or((&first, ""));
            dest = format!("{} ({}).{}", stem, k, ext);
            k += 1;
        }
        plan.tracks.push(ExportItem { id: t.id, source, dest, size });
    }
    for (p, n) in files {
        if !claimed.contains(&p.to_ascii_lowercase()) && !excluded(p) {
            plan.others.push((p.clone(), *n));
        }
    }
    plan
}

/// Every regular file on a FAT volume, as ('/' path, size), walking from
/// the root by cluster (directories visited once, so a damaged volume with
/// a directory loop still ends).
pub fn fat_files(fs: &mut crate::fat::Fat32) -> Result<Vec<(String, u64)>, String> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut stack = vec![(String::new(), fs.root_cluster)];
    while let Some((prefix, cluster)) = stack.pop() {
        if !seen.insert(cluster) {
            continue;
        }
        for e in fs.read_dir(cluster)? {
            if e.attr & 0x08 != 0 || e.short == "." || e.short == ".." {
                continue;
            }
            let path = if prefix.is_empty() { e.name.clone() } else { format!("{}/{}", prefix, e.name) };
            if e.attr & crate::fat::ATTR_DIRECTORY != 0 {
                if e.first_cluster >= 2 {
                    stack.push((path, e.first_cluster));
                }
            } else {
                out.push((path, e.size as u64));
            }
        }
    }
    out.sort();
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn track(id: u32, title: Option<&str>, artist: Option<&str>, album: Option<&str>, nr: u32, loc: &str) -> Track {
        Track {
            id, title: title.map(String::from), artist: artist.map(String::from), album: album.map(String::from),
            track_nr: nr, location: Some(loc.to_string()), ..Default::default()
        }
    }

    #[test]
    fn fat_files_lists_an_initialized_card() {
        use crate::block::MemDevice;
        use crate::card::{flash, FlashOptions};
        let mut d = MemDevice { data: vec![0; 300 << 20] };
        let mut fw = vec![0u8; 4096];
        fw[0..4].copy_from_slice(b"{{~~");
        flash(&mut d, fw, FlashOptions { format: true, ..Default::default() }).unwrap();
        let mut ent = crate::entropy::Entropy::new(1, 0);
        crate::ops::init(&mut d, "iPod", true, &mut ent).unwrap();
        let mut fs = crate::ops::open_fat(&mut d).unwrap();
        let files = fat_files(&mut fs).unwrap();
        assert!(files.iter().any(|(p, n)| p == "iPod_Control/iTunes/iTunesDB" && *n > 0), "{:?}", files);
        assert!(files.iter().all(|(p, _)| !p.ends_with('/')));
    }

    #[test]
    fn sanitize_rules() {
        assert_eq!(sanitize("AC/DC: Live?", "x"), "ACDC Live");
        assert_eq!(sanitize("Trailing... ", "x"), "Trailing");
        assert_eq!(sanitize("..", "Unknown Album"), "Unknown Album");
        assert_eq!(sanitize("\u{7}", "Unknown Artist"), "Unknown Artist");
        assert!(sanitize(&"é".repeat(200), "x").len() <= 255);
    }

    #[test]
    fn layout_fallbacks_collisions_and_others() {
        let lib = Library {
            name: "iPod".into(), dbid: 0, playlists: Vec::new(),
            tracks: vec![
                track(1, Some("Blue Monday"), Some("New Order"), Some("Singles"), 3, ":iPod_Control:Music:F00:fp1.mp3"),
                track(2, None, None, None, 0, ":iPod_Control:Music:F01:fp2.m4a"),
                track(3, Some("Blue Monday"), Some("New Order"), Some("Singles"), 3, ":iPod_Control:Music:F02:fp3.mp3"),
                track(4, Some("Gone"), Some("X"), Some("Y"), 1, ":iPod_Control:Music:F03:fp4.mp3"),
            ],
        };
        let files: Vec<(String, u64)> = [
            ("iPod_Control/Music/F00/FP1.MP3", 10), ("iPod_Control/Music/F01/fp2.m4a", 20),
            ("iPod_Control/Music/F02/fp3.mp3", 30), ("iPod_Control/iTunes/iTunesDB", 5),
            ("iPod_Control/Music/F05/fp9.mp3", 40), ("Notes/thesis.doc", 50), ("._thesis.doc", 1),
            (".Trashes/501/x", 2),
        ].iter().map(|(p, n)| (p.to_string(), *n)).collect();
        let p = plan_export(&lib, &files);
        let dests: Vec<&str> = p.tracks.iter().map(|i| i.dest.as_str()).collect();
        assert_eq!(dests, ["New Order/Singles/03 Blue Monday.mp3", "Unknown Artist/Unknown Album/fp2.m4a",
                           "New Order/Singles/03 Blue Monday (2).mp3"]);
        assert_eq!(p.tracks[0].source, "iPod_Control/Music/F00/FP1.MP3");
        assert_eq!(p.missing, [(4, ":iPod_Control:Music:F03:fp4.mp3".to_string())]);
        assert_eq!(p.others, [("iPod_Control/Music/F05/fp9.mp3".to_string(), 40), ("Notes/thesis.doc".to_string(), 50)]);
    }
}
