//! cli.LineWindow and cli.EtaEstimator, and the monotonic clock they read.

use crate::term;
use std::collections::VecDeque;
use std::io::Write;

/// time.monotonic. Frozen at 1000.0 when FLASHPOD_NOW pins the clock (the
/// test harness freezes Python's at that value); otherwise counting up from
/// 1000.0 at the first call, so a throttle seeded at 0.0 lets its first tick
/// through as Python's does.
pub fn monotonic() -> f64 {
    static START: std::sync::OnceLock<Option<std::time::Instant>> = std::sync::OnceLock::new();
    let start = START.get_or_init(|| {
        if std::env::var_os("FLASHPOD_NOW").is_some() { None } else { Some(std::time::Instant::now()) }
    });
    1000.0 + start.map_or(0.0, |t| t.elapsed().as_secs_f64())
}

fn clip(s: &str, width: usize) -> String {
    s.chars().take(width.saturating_sub(1).max(1)).collect()
}

/// Scrolling n-line status window: add() lines roll through it, note()
/// lines persist above it. Off a terminal add() prints to stdout, note() to
/// stderr, and progress (update, set_status, transient adds) is dropped.
pub struct LineWindow {
    lines: VecDeque<String>,
    size: usize,
    drawn: usize,
    status: Option<String>,
    tty: bool,
}

impl LineWindow {
    pub fn new() -> Self {
        LineWindow { lines: VecDeque::new(), size: 4, drawn: 0, status: None, tty: term::stdout_isatty() }
    }

    fn erase(&mut self) {
        if self.drawn > 0 {
            print!("\x1b[{}F\x1b[J", self.drawn);
            self.drawn = 0;
        }
    }

    fn draw(&mut self) {
        let width = term::terminal_size().0;
        let mut out = std::io::stdout().lock();
        for line in &self.lines {
            let _ = writeln!(out, "{}", clip(line, width));
        }
        if let Some(s) = &self.status {
            let _ = writeln!(out, "{}", clip(s, width));
        }
        self.drawn = self.lines.len() + usize::from(self.status.is_some());
        let _ = out.flush();
    }

    pub fn set_status(&mut self, line: Option<String>) {
        if !self.tty || line == self.status {
            return;
        }
        self.erase();
        self.status = line;
        self.draw();
    }

    pub fn add(&mut self, line: String, transient: bool) {
        if !self.tty {
            if !transient {
                println!("{}", line);
                let _ = std::io::stdout().flush();
            }
            return;
        }
        self.erase();
        if self.lines.len() == self.size {
            self.lines.pop_front();
        }
        self.lines.push_back(line);
        self.draw();
    }

    pub fn update(&mut self, line: String) {
        if !self.tty {
            return;
        }
        if self.lines.is_empty() {
            self.add(line, false);
            return;
        }
        self.erase();
        *self.lines.back_mut().unwrap() = line;
        self.draw();
    }

    pub fn note(&mut self, line: &str) {
        if !self.tty {
            eprintln!("{}", line);
            return;
        }
        self.erase();
        println!("{}", line);
        self.draw();
    }

    pub fn clear(&mut self) {
        if !self.tty {
            return;
        }
        self.erase();
        self.lines.clear();
        self.status = None;
    }
}

/// cli._fmt_remaining.
fn fmt_remaining(sec: f64) -> String {
    let s = |n: i64| if n != 1 { "s" } else { "" };
    if sec >= 3600.0 {
        let (hours, rest) = ((sec as i64) / 3600, (sec as i64) % 3600);
        let minutes = rest / 60;
        return format!("{} hour{} {} minute{} remaining", hours, s(hours), minutes, s(minutes));
    }
    if sec >= 120.0 {
        let minutes = py_round(sec / 60.0);
        if minutes >= 60 {
            return "1 hour 0 minutes remaining".to_string();
        }
        return format!("{} minutes remaining", minutes.max(2));
    }
    if sec >= 45.0 {
        return "about a minute remaining".to_string();
    }
    "less than a minute remaining".to_string()
}

/// Python's round() on a float: half to even.
fn py_round(x: f64) -> i64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && (r as i64) % 2 != 0 {
        (r - x.signum()) as i64
    } else {
        r as i64
    }
}

/// cli.EtaEstimator: a stable time-remaining for a batch of known size.
pub struct EtaEstimator {
    total: i64,
    start: f64,
    last_t: f64,
    last_done: i64,
    done: i64,
    rate: Option<f64>,
    anchor: Option<(f64, f64)>,
    breach_since: Option<(i32, f64)>,
}

impl EtaEstimator {
    const WARMUP: f64 = 5.0;
    const TAU: f64 = 60.0;
    const REANCHOR_FRAC: f64 = 0.15;
    const REANCHOR_ABS: f64 = 20.0;
    const PERSIST: f64 = 8.0;

    pub fn new(total: i64) -> Self {
        let now = monotonic();
        EtaEstimator { total, start: now, last_t: now, last_done: 0, done: 0, rate: None, anchor: None, breach_since: None }
    }

    pub fn exclude(&mut self, nbytes: i64) {
        self.total = (self.total - nbytes).max(0);
    }

    pub fn update(&mut self, done: i64) {
        let now = monotonic();
        let dt = now - self.last_t;
        self.done = done;
        if dt <= 0.0 {
            return;
        }
        let inst = ((done - self.last_done) as f64 / dt).max(0.0);
        let elapsed = now - self.start;
        if elapsed <= Self::TAU {
            self.rate = if elapsed > 0.0 { Some(done as f64 / elapsed) } else { None };
        } else {
            let alpha = 1.0 - (-dt / Self::TAU).exp();
            let r = self.rate.unwrap_or(0.0);
            self.rate = Some(r + alpha * (inst - r));
        }
        self.last_t = now;
        self.last_done = done;
    }

    pub fn remaining_text(&mut self) -> Option<String> {
        let now = monotonic();
        let rate = match self.rate {
            Some(r) if now - self.start >= Self::WARMUP && r > 1e-9 && self.total > 0 => r,
            _ => return None,
        };
        let raw = ((self.total - self.done) as f64 / rate).max(0.0);
        let (anchor_eta, anchor_t) = *self.anchor.get_or_insert((raw, now));
        let mut shown = (anchor_eta - (now - anchor_t)).max(0.0);
        let band = (Self::REANCHOR_FRAC * shown.max(raw)).max(Self::REANCHOR_ABS);
        let side = if (raw - shown).abs() <= band { 0 } else if raw > shown { 1 } else { -1 };
        match self.breach_since {
            _ if side == 0 => self.breach_since = None,
            Some((s, _)) if s != side => self.breach_since = Some((side, now)),
            None => self.breach_since = Some((side, now)),
            Some((_, since)) if now - since >= Self::PERSIST => {
                let eta = if side > 0 { raw + 0.4 * band } else { raw };
                self.anchor = Some((eta, now));
                self.breach_since = None;
                shown = eta;
            }
            Some(_) => {}
        }
        Some(fmt_remaining(shown))
    }
}
