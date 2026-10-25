//! Terminal helpers that behave like the Python CLI's: ipod_flash.color
//! (ANSI only when stderr is a terminal) and input().

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
