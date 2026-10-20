//! mutagen.easymp4.EasyMP4: the atom tree, MP4Info (the first sound track's
//! mdhd, its sample entry's rate and bitrate, or the movie's mvhd), the
//! iTunes `ilst` tags and the `chpl` chapters. mutagen gives up on the whole
//! file for some malformed atoms and keeps going for others; both are kept.

use super::aac::pce;
use super::bits::{BitErr, BitReader};
use super::tables::GENRES;
use super::{err, first, BytesIo, EasyTags, Loaded, PyFile, Stream, TagError, R};

const CONTAINERS: [&[u8; 4]; 10] =
    [b"moov", b"udta", b"trak", b"mdia", b"meta", b"ilst", b"stbl", b"minf", b"moof", b"traf"];

/// Python's recursion limit would stop a deeper tree; keep the stack safe.
const MAX_DEPTH: usize = 400;

#[derive(Debug)]
struct Atom {
    name: [u8; 4],
    offset: u64,
    length: u64,
    dataoffset: u64,
    children: Option<Vec<Atom>>,
}

impl Atom {
    fn parse<S: Stream + ?Sized>(f: &mut S, level: usize) -> R<Atom> {
        if level > MAX_DEPTH {
            return err("maximum recursion depth exceeded");
        }
        let offset = f.tell();
        let d = f.read(8)?;
        if d.len() < 8 {
            return err("truncated data");
        }
        let mut length = u32::from_be_bytes([d[0], d[1], d[2], d[3]]) as u64;
        let name = [d[4], d[5], d[6], d[7]];
        let mut dataoffset = offset + 8;
        if length == 1 {
            let e = f.read(8)?;
            if e.len() < 8 {
                return err("truncated data");
            }
            length = u64::from_be_bytes([e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7]]);
            dataoffset += 8;
            if length < 16 {
                return err("64 bit atom length can only be 16 and higher");
            }
        } else if length == 0 {
            if level != 0 {
                return err("only a top-level atom can have zero length");
            }
            length = f.len().saturating_sub(offset);
            f.seek(offset + 8);
        } else if length < 8 {
            return err("atom length can only be 0, 1 or 8 and higher");
        }
        let end = match offset.checked_add(length) {
            Some(e) if e <= i64::MAX as u64 => e,
            _ => return err("Python int too large to convert to C long"),
        };
        let mut children = None;
        if CONTAINERS.contains(&&name) {
            let mut kids = Vec::new();
            f.seek_rel(if &name == b"meta" { 4 } else { 0 });
            while f.tell() < end {
                kids.push(Atom::parse(f, level + 1)?);
            }
            children = Some(kids);
        } else {
            f.seek(end);
        }
        Ok(Atom { name, offset, length, dataoffset, children })
    }

    fn datalength(&self) -> u64 {
        self.length - (self.dataoffset - self.offset)
    }

    /// Atom.read: (all data read, data)
    fn read<S: Stream + ?Sized>(&self, f: &mut S) -> R<(bool, Vec<u8>)> {
        f.seek(self.dataoffset);
        let d = f.read(self.datalength())?;
        Ok((d.len() as u64 == self.datalength(), d))
    }

    /// atom[names]: the first child of each name in turn
    fn get(&self, path: &[&[u8; 4]]) -> Option<&Atom> {
        match path.split_first() {
            None => Some(self),
            Some((n, rest)) => self.children.as_ref()?.iter().find(|c| &&c.name == n)?.get(rest),
        }
    }
}

struct Atoms(Vec<Atom>);

impl Atoms {
    fn get(&self, path: &[&[u8; 4]]) -> Option<&Atom> {
        let (n, rest) = path.split_first()?;
        self.0.iter().find(|a| &&a.name == n)?.get(rest)
    }
}

fn be32(d: &[u8]) -> u64 {
    u32::from_be_bytes([d[0], d[1], d[2], d[3]]) as u64
}

/// parse_full_atom: (version, payload); None: ValueError
fn full_atom(d: &[u8]) -> Option<(u8, &[u8])> {
    (d.len() >= 4).then(|| (d[0], &d[4..]))
}

