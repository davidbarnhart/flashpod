//! Reading the firmware on a card: firmware.describe_installed with its
//! helpers (walk_directory, fixes_present, aupd_armed). The catalog and the
//! fixes table are the same JSON files the Python package ships, embedded.

use crate::layout::{FW_BLOCKS, SECTOR};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub const CATALOG_JSON: &str = include_str!("../../../flashpod/firmware/firmware.json");
pub const FIXES_JSON: &str = include_str!("../../../flashpod/firmware/fixes.json");

pub const PP5002_OSOS_ADDR: u32 = 0x2800_0000;
pub const VERSION_WORD_OFFSET: usize = 0x3174c;

pub fn version_word_means(w: u32) -> Option<&'static str> {
    match w {
        0x0150_8000 => Some("1.5"),
        0x0160_8000 => Some("1.6"),
        _ => None,
    }
}

pub fn known_osos(sha: &str) -> Option<&'static str> {
    match sha {
        "109ed777110982e2a31d7f724e4a7f78b73bef17bee90952e2c35c79f0714e21" => Some("openpod 1.6 gate build (withdrawn)"),
        _ => None,
    }
}

pub fn known_aupd(sha: &str) -> Option<&'static str> {
    match sha {
        "dc68f0fc5d9aae12a30a293507969821915b432d07e1e6b83ccf861650a656fb" => {
            Some("flashpod 1.6 (openpod's disk-mode program, with large disks)")
        }
        "b32e141d98ad7c0cb94a1829764a491fdbd8980c86982610c2663700ab85b130" => Some("Apple 1.5"),
        _ => None,
    }
}

pub fn catalog() -> Vec<Value> {
    serde_json::from_str::<Value>(CATALOG_JSON)
        .ok()
        .and_then(|v| v.get("firmwares").cloned())
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
}

pub fn fixes_table() -> Option<Value> {
    serde_json::from_str(FIXES_JSON).ok()
}

pub fn sha256_hex(b: &[u8]) -> String {
    let d = Sha256::digest(b);
    d.iter().map(|x| format!("{:02x}", x)).collect()
}

/// firmware.walk_directory: (entry offset, type, 8 fields).
pub fn walk_directory(fw: &[u8], base: usize) -> Vec<(usize, [u8; 4], [u32; 8])> {
    let mut out = Vec::new();
    let mut off = base;
    while off < base + 10 * 40 {
        if off + 40 > fw.len() || fw[off..off + 4] == [0, 0, 0, 0] {
            break;
        }
        let typ = [fw[off + 7], fw[off + 6], fw[off + 5], fw[off + 4]];
        let mut f = [0u32; 8];
        for (k, v) in f.iter_mut().enumerate() {
            let o = off + 8 + 4 * k;
            *v = u32::from_le_bytes(fw[o..o + 4].try_into().unwrap());
        }
        out.push((off, typ, f));
        off += 40;
    }
    out
}

/// firmware.aupd_armed.
pub fn aupd_armed(fw: &[u8]) -> Option<bool> {
    if fw.len() < 0x108 || &fw[0x100..0x104] != b"]ih[" {
        return None;
    }
    let ptr = u32::from_le_bytes(fw[0x104..0x108].try_into().unwrap()) as usize;
    let mut ids = Vec::new();
    for base in [ptr, ptr + 0x200] {
        for (_o, typ, f) in walk_directory(fw, base) {
            if &typ == b"aupd" {
                ids.push(f[0]);
            }
        }
    }
    if ids.len() != 2 || (ids[0] == 0) != (ids[1] == 0) {
        return None;
    }
    Some(ids[0] == 0)
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len() / 2).map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap_or(0)).collect()
}

fn parse_addr(s: &str) -> i64 {
    i64::from_str_radix(s.trim_start_matches("0x"), 16).unwrap_or(0)
}

/// firmware.fixes_present: names of the fixes whose edits and appended code
/// are all there. `aupd` None means "not read".
pub fn fixes_present(osos: &[u8], addr: u32, table: &Value, aupd: Option<(&[u8], u32)>) -> Vec<String> {
    let mut found = Vec::new();
    for fix in table.get("fixes").and_then(Value::as_array).cloned().unwrap_or_default() {
        let mut ok = true;
        for e in fix.get("edits").and_then(Value::as_array).cloned().unwrap_or_default() {
            let image = e.get("image").and_then(Value::as_str).unwrap_or("osos");
            let (data, base) = match image {
                "osos" => (osos, addr),
                "aupd" => match aupd {
                    Some(a) => a,
                    None => {
                        ok = false;
                        break;
                    }
                },
                _ => {
                    ok = false;
                    break;
                }
            };
            let off = parse_addr(e["at"].as_str().unwrap_or("0")) - base as i64;
            let new = hex(e["patched"].as_str().unwrap_or(""));
            if !(off >= 0 && off as usize + new.len() <= data.len())
                || data[off as usize..off as usize + new.len()] != new[..]
            {
                ok = false;
                break;
            }
        }
        if ok {
            if let Some(ap) = fix.get("append").filter(|a| !a.is_null()) {
                let off = parse_addr(ap["at"].as_str().unwrap_or("0")) - addr as i64;
                let n = ap["bytes"].as_u64().unwrap_or(0) as usize;
                let blob: &[u8] = if off >= 0 {
                    crate::text::slice(osos, off as usize, off as usize + n)
                } else {
                    &[]
                };
                ok = blob.len() == n && sha256_hex(blob) == ap["sha256"].as_str().unwrap_or("");
            }
        }
        if ok {
            found.push(fix["name"].as_str().unwrap_or("").to_string());
        }
    }
    found
}

