//! Python's string behaviour where the reference depends on it.

/// `str.casefold()`: full Unicode case folding.
pub fn casefold(s: &str) -> String {
    caseless::default_case_fold_str(s)
}

/// `bytes.decode("latin-1")`.
pub(crate) fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

/// `bytes.decode("utf-16-le", "replace")`: lone surrogates and a trailing
/// odd byte each become U+FFFD.
pub(crate) fn utf16le_replace(b: &[u8]) -> String {
    let units: Vec<u16> = b.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
    let mut s = String::from_utf16_lossy(&units);
    if b.len() % 2 == 1 {
        s.push('\u{FFFD}');
    }
    s
}

/// `str.rstrip()` (Python's whitespace set, which isn't Rust's).
pub(crate) fn py_rstrip(s: &str) -> &str {
    s.trim_end_matches(|c: char| {
        matches!(c, '\t' | '\n' | '\u{b}' | '\u{c}' | '\r' | '\u{1c}'..='\u{1f}' | ' ' | '\u{85}' | '\u{a0}')
            || (c as u32 > 0xff && c.is_whitespace())
    })
}

/// `struct.unpack_from("<I", b, off)`, with struct.error's message.
pub(crate) fn u32_at(b: &[u8], off: usize) -> Result<u32, String> {
    if off + 4 > b.len() {
        return Err(unpack_err(4, off, b.len()));
    }
    Ok(u32::from_le_bytes(b[off..off + 4].try_into().unwrap()))
}

pub(crate) fn u64_at(b: &[u8], off: usize) -> Result<u64, String> {
    if off + 8 > b.len() {
        return Err(unpack_err(8, off, b.len()));
    }
    Ok(u64::from_le_bytes(b[off..off + 8].try_into().unwrap()))
}

pub(crate) fn u16_at(b: &[u8], off: usize) -> Result<u16, String> {
    if off + 2 > b.len() {
        return Err(unpack_err(2, off, b.len()));
    }
    Ok(u16::from_le_bytes(b[off..off + 2].try_into().unwrap()))
}

fn unpack_err(size: usize, off: usize, len: usize) -> String {
    format!(
        "unpack_from requires a buffer of at least {} bytes for unpacking {} bytes at offset {} (actual buffer size is {})",
        off + size, size, off, len
    )
}

/// Python's `buf[a:b]`, clamped.
pub(crate) fn slice(b: &[u8], a: usize, end: usize) -> &[u8] {
    let a = a.min(b.len());
    let end = end.min(b.len()).max(a);
    &b[a..end]
}

/// Python's repr() of a str, for the messages that quote a path.
pub fn py_repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::new();
    out.push(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_behaviour() {
        assert_eq!(utf16le_replace(b"\x41\x00\x42"), "A\u{FFFD}");
        assert_eq!(utf16le_replace(b"\x00\xd8\x41\x00"), "\u{FFFD}A");
        assert_eq!(casefold("ß"), "ss");
        assert_eq!(casefold("ﬁ"), "fi");
        assert_eq!(py_rstrip("IPOD   \u{a0}"), "IPOD");
        assert_eq!(py_rstrip("X\0 "), "X\0");
        assert_eq!(u32_at(b"abc", 2).unwrap_err(),
                   "unpack_from requires a buffer of at least 6 bytes for unpacking 4 bytes at offset 2 (actual buffer size is 3)");
    }
}
