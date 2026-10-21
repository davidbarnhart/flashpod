//! mutagen.mp3.EasyMP3: the ID3 tag (id3.rs) and MPEGInfo, the length and
//! bitrate from the first frames: a Xing/Info or VBRI header when present,
//! else the file size over the first frame's bitrate.

use super::{err, id3, EasyTags, Loaded, PyFile, Stream, R};

/// round-half-even of an exactly represented float (mutagen's intround)
fn intround(v: f64) -> i64 {
    v.round_ties_even() as i64
}

struct Frame {
    offset: u64,
    bitrate: i64,
    sample_rate: i64,
    /// None: no length from a VBR header (mutagen leaves the attribute unset)
    length: Option<f64>,
    sketchy: bool,
}

const BITRATES: [[i64; 15]; 5] = [
    [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448], // 1, layer 1
    [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],    // 1, layer 2
    [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],     // 1, layer 3
    [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],    // 2/2.5, layer 1
    [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],         // 2/2.5, layers 2-3
];

/// MPEGFrame(fileobj) at the current position; Ok(None): HeaderNotFoundError.
fn frame(f: &mut PyFile) -> R<Option<Frame>> {
    let offset = f.tell();
    let h = f.read(4)?;
    if h.len() != 4 {
        return Ok(None);
    }
    let w = u32::from_be_bytes([h[0], h[1], h[2], h[3]]);
    if w >> 21 != 0x7ff {
        return Ok(None);
    }
    let version = (w >> 19) & 3;
    let layer = (w >> 17) & 3;
    let bitrate_i = ((w >> 12) & 0xf) as usize;
    let sr_i = ((w >> 10) & 3) as usize;
    let padding = ((w >> 9) & 1) as i64;
    let mode = (w >> 6) & 3;
    if version == 1 || layer == 0 || sr_i == 3 || bitrate_i == 0xf || bitrate_i == 0 {
        return Ok(None);
    }
    // version: 0 = 2.5, 2 = 2, 3 = 1
    let v1 = version == 3;
    let layer = 4 - layer as i64;
    let table = if v1 { (layer - 1) as usize } else if layer == 1 { 3 } else { 4 };
    let bitrate = BITRATES[table][bitrate_i] * 1000;
    let sample_rate = match version {
        3 => [44100, 48000, 32000][sr_i],
        2 => [22050, 24000, 16000][sr_i],
        _ => [11025, 12000, 8000][sr_i],
    };
    let (frame_size, slot) = if layer == 1 {
        (384, 4)
    } else if !v1 && layer == 3 {
        (576, 1)
    } else {
        (1152, 1)
    };
    let frame_length = ((frame_size / 8 * bitrate) / sample_rate + padding) * slot;
    let mut fr = Frame { offset, bitrate, sample_rate, length: None, sketchy: true };
    if layer == 3 {
        vbr_header(f, &mut fr, v1, mode, frame_size, frame_length)?;
    }
    f.seek(offset + frame_length as u64);
    Ok(Some(fr))
}

/// MPEGFrame._parse_vbr_header: Xing/Info (with LAME's delay and padding),
/// else VBRI.
fn vbr_header(f: &mut PyFile, fr: &mut Frame, v1: bool, mode: u32, frame_size: i64, frame_length: i64) -> R<()> {
    let xing_off = match (v1, mode != 3) {
        (true, true) => 36,
        (true, false) => 21,
        (false, true) => 21,
        (false, false) => 13,
    };
    f.seek(fr.offset + xing_off);
    if let Some(x) = xing(f)? {
        fr.sketchy = false;
        if let Some(frames) = x.frames {
            let mut samples = frame_size * frames;
            if let Some(bytes) = x.bytes {
                if samples > 0 {
                    let audio = (bytes - frame_length).max(0);
                    fr.bitrate = intround((audio * 8 * fr.sample_rate) as f64 / samples as f64);
                }
            }
            if let Some((delay, pad)) = x.lame {
                samples -= delay + pad;
            }
            if samples < 0 {
                samples = 0;
            }
            fr.length = Some(samples as f64 / fr.sample_rate as f64);
        }
        return Ok(());
    }
    f.seek(fr.offset + 36);
    if let Some((bytes, frames)) = vbri(f)? {
        fr.sketchy = false;
        let length = (frame_size * frames) as f64 / fr.sample_rate as f64;
        fr.length = Some(length);
        if length != 0.0 {
            fr.bitrate = ((bytes * 8) as f64 / length) as i64;
        }
    }
    Ok(())
}