/// int / int true division, correctly rounded as Python does it
fn py_truediv(a: u64, b: u64) -> f64 {
    if a < (1u64 << 53) {
        return a as f64 / b as f64;
    }
    let n = (a as u128) << 64;
    let (q, r) = (n / b as u128, n % b as u128);
    let h = 127 - q.leading_zeros() as i32;
    let shift = h - 52;
    let mut m = (q >> shift) as u64;
    let rest = q & ((1u128 << shift) - 1);
    let half = 1u128 << (shift - 1);
    if rest > half || (rest == half && (r != 0 || m & 1 == 1)) {
        m += 1;
    }
    m as f64 * 2f64.powi(shift - 64)
}

#[derive(Default)]
struct Info {
    length: f64,
    bitrate: i64,
    sample_rate: i64,
}

fn mp4_info(atoms: &Atoms, f: &mut PyFile) -> R<Info> {
    let mut info = Info::default();
    let moov = match atoms.get(&[b"moov"]) {
        Some(m) => m,
        None => return err("not a MP4 file"),
    };
    let traks = moov.children.iter().flatten().filter(|c| &c.name == b"trak");
    let mut sound = None;
    for trak in traks {
        let hdlr = match trak.get(&[b"mdia", b"hdlr"]) {
            Some(h) => h,
            None => return err("'hdlr' not found"),
        };
        let (ok, data) = hdlr.read(f)?;
        if !ok {
            return err("Not enough data");
        }
        if data.get(8..12) == Some(b"soun") {
            sound = Some(trak);
            break;
        }
    }
    let trak = match sound {
        Some(t) => t,
        None => {
            // no sound track: the movie length from mvhd, if there is one
            match moov.get(&[b"mvhd"]) {
                None => return Ok(info), // MP4NoTrackError, ignored
                Some(mvhd) => {
                    let (ok, data) = mvhd.read(f)?;
                    if !ok {
                        return err("Invalid mvhd");
                    }
                    let (version, d) = match full_atom(&data) {
                        Some(x) => x,
                        None => return err("not enough data"),
                    };
                    let (timescale, duration) = match version {
                        0 if d.len() >= 16 => (be32(&d[8..12]), be32(&d[12..16])),
                        1 if d.len() >= 28 => (be32(&d[16..20]), u64::from_be_bytes(d[20..28].try_into().unwrap())),
                        0 | 1 => return err("unpack requires a buffer"),
                        _ => return Ok(info),
                    };
                    if timescale == 0 {
                        return err("division by zero");
                    }
                    info.length = py_truediv(duration, timescale);
                    return Ok(info);
                }
            }
        }
    };
    let mdhd = match trak.get(&[b"mdia", b"mdhd"]) {
        Some(m) => m,
        None => return err("'mdhd' not found"),
    };
    let (ok, data) = mdhd.read(f)?;
    if !ok {
        return err("Not enough data");
    }
    let (version, d) = match full_atom(&data) {
        Some(x) => x,
        None => return err("not enough data"),
    };
    let (unit, length) = match version {
        0 if d.len() >= 16 => (be32(&d[8..12]), be32(&d[12..16])),
        1 if d.len() >= 28 => (be32(&d[16..20]), u64::from_be_bytes(d[20..28].try_into().unwrap())),
        0 | 1 => return err("unpack requires a buffer"),
        v => return err(format!("Unknown mdhd version {}", v)),
    };
    info.length = if unit == 0 { 0.0 } else { length as f64 / unit as f64 };
    if let Some(stsd) = trak.get(&[b"mdia", b"minf", b"stbl", b"stsd"]) {
        parse_stsd(stsd, f, &mut info)?;
    }
    Ok(info)
}

fn parse_stsd(atom: &Atom, f: &mut PyFile, info: &mut Info) -> R<()> {
    let (ok, data) = atom.read(f)?;
    if !ok {
        return err("Invalid stsd");
    }
    let (version, d) = match full_atom(&data) {
        Some(x) => x,
        None => return err("not enough data"),
    };
    if version != 0 {
        return err("Unsupported stsd version");
    }
    if d.len() < 4 {
        return err("unpack_from requires a buffer");
    }
    if be32(d) == 0 {
        return Ok(());
    }
    let mut ef = BytesIo::new(d[4..].to_vec());
    let entry = Atom::parse(&mut ef, 0)?;
    sample_entry(&entry, &mut ef, info)
}

