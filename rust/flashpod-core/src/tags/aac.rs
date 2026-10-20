//! mutagen.aac.AAC: an ADTS stream (its first frames give the rate and the
//! bitrate, the file size the length) or an ADIF header. No tags.
//!
//! mutagen's ADTS bitrate is a float (Python's flashpod then fails to write
//! the database); it is always a whole number, so it fits the i64 here.

use super::bits::{BitErr, BitReader};
use super::{err, EasyTags, Loaded, PyFile, Stream, R};

const FREQS: [i64; 13] = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];

/// float // float, as CPython's float_divmod computes it
fn py_floordiv(vx: f64, wx: f64) -> f64 {
    let mut m = vx % wx;
    let mut div = (vx - m) / wx;
    if m != 0.0 {
        if (wx < 0.0) != (m < 0.0) {
            m += wx;
            div -= 1.0;
        }
    }
    let _ = m;
    if div != 0.0 {
        let mut fl = div.floor();
        if div - fl > 0.5 {
            fl += 1.0;
        }
        fl
    } else {
        (0.0f64).copysign(vx / wx)
    }
}

/// ProgramConfigElement(r): (sampling_frequency_index, channels)
pub fn pce<S: Stream + ?Sized>(r: &mut BitReader<'_, S>) -> Result<(u64, u64), BitErr> {
    r.bits(4)?;
    r.bits(2)?;
    let sfi = r.bits(4)?;
    let front = r.bits(4)?;
    let side = r.bits(4)?;
    let back = r.bits(4)?;
    let lfe = r.bits(2)?;
    let assoc = r.bits(3)?;
    let cc = r.bits(4)?;
    // mono mixdown, stereo mixdown, matrix mixdown index
    for n in [4, 4, 3] {
        if r.bits(1)? == 1 {
            r.skip(n)?;
        }
    }
    let mut channels = 0;
    for _ in 0..front + side + back {
        channels += 1;
        if r.bits(1)? != 0 {
            channels += 1;
        }
        r.skip(4)?;
    }
    channels += lfe;
    r.skip(4 * lfe)?;
    r.skip(4 * assoc)?;
    r.skip(5 * cc)?;
    r.align();
    let comment = r.bits(8)?;
    r.skip(8 * comment)?;
    Ok((sfi, channels))
}

struct Adts {
    fixed: Option<[u64; 9]>,
    parsed: u32,
    samples: i64,
    payload: f64,
    start: f64,
    last: f64,
    offset: i64,
}

impl Adts {
    fn frequency(&self) -> i64 {
        FREQS.get(self.fixed.unwrap()[4] as usize).copied().unwrap_or(0)
    }
}

/// _ADTSStream.sync: find the next 0xFFF sync
fn sync(r: &mut BitReader<'_, PyFile>, max_bytes: i64) -> R<bool> {
    let mut max = max_bytes.max(2);
    r.align();
    while max > 0 {
        let b = match r.bytes(1) {
            Ok(b) => b,
            Err(BitErr::Short) => return Ok(false),
            Err(BitErr::Io(e)) => return Err(e),
        };
        if b[0] == 0xff {
            match r.bits(4) {
                Ok(0xf) => return Ok(true),
                Ok(_) => {}
                Err(BitErr::Short) => return Ok(false),
                Err(BitErr::Io(e)) => return Err(e),
            }
            r.align();
            max -= 2;
        } else {
            max -= 1;
        }
    }
    Ok(false)
}

/// _ADTSStream._parse_frame; Ok(false) when invalid or cut short
fn parse_frame(s: &mut Adts, r: &mut BitReader<'_, PyFile>) -> R<bool> {
    let res = (|| -> Result<bool, BitErr> {
        let start = r.get_position() - 12;
        let mut key = [0u64; 9];
        for (i, n) in [1u32, 2, 1, 2, 4, 1, 3, 1, 1].iter().enumerate() {
            key[i] = r.bits(*n)?;
        }
        match s.fixed {
            None => s.fixed = Some(key),
            Some(k) if k != key => return Ok(false),
            _ => {}
        }
        let protection_absent = key[2];
        r.skip(2)?;
        let frame_length = r.bits(13)? as i64;
        r.skip(11)?;
        let nordbif = r.bits(2)? as i64;
        let mut crc = 0i64;
        if protection_absent == 0 {
            crc += (nordbif + 1) * 16;
            if nordbif != 0 {
                crc *= 2;
            }
        }
        let left = frame_length * 8 - (r.get_position() - start);
        if left < 0 {
            return Ok(false);
        }
        r.skip(left as u64)?;
        s.payload += (left - crc) as f64 / 8.0;
        s.samples += (nordbif + 1) * 1024;
        s.last = r.get_position() as f64 / 8.0;
        s.parsed += 1;
        Ok(true)
    })();
    match res {
        Ok(v) => Ok(v),
        Err(BitErr::Short) => Ok(false),
        Err(BitErr::Io(e)) => Err(e),
    }
}

