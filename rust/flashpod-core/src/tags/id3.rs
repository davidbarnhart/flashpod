//! mutagen.id3.ID3.load (translate to v2.4, ID3v1 merged in) as EasyID3 sees
//! it. Only the frames behind flashpod's keys are kept (TIT2 TPE1 TALB TCON
//! TCOM TRCK TDRC, plus TYER/TDAT/TIME until they become TDRC): mutagen drops
//! a frame it cannot parse instead of raising, and no other frame shares
//! these frames' keys, so skipping the rest changes nothing. What raises is
//! the tag header, the extended header and a tag cut short by the end of
//! the file.

use super::pytext::{self, Codec};
use super::tables::{GENRES, ID3_FRAMES};
use super::{err, first, EasyTags, Stream, R};

const OURS: [&str; 10] = ["TIT2", "TPE1", "TALB", "TCON", "TCOM", "TRCK", "TDRC", "TYER", "TDAT", "TIME"];

/// ID3v2.2 names of our frames, and what they upgrade to.
fn v22_name(name: &str) -> Option<&'static str> {
    Some(match name {
        "TT2" => "TIT2",
        "TP1" => "TPE1",
        "TAL" => "TALB",
        "TCO" => "TCON",
        "TCM" => "TCOM",
        "TRK" => "TRCK",
        "TYE" => "TYER",
        "TDA" => "TDAT",
        "TIM" => "TIME",
        _ => return None,
    })
}

#[derive(Debug, Default, Clone)]
pub struct Id3Tags {
    /// HashKey -> text values (TDRC: normalized time stamps), in insertion order
    frames: Vec<(&'static str, Vec<String>)>,
    /// the tag's size including its header; 0 for an ID3v1-only tag
    pub size: u64,
}

impl Id3Tags {
    fn get(&self, k: &str) -> Option<&Vec<String>> {
        self.frames.iter().find(|(n, _)| *n == k).map(|(_, v)| v)
    }
    fn contains(&self, k: &str) -> bool {
        self.get(k).is_some()
    }
    fn pop(&mut self, k: &str) -> Vec<String> {
        match self.frames.iter().position(|(n, _)| *n == k) {
            Some(i) => self.frames.remove(i).1,
            None => Vec::new(),
        }
    }
    fn set(&mut self, k: &'static str, v: Vec<String>) {
        match self.frames.iter_mut().find(|(n, _)| *n == k) {
            Some(slot) => slot.1 = v,
            None => self.frames.push((k, v)),
        }
    }
    /// ID3Tags._add(frame, strict=False): new values merge into a text frame
    fn merge(&mut self, k: &'static str, v: Vec<String>) {
        match self.frames.iter_mut().find(|(n, _)| *n == k) {
            Some(slot) => {
                for x in v {
                    if !slot.1.contains(&x) {
                        slot.1.push(x);
                    }
                }
            }
            None => self.frames.push((k, v)),
        }
    }

