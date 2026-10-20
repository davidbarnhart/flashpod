//! Audio tags and stream info, read the way flashpod's Python reads them:
//! `tracks.read_audio` / `read_audio_stream` over mutagen (the reference),
//! reduced to what `make_track` takes from the result (`library::AudioInfo`).
//!
//! mutagen's behaviour is the specification, quirks included: which format
//! wins the sniffing, ID3v1 merged under ID3v2, `(17)Rock` genres, TYER/TDAT
//! turned into a date, the MPEG length from a Xing/VBRI header or the file
//! size, FLAC's bitrate from the audio bytes, MP4 `gnre`, and the cases where
//! mutagen raises (the file is then "unreadable"). WAV and AIFF tags come back
//! as raw ID3 frames that flashpod's key lookup never matches, so those files
//! take their title from the file name, as in Python.
//!
//! Formats: MP3 (EasyMP3), FLAC, MP4/M4A (EasyMP4), AAC (ADTS/ADIF), AIFF,
//! WAVE. A file whose sniffing picks another mutagen format (Ogg, APE, ...)
//! is reported unreadable here, where Python would read it: a known
//! difference, outside the extensions flashpod adds. Error messages are
//! mutagen-like but not identical.
//!
//! Proved file by file against mutagen by `scripts/rust_parity.py`.

mod aac;
mod bits;
mod flac;
mod id3;
mod iff;
mod mp4;
mod mpeg;
mod pytext;
mod tables;

use crate::library::{AudioInfo, TagResult};

/// A mutagen exception: the file is unreadable.
#[derive(Debug, Clone)]
pub struct TagError(pub String);

pub type R<T> = Result<T, TagError>;

pub(crate) fn err<T>(msg: impl Into<String>) -> R<T> {
    Err(TagError(msg.into()))
}

/// Random access to a file's bytes: a host file, or a file on a card.
pub trait ReadAt {
    fn size(&mut self) -> std::io::Result<u64>;
    /// Read up to buf.len() bytes at `off`; fewer only at the end.
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> std::io::Result<usize>;
}

impl ReadAt for std::fs::File {
    fn size(&mut self) -> std::io::Result<u64> {
        Ok(self.metadata()?.len())
    }
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> std::io::Result<usize> {
        use std::io::{Read, Seek, SeekFrom};
        self.seek(SeekFrom::Start(off))?;
        let mut n = 0;
        while n < buf.len() {
            match self.read(&mut buf[n..])? {
                0 => break,
                k => n += k,
            }
        }
        Ok(n)
    }
}

impl ReadAt for &[u8] {
    fn size(&mut self) -> std::io::Result<u64> {
        Ok(self.len() as u64)
    }
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> std::io::Result<usize> {
        if off >= self.len() as u64 {
            return Ok(0);
        }
        let s = &self[off as usize..];
        let n = s.len().min(buf.len());
        buf[..n].copy_from_slice(&s[..n]);
        Ok(n)
    }
}

/// A Python file object over a [`ReadAt`]: reads return short at the end,
/// seeking past the end is allowed, and a seek before the start lands at 0
/// (as flashpod's FAT file does; a host file raises EINVAL, which mutagen
/// only provokes where it handles both the same way).
pub struct PyFile<'a> {
    src: &'a mut dyn ReadAt,
    pos: u64,
    size: u64,
}

impl<'a> PyFile<'a> {
    pub fn new(src: &'a mut dyn ReadAt) -> R<Self> {
        let size = src.size().map_err(io)?;
        Ok(PyFile { src, pos: 0, size })
    }
    pub fn size(&self) -> u64 {
        self.size
    }
}

fn io(e: std::io::Error) -> TagError {
    TagError(format!("[Errno] {}", e))
}