fn short(e: BitErr) -> TagError {
    match e {
        BitErr::Short => TagError("not enough data".into()),
        BitErr::Io(e) => e,
    }
}

/// AudioSampleEntry: sample rate, then the codec box's bitrate and rate
fn sample_entry(atom: &Atom, ef: &mut BytesIo, info: &mut Info) -> R<()> {
    let (ok, data) = atom.read(ef)?;
    if !ok {
        return err("too short atom");
    }
    let mut b = BytesIo::new(data);
    let sample_rate;
    {
        let mut r = BitReader::new(&mut b);
        let res: Result<u64, BitErr> = (|| {
            r.skip(6 * 8)?;
            r.skip(2 * 8)?;
            r.skip(8 * 8)?;
            r.bits(16)?; // channels
            r.bits(16)?; // sample size
            r.skip(2 * 8)?;
            r.skip(2 * 8)?;
            Ok(r.bits(32)? >> 16)
        })();
        sample_rate = res.map_err(short)?;
    }
    info.sample_rate = sample_rate as i64;
    info.bitrate = 0;
    let extra = Atom::parse(&mut b, 0)?;
    match (&atom.name, &extra.name) {
        (b"mp4a", b"esds") => esds(&extra, &mut b, info),
        (b"alac", b"alac") => alac(&extra, &mut b, info),
        (b"ac-3", b"dac3") => dac3(&extra, &mut b, info),
        _ => Ok(()),
    }
}

fn dac3(atom: &Atom, f: &mut BytesIo, info: &mut Info) -> R<()> {
    let (ok, data) = atom.read(f)?;
    if !ok {
        return err("truncated dac3 atom");
    }
    let mut b = BytesIo::new(data);
    let mut r = BitReader::new(&mut b);
    let code = (|| -> Result<u64, BitErr> {
        r.skip(10)?;
        r.bits(3)?;
        r.bits(1)?;
        let c = r.bits(5)?;
        r.skip(5)?;
        Ok(c)
    })()
    .map_err(short)?;
    const RATES: [i64; 19] = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640];
    if let Some(k) = RATES.get(code as usize) {
        info.bitrate = k * 1000;
    }
    Ok(())
}

fn alac(atom: &Atom, f: &mut BytesIo, info: &mut Info) -> R<()> {
    let (ok, data) = atom.read(f)?;
    if !ok {
        return err("truncated alac atom");
    }
    let (version, d) = match full_atom(&data) {
        Some(x) => x,
        None => return err("not enough data"),
    };
    if version != 0 {
        return err(format!("Unsupported version {}", version));
    }
    let mut b = BytesIo::new(d.to_vec());
    let mut r = BitReader::new(&mut b);
    let res = (|| -> Result<Option<(u64, u64)>, BitErr> {
        r.skip(32)?;
        if r.bits(8)? != 0 {
            return Ok(None);
        }
        r.bits(8)?;
        r.skip(24)?;
        r.bits(8)?;
        r.skip(48)?;
        let bitrate = r.bits(32)?;
        let rate = r.bits(32)?;
        Ok(Some((bitrate, rate)))
    })()
    .map_err(short)?;
    if let Some((bitrate, rate)) = res {
        info.bitrate = bitrate as i64;
        info.sample_rate = rate as i64;
    }
    Ok(())
}

/// BaseDescriptor._parse_desc_length_file; None: ValueError
fn desc_length(f: &mut BytesIo) -> R<Option<u64>> {
    let mut v = 0u64;
    for _ in 0..4 {
        let b = f.read(1)?;
        let b = match b.first() {
            Some(&b) => b,
            None => return Ok(None),
        };
        v = (v << 7) | (b & 0x7f) as u64;
        if b >> 7 == 0 {
            return Ok(Some(v));
        }
    }
    Ok(None)
}

fn desc_err() -> TagError {
    TagError("invalid descriptor".into())
}

