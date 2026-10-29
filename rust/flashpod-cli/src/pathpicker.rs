//! pathpicker.py: the Miller-column directory/file picker `add` opens when
//! it is given no paths. Directories you traverse stay on screen as
//! side-by-side columns; Space selects, Shift+Up/Dn (or the v lock) sweeps,
//! `.` shows dotfiles, Enter returns the selection, Esc or q cancels.
//!
//! Text widths are counted in code points, as Python's len() counts them.

use std::collections::HashMap;
use std::io::Write;

/// len() of a str.
fn clen(s: &str) -> usize {
    s.chars().count()
}

/// s[:n]
fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// s[n:]
fn tail(s: &str, n: usize) -> String {
    s.chars().skip(n).collect()
}

/// s.ljust(w)
fn ljust(s: &str, w: usize) -> String {
    let n = clen(s);
    if n >= w {
        s.to_string()
    } else {
        format!("{}{}", s, " ".repeat(w - n))
    }
}

/// s[a:b] with Python's clamping (a <= b, both non-negative).
fn mid(s: &str, a: usize, b: usize) -> String {
    s.chars().skip(a).take(b.saturating_sub(a)).collect()
}

/// s[-k:] for any integer k, as Python slices it.
fn from_neg(s: &str, k: i64) -> String {
    let n = clen(s) as i64;
    let start = if -k < 0 { (n - k).max(0) } else { (-k).min(n) };
    tail(s, start as usize)
}

/// os.path.dirname
fn dirname(p: &str) -> String {
    let i = p.rfind('/').map(|i| i + 1).unwrap_or(0);
    let head = &p[..i];
    if !head.is_empty() && head.chars().any(|c| c != '/') {
        head.trim_end_matches('/').to_string()
    } else {
        head.to_string()
    }
}

/// os.path.join(a, b) for a relative b.
fn join(a: &str, b: &str) -> String {
    if a.is_empty() || a.ends_with('/') {
        format!("{}{}", a, b)
    } else {
        format!("{}/{}", a, b)
    }
}

/// os.path.abspath
fn abspath(p: &str) -> String {
    let p = if p.starts_with('/') {
        p.to_string()
    } else {
        join(&std::env::current_dir().map(|d| d.to_string_lossy().into_owned()).unwrap_or_default(), p)
    };
    let initial = if p.starts_with("//") && !p.starts_with("///") { "//" } else if p.starts_with('/') { "/" } else { "" };
    let mut comps: Vec<&str> = Vec::new();
    for c in p.split('/') {
        if c.is_empty() || c == "." {
            continue;
        }
        if c != ".." || (initial.is_empty() && comps.is_empty()) || comps.last() == Some(&"..") {
            comps.push(c);
        } else if !comps.is_empty() {
            comps.pop();
        }
    }
    let out = format!("{}{}", initial, comps.join("/"));
    if out.is_empty() { ".".into() } else { out }
}

fn is_dir(p: &str) -> bool {
    std::fs::metadata(p).map(|m| m.is_dir()).unwrap_or(false)
}

#[derive(Clone)]
struct Entry {
    name: String,
    path: String,
    is_dir: bool,
}

/// DirectoryLister.list: directories first, then files, each group sorted
/// case-insensitively; the error text when the directory can't be read.
fn list(path: &str, show_hidden: bool) -> (Vec<Entry>, Option<String>) {
    let rd = match std::fs::read_dir(path) {
        Ok(rd) => rd,
        Err(e) => return (Vec::new(), Some(crate::flash::os_error_text(&e, path))),
    };
    let mut entries = Vec::new();
    for it in rd.flatten() {
        let name = it.file_name().to_string_lossy().into_owned();
        if !show_hidden && name.starts_with('.') {
            continue;
        }
        let full = join(path, &name);
        let d = is_dir(&full);
        entries.push(Entry { name, path: full, is_dir: d });
    }
    entries.sort_by(|a, b| (!a.is_dir, a.name.to_lowercase()).cmp(&(!b.is_dir, b.name.to_lowercase())));
    (entries, None)
}

/// SelectionSet: the selected paths in the order they were picked.
#[derive(Default)]
struct Selection(Vec<String>);

