//! mutagen.aiff.AIFF and mutagen.wave.WAVE over the IFF chunk reader. Their
//! tags are a plain ID3 tag in an 'ID3 '/'id3 ' chunk, which flashpod reads
//! with EasyID3's getters (tracks.easy_tags, since 0.5.2).

use super::{err, id3, pytext, EasyTags, Loaded, PyFile, Stream, TagError, R};

enum ChunkErr {
    /// InvalidChunk (EmptyChunk included): ends a chunk list
    Invalid,
    /// anything else mutagen raises
    Fatal(TagError),
}

impl From<TagError> for ChunkErr {
    fn from(e: TagError) -> Self {
        ChunkErr::Fatal(e)
    }
}

#[derive(Clone)]
struct Chunk {
    id: String,
    data_size: u64,
    data_offset: u64,
    offset: u64,
    size: u64,
    container: bool,
    name: String,
}

/// IffChunk.parse for AIFF (big-endian, FORM containers) or RIFF
/// (little-endian, RIFF/LIST containers)
fn parse(f: &mut PyFile, riff: bool) -> Result<Chunk, ChunkErr> {
    let h = f.read(8)?;
    if h.len() < 8 {
        return Err(ChunkErr::Invalid);
    }
    let raw = [h[4], h[5], h[6], h[7]];
    let data_size = if riff { u32::from_le_bytes(raw) } else { u32::from_be_bytes(raw) } as u64;
    if !h[..4].is_ascii() {
        return Err(ChunkErr::Invalid);
    }
    let id: String = std::str::from_utf8(&h[..4]).unwrap().trim_end_matches(pytext::is_space).to_string();
    let valid = !id.is_empty() && id.len() <= 4 && id.chars().all(|c| (' '..='~').contains(&c));
    if !valid {
        return Err(ChunkErr::Invalid);
    }
    let data_offset = f.tell();
    let mut c = Chunk {
        container: if riff { id == "RIFF" || id == "LIST" } else { id == "FORM" },
        id,
        data_size,
        data_offset,
        offset: data_offset - 8,
        size: 8 + data_size + data_size % 2,
        name: String::new(),
    };
    if c.container {
        if c.data_size < 4 {
            return Err(ChunkErr::Invalid);
        }
        let n = f.read(4)?;
        if !n.is_ascii() {
            return Err(ChunkErr::Fatal(TagError("'ascii' codec can't decode the chunk name".into())));
        }
        c.name = String::from_utf8(n).unwrap();
    }
    Ok(c)
}

/// IffContainerChunkMixin.subchunks
fn subchunks(f: &mut PyFile, root: &Chunk, riff: bool) -> R<Vec<Chunk>> {
    let mut out = Vec::new();
    let mut next = root.data_offset + 4;
    while next < root.offset + root.size {
        f.seek(next);
        match parse(f, riff) {
            Ok(c) => {
                next = c.offset + c.size;
                out.push(c);
            }
            Err(ChunkErr::Invalid) => break,
            Err(ChunkErr::Fatal(e)) => return Err(e),
        }
    }
    Ok(out)
}

/// The root chunk; Err(Invalid) where mutagen raises InvalidChunk
fn root(f: &mut PyFile, riff: bool) -> Result<Chunk, ChunkErr> {
    f.seek(0);
    let r = parse(f, riff)?;
    if r.id != if riff { "RIFF" } else { "FORM" } {
        return Err(ChunkErr::Invalid);
    }
    Ok(r)
}

fn fatal_or(e: ChunkErr, msg: &str) -> TagError {
    match e {
        ChunkErr::Fatal(t) => t,
        ChunkErr::Invalid => TagError(msg.into()),
    }
}

/// The ID3 chunk's tag at `at` (None: no chunk, ID3NoHeaderError).
fn id3_chunk(f: &mut PyFile, at: Option<u64>) -> R<Option<id3::Id3Tags>> {
    match at {
        Some(off) => {
            f.seek(off);
            id3::load(f)
        }
        None => Ok(None),
    }
}

fn easy(tags: &Option<id3::Id3Tags>) -> R<EasyTags> {
    match tags {
        Some(t) => t.easy(),
        None => Ok(EasyTags::default()),
    }
}

/// read_float: the 80-bit sample rate; Err for an overflow
fn read_float(d: &[u8]) -> Result<f64, ()> {
    let expon = i16::from_be_bytes([d[0], d[1]]) as i32;
    let himant = u32::from_be_bytes([d[2], d[3], d[4], d[5]]) as u64;
    let lomant = u32::from_be_bytes([d[6], d[7], d[8], d[9]]) as u64;
    let (sign, expon) = if expon < 0 { (-1.0, expon + 0x8000) } else { (1.0, expon) };
    if expon == 0 && himant == 0 && lomant == 0 {
        return Ok(0.0);
    }
    if expon == 0x7fff {
        return Err(());
    }
    let e = expon - 16383 - 63;
    if e > 1023 {
        return Err(()); // pow(2.0, e) raises OverflowError
    }
    // 2.0 ** e exactly (subnormal down to 2**-1074, then 0.0)
    let p = if e >= -1022 {
        f64::from_bits(((e + 1023) as u64) << 52)
    } else if e >= -1074 {
        f64::from_bits(1u64 << (e + 1074))
    } else {
        0.0
    };
    let mant = ((himant as u128) << 32 | lomant as u128) as f64;
    Ok(sign * (mant * p))
}