    /// EasyID3's view: the first value of each of flashpod's keys.
    pub fn easy(&self) -> R<EasyTags> {
        let genre = match self.get("TCON") {
            Some(t) => Some(genres(t)?),
            None => None,
        };
        Ok(EasyTags {
            title: first(self.get("TIT2")),
            artist: first(self.get("TPE1")),
            album: first(self.get("TALB")),
            genre: first(genre.as_ref()),
            composer: first(self.get("TCOM")),
            tracknumber: first(self.get("TRCK")),
            date: first(self.get("TDRC")),
        })
    }
}

/// BitPaddedInt(bytes): 7 bits per byte, big-endian
fn bpi(b: &[u8]) -> u64 {
    b.iter().fold(0u64, |a, &x| (a << 7) | (x & 0x7f) as u64)
}

fn bpi_int(v: u32) -> u64 {
    bpi(&v.to_be_bytes())
}

fn synchsafe(b: &[u8]) -> bool {
    b.iter().all(|&x| x & 0x80 == 0)
}

/// unsynch.decode
fn unsynch(v: &[u8]) -> Option<Vec<u8>> {
    let frags: Vec<&[u8]> = v.split(|&b| b == 0xff).collect();
    if frags.len() > 1 && frags.last().unwrap().is_empty() {
        return None;
    }
    let mut out = Vec::with_capacity(v.len());
    for (i, f) in frags.iter().enumerate() {
        if i > 0 {
            if f.is_empty() || f[0] >= 0xe0 {
                return None;
            }
            out.push(0xff);
            out.extend_from_slice(if f[0] == 0 { &f[1..] } else { f });
        } else {
            out.extend_from_slice(f);
        }
    }
    Some(out)
}

/// zlib.decompress: a complete stream (trailing bytes ignored), checksum checked.
fn zlib_decompress(data: &[u8]) -> Option<Vec<u8>> {
    use flate2::{Decompress, FlushDecompress, Status};
    let mut d = Decompress::new(true);
    let mut out = Vec::new();
    let mut buf = vec![0u8; 32768];
    let mut inp = data;
    loop {
        let (in0, out0) = (d.total_in(), d.total_out());
        let st = d.decompress(inp, &mut buf, FlushDecompress::None).ok()?;
        let used = (d.total_in() - in0) as usize;
        let made = (d.total_out() - out0) as usize;
        out.extend_from_slice(&buf[..made]);
        inp = &inp[used..];
        if st == Status::StreamEnd {
            return Some(out);
        }
        if used == 0 && made == 0 {
            return None; // input ran out first: truncated
        }
    }
}

enum HeaderFail {
    NoHeader,
    Unsupported(String),
}

struct Header {
    vmaj: u8,
    size: u64,
    unsynch: bool,
    extended: bool,
    ext_len: u64,
}

/// ID3Header(fileobj)
fn header<S: Stream + ?Sized>(f: &mut S) -> R<Result<Header, HeaderFail>> {
    let d = f.read(10)?;
    if d.len() != 10 {
        return Ok(Err(HeaderFail::NoHeader));
    }
    let (vmaj, flags, size_b) = (d[3], d[5], &d[6..10]);
    let size = bpi(size_b) + 10;
    if &d[..3] != b"ID3" {
        return Ok(Err(HeaderFail::NoHeader));
    }
    if !(2..=4).contains(&vmaj) {
        return Ok(Err(HeaderFail::Unsupported(format!("ID3v2.{} not supported", vmaj))));
    }
    if !synchsafe(size_b) {
        return err("Header size not synchsafe");
    }
    if (vmaj == 4 && flags & 0x0f != 0) || (vmaj == 3 && flags & 0x1f != 0) {
        return err(format!("has invalid flags {:#04x}", flags));
    }
    let mut h = Header { vmaj, size, unsynch: flags & 0x80 != 0, extended: flags & 0x40 != 0, ext_len: 0 };
    if h.extended {
        let ext = match f.read_exact_or_none(4)? {
            Some(e) => e,
            None => return err("not enough data"),
        };
        let id = pytext::ascii_replace(&ext);
        let extsize: i64 = if ID3_FRAMES.contains(&id.as_str()) {
            // a tagger set the flag but wrote no extended header
            h.extended = false;
            f.seek_rel(-4);
            0
        } else if vmaj == 4 {
            let s = bpi(&ext) as i64 - 4;
            if !synchsafe(&ext) {
                return err("Extended header size not synchsafe");
            }
            s
        } else {
            u32::from_be_bytes([ext[0], ext[1], ext[2], ext[3]]) as i64
        };
        if extsize < 0 {
            return err("invalid extended header size");
        }
        if f.read_exact_or_none(extsize as u64)?.is_none() {
            return err("not enough data");
        }
        h.ext_len = extsize as u64;
    }
    Ok(Ok(h))
}

/// determine_bpi: whether a v2.4 tag's frame sizes are synchsafe (iTunes
/// once wrote plain ints).
fn sizes_are_plain(data: &[u8]) -> bool {
    let known = |n: &[u8]| std::str::from_utf8(n).map(|s| ID3_FRAMES.contains(&s)).unwrap_or(false) && n.is_ascii();
    let scan = |synchsafe: bool| -> (i64, i64) {
        let len = data.len() as i64;
        let mut o: i64 = 0;
        let mut count = 0;
        while o < len - 10 {
            let part = &data[o as usize..o as usize + 10];
            if part == [0u8; 10] {
                return (count, -((len - o).rem_euclid(10)));
            }
            let raw = u32::from_be_bytes([part[4], part[5], part[6], part[7]]);
            let size = if synchsafe { bpi_int(raw) } else { raw as u64 };
            o += 10 + size as i64;
            if known(&part[..4]) {
                count += 1;
            }
        }
        (count, o - len)
    };
    let (asbpi, bpioff) = scan(true);
    let (asint, intoff) = scan(false);
    asint > asbpi || (asint == asbpi && bpioff >= 1 && intoff <= 1)
}

/// EncodedTextSpec.read for one value, with mutagen's repairs of bad
/// termination and a missing BOM; None where every attempt fails.
fn text_value(data: &[u8], enc: u8, v24: bool) -> Option<(String, Vec<u8>)> {
    let codec = [Codec::Latin1, Codec::Utf16, Codec::Utf16Be, Codec::Utf8][enc as usize];
    let mut tries: Vec<Vec<u8>> = vec![data.to_vec()];
    let nul = |d: &[u8]| [d, &[0u8][..]].concat();
    if enc == 2 {
        tries.push(nul(data));
    } else if enc == 1 {
        tries.push(nul(data));
        tries.push([&[0xff, 0xfe][..], data].concat());
        tries.push(nul(&[&[0xff, 0xfe][..], data].concat()));
    }
    for t in tries {
        if let Ok((v, mut rest)) = pytext::decode_terminated(&t, codec) {
            if !v24 && rest.iter().all(|&b| b == 0) {
                rest.clear();
            }
            return Some((v, rest));
        }
    }
    None
}

/// A text frame's values (Frame._readData); None: junk, dropped.
fn text_frame(data: &[u8], v24: bool, stamps: bool) -> Option<Vec<String>> {
    let (&enc, data) = data.split_first()?;
    if enc > 3 || data.is_empty() {
        return None;
    }
    let mut out = Vec::new();
    let mut cur = data.to_vec();
    while !cur.is_empty() {
        let (v, rest) = text_value(&cur, enc, v24)?;
        out.push(if stamps { timestamp(&v) } else { v });
        cur = rest;
    }
    Some(out)
}

/// Frame._fromData's flag handling; None: junk or unsupported, dropped.
fn frame_payload(vmaj: u8, tag_unsynch: bool, flags: u16, data: &[u8]) -> Option<Vec<u8>> {
    let mut data = data.to_vec();
    if vmaj == 4 {
        let mut datalen = Vec::new();
        if flags & (0x0008 | 0x0001) != 0 {
            let k = data.len().min(4);
            datalen = data[..k].to_vec();
            data = data[k..].to_vec();
        }
        if flags & 0x0002 != 0 || tag_unsynch {
            if let Some(d) = unsynch(&data) {
                data = d;
            }
        }
        if flags & 0x0004 != 0 {
            return None; // encrypted
        }
        if flags & 0x0008 != 0 {
            data = match zlib_decompress(&data) {
                Some(d) => d,
                None => zlib_decompress(&[datalen, data].concat())?,
            };
        }
    } else if vmaj == 3 {
        if flags & 0x0080 != 0 {
            if data.len() < 4 {
                return None;
            }
            data = data[4..].to_vec();
        }
        if flags & 0x0040 != 0 {
            return None;
        }
        if flags & 0x0080 != 0 {
            data = zlib_decompress(&data)?;
        }
    }
    Some(data)
}

fn our(name: &str) -> Option<&'static str> {
    OURS.iter().copied().find(|&o| o == name)
}