impl Selection {
    fn contains(&self, p: &str) -> bool {
        self.0.iter().any(|x| x == p)
    }
    fn toggle(&mut self, p: &str) {
        if !self.discard(p) {
            self.0.push(p.to_string());
        }
    }
    fn add(&mut self, p: &str) {
        if !self.contains(p) {
            self.0.push(p.to_string());
        }
    }
    fn discard(&mut self, p: &str) -> bool {
        match self.0.iter().position(|x| x == p) {
            Some(i) => {
                self.0.remove(i);
                true
            }
            None => false,
        }
    }
}

/// _UnixKeyReader: one keystroke as a symbolic name. Err is an OSError's text.
struct Keys;

const ESC_MAP: [(&str, &str); 20] = [
    ("[A", "up"), ("[B", "down"), ("[C", "right"), ("[D", "left"),
    ("[H", "home"), ("[F", "end"), ("[1~", "home"), ("[4~", "end"),
    ("[5~", "pgup"), ("[6~", "pgdn"),
    ("OA", "up"), ("OB", "down"), ("OC", "right"), ("OD", "left"),
    ("OH", "home"), ("OF", "end"),
    ("[1;2A", "shift+up"), ("[1;2B", "shift+down"),   // xterm-style
    ("[a", "shift+up"), ("[b", "shift+down"),         // rxvt-style
];

/// What a key read gives back: a key, or ^C (KeyboardInterrupt).
enum Key {
    Name(String),
    Interrupt,
}

impl Keys {
    fn read_one() -> Result<Vec<u8>, String> {
        let mut b = [0u8; 1];
        loop {
            let e = match crate::sys::read_stdin(&mut b) {
                Ok(n) => return Ok(b[..n].to_vec()),
                Err(e) => e,
            };
            if e.kind() != std::io::ErrorKind::Interrupted {
                let code = e.raw_os_error().unwrap_or(0);
                let msg = e.to_string();
                let msg = msg.split(" (os error").next().unwrap_or(&msg).to_string();
                return Err(format!("[Errno {}] {}", code, msg));
            }
        }
    }

    fn read(&self) -> Result<Key, String> {
        let ch = Self::read_one()?;
        if ch != [0x1b] {
            // one byte, decoded utf-8 with errors="replace"
            let s = String::from_utf8_lossy(&ch).into_owned();
            return Ok(match s.as_str() {
                "\r" | "\n" => Key::Name("enter".into()),
                " " => Key::Name("space".into()),
                "\x03" => Key::Interrupt,
                _ => Key::Name(s),
            });
        }
        // A bare Esc press and an escape sequence both start with \x1b; only
        // the sequence has more bytes already pending (or arriving within a
        // few ms on a slow line).
        let mut seq = String::new();
        while clen(&seq) < 8 {
            if !crate::sys::stdin_ready(50) {
                break;
            }
            let b = Self::read_one()?;
            if b.is_empty() {
                break;
            }
            seq.push(if b[0] < 0x80 { b[0] as char } else { '\u{fffd}' });
            if let Some((_, k)) = ESC_MAP.iter().find(|(s, _)| *s == seq) {
                return Ok(Key::Name(k.to_string()));
            }
            let first = seq.chars().next().unwrap();
            let last = seq.chars().last().unwrap();
            // final byte of a CSI sequence is 0x40-0x7e
            if first == '[' && clen(&seq) >= 2 && ('\x40'..='\x7e').contains(&last) {
                return Ok(Key::Name("unknown".into()));
            }
            if first == 'O' && clen(&seq) >= 2 {
                return Ok(Key::Name("unknown".into()));
            }
            if first != '[' && first != 'O' {
                return Ok(Key::Name("unknown".into()));
            }
        }
        Ok(Key::Name(if seq.is_empty() { "escape" } else { "unknown" }.into()))
    }
}

/// Screen: alternate buffer, hidden cursor, autowrap off, until dropped.
struct Screen;

impl Screen {
    fn new() -> Screen {
        out("\x1b[?1049h\x1b[?25l\x1b[?7l");
        Screen
    }
    fn draw(&self, lines: &[String]) {
        // Repaint in place: home the cursor, rewrite every line erasing its
        // tail, then erase whatever is left below the frame.
        out(&format!("\x1b[H{}\x1b[K\x1b[J", lines.join("\x1b[K\r\n")));
    }
}

impl Drop for Screen {
    fn drop(&mut self) {
        out("\x1b[?1049l\x1b[?25h\x1b[?7h");
    }
}

