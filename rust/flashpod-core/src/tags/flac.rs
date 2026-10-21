//! mutagen.flac.FLAC: the metadata blocks (every one parsed, as mutagen
//! does, so a broken picture or cue sheet makes the file unreadable), the
//! first Vorbis comment block as the tags, and the bitrate from the audio
//! bytes after the metadata.

use super::{err, first, BytesIo, EasyTags, Loaded, PyFile, Stream, R};

/// StrictFileObject.read: all n bytes or mutagen's error
fn strict<S: Stream + ?Sized>(f: &mut S, n: u64) -> R<Vec<u8>> {
    let d = f.read(n)?;
    if d.len() as u64 != n {
        return err(format!("file said {} bytes, read {} bytes", n, d.len()));
    }
    Ok(d)
}

fn be(d: &[u8]) -> u64 {
    d.iter().fold(0u64, |a, &b| (a << 8) | b as u64)
}

fn le32(d: &[u8]) -> u64 {
    u32::from_le_bytes([d[0], d[1], d[2], d[3]]) as u64
}

/// StreamInfo.load: (sample_rate, total_samples)
fn stream_info(data: Vec<u8>) -> R<(u64, u64)> {
    let mut b = BytesIo::new(data);
    strict(&mut b, 10)?; // block sizes, frame sizes
    let sample_first = be(&strict(&mut b, 2)?);
    let scb = be(&strict(&mut b, 1)?);
    let bps_total = be(&strict(&mut b, 5)?);
    let sample_rate = (sample_first << 4) + (scb >> 4);
    if sample_rate == 0 {
        return err("A sample rate value of 0 is invalid");
    }
    strict(&mut b, 16)?; // md5
    Ok((sample_rate, bps_total & 0xF_FFFF_FFFF))
}

/// VComment.load(errors='replace', framing=False), straight from the file:
/// FLAC distrusts the block size for comments.
fn vorbis_comment<S: Stream + ?Sized>(f: &mut S) -> R<Vec<(String, String)>> {
    let vlen = le32(&strict(f, 4)?);
    strict(f, vlen)?;
    let count = le32(&strict(f, 4)?);
    let mut out = Vec::new();
    for i in 0..count {
        let len = le32(&strict(f, 4)?);
        let s = String::from_utf8_lossy(&strict(f, len)?).into_owned();
        let (tag, value) = match s.split_once('=') {
            Some((t, v)) => (t.to_string(), v.to_string()),
            None => (format!("unknown{}", i), s.clone()),
        };
        let tag: String = tag.chars().map(|c| if c.is_ascii() { c } else { '?' }).collect();
        // is_valid_key: ' '..='}' but not '=', non-empty
        if !tag.is_empty() && tag.chars().all(|c| (' '..='}').contains(&c) && c != '=') {
            out.push((tag, value));
        }
    }
    Ok(out)
}

/// CueSheet.load from the block bytes
fn cue_sheet(data: Vec<u8>) -> R<()> {
    let mut b = BytesIo::new(data);
    let h = strict(&mut b, 396)?;
    let tracks = h[395];
    for _ in 0..tracks {
        let t = strict(&mut b, 36)?;
        for _ in 0..t[35] {
            strict(&mut b, 12)?;
        }
    }
    Ok(())
}

/// Picture.load from the block bytes
fn picture(data: Vec<u8>) -> R<()> {
    let mut b = BytesIo::new(data);
    let h = strict(&mut b, 8)?;
    strict(&mut b, be(&h[4..8]))?;
    let n = be(&strict(&mut b, 4)?);
    strict(&mut b, n)?;
    let d = strict(&mut b, 20)?;
    strict(&mut b, be(&d[16..20]))?;
    Ok(())
}

pub fn load(f: &mut PyFile) -> R<Loaded> {
    // __check_header: "fLaC", possibly after one ID3 tag
    f.seek(0);
    let h = strict(f, 4)?;
    if h != b"fLaC" {
        let mut ok = false;
        if h.starts_with(b"ID3") {
            let s = strict(f, 6)?;
            let size = 14 + s[2..].iter().fold(0u64, |a, &x| (a << 7) | (x & 0x7f) as u64);
            f.seek(size - 4);
            ok = strict(f, 4)? == b"fLaC";
        }
        if !ok {
            return err("is not a valid FLAC file");
        }
    }
    let mut info: Option<(u64, u64)> = None;
    let mut tags: Option<Vec<(String, String)>> = None;
    let (mut cuesheet, mut seektable) = (false, false);
    loop {
        let byte = strict(f, 1)?[0];
        let size = be(&strict(f, 3)?);
        let code = byte & 0x7f;
        let last = byte & 0x80 != 0;
        match code {
            4 => {
                let vc = vorbis_comment(f)?;
                if tags.is_none() {
                    tags = Some(vc);
                }
            }
            _ => {
                let data = strict(f, size)?;
                match code {
                    0 => {
                        let si = stream_info(data)?;
                        info.get_or_insert(si);
                    }
                    3 => {
                        if seektable {
                            return err("> 1 SeekTable block found");
                        }
                        seektable = true;
                    }
                    5 => {
                        cue_sheet(data)?;
                        if cuesheet {
                            return err("> 1 CueSheet block found");
                        }
                        cuesheet = true;
                    }
                    6 => picture(data)?,
                    _ => {}
                }
            }
        }
        if last {
            break;
        }
    }
    let (sample_rate, total) = match info {
        Some(i) => i,
        None => return err("Stream info block not found"),
    };
    let length = total as f64 / sample_rate as f64;
    let bitrate = if length != 0.0 {
        let start = f.tell();
        ((f.size() as i64 - start as i64) as f64 * 8.0 / length) as i64
    } else {
        0
    };
    let tags = match &tags {
        None => EasyTags::default(),
        Some(vc) => {
            let get = |k: &str| -> Option<Vec<String>> {
                let v: Vec<String> =
                    vc.iter().filter(|(t, _)| t.to_ascii_lowercase() == k).map(|(_, v)| v.clone()).collect();
                (!v.is_empty()).then_some(v)
            };
            EasyTags {
                title: first(get("title").as_ref()),
                artist: first(get("artist").as_ref()),
                album: first(get("album").as_ref()),
                genre: first(get("genre").as_ref()),
                composer: first(get("composer").as_ref()),
                tracknumber: first(get("tracknumber").as_ref()),
                date: first(get("date").as_ref()),
            }
        }
    };
    Ok(Loaded { tags, length, bitrate, sample_rate: sample_rate as i64 })
}