struct Xing {
    frames: Option<i64>,
    bytes: Option<i64>,
    /// LAME header: (encoder delay, end padding)
    lame: Option<(i64, i64)>,
}

fn be32(d: &[u8]) -> i64 {
    u32::from_be_bytes([d[0], d[1], d[2], d[3]]) as i64
}

/// XingHeader(fileobj); Ok(None): XingHeaderError.
fn xing(f: &mut PyFile) -> R<Option<Xing>> {
    let d = f.read(8)?;
    if d.len() != 8 || (&d[..4] != b"Xing" && &d[..4] != b"Info") {
        return Ok(None);
    }
    let flags = be32(&d[4..]);
    let mut x = Xing { frames: None, bytes: None, lame: None };
    let field = |f: &mut PyFile, n: u64| -> R<Option<Vec<u8>>> { f.read_exact_or_none(n) };
    if flags & 1 != 0 {
        match field(f, 4)? {
            Some(v) => x.frames = Some(be32(&v)),
            None => return Ok(None),
        }
    }
    if flags & 2 != 0 {
        match field(f, 4)? {
            Some(v) => x.bytes = Some(be32(&v)),
            None => return Ok(None),
        }
    }
    if flags & 4 != 0 && field(f, 100)?.is_none() {
        return Ok(None);
    }
    let mut vbr_scale_ok = true;
    if flags & 8 != 0 {
        vbr_scale_ok = field(f, 4)?.is_some();
        if !vbr_scale_ok {
            return Ok(None);
        }
    }
    let _ = vbr_scale_ok;
    if lame_version(f)? {
        x.lame = lame_header(f)?;
    }
    Ok(Some(x))
}

/// LAMEHeader.parse_version: true when an extended header follows (and the
/// file is then 9 bytes into the version string).
fn lame_version(f: &mut PyFile) -> R<bool> {
    let data = f.read(20)?;
    if data.len() != 20 || !(data.starts_with(b"LAME") || data.starts_with(b"L3.99")) {
        return Ok(false);
    }
    let strip = data.iter().position(|c| !b"EMAL".contains(c)).unwrap_or(data.len());
    let data = &data[strip..];
    if data.is_empty() {
        return Ok(false);
    }
    let major = data[0];
    let rest = &data[1..];
    let dots = rest.iter().position(|&c| c != b'.').unwrap_or(rest.len());
    let rest = &rest[dots..];
    let nminor = rest.iter().take_while(|c| c.is_ascii_digit()).count();
    if !major.is_ascii_digit() || nminor == 0 {
        return Ok(false);
    }
    let major = (major - b'0') as u64;
    let minor: u64 = match std::str::from_utf8(&rest[..nminor]).ok().and_then(|s| s.parse().ok()) {
        Some(m) => m,
        None => u64::MAX, // a very long run of digits still compares as >= 90
    };
    let after = &rest[nminor..];
    let ver = (major, minor);
    let n = after.len();
    let paren = n >= 11 && after[n - 11] == b'(';
    if ver < (3, 90) || (ver == (3, 90) && paren) {
        return Ok(false);
    }
    if n < 11 {
        return Ok(false);
    }
    f.seek_rel(-11);
    Ok(true)
}

/// LAMEHeader(xing, fileobj): the delay and padding, or None (LAMEError).
fn lame_header(f: &mut PyFile) -> R<Option<(i64, i64)>> {
    let p = f.read(27)?;
    if p.len() != 27 || p[0] >> 4 != 0 {
        return Ok(None);
    }
    // bytes: 0 revision/vbr method, 1 lowpass, 2-5 peak, 6-7 track gain,
    // 8-9 album gain, 10 flags/ath, 11 bitrate, 12-14 delay 12 + padding 12
    let delay = ((p[12] as i64) << 4) | (p[13] as i64 >> 4);
    let pad = (((p[13] & 0xf) as i64) << 8) | p[14] as i64;
    Ok(Some((delay, pad)))
}