fn read_frames(tags: &mut Id3Tags, h: &Header, mut data: Vec<u8>) {
    if h.vmaj < 4 && h.unsynch {
        if let Some(d) = unsynch(&data) {
            data = d;
        }
    }
    let v24 = h.vmaj >= 4;
    let mut d = &data[..];
    if h.vmaj >= 3 {
        let plain = h.vmaj == 3 || sizes_are_plain(d);
        while d.len() >= 10 {
            let (hdr, rest) = d.split_at(10);
            let name = &hdr[..4];
            if name.iter().all(|&b| b == 0) {
                break;
            }
            let raw = u32::from_be_bytes([hdr[4], hdr[5], hdr[6], hdr[7]]);
            let size = if plain { raw as u64 } else { bpi_int(raw) };
            let flags = u16::from_be_bytes([hdr[8], hdr[9]]);
            let take = (size.min(rest.len() as u64)) as usize;
            let framedata = &rest[..take];
            d = &rest[take..];
            if size == 0 || !name.is_ascii() {
                continue;
            }
            let name = std::str::from_utf8(name).unwrap();
            let id = if name.ends_with('\0') { v22_name(&name[..3]) } else { our(name) };
            if let Some(id) = id {
                if let Some(p) = frame_payload(h.vmaj, h.unsynch, flags, framedata) {
                    if let Some(v) = text_frame(&p, v24, id == "TDRC") {
                        tags.merge(id, v);
                    }
                }
            }
        }
    } else {
        while d.len() >= 6 {
            let (hdr, rest) = d.split_at(6);
            let name = &hdr[..3];
            if name.iter().all(|&b| b == 0) {
                break;
            }
            let size = u32::from_be_bytes([0, hdr[3], hdr[4], hdr[5]]) as usize;
            let take = size.min(rest.len());
            let framedata = &rest[..take];
            d = &rest[take..];
            if size == 0 || !name.is_ascii() {
                continue;
            }
            if let Some(id) = v22_name(std::str::from_utf8(name).unwrap()) {
                if let Some(v) = text_frame(framedata, false, false) {
                    tags.merge(id, v);
                }
            }
        }
    }
}

