//! Python's text rules where mutagen leans on them: str.isdecimal, int(),
//! regex `\s`, the latin-1 / UTF-8 / UTF-16 codecs (strict, and
//! decode_terminated's incremental reading).

use super::tables::{DECIMAL_ZEROS, PY_SPACE};

/// The value of a Unicode decimal digit (str.isdecimal), else None.
pub fn decimal_value(c: char) -> Option<u32> {
    let cp = c as u32;
    let i = DECIMAL_ZEROS.partition_point(|&z| z <= cp);
    if i == 0 {
        return None;
    }
    let z = DECIMAL_ZEROS[i - 1];
    (cp - z < 10).then(|| cp - z)
}

pub fn is_decimal_str(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| decimal_value(c).is_some())
}

/// str.isspace / regex `\s`.
pub fn is_space(c: char) -> bool {
    PY_SPACE.binary_search(&(c as u32)).is_ok()
}

/// CPython refuses int() of a string with more digits than this.
const MAX_STR_DIGITS: usize = 4300;

/// int(s) for base 10, as a normalized decimal string ("-12", "0"); None
/// where Python raises ValueError. Leading/trailing whitespace is stripped
/// as int() does (str.isspace minus U+001C..U+001F).
pub fn py_int(s: &str) -> Option<String> {
    let int_space = |c: char| is_space(c) && !('\u{1c}'..='\u{1f}').contains(&c);
    let s = s.trim_matches(int_space);
    let (neg, body) = match s.chars().next() {
        Some('-') => (true, &s[1..]),
        Some('+') => (false, &s[1..]),
        _ => (false, s),
    };
    let mut digits = String::new();
    let mut prev_digit = false;
    for c in body.chars() {
        if c == '_' {
            if !prev_digit {
                return None;
            }
            prev_digit = false;
        } else if let Some(v) = decimal_value(c) {
            digits.push(char::from(b'0' + v as u8));
            prev_digit = true;
        } else {
            return None;
        }
    }
    if !prev_digit || digits.len() > MAX_STR_DIGITS {
        return None;
    }
    let t = digits.trim_start_matches('0');
    Some(if t.is_empty() {
        "0".to_string()
    } else if neg {
        format!("-{}", t)
    } else {
        t.to_string()
    })
}

/// int(s) < n for a string that int() accepts; Err where int() raises.
pub fn int_less_than(s: &str, n: u64) -> Result<bool, ()> {
    let v = py_int(s).ok_or(())?;
    if v.starts_with('-') {
        return Ok(true);
    }
    Ok(v.len() < 20 && v.parse::<u64>().map(|x| x < n).unwrap_or(false))
}

/// '%0Nd' % int(s) for a normalized decimal string.
pub fn zero_pad(v: &str, width: usize) -> String {
    match v.strip_prefix('-') {
        Some(d) => format!("-{:0>w$}", d, w = width.saturating_sub(1)),
        None => format!("{:0>w$}", v, w = width),
    }
}

pub fn latin1(b: &[u8]) -> String {
    b.iter().map(|&c| c as char).collect()
}

/// bytes.decode('ascii', 'replace')
pub fn ascii_replace(b: &[u8]) -> String {
    b.iter().map(|&c| if c < 0x80 { c as char } else { '\u{fffd}' }).collect()
}

/// bytes.strip() / rstrip(): ASCII whitespace.
pub fn bytes_strip(b: &[u8]) -> &[u8] {
    let ws = |c: &u8| matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c);
    let start = b.iter().position(|c| !ws(c)).unwrap_or(b.len());
    let end = b.iter().rposition(|c| !ws(c)).map(|i| i + 1).unwrap_or(start);
    &b[start..end.max(start)]
}

#[derive(Clone, Copy, PartialEq)]
pub enum Codec {
    Latin1,
    /// 'utf16': a BOM picks the byte order, else little-endian (native)
    Utf16,
    Utf16Be,
    Utf8,
}

/// mutagen._util.decode_terminated(data, codec, strict=False): the text up
/// to the first NUL character and the bytes after it; Err where the codec
/// raises.
pub fn decode_terminated(data: &[u8], codec: Codec) -> Result<(String, Vec<u8>), ()> {
    match codec {
        Codec::Latin1 => Ok(match data.iter().position(|&b| b == 0) {
            Some(i) => (latin1(&data[..i]), data[i + 1..].to_vec()),
            None => (latin1(data), Vec::new()),
        }),
        Codec::Utf8 => {
            let (head, rest) = match data.iter().position(|&b| b == 0) {
                Some(i) => (&data[..i], data[i + 1..].to_vec()),
                None => (data, Vec::new()),
            };
            let s = std::str::from_utf8(head).map_err(|_| ())?;
            Ok((s.to_string(), rest))
        }
        Codec::Utf16 | Codec::Utf16Be => {
            let mut i = 0usize;
            let mut le = codec == Codec::Utf16;
            if codec == Codec::Utf16 && data.len() >= 2 {
                if data[..2] == [0xff, 0xfe] {
                    i = 2;
                } else if data[..2] == [0xfe, 0xff] {
                    le = false;
                    i = 2;
                }
            }
            let unit = |j: usize| -> u16 {
                if le {
                    u16::from_le_bytes([data[j], data[j + 1]])
                } else {
                    u16::from_be_bytes([data[j], data[j + 1]])
                }
            };
            let mut out = String::new();
            while i + 1 < data.len() {
                let u = unit(i);
                i += 2;
                if (0xd800..0xdc00).contains(&u) {
                    if i + 1 >= data.len() {
                        return Err(()); // truncated surrogate pair at the end
                    }
                    let u2 = unit(i);
                    if !(0xdc00..0xe000).contains(&u2) {
                        return Err(());
                    }
                    i += 2;
                    let c = 0x10000 + (((u as u32) - 0xd800) << 10) + ((u2 as u32) - 0xdc00);
                    out.push(char::from_u32(c).unwrap());
                } else if (0xdc00..0xe000).contains(&u) {
                    return Err(());
                } else if u == 0 {
                    return Ok((out, data[i..].to_vec()));
                } else {
                    out.push(char::from_u32(u as u32).unwrap());
                }
            }
            if i < data.len() {
                return Err(()); // a dangling odd byte: "truncated data"
            }
            Ok((out, Vec::new()))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ints() {
        assert_eq!(py_int("007").as_deref(), Some("7"));
        assert_eq!(py_int("+5").as_deref(), Some("5"));
        assert_eq!(py_int("٣_4").as_deref(), Some("34"));
        assert_eq!(py_int("5_"), None);
        assert_eq!(py_int("_5"), None);
        assert_eq!(py_int(""), None);
        assert_eq!(py_int("\u{1c}5"), None);
        assert_eq!(py_int(" 5\t").as_deref(), Some("5"));
        assert_eq!(zero_pad("7", 4), "0007");
        assert_eq!(zero_pad("12345", 4), "12345");
    }

    #[test]
    fn utf16() {
        let d = [0xff, 0xfe, b'a', 0, 0, 0, b'b', 0];
        assert_eq!(decode_terminated(&d, Codec::Utf16), Ok(("a".into(), vec![b'b', 0])));
        assert_eq!(decode_terminated(&[b'a', 0, b'b'], Codec::Utf16), Err(()));
        assert_eq!(decode_terminated(&[0, b'a', 0, 0], Codec::Utf16Be), Ok(("a".into(), vec![])));
    }
}
