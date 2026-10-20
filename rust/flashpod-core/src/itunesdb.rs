//! iTunesDB reading: itunesdb.parse_bytes, with the same strict structure
//! checks and messages (inspect reports a corrupt DB's message).

use crate::entropy::Entropy;
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
pub const MAC_EPOCH_OFFSET: i64 = 2_082_844_800;
pub const MARKER_MP3: [u8; 4] = *b"\x00MP3";
pub const MARKER_FLAC: [u8; 4] = *b"\x00FLA";
const STR_MHODS: [u32; 9] = [1, 2, 3, 4, 5, 6, 7, 8, 12];

#[derive(Debug, Clone, Default)]
pub struct Track {
    pub time_added: u32,
    pub marker: [u8; 4],
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

impl Library {
    /// itunesdb.Library(name): one id draw.
    pub fn new(name: &str, ent: &mut Entropy) -> Self {
        Library { name: name.to_string(), tracks: Vec::new(), dbid: ent.rng.getrandbits(64) }
    }

    pub fn next_track_id(&self) -> u32 {
        self.tracks.iter().map(|t| t.id).max().unwrap_or(51) + 1
    }
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
    /// itunesdb.Track(): "now" for time added, then one id draw.
    pub fn new(ent: &mut Entropy) -> Self {
        let time_added = (ent.now + MAC_EPOCH_OFFSET) as u32;
        Track { time_added, marker: MARKER_MP3, dbid: ent.rng.getrandbits(64), ..Default::default() }
    }

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

fn parse_mhit(buf: &[u8], off: usize, ent: &mut Entropy) -> Result<(Track, usize), String> {
    let hlen = u32_at(buf, off + 4)? as usize;
    let tlen = u32_at(buf, off + 8)? as usize;
    let nmhod = u32_at(buf, off + 12)?;
    let fresh = Track::new(ent); // Python's Track() draws an id here
    let mut t = Track {
        time_added: fresh.time_added,
        marker: fresh.marker,
        dbid: fresh.dbid,
        id: u32_at(buf, off + 16)?,
        size: u32_at(buf, off + 36)?,
        tracklen: u32_at(buf, off + 40)?,
        track_nr: u32_at(buf, off + 44)?,
        year: u32_at(buf, off + 52)?,
        bitrate: u32_at(buf, off + 56)?,
        samplerate: u32_at(buf, off + 60)? >> 16,
        ..Default::default()
    };
    // the parser keeps Track()'s marker and time added (it reads neither)
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
    parse_bytes_with(buf, &mut Entropy::new(0, 0))
}

/// parse_bytes, making Python's id draws (one for the Library, one per
/// track) from `ent`.
pub fn parse_bytes_with(buf: &[u8], ent: &mut Entropy) -> Result<Library, String> {
    if slice(buf, 0, 4) != b"mhbd" {
        return Err("not an iTunesDB (no mhbd header)".into());
    }
    if buf.len() < 24 {
        return Err(format!("corrupt iTunesDB: {} bytes is shorter than its header", buf.len()));
    }
    let mut lib = Library::new("iPod", ent);
    lib.dbid = if buf.len() >= 32 { u64_at(buf, 24)? } else { 0 };
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
            parse_tracks(buf, off + sd_hlen, &mut lib, off + sd_tlen, ent)?;
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

fn parse_tracks(buf: &[u8], mut off: usize, lib: &mut Library, end: usize, ent: &mut Entropy) -> Result<(), String> {
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
        let (t, next) = parse_mhit(buf, off, ent)?;
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


// ---------------------------------------------------------------------------
// Writing: itunesdb.serialize, the old two-dataset format.
// ---------------------------------------------------------------------------
fn mk_string_mhod(typ: u32, text: &str) -> Vec<u8> {
    let data: Vec<u8> = text.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
    let mut out = b"mhod".to_vec();
    for v in [24u32, 24 + 16 + data.len() as u32, typ, 0, 0, 1, data.len() as u32, 0, 0] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(&data);
    out
}

fn mk_pos_mhod(position: u32) -> Vec<u8> {
    let mut out = b"mhod".to_vec();
    for v in [24u32, 24 + 20, MHOD_PLAYLIST_POS, 0, 0, position, 0, 0, 0, 0] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

fn put(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_le_bytes());
}

fn mk_mhit(t: &Track) -> Vec<u8> {
    let mut mhods = Vec::new();
    let mut n = 0u32;
    for (typ, val) in [(MHOD_TITLE, &t.title), (MHOD_LOCATION, &t.location), (MHOD_ALBUM, &t.album),
                       (MHOD_ARTIST, &t.artist), (MHOD_GENRE, &t.genre), (MHOD_FILETYPE, &t.filetype),
                       (MHOD_COMPOSER, &t.composer)] {
        if let Some(v) = val.as_deref().filter(|v| !v.is_empty()) {
            mhods.extend(mk_string_mhod(typ, v));
            n += 1;
        }
    }
    const HLEN: usize = 156;
    let mut h = vec![0u8; HLEN];
    h[0..4].copy_from_slice(b"mhit");
    put(&mut h, 4, HLEN as u32);
    put(&mut h, 8, (HLEN + mhods.len()) as u32);
    put(&mut h, 12, n);
    put(&mut h, 16, t.id);
    put(&mut h, 20, 1);
    h[24..28].copy_from_slice(&t.marker);
    put(&mut h, 32, t.time_added);
    put(&mut h, 36, t.size);
    put(&mut h, 40, t.tracklen);
    put(&mut h, 44, t.track_nr);
    put(&mut h, 52, t.year);
    put(&mut h, 56, t.bitrate);
    put(&mut h, 60, (t.samplerate & 0xFFFF) << 16);
    put(&mut h, 88, t.time_added);
    h[96..104].copy_from_slice(&t.dbid.to_le_bytes());
    h[104] = 1;
    h.extend(mhods);
    h
}

fn mk_mhyp(name: &str, ids: &[u32], master: bool, ent: &mut Entropy) -> Vec<u8> {
    let mhods = mk_string_mhod(MHOD_TITLE, name);
    let now = (ent.now + MAC_EPOCH_OFFSET) as u32;
    let mut items = Vec::new();
    for (k, &tid) in ids.iter().enumerate() {
        let pos = k as u32 + 1;
        let pm = mk_pos_mhod(pos);
        let mut item = b"mhip".to_vec();
        for v in [76u32, 76 + pm.len() as u32, 1, 0, pos, tid, now] {
            item.extend_from_slice(&v.to_le_bytes());
        }
        item.resize(76, 0);
        item.extend(pm);
        items.extend(item);
    }
    const HLEN: usize = 108;
    let mut h = vec![0u8; HLEN];
    h[0..4].copy_from_slice(b"mhyp");
    put(&mut h, 4, HLEN as u32);
    put(&mut h, 8, (HLEN + mhods.len() + items.len()) as u32);
    put(&mut h, 12, 1);
    put(&mut h, 16, ids.len() as u32);
    put(&mut h, 20, master as u32);
    put(&mut h, 24, now);
    h[28..36].copy_from_slice(&ent.rng.getrandbits(64).to_le_bytes());
    h.extend(mhods);
    h.extend(items);
    h
}

fn mk_mhsd(sd_type: u32, payload: &[u8]) -> Vec<u8> {
    let mut out = b"mhsd".to_vec();
    for v in [96u32, 96 + payload.len() as u32, sd_type] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.resize(96, 0);
    out.extend_from_slice(payload);
    out
}

/// itunesdb.serialize (one id draw, for the master playlist).
pub fn serialize(lib: &Library, ent: &mut Entropy) -> Vec<u8> {
    let tracks: Vec<u8> = lib.tracks.iter().flat_map(mk_mhit).collect();
    let mut mhlt = b"mhlt".to_vec();
    mhlt.extend_from_slice(&92u32.to_le_bytes());
    mhlt.extend_from_slice(&(lib.tracks.len() as u32).to_le_bytes());
    mhlt.resize(92, 0);
    mhlt.extend(tracks);
    let sd1 = mk_mhsd(1, &mhlt);
    let ids: Vec<u32> = lib.tracks.iter().map(|t| t.id).collect();
    let mpl = mk_mhyp(&lib.name, &ids, true, ent);
    let mut mhlp = b"mhlp".to_vec();
    mhlp.extend_from_slice(&92u32.to_le_bytes());
    mhlp.extend_from_slice(&1u32.to_le_bytes());
    mhlp.resize(92, 0);
    mhlp.extend(mpl);
    let sd2 = mk_mhsd(2, &mhlp);
    const HLEN: usize = 104;
    let mut h = vec![0u8; HLEN];
    h[0..4].copy_from_slice(b"mhbd");
    put(&mut h, 4, HLEN as u32);
    put(&mut h, 8, (HLEN + sd1.len() + sd2.len()) as u32);
    put(&mut h, 12, 1);
    put(&mut h, 16, 0x0b);
    put(&mut h, 20, 2);
    h[24..32].copy_from_slice(&lib.dbid.to_le_bytes());
    h.extend(sd1);
    h.extend(sd2);
    h
}