/// firmware.describe_installed. `read(offset, n)` reads the firmware
/// partition (short at its end, empty past it).
pub fn describe_installed(read: &mut dyn FnMut(i64, usize) -> Vec<u8>, catalog: &[Value]) -> Value {
    if read(0, 4) != b"{{~~" {
        return json!({"present": false, "reason": "no STOP boot block"});
    }
    let hdr = read(0x100, 0x10);
    if hdr.len() < 4 || &hdr[0..4] != b"]ih[" {
        return json!({"present": false, "reason": "no [hi] volume header"});
    }
    let ptr = u32::from_le_bytes(hdr[4..8].try_into().unwrap()) as i64;
    let fmtver = u16::from_le_bytes(hdr[0x0a..0x0c].try_into().unwrap());
    let shift: i64 = if fmtver == 3 { 0x200 } else { 0 };
    let dir = read(ptr + 0x200, 10 * 40);
    let mut images = Vec::new();
    let mut osos: Option<[u32; 8]> = None;
    let mut aupd: Option<[u32; 8]> = None;
    for (_o, typ, f) in walk_directory(&dir, 0) {
        images.push(json!({"type": crate::text::latin1(&typ), "id": f[0], "dev_offset": f[1],
                           "length": f[2], "addr": f[3], "checksum": f[5], "vers": f[6],
                           "load_addr": f[7]}));
        if &typ == b"osos" && osos.is_none() {
            osos = Some(f);
        }
        if &typ == b"aupd" && aupd.is_none() {
            aupd = Some(f);
        }
    }
    let mut out = Map::new();
    out.insert("present".into(), json!(true));
    out.insert("format".into(), json!(fmtver));
    out.insert("images".into(), Value::Array(images));
    let o = match osos {
        None => {
            out.insert("present".into(), json!(false));
            out.insert("reason".into(), json!("no osos image in the directory"));
            return Value::Object(out);
        }
        Some(o) => o,
    };
    if o[2] as u64 > FW_BLOCKS * SECTOR {
        out.insert("present".into(), json!(false));
        out.insert("reason".into(), json!("osos length past the partition"));
        return Value::Object(out);
    }
    let payload = read(o[1] as i64 + shift, o[2] as usize);
    let digest = sha256_hex(&payload);
    let sum: u64 = payload.iter().map(|&b| b as u64).sum();
    out.insert("osos".into(), json!({"checksum_ok": (sum & 0xFFFF_FFFF) as u32 == o[5], "sha256": digest,
                                     "length": o[2], "addr": o[3]}));
    let mut word = None;
    if o[3] == PP5002_OSOS_ADDR && payload.len() >= VERSION_WORD_OFFSET + 4 {
        word = Some(u32::from_le_bytes(payload[VERSION_WORD_OFFSET..VERSION_WORD_OFFSET + 4].try_into().unwrap()));
    }
    let means = word.and_then(version_word_means);
    out.insert("version_word".into(), match (word, means) {
        (Some(w), Some(_)) => json!(format!("0x{:08x}", w)),
        _ => Value::Null,
    });
    out.insert("version_word_means".into(), json!(means));
    let m = catalog.iter().find(|e| e.get("osos_sha256").and_then(Value::as_str) == Some(digest.as_str()));
    if let Some(e) = m {
        let maker = e.get("maker").and_then(Value::as_str).unwrap_or("apple");
        let version = e.get("version").and_then(Value::as_str).unwrap_or("?");
        out.insert("build".into(), json!(format!("{} {}", if maker == "apple" { "stock" } else { maker }, version)));
        out.insert("maker".into(), json!(maker));
        out.insert("catalog_file".into(), e.get("file").cloned().unwrap_or(Value::Null));
        out.insert("models".into(), e.get("models").cloned().unwrap_or(json!([])));
    } else if let Some(label) = known_osos(&digest) {
        out.insert("build".into(), json!(label));
        out.insert("catalog_file".into(), Value::Null);
        out.insert("maker".into(), Value::Null);
    } else {
        out.insert("build".into(), json!("unrecognized"));
        out.insert("catalog_file".into(), Value::Null);
        out.insert("maker".into(), Value::Null);
    }
    let mut aupd_code: Option<(Vec<u8>, u32)> = None;
    if let Some(a) = aupd {
        if a[2] > 0 && a[2] as u64 <= FW_BLOCKS * SECTOR {
            let blob = read(a[1] as i64 + shift, a[2] as usize);
            if blob.len() == a[2] as usize {
                let d = sha256_hex(&blob);
                let head = read(0, (ptr + 0x200 + 400) as usize);
                out.insert("aupd".into(), json!({"sha256": d, "length": a[2], "addr": a[3],
                                                 "known": known_aupd(&d), "armed": aupd_armed(&head)}));
                aupd_code = Some((blob, a[3]));
            }
        }
    }
    let fixes = if o[3] == PP5002_OSOS_ADDR {
        match fixes_table() {
            Some(t) => json!(fixes_present(&payload, o[3], &t, aupd_code.as_ref().map(|(b, ad)| (b.as_slice(), *ad)))),
            None => Value::Null,
        }
    } else {
        Value::Null
    };
    out.insert("fixes".into(), fixes);
    Value::Object(out)
}