pub fn load_aiff(f: &mut PyFile) -> R<Loaded> {
    // tags first (_IFFID3): InvalidChunk/KeyError mean no tag
    let at = match root(f, false) {
        Ok(r) => subchunks(f, &r, false)?.into_iter().find(|c| c.id == "ID3").map(|c| c.data_offset),
        Err(ChunkErr::Invalid) => None,
        Err(ChunkErr::Fatal(e)) => return Err(e),
    };
    let tags = id3_chunk(f, at)?;
    // AIFFInfo
    let r = root(f, false).map_err(|e| fatal_or(e, "Invalid chunk"))?;
    let comm = match subchunks(f, &r, false)?.into_iter().find(|c| c.id == "COMM") {
        Some(c) => c,
        None => return err("\"No 'COMM' chunk found\""),
    };
    f.seek(comm.data_offset);
    let d = f.read(comm.data_size)?;
    if d.len() < 18 {
        return err("");
    }
    let channels = i16::from_be_bytes([d[0], d[1]]) as i128;
    let frames = u32::from_be_bytes([d[2], d[3], d[4], d[5]]) as f64;
    let sample_size = i16::from_be_bytes([d[6], d[7]]) as i128;
    let rate = match read_float(&d[8..18]) {
        Ok(v) if v.is_finite() => v.trunc(),
        _ => return err("Invalid sample rate"),
    };
    if rate < 0.0 {
        return err("Invalid sample rate");
    }
    if rate >= 9.2e18 {
        return err("sample rate out of range for the Rust port");
    }
    let rate = rate as i64;
    let length = if rate != 0 { frames / rate as f64 } else { 0.0 };
    let bitrate = channels * sample_size * rate as i128;
    if bitrate > i64::MAX as i128 || bitrate < i64::MIN as i128 {
        return err("bitrate out of range for the Rust port");
    }
    Ok(Loaded { tags: easy(&tags)?, length, bitrate: bitrate as i64, sample_rate: rate })
}

/// _WaveFile: the root's chunks, with the first 'ID3' renamed 'id3'
fn wave_file(f: &mut PyFile) -> Result<Vec<Chunk>, ChunkErr> {
    let r = root(f, true)?;
    if r.name != "WAVE" {
        return Err(ChunkErr::Fatal(TagError("Expected RIFF/WAVE.".into())));
    }
    let mut kids = subchunks(f, &r, true)?;
    if let Some(c) = kids.iter_mut().find(|c| c.id == "ID3") {
        c.id = "id3".into();
    }
    Ok(kids)
}

pub fn load_wave(f: &mut PyFile) -> R<Loaded> {
    // WaveStreamInfo
    let kids = wave_file(f).map_err(|e| fatal_or(e, "Invalid chunk"))?;
    let fmt = match kids.iter().find(|c| c.id == "fmt") {
        Some(c) => c.clone(),
        None => return err("\"No 'fmt' chunk found\""),
    };
    f.seek(fmt.data_offset);
    let d = f.read(fmt.data_size)?;
    if d.len() < 16 {
        return err("Invalid chunk");
    }
    let channels = u16::from_le_bytes([d[2], d[3]]) as i64;
    let rate = u32::from_le_bytes([d[4], d[5], d[6], d[7]]) as i64;
    let block_align = u16::from_le_bytes([d[12], d[13]]) as u64;
    let bits = u16::from_le_bytes([d[14], d[15]]) as i64;
    let bitrate = channels * bits * rate;
    let mut samples = 0.0;
    if block_align > 0 {
        if let Some(data) = kids.iter().find(|c| c.id == "data") {
            samples = data.data_size as f64 / block_align as f64;
        }
    }
    let length = if rate > 0 { samples / rate as f64 } else { 0.0 };
    // _WaveID3: InvalidChunk/KeyError mean no tag
    let at = match wave_file(f) {
        Ok(k) => k.into_iter().find(|c| c.id == "id3").map(|c| c.data_offset),
        Err(ChunkErr::Invalid) => None,
        Err(ChunkErr::Fatal(e)) => return Err(e),
    };
    let tags = id3_chunk(f, at)?;
    Ok(Loaded { tags: easy(&tags)?, length, bitrate, sample_rate: rate })
}