/// The file-object protocol mutagen's readers use; also an in-memory BytesIO.
pub trait Stream {
    fn read(&mut self, n: u64) -> R<Vec<u8>>;
    fn tell(&self) -> u64;
    fn seek(&mut self, pos: u64);
    fn len(&self) -> u64;
    fn seek_rel(&mut self, d: i64) {
        let p = self.tell() as i128 + d as i128;
        self.seek(p.max(0) as u64);
    }
    fn read_all(&mut self) -> R<Vec<u8>> {
        let n = self.len().saturating_sub(self.tell());
        self.read(n)
    }
    /// read exactly n bytes or None (mutagen's "not enough data")
    fn read_exact_or_none(&mut self, n: u64) -> R<Option<Vec<u8>>> {
        let d = self.read(n)?;
        Ok(if d.len() as u64 == n { Some(d) } else { None })
    }
}

impl Stream for PyFile<'_> {
    fn read(&mut self, n: u64) -> R<Vec<u8>> {
        let n = n.min(self.size.saturating_sub(self.pos));
        let mut buf = vec![0u8; n as usize];
        let mut got = 0usize;
        while got < buf.len() {
            let k = self.src.read_at(self.pos + got as u64, &mut buf[got..]).map_err(io)?;
            if k == 0 {
                break;
            }
            got += k;
        }
        buf.truncate(got);
        self.pos += got as u64;
        Ok(buf)
    }
    fn tell(&self) -> u64 {
        self.pos
    }
    fn seek(&mut self, pos: u64) {
        self.pos = pos;
    }
    fn len(&self) -> u64 {
        self.size
    }
}

/// io.BytesIO over owned bytes.
pub struct BytesIo {
    pub data: Vec<u8>,
    pub pos: u64,
}

impl BytesIo {
    pub fn new(data: Vec<u8>) -> Self {
        BytesIo { data, pos: 0 }
    }
}

impl Stream for BytesIo {
    fn read(&mut self, n: u64) -> R<Vec<u8>> {
        let len = self.data.len() as u64;
        if self.pos >= len {
            return Ok(Vec::new());
        }
        let end = len.min(self.pos.saturating_add(n));
        let out = self.data[self.pos as usize..end as usize].to_vec();
        self.pos = end;
        Ok(out)
    }
    fn tell(&self) -> u64 {
        self.pos
    }
    fn seek(&mut self, pos: u64) {
        self.pos = pos;
    }
    fn len(&self) -> u64 {
        self.data.len() as u64
    }
}

/// What a loaded file gives flashpod: the easy tags (first value of each
/// key, None when absent or empty) and the stream info.
#[derive(Debug, Clone, Default)]
pub struct Loaded {
    pub tags: EasyTags,
    pub length: f64,
    pub bitrate: i64,
    pub sample_rate: i64,
}

#[derive(Debug, Clone, Default)]
pub struct EasyTags {
    pub title: Option<String>,
    pub artist: Option<String>,
    pub album: Option<String>,
    pub genre: Option<String>,
    pub composer: Option<String>,
    pub tracknumber: Option<String>,
    pub date: Option<String>,
}

/// tracks.first: the first value, as None when it is empty.
pub(crate) fn first(values: Option<&Vec<String>>) -> Option<String> {
    values.and_then(|v| v.first()).filter(|s| !s.is_empty()).cloned()
}

/// mutagen's format candidates for `mutagen.File(easy=True)`, by class name.
const KINDS: [&str; 24] = [
    "EasyMP3", "EasyTrueAudio", "OggTheora", "OggSpeex", "OggVorbis", "OggFLAC", "FLAC", "AIFF",
    "APEv2File", "EasyMP4", "EasyID3FileType", "WavPack", "Musepack", "MonkeysAudio", "OptimFROG",
    "ASF", "OggOpus", "AAC", "AC3", "SMF", "TAK", "DSF", "DSDIFF", "WAVE",
];