// ---------------------------------------------------------------------------
// Loading firmware for a flash: firmware.load_firmware, fixup_directories,
// validate_firmware, arm_firmware. Err strings are the Python ValueError
// messages (card wraps them as FirmwareInvalid "firmware error: ...").
// ---------------------------------------------------------------------------

/// The Firmware-* image inside raw bytes / a .bin.gz / an .ipsw (zip),
/// by magic bytes, as load_firmware does (before the fix-ups).
pub fn unwrap_container(data: &[u8]) -> Result<Vec<u8>, String> {
    use std::io::Read;
    if data.starts_with(b"PK") {
        let mut zf = zip::ZipArchive::new(std::io::Cursor::new(data)).map_err(|e| e.to_string())?;
        let name = zf
            .file_names()
            .find(|n| n.rsplit('/').next().unwrap_or("").starts_with("Firmware"))
            .map(String::from)
            .ok_or_else(String::new)?; // Python: StopIteration, whose str() is ""
        let mut f = zf.by_name(&name).map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        f.read_to_end(&mut out).map_err(|e| e.to_string())?;
        Ok(out)
    } else if data.starts_with(b"\x1f\x8b") {
        let mut out = Vec::new();
        flate2::read::MultiGzDecoder::new(data).read_to_end(&mut out).map_err(|e| e.to_string())?;
        Ok(out)
    } else {
        Ok(data.to_vec())
    }
}

fn ptr_of(fw: &[u8]) -> usize {
    u32::from_le_bytes(fw[0x104..0x108].try_into().unwrap()) as usize
}

/// firmware.fixup_directories: osos-and-others load address to 0xFFFFFFFF,
/// aupd id 1 ("performed") and its load address its RAM address, in both
/// directories. Not a firmware volume: unchanged.
pub fn fixup_directories(fw: &[u8]) -> Vec<u8> {
    let mut fw = fw.to_vec();
    if fw.len() < 0x108 || &fw[0x100..0x104] != b"]ih[" {
        return fw;
    }
    let ptr = ptr_of(&fw);
    for base in [ptr, ptr + 0x200] {
        for (off, typ, f) in walk_directory(&fw.clone(), base) {
            if &typ == b"aupd" {
                fw[off + 8..off + 12].copy_from_slice(&1u32.to_le_bytes());
                fw[off + 36..off + 40].copy_from_slice(&f[3].to_le_bytes());
            } else {
                fw[off + 36..off + 40].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
            }
        }
    }
    fw
}

/// firmware.validate_firmware.
pub fn validate_firmware(fw: &[u8]) -> Result<(), String> {
    if fw.len() < 4 || &fw[0..4] != b"{{~~" {
        return Err("not an iPod firmware image (missing STOP boot block)".into());
    }
    if fw.len() < 0x104 || &fw[0x100..0x104] != b"]ih[" {
        return Err("missing [hi] volume header at 0x100".into());
    }
    let ptr = ptr_of(fw);
    let fmtver = u16::from_le_bytes(fw[0x10a..0x10c].try_into().unwrap());
    let shift = if fmtver == 3 { 0x200 } else { 0 };
    let mut found = false;
    for (_off, typ, f) in walk_directory(fw, ptr + 0x200) {
        if &typ == b"osos" {
            found = true;
            let (dev, len, cksum, load) = (f[1] as usize, f[2] as usize, f[5], f[7]);
            let got: u64 = crate::text::slice(fw, dev + shift, dev + shift + len).iter().map(|&b| b as u64).sum();
            let got = (got & 0xFFFF_FFFF) as u32;
            if got != cksum {
                return Err(format!("osos checksum mismatch: stored 0x{:08x} got 0x{:08x}", cksum, got));
            }
            if load != 0xFFFF_FFFF {
                return Err(format!("osos loadAddr 0x{:08x} not fixed up (install-state must be 0xFFFFFFFF)", load));
            }
        }
    }
    if !found {
        return Err("no osos image in the firmware directory".into());
    }
    if fw.len() as u64 > FW_BLOCKS * SECTOR {
        return Err(format!("firmware ({} bytes) larger than the 32 MiB firmware partition", fw.len()));
    }
    Ok(())
}