fn out(s: &str) {
    let mut o = std::io::stdout().lock();
    let _ = o.write_all(s.as_bytes());
    let _ = o.flush();
}

struct Column {
    path: String,
    entries: Vec<Entry>,
    error: Option<String>,
    cursor: usize,
    scroll: usize,
}

pub type Count = Box<dyn Fn(&str) -> i64>;
pub type Status = Box<dyn Fn(i64) -> (String, bool)>;

pub struct DirectoryPicker {
    show_hidden: bool,
    selection: Selection,
    count: Option<Count>,
    status: Option<Status>,
    count_cache: HashMap<String, i64>,
    header_rows: usize,
    columns: Vec<Column>,
    active: usize,
    sweep: bool,
    sweep_op: Option<&'static str>,
    sweep_key: Option<String>,
    openable: HashMap<String, bool>,
    bump: bool,
    first_vis: usize,
    page: usize,
}

const HEADER_ROWS: usize = 2;
const FOOTER_ROWS: usize = 2;
const HELP1: &str = "Up/Dn move   Right open   Left up   Space select   Shift+Up/Dn sweep select";
const HELP2: &str = "v sweep lock   [.] hidden   Enter done   Esc cancel";
const MIN_COL_WIDTH: usize = 20;
const MAX_COL_WIDTH: usize = 42;
const STYLE_DIR: &str = "\x1b[47;30m"; // folders: white background, black text
const STYLE_FILE: &str = "\x1b[40;37m"; // files: black background, white text
const GUTTER: usize = 2; // room for the left/right affordance arrows
const MOVE_KEYS: [&str; 8] = ["up", "down", "shift+up", "shift+down", "pgup", "pgdn", "home", "end"];

enum Outcome {
    Going,
    Done(Option<Vec<String>>),
}

impl DirectoryPicker {
    pub fn new(start_dir: &str, count: Option<Count>, status: Option<Status>) -> Self {
        let start = abspath(start_dir);
        let header_rows = HEADER_ROWS + if status.is_some() { 1 } else { 0 };
        let mut p = DirectoryPicker {
            show_hidden: false,
            selection: Selection::default(),
            count,
            status,
            count_cache: HashMap::new(),
            header_rows,
            columns: Vec::new(),
            active: 0,
            sweep: false,
            sweep_op: None,
            sweep_key: None,
            openable: HashMap::new(),
            bump: false,
            first_vis: 0,
            page: 1,
        };
        let col = p.make_column(&start, None);
        p.columns.push(col);
        p
    }

    // -- model --------------------------------------------------------------

    fn make_column(&self, path: &str, remember: Option<&str>) -> Column {
        let mut col = Column { path: path.to_string(), entries: Vec::new(), error: None, cursor: 0, scroll: 0 };
        self.fill(&mut col, remember);
        col
    }

    /// (Re)list a column; the cursor goes to `remember` if it's there, else
    /// to the top.
    fn fill(&self, col: &mut Column, remember: Option<&str>) {
        let (entries, error) = list(&col.path, self.show_hidden);
        col.entries = entries;
        col.error = error;
        col.scroll = 0;
        col.cursor = 0;
        if let Some(r) = remember {
            if let Some(i) = col.entries.iter().position(|e| e.path == r) {
                col.cursor = i;
            }
        }
    }

    fn go_up(&mut self) {
        if self.active > 0 {
            self.active -= 1;
            return;
        }
        let top = self.columns[0].path.clone();
        let parent = dirname(&top);
        if parent != top {
            let col = self.make_column(&parent, Some(&top));
            self.columns.insert(0, col);
            self.first_vis += 1; // existing columns all shifted right
        }
    }

    fn open(&mut self, path: &str) {
        let nxt = self.active + 1;
        if nxt < self.columns.len() && self.columns[nxt].path == path {
            self.active = nxt;
            return;
        }
        self.columns.truncate(nxt);
        let col = self.make_column(path, None);
        self.columns.push(col);
        self.active = nxt;
    }

    fn start_sweep(&mut self) {
        if self.sweep_op.is_none() {
            let col = &self.columns[self.active];
            let selected = self.selection.contains(&col.entries[col.cursor].path);
            self.sweep_op = Some(if selected { "discard" } else { "add" });
        }
    }

    fn sweep_apply(&mut self, path: &str) {
        if self.sweep_op == Some("discard") {
            self.selection.discard(path);
        } else {
            self.selection.add(path);
        }
    }