fn esds(atom: &Atom, f: &mut BytesIo, info: &mut Info) -> R<()> {
    let (ok, data) = atom.read(f)?;
    if !ok {
        return err("truncated esds atom");
    }
    let (version, d) = match full_atom(&data) {
        Some(x) => x,
        None => return err("not enough data"),
    };
    if version != 0 {
        return err(format!("Unsupported version {}", version));
    }
    let mut b = BytesIo::new(d.to_vec());
    let tag = BitReader::new(&mut b).bits(8).map_err(short)?;
    if tag != 3 {
        return err(format!("unexpected descriptor: {}", tag));
    }
    // ES_Descriptor.parse
    let len = desc_length(&mut b)?.ok_or_else(desc_err)?;
    let pos = b.tell();
    {
        let mut r = BitReader::new(&mut b);
        let t = (|| -> Result<u64, BitErr> {
            r.bits(16)?;
            let dep = r.bits(1)?;
            let url = r.bits(1)?;
            let ocr = r.bits(1)?;
            r.bits(5)?;
            if dep != 0 {
                r.bits(16)?;
            }
            if url != 0 {
                let n = r.bits(8)?;
                r.bytes(n)?;
            }
            if ocr != 0 {
                r.bits(16)?;
            }
            r.bits(8)
        })()
        .map_err(|_| desc_err())?;
        if t != 4 {
            return Err(desc_err());
        }
    }
    let (avg, rate) = decoder_config(&mut b)?;
    let left = len as i64 - (b.tell() as i64 - pos as i64);
    if left > 0 {
        b.seek_rel(left);
    }
    info.bitrate = avg as i64;
    if rate != 0 {
        info.sample_rate = rate as i64;
    }
    Ok(())
}

/// DecoderConfigDescriptor.parse: (avgBitrate, the decoder info's sample rate or 0)
fn decoder_config(b: &mut BytesIo) -> R<(u64, u64)> {
    let len = desc_length(b)?.ok_or_else(desc_err)?;
    let pos = b.tell();
    let (avg, tag) = {
        let mut r = BitReader::new(b);
        (|| -> Result<(u64, Option<u64>), BitErr> {
            let oti = r.bits(8)?;
            let st = r.bits(6)?;
            r.bits(1)?;
            r.bits(1)?;
            r.bits(24)?;
            r.bits(32)?;
            let avg = r.bits(32)?;
            if (oti, st) != (0x40, 5) {
                return Ok((avg, None));
            }
            if len as i64 * 8 == r.get_position() {
                return Ok((avg, None));
            }
            Ok((avg, Some(r.bits(8)?)))
        })()
        .map_err(|_| desc_err())?
    };
    let mut rate = 0;
    if tag == Some(5) {
        rate = decoder_specific(b)?;
    }
    let left = len as i64 - (b.tell() as i64 - pos as i64);
    if left > 0 {
        b.seek_rel(left);
    }
    Ok((avg, rate))
}

const FREQS: [u64; 13] = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];

/// DecoderSpecificInfo.parse: its sample_rate property (0 = unknown)
fn decoder_specific(b: &mut BytesIo) -> R<u64> {
    let len = desc_length(b)?.ok_or_else(desc_err)?;
    let pos = b.tell();
    let rate = {
        let mut r = BitReader::new(b);
        dsi(&mut r, len).map_err(|_| desc_err())?
    };
    let left = len as i64 - (b.tell() as i64 - pos as i64);
    if left > 0 {
        b.seek_rel(left);
    }
    Ok(rate)
}