fn score(kind: &str, name: &str, lname: &str, h: &[u8], f: &mut PyFile) -> R<i64> {
    let ends = |s: &str, e: &str| s.ends_with(e) as i64;
    let sw = |p: &[u8]| h.starts_with(p) as i64;
    let has = |p: &[u8]| h.windows(p.len()).any(|w| w == p) as i64;
    Ok(match kind {
        "EasyMP3" => {
            (sw(b"ID3") | sw(b"\xFF\xF2") | sw(b"\xFF\xF3") | sw(b"\xFF\xFA") | sw(b"\xFF\xFB")) * 2
                + ends(lname, ".mp3") + ends(lname, ".mp2") + ends(lname, ".mpg") + ends(lname, ".mpeg")
        }
        "EasyTrueAudio" => sw(b"ID3") + sw(b"TTA") + ends(lname, ".tta") * 2,
        "OggTheora" => sw(b"OggS") * (has(b"\x80theora") + has(b"\x81theora")) * 2,
        "OggSpeex" => sw(b"OggS") * has(b"Speex   "),
        "OggVorbis" => sw(b"OggS") * has(b"\x01vorbis"),
        "OggFLAC" => sw(b"OggS") * (has(b"FLAC") + has(b"fLaC")),
        "FLAC" => sw(b"fLaC") + ends(lname, ".flac") * 3,
        "AIFF" => sw(b"FORM") * 2 + ends(lname, ".aif") + ends(lname, ".aiff") + ends(lname, ".aifc"),
        "APEv2File" => {
            // seek_end(fileobj, 160); footer = read()
            let size = f.size();
            f.seek(size.saturating_sub(160));
            let footer = f.read_all()?;
            (footer.windows(8).any(|w| w == b"APETAGEX") as i64) - sw(b"ID3")
        }
        "EasyMP4" => has(b"ftyp") + has(b"mp4"),
        "EasyID3FileType" => sw(b"ID3"),
        "WavPack" => sw(b"wvpk") * 2,
        "Musepack" => sw(b"MP+") + sw(b"MPCK") + ends(lname, ".mpc"),
        "MonkeysAudio" => sw(b"MAC ") + ends(lname, ".ape"),
        "OptimFROG" => sw(b"OFR") + ends(lname, ".ofr") + ends(lname, ".ofs"),
        "ASF" => sw(&[0x30, 0x26, 0xb2, 0x75, 0x8e, 0x66, 0xcf, 0x11, 0xa6, 0xd9, 0x00, 0xaa, 0x00, 0x62, 0xce, 0x6c]) * 2,
        "OggOpus" => sw(b"OggS") * has(b"OpusHead"),
        "AAC" => {
            let s = (lname.ends_with(".aac") || lname.ends_with(".adts") || lname.ends_with(".adif")) as i64;
            s + has(b"ADIF")
        }
        // these two test the name as given, not lowercased
        "AC3" => sw(b"\x0b\x77") * 2 + (name.ends_with(".ac3") || name.ends_with(".eac3")) as i64,
        "SMF" => (sw(b"MThd") == 1 && (lname.ends_with(".mid") || lname.ends_with(".midi"))) as i64,
        "TAK" => sw(b"tBaK") + ends(lname, ".tak"),
        "DSF" => sw(b"DSD ") * 2 + ends(lname, ".dsf"),
        "DSDIFF" => sw(b"FRM8") * 2 + ends(name, ".dff"),
        "WAVE" => sw(b"RIFF") + (h.get(8..12) == Some(b"WAVE")) as i64 + ends(lname, ".wav") + ends(lname, ".wave"),
        _ => 0,
    })
}

/// mutagen.File(fileobj, easy=True): Ok(None) when nothing scores.
fn mutagen_file(name: &str, f: &mut PyFile) -> R<Option<Loaded>> {
    f.seek(0);
    let header = f.read(128)?;
    let lname = name.to_lowercase();
    let mut best: Option<(i64, &str)> = None;
    for k in KINDS {
        let s = score(k, name, &lname, &header, f)?;
        // results.sort() on (score, name); the last one wins
        if best.map_or(true, |(bs, bn)| (s, k) > (bs, bn)) {
            best = Some((s, k));
        }
    }
    let (s, kind) = best.unwrap();
    if s <= 0 {
        return Ok(None);
    }
    f.seek(0);
    load_kind(kind, f).map(Some)
}