    fn move_to(&mut self, target: i64, sweeping: bool) {
        let n = self.columns[self.active].entries.len() as i64;
        let target = target.min(n - 1).max(0) as usize;
        if sweeping {
            let cur = self.columns[self.active].cursor;
            let (lo, hi) = if cur <= target { (cur, target) } else { (target, cur) };
            let paths: Vec<String> = self.columns[self.active].entries[lo..=hi].iter().map(|e| e.path.clone()).collect();
            for p in paths {
                self.sweep_apply(&p);
            }
        }
        self.columns[self.active].cursor = target;
    }

    fn handle(&mut self, key: &str) -> Outcome {
        let n = self.columns[self.active].entries.len();
        if key == "enter" {
            return Outcome::Done(Some(self.selection.0.clone()));
        }
        if key == "escape" || key == "q" {
            return Outcome::Done(None);
        }
        // Any key that doesn't continue a sweep ends the current run (the
        // next sweep re-decides select-vs-deselect from its start entry).
        let is_move = MOVE_KEYS.contains(&key);
        let continues = key == "v" || (is_move && (self.sweep || key.starts_with("shift+")));
        if !continues {
            self.sweep_op = None;
            self.sweep_key = None;
        }
        if is_move && n > 0 {
            let sweeping = self.sweep || key.starts_with("shift+");
            if sweeping {
                // a shift-sweep that changes direction is a new gesture:
                // re-decide select-vs-deselect from the entry it starts on
                // (under the v-lock the operation holds until lock-off)
                if !self.sweep && key.starts_with("shift+") && self.sweep_key.as_deref() != Some(key) {
                    self.sweep_op = None;
                }
                self.sweep_key = Some(key.to_string());
                self.start_sweep();
            }
            let base = key.rsplit('+').next().unwrap();
            let cur = self.columns[self.active].cursor as i64;
            let target = match base {
                "up" => cur - 1,
                "down" => cur + 1,
                "pgup" => cur - self.page as i64,
                "pgdn" => cur + self.page as i64,
                "home" => 0,
                _ => n as i64 - 1, // end
            };
            self.move_to(target, sweeping);
        } else if key == "v" {
            self.sweep = !self.sweep;
            if self.sweep && n > 0 {
                self.start_sweep();
                let col = &self.columns[self.active];
                let p = col.entries[col.cursor].path.clone();
                self.sweep_apply(&p);
            } else {
                self.sweep_op = None;
            }
        } else if key == "left" {
            if self.can_go_up() {
                self.go_up();
            } else {
                self.bump = true;
            }
        } else if key == "right" && n > 0 {
            let col = &self.columns[self.active];
            let entry = col.entries[col.cursor].clone();
            if entry.is_dir && self.has_children(&entry.path) {
                self.open(&entry.path);
            } else {
                // file, or nothing inside: the same test as the arrow
                self.bump = true;
            }
        } else if key == "space" && n > 0 {
            let col = &self.columns[self.active];
            let p = col.entries[col.cursor].path.clone();
            self.selection.toggle(&p);
        } else if key == "." {
            self.show_hidden = !self.show_hidden;
            self.openable.clear(); // probe results depend on the filter
            let mut cols = std::mem::take(&mut self.columns);
            for c in cols.iter_mut() {
                let here = c.entries.get(c.cursor).map(|e| e.path.clone());
                self.fill(c, here.as_deref());
            }
            self.columns = cols;
        }
        Outcome::Going
    }

    /// The summed count() over the current selection (cached per path).
    fn selected_total(&mut self) -> i64 {
        let mut total = 0;
        for p in self.selection.0.clone() {
            if !self.count_cache.contains_key(&p) {
                let v = (self.count.as_ref().unwrap())(&p);
                self.count_cache.insert(p.clone(), v);
            }
            total += self.count_cache[&p];
        }
        total
    }

    fn can_go_up(&self) -> bool {
        if self.active > 0 {
            return true;
        }
        let path = &self.columns[self.active].path;
        dirname(path) != *path
    }

    /// True when the directory has at least one entry the current filter
    /// would show; probed lazily and cached per path.
    fn has_children(&mut self, path: &str) -> bool {
        if let Some(&v) = self.openable.get(path) {
            return v;
        }
        let found = match std::fs::read_dir(path) {
            Ok(rd) => rd.flatten().any(|it| self.show_hidden || !it.file_name().to_string_lossy().starts_with('.')),
            Err(_) => false,
        };
        self.openable.insert(path.to_string(), found);
        found
    }