/// find_id3v1 + ParseID3v1: the frames of a trailing ID3v1 tag, or None.
fn find_id3v1<S: Stream + ?Sized>(f: &mut S, v2_version: u8) -> R<Option<Vec<(&'static str, Vec<String>)>>> {
    let old = f.tell();
    let size = f.len();
    f.seek(size.saturating_sub(131));
    let data = f.read(131)?;
    f.seek(old);
    let find = |n: &[u8]| data.windows(n.len()).position(|w| w == n);
    let idx = match find(b"TAG") {
        Some(i) => i,
        None => return Ok(None),
    };
    if let Some(a) = find(b"APETAGEX") {
        // b"APETAGEX".index(b"TAG") == 3: this TAG is the APE footer's
        if idx == a + 3 {
            return Ok(None);
        }
    }
    let d = &data[idx..];
    if d.len() > 128 || d.len() < 124 {
        return Ok(None);
    }
    let yl = d.len() - 124;
    let fix = |b: &[u8]| {
        let b = b.split(|&c| c == 0).next().unwrap();
        pytext::latin1(pytext::bytes_strip(b))
    };
    let title = fix(&d[3..33]);
    let artist = fix(&d[33..63]);
    let album = fix(&d[63..93]);
    let year = fix(&d[93..93 + yl]);
    let comment = &d[93 + yl..123 + yl];
    let genre = d[123 + yl];
    let track = if comment[28] == 0 { comment[29] } else { 0 };
    let mut frames: Vec<(&'static str, Vec<String>)> = Vec::new();
    if !title.is_empty() {
        frames.push(("TIT2", vec![title]));
    }
    if !artist.is_empty() {
        frames.push(("TPE1", vec![artist]));
    }
    if !album.is_empty() {
        frames.push(("TALB", vec![album]));
    }
    if !year.is_empty() {
        if v2_version == 3 {
            frames.push(("TYER", vec![year]));
        } else {
            // TDRC(text=year): a str splits on ','
            frames.push(("TDRC", year.split(',').map(timestamp).collect()));
        }
    }
    if track != 0 {
        frames.push(("TRCK", vec![track.to_string()]));
    }
    if genre != 255 {
        frames.push(("TCON", vec![genre.to_string()]));
    }
    Ok(Some(frames))
}

/// ID3TimeStamp(text).text: the parts re-joined as YYYY-MM-DD HH:MM:SS.
pub fn timestamp(text: &str) -> String {
    let s = format!("{}:::::", text);
    let mut parts: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if "-T:/.".contains(c) {
            parts.push(std::mem::take(&mut cur));
        } else if pytext::is_space(c) {
            while chars.peek().map_or(false, |&n| pytext::is_space(n)) {
                chars.next();
            }
            parts.push(std::mem::take(&mut cur));
        } else {
            cur.push(c);
        }
    }
    parts.push(cur);
    let seps = ['-', '-', ' ', ':', ':', 'x'];
    let mut out = String::new();
    for (i, p) in parts.iter().take(6).enumerate() {
        match pytext::py_int(p) {
            Some(v) => {
                out.push_str(&pytext::zero_pad(&v, if i == 0 { 4 } else { 2 }));
                out.push(seps[i]);
            }
            None => break,
        }
    }
    out.pop();
    out
}

/// TCON.genres
pub fn genres(text: &[String]) -> R<Vec<String>> {
    let gname = |i: usize| GENRES.get(i).copied().unwrap_or("Unknown").to_string();
    let mut out: Vec<String> = Vec::new();
    for value in text {
        if pytext::is_decimal_str(value) {
            if pytext::int_less_than(value, 256).map_err(|_| too_long())? {
                let v: usize = pytext::py_int(value).unwrap().parse().unwrap();
                out.push(gname(v));
                continue;
            }
        }
        if value == "CR" {
            out.push("Cover".into());
        } else if value == "RX" {
            out.push("Remix".into());
        } else if !value.is_empty() {
            // ((?:\((?P<id>[0-9]+|RX|CR)\))*)(?P<str>.+)?
            let b = value.as_bytes();
            let mut p = 0;
            let mut ids: Vec<&str> = Vec::new();
            loop {
                if b.get(p) != Some(&b'(') {
                    break;
                }
                let q = p + 1;
                let digits = b[q..].iter().take_while(|c| c.is_ascii_digit()).count();
                let end = if digits > 0 && b.get(q + digits) == Some(&b')') {
                    q + digits
                } else if b[q..].starts_with(b"RX)") || b[q..].starts_with(b"CR)") {
                    q + 2
                } else {
                    break;
                };
                ids.push(&value[q..end]);
                p = end + 1;
            }
            let rest = &value[p..];
            let name: String = rest.chars().take_while(|&c| c != '\n').collect();
            let mut new: Vec<String> = Vec::new();
            for gid in ids {
                if gid.bytes().all(|c| c.is_ascii_digit()) {
                    if pytext::int_less_than(gid, GENRES.len() as u64).map_err(|_| too_long())? {
                        new.push(gname(pytext::py_int(gid).unwrap().parse().unwrap()));
                    } else {
                        new.push("Unknown".into());
                    }
                } else if gid == "CR" {
                    new.push("Cover".into());
                } else {
                    new.push("Remix".into());
                }
            }
            if !name.is_empty() {
                let name = match name.strip_prefix("((") {
                    Some(r) => format!("({}", r),
                    None => name,
                };
                if !new.contains(&name) {
                    new.push(name);
                }
            }
            out.extend(new);
        }
    }
    Ok(out)
}

