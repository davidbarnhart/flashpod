//! Terminal helpers that behave like the Python CLI's: ipod_flash.color
//! (ANSI only when stderr is a terminal), input(), ask_yes,
//! shutil.get_terminal_size and the cbreak key reader of _select_cursor.

use std::io::{BufRead, IsTerminal, Write};

pub const C_RED: &str = "\x1b[31m";
pub const C_YEL: &str = "\x1b[33m";
pub const C_GRN: &str = "\x1b[32m";
pub const C_CYN: &str = "\x1b[36m";
pub const C_DIM: &str = "\x1b[2m";
pub const C_RST: &str = "\x1b[0m";

pub fn color(s: &str, c: &str) -> String {
    if std::io::stderr().is_terminal() {
        format!("{}{}{}", c, s, C_RST)
    } else {
        s.to_string()
    }
}

pub fn stdin_isatty() -> bool {
    std::io::stdin().is_terminal()
}

pub fn stderr_isatty() -> bool {
    std::io::stderr().is_terminal()
}

pub fn stdout_isatty() -> bool {
    std::io::stdout().is_terminal()
}

/// Python's input(prompt): the prompt on stdout, one line back without its
/// newline; None at end of input.
pub fn input_line(prompt: &str) -> Option<String> {
    print!("{}", prompt);
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            if line.ends_with('\n') {
                line.pop();
                if line.ends_with('\r') {
                    line.pop();
                }
            }
            Some(line)
        }
    }
}

/// cli.ask_yes: with `default_yes` an empty answer is yes; otherwise only an
/// explicit yes counts. End of input is always no.
pub fn ask_yes(prompt: &str, default_yes: bool) -> bool {
    let ans = match input_line(prompt) {
        Some(a) => a.trim().to_lowercase(),
        None => {
            println!();
            return false;
        }
    };
    if ans.is_empty() && default_yes {
        return true;
    }
    matches!(ans.as_str(), "y" | "yes" | "aye" | "yea" | "arr")
}

/// shutil.get_terminal_size((80, 24)): COLUMNS / LINES when set, else the
/// size of the terminal on stdout, else the fallback.
pub fn terminal_size() -> (usize, usize) {
    let env = |k: &str| std::env::var(k).ok().and_then(|v| v.parse::<usize>().ok()).unwrap_or(0);
    let (mut cols, mut lines) = (env("COLUMNS"), env("LINES"));
    if cols == 0 || lines == 0 {
        let (c, l) = crate::sys::terminal_size().unwrap_or((80, 24));
        if cols == 0 {
            cols = if c > 0 { c } else { 80 };
        }
        if lines == 0 {
            lines = if l > 0 { l } else { 24 };
        }
    }
    (cols, lines)
}

/// stdin in cbreak mode (no echo, no line editing) until dropped. Signals
/// are off too, so ^C arrives as a key (Python's KeyboardInterrupt there
/// cancels the selector the same way).
pub struct Cbreak(#[allow(dead_code)] crate::sys::RawInput);

impl Cbreak {
    pub fn new() -> Option<Self> {
        crate::sys::RawInput::new().ok().map(Cbreak)
    }
}

fn read_byte(n: usize) -> Vec<u8> {
    let mut buf = vec![0u8; n];
    let got = crate::sys::read_stdin(&mut buf).unwrap_or(0);
    buf.truncate(got);
    buf
}

/// cli._read_key: one keypress, normalized; arrow escapes decoded, a lone
/// Esc is "cancel".
pub fn read_key() -> Option<&'static str> {
    let ch = read_byte(1);
    match ch.as_slice() {
        [] => Some("cancel"),
        [0x1b] => {
            if !crate::sys::stdin_ready(50) {
                return Some("cancel");
            }
            match read_byte(2).as_slice() {
                b"[A" => Some("up"),
                b"[B" => Some("down"),
                _ => None,
            }
        }
        [b'\r'] | [b'\n'] => Some("accept"),
        [b' '] => Some("space"),
        [0x03] => Some("cancel"),
        [c] => match c.to_ascii_lowercase() {
            b'k' => Some("up"),
            b'j' => Some("down"),
            b'a' => Some("all"),
            b'n' => Some("none"),
            b'q' => Some("cancel"),
            _ => None,
        },
        _ => None,
    }
}