    // -- view ---------------------------------------------------------------

    fn row_text(e: &Entry, selected: bool) -> String {
        format!("  [{}] {}{}", if selected { "*" } else { " " }, e.name, if e.is_dir { "/" } else { "" })
    }

    fn column_rows(&self, col: &Column) -> Vec<String> {
        let mut rows: Vec<String> = col.entries.iter().map(|e| Self::row_text(e, self.selection.contains(&e.path))).collect();
        if let Some(err) = &col.error {
            rows.push(format!("  (unreadable: {})", err));
        } else if col.entries.is_empty() {
            rows.push("  (empty)".into());
        }
        rows
    }

    /// A listing row with the folder/file colour on just the name; bold
    /// styles the whole row (the trail highlight).
    fn name_styled(text: &str, entry: &Entry, width: usize, bold: bool) -> String {
        let plain = ljust(&head(text, width), width);
        let start = GUTTER + 4; // past "  [x] "
        let end = clen(text).min(width);
        if end <= start {
            return if bold { format!("\x1b[1m{}\x1b[0m", plain) } else { plain };
        }
        let base = if entry.is_dir { STYLE_DIR } else { STYLE_FILE };
        let (b_on, b_re) = if bold { ("\x1b[1m", "\x1b[0m\x1b[1m") } else { ("", "\x1b[0m") };
        format!("{}{}{}{}{}{}{}", b_on, head(&plain, start), base, mid(&plain, start, end), b_re, tail(&plain, end),
                if bold { "\x1b[0m" } else { "" })
    }

    /// The active cursor row: '← ' when Left can go up, ' →' when the
    /// highlighted directory has contents to open.
    fn cursor_cell(&mut self, ci: usize, width: usize) -> String {
        let col = &self.columns[ci];
        let entry = col.entries[col.cursor].clone();
        let body_w = width.saturating_sub(2 * GUTTER);
        let body = tail(&Self::row_text(&entry, self.selection.contains(&entry.path)), GUTTER);
        let left = if self.can_go_up() { "← " } else { "  " };
        let right = if entry.is_dir && self.has_children(&entry.path) { " →" } else { "  " };
        format!("{}{}{}", left, ljust(&head(&body, body_w), body_w), right)
    }

    /// Which consecutive columns to show: keep the active one visible,
    /// moving the window as little as possible.
    fn visible_range(&mut self, widths: &[usize], term_cols: usize) -> (usize, usize) {
        let last_fitting = |first: usize| {
            let (mut total, mut last) = (0usize, first);
            for (i, w) in widths.iter().enumerate().skip(first) {
                total += w + if i > first { 1 } else { 0 }; // +separator
                if total > term_cols && i > first {
                    break;
                }
                last = i;
            }
            last
        };
        let span = |f: usize, l: usize| widths[f..=l].iter().sum::<usize>() + (l - f);
        let mut first = self.first_vis.min(self.active);
        while last_fitting(first) < self.active {
            first += 1;
        }
        // reclaim leftover width: pull ancestor columns back into view as
        // long as doing so evicts nothing currently visible
        while first > 0 && span(first - 1, last_fitting(first)) <= term_cols {
            first -= 1;
        }
        self.first_vis = first;
        (first, last_fitting(first))
    }