fn too_long() -> super::TagError {
    super::TagError("Exceeds the limit (4300 digits) for integer string conversion".into())
}

/// ID3.load from the current position. Ok(None): ID3NoHeaderError.
pub fn load<S: Stream + ?Sized>(f: &mut S) -> R<Option<Id3Tags>> {
    let mut tags = Id3Tags::default();
    match header(f)? {
        Err(fail) => {
            let v1 = find_id3v1(f, 4)?;
            match (v1, fail) {
                (None, HeaderFail::NoHeader) => return Ok(None),
                (None, HeaderFail::Unsupported(m)) => return err(m),
                (Some(frames), _) => {
                    for (k, v) in frames {
                        if !tags.contains(k) {
                            tags.set(k, v);
                        }
                    }
                }
            }
        }
        Ok(h) => {
            let mut size = h.size as i64 - 10;
            if h.extended {
                size -= 4 + h.ext_len as i64;
            }
            if size < 0 {
                return err("size must not be negative");
            }
            let data = match f.read_exact_or_none(size as u64)? {
                Some(d) => d,
                None => return err("not enough data"),
            };
            tags.size = h.size;
            read_frames(&mut tags, &h, data);
            if let Some(frames) = find_id3v1(f, if h.vmaj == 4 { 4 } else { 3 })? {
                for (k, v) in frames {
                    if !tags.contains(k) {
                        tags.set(k, v);
                    }
                }
            }
        }
    }
    update_to_v24(&mut tags)?;
    Ok(Some(tags))
}

fn update_to_v24(t: &mut Id3Tags) -> R<()> {
    if let Some(v) = t.get("TCON").cloned() {
        let g = genres(&v)?;
        t.set("TCON", g);
    }
    let (tyer, tdat, time) = (t.pop("TYER"), t.pop("TDAT"), t.pop("TIME"));
    let n = tyer.len().max(tdat.len()).max(time.len());
    let at = |v: &Vec<String>, i: usize| v.get(i).cloned().unwrap_or_default();
    let digits = |s: &str, n: usize| s.len() == n && s.bytes().all(|c| c.is_ascii_digit());
    let mut stamps = Vec::new();
    for i in 0..n {
        let (y, d, tm) = (at(&tyer, i), at(&tdat, i), at(&time, i));
        let mut ts = String::new();
        // ([0-9]{4})(-[0-9]{2}-[0-9]{2})?\Z
        let yb = y.as_bytes();
        let dig = |r: std::ops::Range<usize>| yb[r].iter().all(|c| c.is_ascii_digit());
        let ym = if digits(&y, 4) {
            Some((y.clone(), None))
        } else if yb.len() == 10 && dig(0..4) && yb[4] == b'-' && dig(5..7) && yb[7] == b'-' && dig(8..10) {
            Some((y[..4].to_string(), Some(y[4..].to_string())))
        } else {
            None
        };
        if let Some((year, mut md)) = ym {
            ts.push_str(&year);
            if digits(&d, 4) {
                md = Some(format!("-{}-{}", &d[2..4], &d[..2]));
            }
            if let Some(md) = md {
                ts.push_str(&md);
                if digits(&tm, 4) {
                    ts.push_str(&format!("T{}:{}:00", &tm[..2], &tm[2..4]));
                }
            }
        }
        if !ts.is_empty() {
            stamps.push(ts);
        }
    }
    if !stamps.is_empty() && !t.contains("TDRC") {
        t.set("TDRC", stamps.iter().map(|s| timestamp(s)).collect());
    }
    Ok(())
}