fn dsi(r: &mut BitReader<'_, BytesIo>, length: u64) -> Result<u64, BitErr> {
    fn aot(r: &mut BitReader<'_, BytesIo>) -> Result<u64, BitErr> {
        let a = r.bits(5)?;
        Ok(if a == 31 { 32 + r.bits(6)? } else { a })
    }
    fn freq(r: &mut BitReader<'_, BytesIo>) -> Result<u64, BitErr> {
        let i = r.bits(4)?;
        Ok(if i == 0xf { r.bits(24)? } else { FREQS.get(i as usize).copied().unwrap_or(0) })
    }
    let bits_left = |r: &BitReader<'_, BytesIo>| length as i64 * 8 - r.get_position();
    let mut a = aot(r)?;
    let sf = freq(r)?;
    let chan = r.bits(4)?;
    let mut sbr: i64 = -1;
    let mut ext_sf = 0u64;
    let ext_aot;
    if a == 5 || a == 29 {
        ext_aot = 5;
        sbr = 1;
        ext_sf = freq(r)?;
        a = aot(r)?;
        if a == 22 {
            r.bits(4)?;
        }
    } else {
        ext_aot = 0;
    }
    let rate = |a: u64, sbr: i64, ext_sf: u64| -> u64 {
        if sbr == 1 {
            ext_sf
        } else if sbr == 0 {
            sf
        } else if ![1, 2, 3, 4, 6, 17, 19, 20, 22].contains(&a) || sf > 24000 {
            sf
        } else {
            0
        }
    };
    if [1, 2, 3, 4, 6, 7, 17, 19, 20, 21, 22, 23].contains(&a) {
        // GASpecificConfig; NotImplementedError ends the parse
        r.skip(1)?;
        if r.bits(1)? != 0 {
            r.skip(14)?;
        }
        let ext = r.bits(1)?;
        if chan == 0 {
            pce(r)?;
        }
        if a == 6 || a == 20 {
            r.skip(3)?;
        }
        if ext != 0 {
            if a == 22 {
                r.skip(16)?;
            }
            if [17, 19, 20, 23].contains(&a) {
                r.skip(3)?;
            }
            if r.bits(1)? != 0 {
                return Ok(rate(a, sbr, ext_sf));
            }
        }
    } else {
        return Ok(rate(a, sbr, ext_sf));
    }
    if [17, 19, 20, 21, 22, 23, 24, 25, 26, 27, 39].contains(&a) {
        let ep = r.bits(2)?;
        if ep == 2 || ep == 3 {
            return Ok(rate(a, sbr, ext_sf));
        }
    }
    if ext_aot != 5 && bits_left(r) >= 16 && r.bits(11)? == 0x2b7 {
        let e = aot(r)?;
        if e == 5 {
            sbr = r.bits(1)? as i64;
            if sbr == 1 {
                ext_sf = freq(r)?;
                if bits_left(r) >= 12 && r.bits(11)? == 0x548 {
                    r.bits(1)?;
                }
            }
        }
        if e == 22 {
            sbr = r.bits(1)? as i64;
            if sbr == 1 {
                ext_sf = freq(r)?;
            }
            r.bits(4)?;
        }
    }
    Ok(rate(a, sbr, ext_sf))
}

/// MP4Tags.__parse_data: the next data atom, Err(()) for MP4MetadataError
fn data_atoms(atom_len: u64, data: &[u8]) -> Result<Vec<(u8, u32, Vec<u8>)>, ()> {
    let mut out = Vec::new();
    let mut pos = 0u64;
    while (pos as i128) < atom_len as i128 - 8 {
        let head = slice(data, pos, 12);
        if head.len() != 12 {
            return Err(());
        }
        let length = be32(head);
        if length < 1 || &head[4..8] != b"data" {
            return Err(());
        }
        let version = head[8];
        let flags = be32(&[0, head[9], head[10], head[11]]) as u32;
        let chunk = if length >= 16 { slice(data, pos + 16, length - 16) } else { &[][..] };
        if (chunk.len() as i128) != length as i128 - 16 {
            return Err(());
        }
        out.push((version, flags, chunk.to_vec()));
        pos += length;
    }
    Ok(out)
}

/// data[start:start+n], clamped like a Python slice
fn slice(data: &[u8], start: u64, n: u64) -> &[u8] {
    let len = data.len() as u64;
    let s = start.min(len);
    let e = start.saturating_add(n).min(len);
    &data[s as usize..e as usize]
}

const TEXT_ATOMS: [&[u8; 4]; 25] = [
    b"\xa9nam", b"\xa9alb", b"\xa9ART", b"aART", b"\xa9wrt", b"\xa9day", b"\xa9cmt", b"desc", b"purd", b"\xa9grp",
    b"\xa9gen", b"\xa9lyr", b"catg", b"keyw", b"\xa9too", b"cprt", b"soal", b"soaa", b"soar", b"sonm", b"soco",
    b"sosn", b"tvsh", b"purl", b"egid",
];

const QUIET_ATOMS: [&[u8; 4]; 20] = [
    b"plID", b"cnID", b"geID", b"atID", b"sfID", b"cmID", b"akID", b"tvsn", b"tves", b"tmpo", b"\xa9mvi", b"\xa9mvc",
    b"cpil", b"pgap", b"pcst", b"shwm", b"stik", b"hdvd", b"rtng", b"gnre",
];