    fn frame(&mut self, term_cols: usize, term_rows: usize) -> Vec<String> {
        let view = (term_rows as i64 - self.header_rows as i64 - FOOTER_ROWS as i64).max(1) as usize;
        self.page = view;

        let all_rows: Vec<Vec<String>> = self.columns.iter().map(|c| self.column_rows(c)).collect();
        let widths: Vec<usize> = all_rows.iter()
            .map(|rows| {
                let longest = if rows.is_empty() { 0 } else { rows.iter().map(|r| clen(r)).max().unwrap() + 1 };
                MIN_COL_WIDTH.max(MAX_COL_WIDTH.min(term_cols).min(longest))
            })
            .collect();
        let (first, last) = self.visible_range(&widths, term_cols);

        let mut header_path = self.columns[self.active].path.clone();
        if clen(&header_path) as i64 > term_cols as i64 - 16 {
            header_path = format!("...{}", from_neg(&header_path, term_cols as i64 - 19));
        }
        let count = format!("{} selected{}", self.selection.0.len(), if self.sweep { " [SWEEP]" } else { "" });
        let pad = (term_cols as i64 - clen(&header_path) as i64 - clen(&count) as i64 - 1).max(1) as usize;
        let mut lines = Vec::new();
        if self.status.is_some() {
            // pinned status bar: a full-width reverse-video top line, alert
            // red when the callback says so
            let total = if self.count.is_some() { self.selected_total() } else { self.selection.0.len() as i64 };
            let (bar, alert) = (self.status.as_ref().unwrap())(total);
            let style = if alert { "\x1b[7;31m" } else { "\x1b[7m" };
            lines.push(format!("{}{}\x1b[0m", style, ljust(&head(&bar, term_cols), term_cols)));
        }
        lines.push(format!("\x1b[1m{}{}{}\x1b[0m", header_path, " ".repeat(pad), count));
        lines.push(String::new());

        for col in &mut self.columns[first..=last] {
            if col.cursor < col.scroll {
                col.scroll = col.cursor;
            }
            if col.cursor >= col.scroll + view {
                col.scroll = col.cursor + 1 - view;
            }
        }

        for r in 0..view {
            let mut parts = Vec::new();
            for i in first..=last {
                let (idx, nent, cursor) = {
                    let col = &self.columns[i];
                    (col.scroll + r, col.entries.len(), col.cursor)
                };
                let rows = &all_rows[i];
                let text = rows.get(idx).cloned().unwrap_or_default();
                let is_entry = idx < nent;
                let on_cursor = is_entry && idx == cursor;
                let cell = if on_cursor && i == self.active {
                    format!("\x1b[7m{}\x1b[0m", self.cursor_cell(i, widths[i]))
                } else if is_entry {
                    Self::name_styled(&text, &self.columns[i].entries[idx], widths[i], on_cursor)
                } else {
                    ljust(&head(&text, widths[i]), widths[i])
                };
                parts.push(cell);
            }
            lines.push(parts.join("│"));
        }

        lines.push(head(HELP1, term_cols));
        lines.push(head(HELP2, term_cols));
        lines
    }

    /// The frame with its column area nudged two cells right: shown for one
    /// beat when a navigation push has nowhere to go.
    fn bumped(&self, lines: Vec<String>) -> Vec<String> {
        let n = lines.len();
        lines.into_iter().enumerate()
            .map(|(i, l)| if i >= self.header_rows && i + FOOTER_ROWS < n { format!("  {}", l) } else { l })
            .collect()
    }

    // -- controller -----------------------------------------------------------

    /// The selected paths in pick order, None on cancel; Err is the text of
    /// the exception Python's picker would raise.
    pub fn run(&mut self) -> Result<Option<Vec<String>>, String> {
        if !(crate::term::stdin_isatty() && crate::term::stdout_isatty()) {
            return Err("directory picker needs an interactive terminal (stdin and stdout must be ttys)".into());
        }
        let keys = Keys;
        let _cbreak = Cbreak::new()?;
        let screen = Screen::new();
        loop {
            let (cols, rows) = crate::term::terminal_size();
            let f = self.frame(cols, rows);
            screen.draw(&f);
            let key = match keys.read()? {
                Key::Interrupt => return Ok(None),
                Key::Name(k) => k,
            };
            if let Outcome::Done(r) = self.handle(&key) {
                return Ok(r);
            }
            if self.bump {
                self.bump = false;
                let f = self.frame(cols, rows);
                screen.draw(&self.bumped(f));
                crate::window::sleep(0.08);
            }
        }
    }
}