fn adts(f: &mut PyFile, start_offset: u64) -> R<Loaded> {
    let mut offset = start_offset as i64;
    let mut found: Option<Adts> = None;
    for _ in 0..10 {
        f.seek(offset.max(0) as u64);
        let mut r = BitReader::new(f);
        let mut s = Adts { fixed: None, parsed: 0, samples: 0, payload: 0.0, start: 0.0, last: 0.0, offset: -1 };
        s.start = r.get_position() as f64 / 8.0;
        s.last = s.start;
        if !sync(&mut r, 512)? {
            return err("sync not found");
        }
        s.offset = (r.get_position() - 12).div_euclid(8);
        offset += s.offset + 1;
        for _ in 0..100 {
            if !parse_frame(&mut s, &mut r)? {
                break;
            }
            if !sync(&mut r, 10)? {
                break;
            }
        }
        let enough = s.parsed >= 3;
        found = Some(s);
        if enough {
            break;
        }
    }
    let s = found.unwrap();
    if s.parsed < 3 {
        return err(format!("no valid stream found (only {} frames)", s.parsed));
    }
    let freq = s.frequency();
    // (8 * payload * frequency) // samples, in floats
    let bitrate = if s.samples == 0 { 0 } else { py_floordiv(8.0 * s.payload * freq as f64, s.samples as f64) as i64 };
    let stream_size = f.size() as i64 - (offset + s.offset);
    let size = s.last - s.start;
    let length =
        if freq != 0 { (s.samples as i128 * stream_size as i128) as f64 / (size * freq as f64) } else { 0.0 };
    Ok(Loaded { tags: EasyTags::default(), length, bitrate, sample_rate: freq })
}

fn adif(f: &mut PyFile) -> R<Loaded> {
    let sample_rate;
    let bitrate;
    {
        let mut r = BitReader::new(f);
        let res = (|| -> Result<(i64, i64), BitErr> {
            if r.bits(1)? != 0 {
                r.skip(72)?;
            }
            r.skip(2)?;
            let bitstream_type = r.bits(1)?;
            let br = r.bits(23)? as i64;
            let npce = r.bits(4)?;
            if bitstream_type == 0 {
                r.skip(20)?;
            }
            let (sfi, _) = pce(&mut r)?;
            let sr = FREQS.get(sfi as usize).copied().unwrap_or(0);
            for _ in 0..npce {
                pce(&mut r)?;
            }
            r.align();
            Ok((br, sr))
        })();
        match res {
            Ok((br, sr)) => {
                bitrate = br;
                sample_rate = sr;
            }
            Err(BitErr::Short) => return err("not enough data"),
            Err(BitErr::Io(e)) => return Err(e),
        }
    }
    let start = f.tell();
    let length = f.size() as i64 - start as i64;
    let length = if bitrate != 0 { 8.0 * length as f64 / bitrate as f64 } else { 0.0 };
    Ok(Loaded { tags: EasyTags::default(), length, bitrate, sample_rate })
}

pub fn load(f: &mut PyFile) -> R<Loaded> {
    f.seek(0);
    let header = f.read(10)?;
    let mut start = 0u64;
    if header.starts_with(b"ID3") {
        let tail = header.get(6..).unwrap_or(&[]);
        start = tail.iter().fold(0u64, |a, &x| (a << 7) | (x & 0x7f) as u64) + 10;
    }
    f.seek(start);
    if f.read(4)? == b"ADIF" {
        adif(f)
    } else {
        adts(f, start)
    }
}