#[derive(Default)]
struct Tags {
    text: Vec<([u8; 4], Vec<String>)>,
    trkn: Vec<(u16, u16)>,
    has_trkn: bool,
}

impl Tags {
    fn add(&mut self, k: [u8; 4], v: Vec<String>) {
        match self.text.iter_mut().find(|(n, _)| *n == k) {
            Some(slot) => slot.1.extend(v),
            None => self.text.push((k, v)),
        }
    }
    fn get(&self, k: &[u8; 4]) -> Option<&Vec<String>> {
        self.text.iter().find(|(n, _)| n == k).map(|(_, v)| v)
    }
}

fn fatal() -> TagError {
    TagError("unpack requires a buffer of more bytes".into())
}

fn mp4_tags(ilst: &Atom, f: &mut PyFile) -> R<Tags> {
    let mut tags = Tags::default();
    for atom in ilst.children.iter().flatten() {
        let (ok, data) = atom.read(f)?;
        if !ok {
            return err("Not enough data");
        }
        let n = &atom.name;
        let alen = atom.length;
        if n == b"----" {
            freeform(alen, &data)?;
        } else if n == b"trkn" || n == b"disk" {
            // __parse_pair: d[2:6] must unpack, else struct.error (fatal)
            let mut pos = 0u64;
            let mut values = Vec::new();
            let mut failed = false;
            while (pos as i128) < alen as i128 - 8 {
                let head = slice(&data, pos, 12);
                if head.len() != 12 || be32(head) < 1 || &head[4..8] != b"data" {
                    failed = true;
                    break;
                }
                let length = be32(head);
                let chunk = if length >= 16 { slice(&data, pos + 16, length - 16) } else { &[][..] };
                if (chunk.len() as i128) != length as i128 - 16 {
                    failed = true;
                    break;
                }
                if chunk.len() < 6 {
                    return Err(fatal());
                }
                values.push((u16::from_be_bytes([chunk[2], chunk[3]]), u16::from_be_bytes([chunk[4], chunk[5]])));
                pos += length;
            }
            if !failed && n == b"trkn" {
                tags.has_trkn = true;
                tags.trkn.extend(values);
            }
        } else if n == b"gnre" {
            if let Ok(items) = data_atoms(alen, &data) {
                let mut vals = Vec::new();
                let mut ok = true;
                for (_, _, d) in items {
                    if d.len() != 2 {
                        ok = false;
                        break;
                    }
                    let g = i16::from_be_bytes([d[0], d[1]]) as i64 - 1;
                    let idx = if g < 0 { g + GENRES.len() as i64 } else { g };
                    match (idx >= 0).then(|| GENRES.get(idx as usize)).flatten() {
                        Some(name) => vals.push(name.to_string()),
                        None => {
                            ok = false;
                            break;
                        }
                    }
                }
                if ok {
                    tags.add(*b"\xa9gen", vals);
                }
            }
        } else if n == b"covr" {
            cover(alen, &data)?;
        } else if QUIET_ATOMS.contains(&n) {
            // integers and booleans: failures stay inside the atom
        } else {
            let implicit = TEXT_ATOMS.contains(&n);
            if let Ok(items) = data_atoms(alen, &data) {
                let mut vals = Vec::new();
                let mut ok = true;
                for (_, flags, d) in items {
                    let flags_ok = if implicit { flags == 0 || flags == 1 } else { flags == 1 };
                    match (flags_ok, String::from_utf8(d)) {
                        (true, Ok(s)) => vals.push(s),
                        _ => {
                            ok = false;
                            break;
                        }
                    }
                }
                if ok {
                    tags.add(*n, vals);
                }
            }
        }
    }
    Ok(tags)
}