fn load_kind(kind: &str, f: &mut PyFile) -> R<Loaded> {
    match kind {
        "EasyMP3" => mpeg::load_mp3(f),
        "EasyID3FileType" => mpeg::load_id3_file(f),
        "FLAC" => flac::load(f),
        "EasyMP4" => mp4::load(f),
        "AAC" => aac::load(f),
        "AIFF" => iff::load_aiff(f),
        "WAVE" => iff::load_wave(f),
        other => err(format!("{} files are not read by the Rust port", other)),
    }
}

/// tracks.read_audio / read_audio_stream: an MP3 by extension goes straight
/// to EasyMP3 and falls back to full detection if that raises.
pub fn read_audio(name: &str, src: &mut dyn ReadAt) -> R<Option<Loaded>> {
    let mut f = PyFile::new(src)?;
    let (_, ext) = crate::library::splitext(name);
    if ext.to_lowercase() == ".mp3" {
        f.seek(0);
        if let Ok(l) = mpeg::load_mp3(&mut f) {
            return Ok(Some(l));
        }
    }
    mutagen_file(name, &mut f)
}

/// make_track's view of a file: AudioInfo, or why it is skipped.
pub fn audio_info(name: &str, src: &mut dyn ReadAt) -> TagResult {
    let size = match src.size() {
        Ok(s) => s,
        Err(e) => return TagResult::Unreadable(io(e).0),
    };
    match read_audio(name, src) {
        Err(TagError(m)) => TagResult::Unreadable(m),
        Ok(None) => TagResult::Unrecognized,
        Ok(Some(l)) => {
            let t = l.tags;
            TagResult::Audio(AudioInfo {
                title: t.title,
                artist: t.artist,
                album: t.album,
                genre: t.genre,
                composer: t.composer,
                tracknumber: t.tracknumber,
                date: t.date,
                length: l.length,
                bitrate: l.bitrate,
                sample_rate: l.sample_rate,
                size,
            })
        }
    }
}

/// Tags read from host files by path (an add).
pub struct FileTags;

impl crate::library::TagSource for FileTags {
    fn read(&mut self, path: &str) -> TagResult {
        match std::fs::File::open(path) {
            Ok(mut f) => audio_info(path, &mut f),
            Err(e) => TagResult::Unreadable(io(e).0),
        }
    }
    fn read_stream(&mut self, path: &str, file: &mut dyn ReadAt) -> TagResult {
        audio_info(path, file)
    }
}

/// A file on a FAT volume, read a cluster at a time as the reader asks
/// (fatfs._FatFile): a tag read touches the head and the tail only.
pub struct FatFile<'f, 'a> {
    fs: &'f mut crate::fat::Fat32<'a>,
    clusters: Vec<u32>,
    size: u64,
    cbytes: u64,
    cache: Option<(usize, Vec<u8>)>,
}

impl<'f, 'a> FatFile<'f, 'a> {
    pub fn open(fs: &'f mut crate::fat::Fat32<'a>, first_cluster: u32, size: u64) -> Result<Self, String> {
        let clusters = if first_cluster >= 2 { fs.chain(first_cluster)? } else { Vec::new() };
        let cbytes = fs.sectors_per_cluster as u64 * 512;
        Ok(FatFile { fs, clusters, size, cbytes, cache: None })
    }
}

impl ReadAt for FatFile<'_, '_> {
    fn size(&mut self) -> std::io::Result<u64> {
        Ok(self.size)
    }
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> std::io::Result<usize> {
        let end = self.size.min(off.saturating_add(buf.len() as u64));
        let mut pos = off;
        let mut n = 0usize;
        while pos < end {
            let idx = (pos / self.cbytes) as usize;
            if idx >= self.clusters.len() {
                break;
            }
            if self.cache.as_ref().map(|c| c.0) != Some(idx) {
                let data = self
                    .fs
                    .read_cluster(self.clusters[idx])
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
                self.cache = Some((idx, data));
            }
            let within = (pos % self.cbytes) as usize;
            let take = ((self.cbytes - within as u64).min(end - pos)) as usize;
            let c = &self.cache.as_ref().unwrap().1;
            buf[n..n + take].copy_from_slice(&c[within..within + take]);
            n += take;
            pos += take as u64;
        }
        Ok(n)
    }
}