/// tty.setcbreak: no echo, no line editing; restored (TCSADRAIN) on drop.
/// Python leaves ISIG on and gets ^C as KeyboardInterrupt; here ISIG is off
/// and ^C arrives as the byte, which cancels the same way.
struct Cbreak(#[allow(dead_code)] crate::sys::RawInput);

impl Cbreak {
    fn new() -> Result<Self, String> {
        crate::sys::RawInput::new().map(Cbreak).map_err(|e| {
            format!("({}, '{}')", e.raw_os_error().unwrap_or(0),
                    e.to_string().split(" (os error").next().unwrap_or(""))
        })
    }
}

/// cli._count_audio: audio files `add` would take under `path` (file or
/// directory), AppleDouble junk excluded.
pub fn count_audio(path: &str) -> i64 {
    fn is_audio(name: &str) -> bool {
        !name.starts_with("._")
            && flashpod_core::library::AUDIO_EXTS.contains(&flashpod_core::library::splitext(name).1.to_lowercase().as_str())
    }
    let md = std::fs::metadata(path);
    if md.as_ref().map(|m| m.is_file()).unwrap_or(false) {
        let base = path.rsplit('/').next().unwrap_or(path);
        return if is_audio(base) { 1 } else { 0 };
    }
    // os.walk: top-down, symlinked directories listed but not followed,
    // unreadable directories skipped
    fn walk(dir: &str, total: &mut i64) {
        let rd = match std::fs::read_dir(dir) {
            Ok(rd) => rd,
            Err(_) => return,
        };
        let mut subdirs = Vec::new();
        for it in rd.flatten() {
            let name = it.file_name().to_string_lossy().into_owned();
            let full = join(dir, &name);
            if is_dir(&full) {
                if !it.file_type().map(|t| t.is_symlink()).unwrap_or(false) {
                    subdirs.push(full);
                }
            } else if is_audio(&name) {
                *total += 1;
            }
        }
        for d in subdirs {
            walk(&d, total);
        }
    }
    let mut total = 0;
    if md.map(|m| m.is_dir()).unwrap_or(false) {
        walk(path, &mut total);
    }
    total
}

/// "{:,}".format(n)
fn commas(n: i64) -> String {
    let s = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{}", out) } else { out }
}

/// round(x) for a float: half to even.
fn round_half_even(x: f64) -> i64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && (r as i64) % 2 != 0 {
        (r - x.signum()) as i64
    } else {
        r as i64
    }
}

/// cli._picker_status: song counts plus an estimated fraction of the
/// firmware DB budget, pricing each new track at the library's average.
pub fn picker_status(existing: Option<i64>, db_bytes: Option<i64>) -> Status {
    let limit = crate::data::options().db_limit;
    let avg = match (existing, db_bytes) {
        (Some(e), Some(b)) if e != 0 && b != 0 => b as f64 / e as f64,
        _ => 700.0,
    };
    let base = db_bytes.unwrap_or(0);
    Box::new(move |selected: i64| {
        let q = |v: Option<i64>| v.map(commas).unwrap_or_else(|| "?".into());
        let mut parts = vec![
            format!("iPod songs: {}", q(existing)),
            format!("selected: +{}", commas(selected)),
            format!("projected: {}", q(existing.map(|e| e + selected))),
        ];
        let mut alert = false;
        if limit != 0 && existing.is_some() {
            let pct = (base as f64 + selected as f64 * avg) * 100.0 / limit as f64;
            parts.push(format!("~{}% of DB budget", round_half_even(pct)));
            alert = pct > 90.0; // red bar: nearly (or actually) over
        }
        (format!("  {}", parts.join("  |  ")), alert)
    })
}

/// cli._invoking_home: the human's home directory, even when flashpod has
/// sudo-elevated itself.
pub fn invoking_home() -> String {
    if let Ok(user) = std::env::var("SUDO_USER") {
        if !user.is_empty() && crate::sys::is_root() {
            if let Some(dir) = crate::sys::home_of(&user) {
                return dir;
            }
        }
    }
    crate::data::expanduser("~")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_helpers() {
        assert_eq!(dirname("/a/b"), "/a");
        assert_eq!(dirname("/a"), "/");
        assert_eq!(dirname("/"), "/");
        assert_eq!(abspath("/a/./b/../c//d/"), "/a/c/d");
        assert_eq!(abspath("//x/../y"), "//y");
        assert_eq!(from_neg("abcdef", 2), "ef");
        assert_eq!(from_neg("abcdef", 0), "abcdef");
        assert_eq!(from_neg("abcdef", -2), "cdef");
        assert_eq!(from_neg("abc", 9), "abc");
        assert_eq!(commas(1234567), "1,234,567");
        assert_eq!(commas(999), "999");
        assert_eq!(round_half_even(2.5), 2);
        assert_eq!(round_half_even(3.5), 4);
        assert_eq!(round_half_even(2.4999), 2);
        assert_eq!(round_half_even(91.6), 92);
    }
}