/// __parse_freeform: only its fatal errors matter here
fn freeform(alen: u64, data: &[u8]) -> R<()> {
    let need = |s: &[u8], n: usize| if s.len() < n { Err(fatal()) } else { Ok(()) };
    let h = slice(data, 0, 4);
    need(h, 4)?;
    let mut pos = be32(h);
    let h = slice(data, pos, 4);
    need(h, 4)?;
    pos = pos.saturating_add(be32(h));
    while (pos as i128) < alen as i128 - 8 {
        let h = slice(data, pos, 8);
        need(h, 8)?;
        let length = be32(h);
        if &h[4..8] != b"data" || length < 1 {
            return Ok(()); // MP4MetadataError: the atom is kept as failed
        }
        if slice(data, pos + 8, 1).is_empty() {
            return err("ord() expected a character, but string of length 0 found");
        }
        need(slice(data, pos + 9, 3), 3)?;
        pos = pos.saturating_add(length);
    }
    Ok(())
}

/// __parse_cover: only its fatal errors matter here
fn cover(alen: u64, data: &[u8]) -> R<()> {
    let mut pos = 0u64;
    while (pos as i128) < alen as i128 - 8 {
        let h = slice(data, pos, 12);
        if h.len() < 12 {
            return Err(fatal());
        }
        let length = be32(h);
        if &h[4..8] != b"data" {
            if &h[4..8] == b"name" {
                if length == 0 {
                    return err("mutagen loops forever on a zero-length 'name' inside 'covr'");
                }
                pos += length;
                continue;
            }
            return Ok(());
        }
        if length < 1 {
            return Ok(());
        }
        pos += length;
    }
    Ok(())
}

/// MP4Chapters.load: only its errors matter here
fn chapters(atoms: &Atoms, f: &mut PyFile) -> R<()> {
    let (mvhd, chpl) = match (atoms.get(&[b"moov", b"udta", b"chpl"]), atoms.get(&[b"moov", b"mvhd"])) {
        (Some(c), Some(m)) => (m, c),
        _ => return Ok(()),
    };
    let (ok, d) = mvhd.read(f)?;
    if !ok {
        return err("Invalid mvhd");
    }
    let version = match d.first() {
        Some(&v) => v,
        None => return err("index out of range"),
    };
    let timescale = match version {
        0 if d.len() >= 20 => be32(&d[12..16]),
        1 if d.len() >= 32 => be32(&d[20..24]),
        0 | 1 => return Err(fatal()),
        _ => 0,
    };
    if timescale == 0 {
        return err("Unable to get timescale");
    }
    let (ok, d) = chpl.read(f)?;
    if !ok {
        return err("Invalid atom");
    }
    let count = match d.get(8) {
        Some(&c) => c,
        None => return err("index out of range"),
    };
    let mut pos = 9usize;
    for _ in 0..count {
        if d.len() < pos + 8 {
            return Err(fatal());
        }
        pos += 8;
        let tl = match d.get(pos) {
            Some(&t) => t as usize,
            None => return err("index out of range"),
        };
        pos += 1;
        let end = (pos + tl).min(d.len());
        if std::str::from_utf8(&d[pos.min(d.len())..end]).is_err() {
            return err("chapter title is not UTF-8");
        }
        pos += tl;
    }
    Ok(())
}

pub fn load(f: &mut PyFile) -> R<Loaded> {
    let end = f.size();
    f.seek(0);
    let mut atoms = Vec::new();
    while f.tell() + 8 <= end {
        atoms.push(Atom::parse(f, 0)?);
    }
    let atoms = Atoms(atoms);
    let info = mp4_info(&atoms, f)?;
    let tags = match atoms.get(&[b"moov", b"udta", b"meta", b"ilst"]) {
        Some(ilst) => Some(mp4_tags(ilst, f)?),
        None => None,
    };
    chapters(&atoms, f)?;
    let easy = match &tags {
        None => EasyTags::default(),
        Some(t) => {
            let trkn: Option<Vec<String>> = t.has_trkn.then(|| {
                t.trkn.iter().map(|&(n, tot)| if tot != 0 { format!("{}/{}", n, tot) } else { n.to_string() }).collect()
            });
            EasyTags {
                title: first(t.get(b"\xa9nam")),
                artist: first(t.get(b"\xa9ART")),
                album: first(t.get(b"\xa9alb")),
                genre: first(t.get(b"\xa9gen")),
                composer: None,
                tracknumber: first(trkn.as_ref()),
                date: first(t.get(b"\xa9day")),
            }
        }
    };
    Ok(Loaded { tags: easy, length: info.length, bitrate: info.bitrate, sample_rate: info.sample_rate })
}
