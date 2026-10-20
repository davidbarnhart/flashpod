//! iTunesDB reading: itunesdb.parse_bytes, with the same strict structure
//! checks and messages (inspect reports a corrupt DB's message).

use crate::text::{slice, u32_at, u64_at, utf16le_replace};
use serde::Serialize;
use std::collections::HashMap;

pub const MHOD_TITLE: u32 = 1;
pub const MHOD_LOCATION: u32 = 2;
pub const MHOD_ALBUM: u32 = 3;
pub const MHOD_ARTIST: u32 = 4;
pub const MHOD_GENRE: u32 = 5;
pub const MHOD_FILETYPE: u32 = 6;
pub const MHOD_COMPOSER: u32 = 12;
pub const MHOD_PLAYLIST_POS: u32 = 100;
const STR_MHODS: [u32; 9] = [1, 2, 3, 4, 5, 6, 7, 8, 12];

#[derive(Debug, Clone, Default)]
pub struct Track {
    pub id: u32,
    pub size: u32,
    pub tracklen: u32,
    pub track_nr: u32,
    pub year: u32,
    pub bitrate: u32,
    pub samplerate: u32,
    pub dbid: u64,
    pub title: Option<String>,
    pub location: Option<String>,
    pub album: Option<String>,
    pub artist: Option<String>,
    pub genre: Option<String>,
    pub filetype: Option<String>,
    pub composer: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Library {
    pub name: String,
    pub tracks: Vec<Track>,
    pub dbid: u64,
}

/// card.track_dict.
#[derive(Debug, Serialize)]
pub struct TrackDict {
    pub id: u32,
    pub title: Option<String>,
    pub artist: Option<String>,
    pub album: Option<String>,
    pub genre: Option<String>,
    pub composer: Option<String>,
    pub track_nr: u32,
    pub year: u32,
    pub length_ms: u32,
    pub size: u32,
    pub bitrate: u32,
    pub samplerate: u32,
    pub filetype: Option<String>,
    pub location: Option<String>,
}

impl Track {
    pub fn to_dict(&self) -> TrackDict {
        TrackDict {
            id: self.id,
            title: self.title.clone(),
            artist: self.artist.clone(),
            album: self.album.clone(),
            genre: self.genre.clone(),
            composer: self.composer.clone(),
            track_nr: self.track_nr,
            year: self.year,
            length_ms: self.tracklen,
            size: self.size,
            bitrate: self.bitrate,
            samplerate: self.samplerate,
            filetype: self.filetype.clone(),
            location: self.location.clone(),
        }
    }
}

enum Mhod {
    Str(String),
    #[allow(dead_code)] // parsed as Python does; nothing reads it yet
    Pos(u32),
}

fn parse_mhods(buf: &[u8], mut off: usize, count: u32) -> Result<HashMap<u32, Mhod>, String> {
    let mut out = HashMap::new();
    for _ in 0..count {
        if slice(buf, off, off + 4) != b"mhod" {
            break;
        }
        let hlen = u32_at(buf, off + 4)? as usize;
        let tlen = u32_at(buf, off + 8)? as usize;
        let typ = u32_at(buf, off + 12)?;
        if STR_MHODS.contains(&typ) {
            let slen = u32_at(buf, off + hlen + 4)? as usize;
            let start = off + hlen + 16;
            out.insert(typ, Mhod::Str(utf16le_replace(slice(buf, start, start + slen))));
        } else if typ == MHOD_PLAYLIST_POS {
            out.insert(typ, Mhod::Pos(u32_at(buf, off + hlen)?));
        }
        off += tlen;
    }
    Ok(out)
}

fn take_str(m: &mut HashMap<u32, Mhod>, k: u32) -> Option<String> {
    match m.remove(&k) {
        Some(Mhod::Str(s)) => Some(s),
        _ => None,
    }
}

fn parse_mhit(buf: &[u8], off: usize) -> Result<(Track, usize), String> {
    let hlen = u32_at(buf, off + 4)? as usize;
    let tlen = u32_at(buf, off + 8)? as usize;
    let nmhod = u32_at(buf, off + 12)?;
    let mut t = Track {
        id: u32_at(buf, off + 16)?,
        size: u32_at(buf, off + 36)?,
        tracklen: u32_at(buf, off + 40)?,
        track_nr: u32_at(buf, off + 44)?,
        year: u32_at(buf, off + 52)?,
        bitrate: u32_at(buf, off + 56)?,
        samplerate: u32_at(buf, off + 60)? >> 16,
        ..Default::default()
    };
    if hlen >= 104 {
        t.dbid = u64_at(buf, off + 96)?;
    }
    let mut m = parse_mhods(buf, off + hlen, nmhod)?;
    t.title = take_str(&mut m, MHOD_TITLE);
    t.location = take_str(&mut m, MHOD_LOCATION);
    t.album = take_str(&mut m, MHOD_ALBUM);
    t.artist = take_str(&mut m, MHOD_ARTIST);
    t.genre = take_str(&mut m, MHOD_GENRE);
    t.filetype = take_str(&mut m, MHOD_FILETYPE);
    t.composer = take_str(&mut m, MHOD_COMPOSER);
    Ok((t, off + tlen))
}

/// itunesdb.parse_bytes. Err is the ValueError / struct.error message.
pub fn parse_bytes(buf: &[u8]) -> Result<Library, String> {
    if slice(buf, 0, 4) != b"mhbd" {
        return Err("not an iTunesDB (no mhbd header)".into());
    }
    if buf.len() < 24 {
        return Err(format!("corrupt iTunesDB: {} bytes is shorter than its header", buf.len()));
    }
    let mut lib = Library {
        name: "iPod".into(),
        tracks: Vec::new(),
        dbid: if buf.len() >= 32 { u64_at(buf, 24)? } else { 0 },
    };
    let hlen = u32_at(buf, 4)? as usize;
    let nchildren = u32_at(buf, 20)?;
    if !(24 <= hlen && hlen <= buf.len()) {
        return Err(format!("corrupt iTunesDB: header length {} (file is {} bytes)", hlen, buf.len()));
    }
    let mut off = hlen;
    let mut tracks_seen = false;
    for i in 0..nchildren {
        if slice(buf, off, off + 4) != b"mhsd" {
            return Err(format!("corrupt iTunesDB: dataset {} of {} missing at offset {}", i + 1, nchildren, off));
        }
        let sd_hlen = u32_at(buf, off + 4)? as usize;
        let sd_tlen = u32_at(buf, off + 8)? as usize;
        let sd_type = u32_at(buf, off + 12)?;
        if sd_tlen < sd_hlen || sd_hlen < 16 || off + sd_tlen > buf.len() {
            return Err(format!(
                "corrupt iTunesDB: dataset {} at offset {} has length {} (header {}, file {} bytes)",
                i + 1, off, sd_tlen, sd_hlen, buf.len()
            ));
        }
        if sd_type == 1 {
            parse_tracks(buf, off + sd_hlen, &mut lib, off + sd_tlen)?;
            tracks_seen = true;
        } else if sd_type == 2 {
            parse_playlists(buf, off + sd_hlen, &mut lib)?;
        }
        off += sd_tlen;
    }
    if !tracks_seen {
        return Err("corrupt iTunesDB: no track dataset".into());
    }
    Ok(lib)
}

fn parse_tracks(buf: &[u8], mut off: usize, lib: &mut Library, end: usize) -> Result<(), String> {
    if slice(buf, off, off + 4) != b"mhlt" {
        return Err(format!("corrupt iTunesDB: no track list at offset {}", off));
    }
    let hlen = u32_at(buf, off + 4)? as usize;
    let ntracks = u32_at(buf, off + 8)?;
    off += hlen;
    for i in 0..ntracks {
        if slice(buf, off, off + 4) != b"mhit" {
            return Err(format!("corrupt iTunesDB: track {} of {} missing at offset {}", i + 1, ntracks, off));
        }
        let tlen = u32_at(buf, off + 8)? as usize;
        if tlen < u32_at(buf, off + 4)? as usize || off + tlen > end {
            return Err(format!("corrupt iTunesDB: track {} at offset {} has length {} past its dataset", i + 1, off, tlen));
        }
        let (t, next) = parse_mhit(buf, off)?;
        lib.tracks.push(t);
        off = next;
    }
    Ok(())
}

fn parse_playlists(buf: &[u8], mut off: usize, lib: &mut Library) -> Result<(), String> {
    if slice(buf, off, off + 4) != b"mhlp" {
        return Ok(());
    }
    let hlen = u32_at(buf, off + 4)? as usize;
    let nlists = u32_at(buf, off + 8)?;
    off += hlen;
    for _ in 0..nlists {
        if slice(buf, off, off + 4) != b"mhyp" {
            break;
        }
        let p_hlen = u32_at(buf, off + 4)? as usize;
        let p_tlen = u32_at(buf, off + 8)? as usize;
        let nmhod = u32_at(buf, off + 12)?;
        let is_master = u32_at(buf, off + 20)? != 0;
        let mut m = parse_mhods(buf, off + p_hlen, nmhod)?;
        if is_master {
            if let Some(name) = take_str(&mut m, MHOD_TITLE) {
                lib.name = name;
            }
        }
        off += p_tlen;
    }
    Ok(())
}