/// VBRIHeader(fileobj): (bytes, frames), or None.
fn vbri(f: &mut PyFile) -> R<Option<(i64, i64)>> {
    let d = f.read(26)?;
    if d.len() != 26 || !d.starts_with(b"VBRI") {
        return Ok(None);
    }
    if u16::from_be_bytes([d[4], d[5]]) != 1 {
        return Ok(None);
    }
    let bytes = be32(&d[10..14]);
    let frames = be32(&d[14..18]);
    let num = u16::from_be_bytes([d[18], d[19]]) as u64;
    let esize = u16::from_be_bytes([d[22], d[23]]) as u64;
    if f.read_exact_or_none(esize * num)?.is_none() {
        return Ok(None);
    }
    if esize != 2 && esize != 4 {
        return Ok(None);
    }
    Ok(Some((bytes, frames)))
}

/// skip_id3: past any number of stacked ID3v2 tags.
fn skip_id3(f: &mut PyFile) -> R<()> {
    loop {
        let d = f.read(10)?;
        let (id, size) = if d.len() == 10 {
            (&d[..3], d[6..10].iter().fold(0u64, |a, &x| (a << 7) | (x & 0x7f) as u64))
        } else {
            (&b""[..], 0)
        };
        if id == b"ID3" && size > 0 {
            f.seek_rel(size as i64);
        } else {
            f.seek_rel(-(d.len() as i64));
            return Ok(());
        }
    }
}

/// MPEGInfo(fileobj, offset): (length, bitrate, sample_rate)
fn mpeg_info(f: &mut PyFile, offset: Option<u64>) -> R<(f64, i64, i64)> {
    f.seek(offset.unwrap_or(0));
    skip_id3(f)?;
    let start = f.tell();
    let window = f.read(1024 * 1024)?;
    let mut max_syncs = 1500;
    let mut first: Option<Frame> = None;
    let mut frames: Vec<Frame> = Vec::new();
    for i in 0..window.len().saturating_sub(1) {
        if window[i] != 0xff || window[i + 1] & 0xe0 != 0xe0 {
            continue;
        }
        max_syncs -= 1;
        if max_syncs <= 0 {
            break;
        }
        f.seek(start + i as u64);
        for _ in 0..4 {
            match frame(f)? {
                None => break,
                Some(fr) => {
                    let done = !fr.sketchy;
                    frames.push(fr);
                    if done {
                        break;
                    }
                }
            }
        }
        if frames.len() >= 2 && first.is_none() {
            first = Some(clone_frame(&frames[0]));
        }
        if frames.last().map_or(false, |fr| !fr.sketchy) {
            first = frames.pop();
            break;
        }
        if frames.len() >= 4 {
            first = Some(frames.swap_remove(0));
            break;
        }
        frames.clear();
    }
    let fr = match first {
        Some(fr) => fr,
        None => return err("can't sync to MPEG frame"),
    };
    let length = match fr.length {
        Some(l) => l,
        None => {
            let content = f.size() as i64 - fr.offset as i64;
            (8 * content) as f64 / fr.bitrate as f64
        }
    };
    Ok((length, fr.bitrate, fr.sample_rate))
}

fn clone_frame(fr: &Frame) -> Frame {
    Frame { offset: fr.offset, bitrate: fr.bitrate, sample_rate: fr.sample_rate, length: fr.length, sketchy: fr.sketchy }
}

fn easy(tags: &Option<id3::Id3Tags>) -> R<EasyTags> {
    match tags {
        Some(t) => t.easy(),
        None => Ok(EasyTags::default()),
    }
}

/// ID3FileType.load with EasyID3, then MPEGInfo
pub fn load_mp3(f: &mut PyFile) -> R<Loaded> {
    f.seek(0);
    let tags = id3::load(f)?;
    let offset = tags.as_ref().map(|t| t.size);
    let (length, bitrate, sample_rate) = mpeg_info(f, offset)?;
    Ok(Loaded { tags: easy(&tags)?, length, bitrate, sample_rate })
}

/// EasyID3FileType: the tag only; its info has length 0 and no bitrate.
pub fn load_id3_file(f: &mut PyFile) -> R<Loaded> {
    f.seek(0);
    let tags = id3::load(f)?;
    Ok(Loaded { tags: easy(&tags)?, length: 0.0, bitrate: 0, sample_rate: 0 })
}