/// firmware.load_firmware on bytes: unwrap, fix up, validate.
pub fn load_firmware(data: &[u8]) -> Result<Vec<u8>, String> {
    let fw = fixup_directories(&unwrap_container(data)?);
    validate_firmware(&fw)?;
    Ok(fw)
}

/// firmware.aupd_of: (sha256, known label) of the live directory's aupd.
pub fn aupd_of(fw: &[u8]) -> Option<(String, Option<&'static str>)> {
    if fw.len() < 0x10c || &fw[0x100..0x104] != b"]ih[" {
        return None;
    }
    let ptr = ptr_of(fw);
    let fmtver = u16::from_le_bytes(fw[0x10a..0x10c].try_into().unwrap());
    let shift = if fmtver == 3 { 0x200 } else { 0 };
    for (_o, typ, f) in walk_directory(fw, ptr + 0x200) {
        if &typ == b"aupd" {
            let blob = crate::text::slice(fw, f[1] as usize + shift, f[1] as usize + shift + f[2] as usize);
            if blob.len() != f[2] as usize {
                return None;
            }
            let d = sha256_hex(blob);
            let k = known_aupd(&d);
            return Some((d, k));
        }
    }
    None
}

/// firmware.arm_firmware: the aupd id back to 0 in both directories,
/// refused unless the program is a known one.
pub fn arm_firmware(fw: &[u8]) -> Result<Vec<u8>, String> {
    let (sha, known) = aupd_of(fw).ok_or("this firmware has no disk-mode program (aupd) to arm")?;
    if known.is_none() {
        return Err(format!(
            "this firmware's disk-mode program (aupd sha256 {}) isn't one flashpod knows is safe to arm", sha));
    }
    let mut out = fw.to_vec();
    let ptr = ptr_of(fw);
    let mut n = 0;
    for base in [ptr, ptr + 0x200] {
        for (off, typ, _f) in walk_directory(fw, base) {
            if &typ == b"aupd" {
                out[off + 8..off + 12].copy_from_slice(&0u32.to_le_bytes());
                n += 1;
            }
        }
    }
    if n != 2 {
        return Err(format!("expected an aupd entry in both directories, found {}", n));
    }
    Ok(out)
}

/// firmware.fix_on_card: does the firmware read by `read(offset, n)` carry
/// fix `name`? None when it can't be told (no firmware volume, no osos, no
/// such fix in the table, or a read failed).
pub fn fix_on_card(read: &mut dyn FnMut(u64, usize) -> Option<Vec<u8>>, name: &str) -> Option<bool> {
    let table = fixes_table()?;
    let fix = table.get("fixes")?.as_array()?.iter().find(|f| f["name"] == name)?.clone();
    if read(0, 4)? != b"{{~~" {
        return None;
    }
    let hdr = read(0x100, 0x10)?;
    if hdr.len() < 0x0c || &hdr[0..4] != b"]ih[" {
        return None;
    }
    let ptr = u32::from_le_bytes(hdr[4..8].try_into().unwrap()) as u64;
    let fmtver = u16::from_le_bytes(hdr[0x0a..0x0c].try_into().unwrap());
    let shift = if fmtver == 3 { 0x200 } else { 0 };
    let dir = read(ptr + 0x200, 400)?;
    let mut osos: Option<(Vec<u8>, u32)> = None;
    let mut aupd: Option<(Vec<u8>, u32)> = None;
    for (_o, typ, f) in walk_directory(&dir, 0) {
        if f[2] as u64 > FW_BLOCKS * SECTOR {
            continue;
        }
        if &typ == b"osos" && osos.is_none() {
            osos = Some((read(f[1] as u64 + shift, f[2] as usize)?, f[3]));
        } else if &typ == b"aupd" && aupd.is_none() {
            aupd = Some((read(f[1] as u64 + shift, f[2] as usize)?, f[3]));
        }
    }
    let (o, addr) = osos?;
    let one = serde_json::json!({"fixes": [fix]});
    let found = fixes_present(&o, addr, &one, aupd.as_ref().map(|(b, a)| (b.as_slice(), *a)));
    Some(found.iter().any(|n| n == name))
}
