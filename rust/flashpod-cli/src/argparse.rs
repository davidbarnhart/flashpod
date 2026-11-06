//! The part of Python 3.14's argparse that flashpod's parser uses, ported so
//! the Rust build parses, errs and prints help the same way: the parse loop
//! (_parse_known_args with its A/O/- pattern), abbreviations, mutually
//! exclusive groups, subparsers with aliases and parents' SUPPRESS options;
//! HelpFormatter (usage wrapping, help columns, textwrap) and the 3.14
//! colour theme, which is used when stdout is a terminal.

use crate::term;
use std::collections::{BTreeMap, HashSet};

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Nargs {
    One,
    Optional,
    Star,
    Plus,
    Parser,
    Zero,
}

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Kind {
    Store,
    StoreTrue,
    Help,
    Version,
    Sub,
}

#[derive(Clone, PartialEq, Debug)]
pub enum Val {
    None,
    Bool(bool),
    Str(String),
    List(Vec<String>),
    Float(f64),
}

pub type Ns = BTreeMap<String, Val>;

#[derive(Clone)]
pub struct Action {
    opts: Vec<String>,
    dest: Option<String>,
    nargs: Nargs,
    kind: Kind,
    choices: Option<Vec<String>>,
    metavar: Option<String>,
    help: Option<String>,
    help_suppressed: bool,
    default: Option<Val>,
    required: bool,
    float: bool,
}

impl Action {
    fn new(kind: Kind) -> Self {
        Action {
            opts: Vec::new(),
            dest: None,
            nargs: Nargs::One,
            kind,
            choices: None,
            metavar: None,
            help: None,
            help_suppressed: false,
            default: Some(Val::None),
            required: false,
            float: false,
        }
    }
}

/// Builder for one add_argument call.
pub struct Arg(Action);

impl Arg {
    /// An optional: `--name` (and others); dest from the first long one.
    pub fn opt(names: &[&str]) -> Self {
        let mut a = Action::new(Kind::Store);
        a.opts = names.iter().map(|s| s.to_string()).collect();
        let long = names.iter().find(|n| n.starts_with("--")).unwrap_or(&names[0]);
        a.dest = Some(long.trim_start_matches('-').replace('-', "_"));
        Arg(a)
    }
    pub fn flag(names: &[&str]) -> Self {
        let mut a = Self::opt(names).0;
        a.kind = Kind::StoreTrue;
        a.nargs = Nargs::Zero;
        a.default = Some(Val::Bool(false));
        Arg(a)
    }
    pub fn pos(dest: &str, nargs: Nargs) -> Self {
        let mut a = Action::new(Kind::Store);
        a.dest = Some(dest.to_string());
        a.nargs = nargs;
        a.required = !matches!(nargs, Nargs::Optional | Nargs::Star);
        Arg(a)
    }
    pub fn help(mut self, h: &str) -> Self {
        self.0.help = Some(h.to_string());
        self
    }
    /// help=SUPPRESS
    pub fn hidden(mut self) -> Self {
        self.0.help_suppressed = true;
        self
    }
    /// default=SUPPRESS
    pub fn no_default(mut self) -> Self {
        self.0.default = None;
        self
    }
    pub fn metavar(mut self, m: &str) -> Self {
        self.0.metavar = Some(m.to_string());
        self
    }
    pub fn choices(mut self, c: &[&str]) -> Self {
        self.0.choices = Some(c.iter().map(|s| s.to_string()).collect());
        self
    }
    /// required=True on an option: missing it is a usage error, and usage
    /// shows it without brackets.
    pub fn required(mut self) -> Self {
        self.0.required = true;
        self
    }

    pub fn float(mut self) -> Self {
        self.0.float = true;
        self
    }
    /// dest=
    pub fn dest(mut self, d: &str) -> Self {
        self.0.dest = Some(d.to_string());
        self
    }
}

pub struct Parser {
    pub prog: String,
    description: Option<String>,
    actions: Vec<Action>,
    positionals: Vec<usize>,
    optionals: Vec<usize>,
    mutex: Vec<Vec<usize>>,
    version: Option<String>,
    /// the subparsers action: (action index, names -> parser, pseudo-actions)
    sub_action: Option<usize>,
    subs: Vec<(String, usize)>,
    sub_help: Vec<Action>,
}

/// All the parsers, the top one first; subparsers refer to each other by index.
pub struct Cli {
    parsers: Vec<Parser>,
}

impl Parser {
    fn new(prog: &str, description: Option<&str>) -> Self {
        let mut p = Parser {
            prog: prog.to_string(),
            description: description.map(String::from),
            actions: Vec::new(),
            positionals: Vec::new(),
            optionals: Vec::new(),
            mutex: Vec::new(),
            version: None,
            sub_action: None,
            subs: Vec::new(),
            sub_help: Vec::new(),
        };
        let mut h = Action::new(Kind::Help);
        h.opts = vec!["-h".into(), "--help".into()];
        h.nargs = Nargs::Zero;
        h.default = None;
        h.help = Some("show this help message and exit".into());
        p.push(h);
        p
    }

    fn push(&mut self, a: Action) -> usize {
        let i = self.actions.len();
        if a.opts.is_empty() {
            self.positionals.push(i);
        } else {
            self.optionals.push(i);
        }
        self.actions.push(a);
        i
    }

    pub fn add(&mut self, a: Arg) -> usize {
        self.push(a.0)
    }

    pub fn add_version(&mut self, version: &str) {
        let mut v = Action::new(Kind::Version);
        v.opts = vec!["--version".into()];
        v.nargs = Nargs::Zero;
        v.default = None;
        v.help = Some("show program's version number and exit".into());
        self.push(v);
        self.version = Some(version.to_string());
    }

    pub fn add_mutex(&mut self, args: Vec<Arg>) {
        let idx: Vec<usize> = args.into_iter().map(|a| self.push(a.0)).collect();
        self.mutex.push(idx);
    }

    fn option_map(&self) -> Vec<(&str, usize)> {
        let mut m = Vec::new();
        for (i, a) in self.actions.iter().enumerate() {
            for o in &a.opts {
                m.push((o.as_str(), i));
            }
        }
        m
    }
}

impl Cli {
    pub fn new(prog: &str, description: &str) -> Self {
        Cli { parsers: vec![Parser::new(prog, Some(description))] }
    }

    pub fn top(&mut self) -> &mut Parser {
        &mut self.parsers[0]
    }

    /// add_subparsers(dest=dest), required.
    pub fn add_subparsers(&mut self, dest: &str) {
        self.add_subparsers_at(0, dest)
    }

    /// add_subparsers on parser `pi` (0 = the top; a subcommand's index
    /// from add_parser_at nests a level): `firmware write`.
    pub fn add_subparsers_at(&mut self, pi: usize, dest: &str) {
        let mut a = Action::new(Kind::Sub);
        a.dest = Some(dest.to_string());
        a.nargs = Nargs::Parser;
        a.required = true;
        let i = self.parsers[pi].push(a);
        self.parsers[pi].sub_action = Some(i);
    }

    /// sub.add_parser(name, aliases, help, parents=[common] when `common`)
    /// under parser `pi`; returns the new parser's index.
    pub fn add_parser_at(&mut self, pi: usize, name: &str, aliases: &[&str], help: &str, common: &[Arg]) -> usize {
        let prog = format!("{} {}", self.parsers[pi].prog, name);
        let mut p = Parser::new(&prog, None);
        for c in common {
            p.push(c.0.clone());
        }
        let idx = self.parsers.len();
        self.parsers.push(p);
        let parent = &mut self.parsers[pi];
        parent.subs.push((name.to_string(), idx));
        for a in aliases {
            parent.subs.push((a.to_string(), idx));
        }
        let mut pseudo = Action::new(Kind::Store);
        let mut mv = name.to_string();
        if !aliases.is_empty() {
            mv += &format!(" ({})", aliases.join(", "));
        }
        pseudo.dest = Some(name.to_string());
        pseudo.metavar = Some(mv);
        pseudo.help = Some(help.to_string());
        parent.sub_help.push(pseudo);
        let choices: Vec<String> = parent.subs.iter().map(|(n, _)| n.clone()).collect();
        let si = parent.sub_action.unwrap();
        parent.actions[si].choices = Some(choices);
        idx
    }

    /// The parser at index `pi`, to add its arguments.
    pub fn parser(&mut self, pi: usize) -> &mut Parser {
        &mut self.parsers[pi]
    }
}

// ------------------------------------------------------------- difflib

/// difflib.SequenceMatcher(None, a, b).ratio(): 2*M/T, M the characters
/// in the matching blocks (no junk: autojunk and the popular-element cut only start at
/// 200 characters).
fn seq_ratio(a: &[char], b: &[char]) -> f64 {
    fn longest(a: &[char], b: &[char], alo: usize, ahi: usize, blo: usize, bhi: usize) -> (usize, usize, usize) {
        // find_longest_match: the longest block, earliest in a, then in b
        let (mut bi, mut bj, mut bs) = (alo, blo, 0usize);
        let mut j2len: std::collections::HashMap<usize, usize> = std::collections::HashMap::new();
        for i in alo..ahi {
            let mut next = std::collections::HashMap::new();
            for j in blo..bhi {
                if b[j] != a[i] {
                    continue;
                }
                let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).copied().unwrap_or(0) + 1;
                next.insert(j, k);
                if k > bs {
                    bi = i + 1 - k;
                    bj = j + 1 - k;
                    bs = k;
                }
            }
            j2len = next;
        }
        (bi, bj, bs)
    }
    let mut matched = 0usize;
    let mut queue = vec![(0, a.len(), 0, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest(a, b, alo, ahi, blo, bhi);
        if k > 0 {
            matched += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    let total = a.len() + b.len();
    if total == 0 { 1.0 } else { 2.0 * matched as f64 / total as f64 }
}

/// difflib.get_close_matches(word, choices, 1): the best choice scoring at
/// least 0.6, a tie going to the greater string (heapq.nlargest on
/// (score, choice)).
pub fn close_match(word: &str, choices: &[String]) -> Option<String> {
    let w: Vec<char> = word.chars().collect();
    let mut best: Option<(f64, &String)> = None;
    for c in choices {
        let x: Vec<char> = c.chars().collect();
        let r = seq_ratio(&x, &w);
        if r < 0.6 {
            continue;
        }
        if best.map_or(true, |(br, bc)| r > br || (r == br && c > bc)) {
            best = Some((r, c));
        }
    }
    best.map(|(_, c)| c.clone())
}

// ---------------------------------------------------------------- errors

/// Python's repr() of a str.
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
            c if c.is_control() => {
                let n = c as u32;
                if n <= 0xff {
                    out.push_str(&format!("\\x{:02x}", n));
                } else if n <= 0xffff {
                    out.push_str(&format!("\\u{:04x}", n));
                } else {
                    out.push_str(&format!("\\U{:08x}", n));
                }
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

/// float(s): surrounding whitespace allowed, underscores between digits.
fn py_float(s: &str) -> Option<f64> {
    let t = s.trim();
    if t.is_empty() {
        return None;
    }
    let b: Vec<char> = t.chars().collect();
    for (i, &c) in b.iter().enumerate() {
        if c == '_' {
            let ok = i > 0 && i + 1 < b.len() && b[i - 1].is_ascii_digit() && b[i + 1].is_ascii_digit();
            if !ok {
                return None;
            }
        }
    }
    let t: String = t.chars().filter(|&c| c != '_').collect();
    let l = t.to_ascii_lowercase();
    let body = l.trim_start_matches(['+', '-']);
    if matches!(body, "inf" | "infinity" | "nan") {
        let neg = l.starts_with('-');
        return Some(match (body, neg) {
            ("nan", _) => f64::NAN,
            (_, true) => f64::NEG_INFINITY,
            _ => f64::INFINITY,
        });
    }
    if !t.chars().all(|c| c.is_ascii_digit() || matches!(c, '+' | '-' | '.' | 'e' | 'E')) {
        return None;
    }
    t.parse::<f64>().ok()
}

struct ArgError {
    arg: Option<String>,
    msg: String,
}

impl ArgError {
    fn text(&self) -> String {
        match &self.arg {
            Some(n) => format!("argument {}: {}", n, self.msg),
            None => self.msg.clone(),
        }
    }
}

/// _get_action_name.
fn action_name(a: &Action) -> Option<String> {
    if !a.opts.is_empty() {
        Some(a.opts.join("/"))
    } else if let Some(m) = &a.metavar {
        Some(m.clone())
    } else if let Some(d) = &a.dest {
        Some(d.clone())
    } else {
        a.choices.as_ref().map(|c| format!("{{{}}}", c.join(",")))
    }
}

fn err(a: Option<&Action>, msg: String) -> ArgError {
    ArgError { arg: a.and_then(action_name), msg }
}

// --------------------------------------------------------------- matching

/// One element of an nargs pattern: a run of `set` characters, min..=max.
#[derive(Clone, Copy)]
struct Tok {
    set: &'static str,
    min: usize,
    max: usize,
}

const INF: usize = usize::MAX;

fn t(set: &'static str, min: usize, max: usize) -> Tok {
    Tok { set, min, max }
}

/// _get_nargs_pattern, as runs of pattern characters.
fn nargs_pattern(a: &Action) -> Vec<Tok> {
    let opt = !a.opts.is_empty();
    match (a.nargs, opt) {
        (Nargs::One, true) => vec![t("A", 1, 1)],
        (Nargs::One, false) => vec![t("-", 0, INF), t("A", 1, 1), t("-", 0, INF)],
        (Nargs::Optional, true) => vec![t("A", 0, 1)],
        (Nargs::Optional, false) => vec![t("-", 0, INF), t("A", 0, 1), t("-", 0, INF)],
        (Nargs::Star, true) => vec![t("A", 0, INF)],
        (Nargs::Star, false) => vec![t("-", 0, INF), t("A-", 0, INF)],
        (Nargs::Plus, true) => vec![t("A", 1, INF)],
        (Nargs::Plus, false) => vec![t("-", 0, INF), t("A", 1, 1), t("A-", 0, INF)],
        (Nargs::Parser, true) => vec![t("A", 1, 1), t("AO", 0, INF)],
        (Nargs::Parser, false) => vec![t("-", 0, INF), t("A", 1, 1), t("-AO", 0, INF)],
        (Nargs::Zero, _) => vec![],
    }
}

/// re.match of the concatenated groups against `s` (greedy, backtracking):
/// each group's matched length, or None.
fn match_groups(groups: &[Vec<Tok>], s: &[u8]) -> Option<Vec<usize>> {
    let flat: Vec<(usize, Tok)> = groups.iter().enumerate().flat_map(|(g, v)| v.iter().map(move |t| (g, *t))).collect();
    fn go(flat: &[(usize, Tok)], i: usize, pos: usize, s: &[u8], lens: &mut Vec<usize>) -> bool {
        if i == flat.len() {
            return true;
        }
        let (_, tok) = flat[i];
        let mut run = 0;
        while pos + run < s.len() && run < tok.max && tok.set.as_bytes().contains(&s[pos + run]) {
            run += 1;
        }
        if run < tok.min {
            return false;
        }
        let mut n = run;
        loop {
            lens.push(n);
            if go(flat, i + 1, pos + n, s, lens) {
                return true;
            }
            lens.pop();
            if n == tok.min {
                return false;
            }
            n -= 1;
        }
    }
    let mut lens = Vec::new();
    if !go(&flat, 0, 0, s, &mut lens) {
        return None;
    }
    let mut out = vec![0; groups.len()];
    for ((g, _), n) in flat.iter().zip(lens) {
        out[*g] += n;
    }
    Some(out)
}

// ---------------------------------------------------------------- parsing

type OptTuple = (Option<usize>, String, Option<String>, Option<String>);

impl Cli {
    /// parse_args: the namespace, or (having printed help, a version or an
    /// error) the exit status.
    pub fn parse_args(&self, args: &[String]) -> Result<Ns, i32> {
        let mut ns = Ns::new();
        let extras = self.parse_known(0, args, &mut ns)?;
        if !extras.is_empty() {
            return Err(self.error(0, &format!("unrecognized arguments: {}", extras.join(" "))));
        }
        Ok(ns)
    }

    fn parse_known(&self, pi: usize, args: &[String], ns: &mut Ns) -> Result<Vec<String>, i32> {
        let p = &self.parsers[pi];
        for a in &p.actions {
            if let (Some(d), Some(def)) = (&a.dest, &a.default) {
                ns.entry(d.clone()).or_insert_with(|| def.clone());
            }
        }
        match self.parse_inner(pi, args, ns) {
            Ok(x) => Ok(x),
            Err(Exit::Code(c)) => Err(c),
            Err(Exit::Arg(e)) => Err(self.error(pi, &e.text())),
        }
    }

    fn error(&self, pi: usize, msg: &str) -> i32 {
        eprint!("{}", self.format_usage(pi));
        eprintln!("{}: error: {}", self.parsers[pi].prog, msg);
        2
    }

    fn parse_optional(&self, p: &Parser, s: &str) -> Option<Vec<OptTuple>> {
        if s.is_empty() || !s.starts_with('-') {
            return None;
        }
        let map = p.option_map();
        if let Some(&(_, i)) = map.iter().find(|(o, _)| *o == s) {
            return Some(vec![(Some(i), s.to_string(), None, None)]);
        }
        if s.chars().count() == 1 {
            return None;
        }
        if let Some((o, v)) = s.split_once('=') {
            if let Some(&(_, i)) = map.iter().find(|(m, _)| *m == o) {
                return Some(vec![(Some(i), o.to_string(), Some("=".into()), Some(v.to_string()))]);
            }
        }
        let mut result = Vec::new();
        let second = s.chars().nth(1).unwrap();
        let (prefix, sep, explicit) = match s.split_once('=') {
            Some((a, b)) => (a, Some("=".to_string()), Some(b.to_string())),
            None => (s, None, None),
        };
        if second == '-' {
            for &(o, i) in &map {
                if o.starts_with(prefix) {
                    result.push((Some(i), o.to_string(), sep.clone(), explicit.clone()));
                }
            }
        } else {
            let short: String = s.chars().take(2).collect();
            let short_explicit: String = s.chars().skip(2).collect();
            for &(o, i) in &map {
                if o == short {
                    result.push((Some(i), o.to_string(), Some(String::new()), Some(short_explicit.clone())));
                } else if o.starts_with(prefix) {
                    result.push((Some(i), o.to_string(), sep.clone(), explicit.clone()));
                }
            }
        }
        if !result.is_empty() {
            return Some(result);
        }
        // a negative number, with no negative-number-like options
        let b = s.as_bytes();
        if b.len() >= 2 && (b[1].is_ascii_digit() || (b[1] == b'.' && b.len() >= 3 && b[2].is_ascii_digit())) {
            return None;
        }
        if s.contains(' ') {
            return None;
        }
        Some(vec![(None, s.to_string(), None, None)])
    }

    fn get_values(&self, a: &Action, strs: &[String]) -> Result<Val, ArgError> {
        let conv = |s: &str| -> Result<Val, ArgError> {
            if a.float {
                py_float(s).map(Val::Float).ok_or_else(|| err(Some(a), format!("invalid float value: {}", py_repr(s))))
            } else {
                Ok(Val::Str(s.to_string()))
            }
        };
        let check = |s: &str| -> Result<(), ArgError> {
            match &a.choices {
                Some(c) if !c.iter().any(|x| x == s) => Err(err(Some(a), format!(
                    "invalid choice: {}{} (choose from {})", py_repr(s),
                    // suggest_on_error=True (Python 3.14), on every parser
                    close_match(s, c).map(|m| format!(", maybe you meant {}?", py_repr(&m))).unwrap_or_default(),
                    c.iter().map(|x| py_repr(x)).collect::<Vec<_>>().join(", ")))),
                _ => Ok(()),
            }
        };
        if strs.is_empty() && a.nargs == Nargs::Optional {
            return Ok(if a.opts.is_empty() { a.default.clone().unwrap_or(Val::None) } else { Val::None });
        }
        if strs.is_empty() && a.nargs == Nargs::Star && a.opts.is_empty() {
            return Ok(match &a.default {
                Some(v) if *v != Val::None => v.clone(),
                _ => Val::List(Vec::new()),
            });
        }
        if strs.len() == 1 && matches!(a.nargs, Nargs::One | Nargs::Optional) {
            let v = conv(&strs[0])?;
            check(&strs[0])?;
            return Ok(v);
        }
        if a.nargs == Nargs::Parser {
            check(&strs[0])?;
            return Ok(Val::List(strs.to_vec()));
        }
        if a.nargs == Nargs::Zero {
            return Ok(Val::List(Vec::new()));
        }
        for s in strs {
            check(s)?;
        }
        Ok(Val::List(strs.to_vec()))
    }

    fn parse_inner(&self, pi: usize, args: &[String], ns: &mut Ns) -> Result<Vec<String>, Exit> {
        let p = &self.parsers[pi];
        let mut conflicts: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for g in &p.mutex {
            for (i, &a) in g.iter().enumerate() {
                let c = conflicts.entry(a).or_default();
                c.extend(&g[..i]);
                c.extend(&g[i + 1..]);
            }
        }
        let mut opt_at: BTreeMap<usize, Vec<OptTuple>> = BTreeMap::new();
        let mut pat = Vec::new();
        let mut dashdash = false;
        for (i, s) in args.iter().enumerate() {
            if dashdash {
                pat.push(b'A');
            } else if s == "--" {
                pat.push(b'-');
                dashdash = true;
            } else {
                match self.parse_optional(p, s) {
                    None => pat.push(b'A'),
                    Some(t) => {
                        opt_at.insert(i, t);
                        pat.push(b'O');
                    }
                }
            }
        }
        let mut seen: HashSet<usize> = HashSet::new();
        let mut seen_nd: HashSet<usize> = HashSet::new();
        let mut extras: Vec<String> = Vec::new();

        let mut take = |this: &Cli, ai: usize, strs: Vec<String>, ns: &mut Ns, extras: &mut Vec<String>| -> Result<(), Exit> {
            let a = &p.actions[ai];
            seen.insert(ai);
            let v = this.get_values(a, &strs).map_err(Exit::Arg)?;
            if !a.opts.is_empty() || !strs.is_empty() {
                seen_nd.insert(ai);
                for c in conflicts.get(&ai).into_iter().flatten() {
                    if seen_nd.contains(c) {
                        return Err(Exit::Arg(err(Some(a), format!("not allowed with argument {}", action_name(&p.actions[*c]).unwrap_or_default()))));
                    }
                }
            }
            match a.kind {
                Kind::Store => {
                    ns.insert(a.dest.clone().unwrap(), v);
                }
                Kind::StoreTrue => {
                    ns.insert(a.dest.clone().unwrap(), Val::Bool(true));
                }
                Kind::Help => {
                    print!("{}", this.format_help(pi));
                    return Err(Exit::Code(0));
                }
                Kind::Version => {
                    println!("{}", p.version.clone().unwrap_or_default());
                    return Err(Exit::Code(0));
                }
                Kind::Sub => {
                    let name = &strs[0];
                    ns.insert(a.dest.clone().unwrap(), Val::Str(name.clone()));
                    let si = p.subs.iter().find(|(n, _)| n == name).map(|(_, i)| *i).unwrap();
                    let mut sub = Ns::new();
                    let rest = this.parse_known(si, &strs[1..], &mut sub).map_err(Exit::Code)?;
                    ns.extend(sub);
                    extras.extend(rest);
                }
            }
            Ok(())
        };

        let mut positionals: Vec<usize> = p.positionals.clone();
        let mut unrecognized: Vec<String> = Vec::new();
        let pats = &pat[..];

        let consume_positionals = |start: usize, positionals: &mut Vec<usize>, ns: &mut Ns, unrec: &mut Vec<String>,
                                   take: &mut dyn FnMut(&Cli, usize, Vec<String>, &mut Ns, &mut Vec<String>) -> Result<(), Exit>|
         -> Result<usize, Exit> {
            let sel = &pats[start..];
            let mut counts = Vec::new();
            for i in (1..=positionals.len()).rev() {
                let groups: Vec<Vec<Tok>> = positionals[..i].iter().map(|&a| nargs_pattern(&p.actions[a])).collect();
                if let Some(c) = match_groups(&groups, sel) {
                    let end: usize = c.iter().sum();
                    counts = c;
                    if end < sel.len() && sel[end] == b'O' {
                        while counts.last() == Some(&0) {
                            counts.pop();
                        }
                    }
                    break;
                }
            }
            let mut idx = start;
            for (k, &n) in counts.iter().enumerate() {
                let ai = positionals[k];
                let a = &p.actions[ai];
                let mut strs: Vec<String> = args[idx..idx + n].to_vec();
                if a.nargs == Nargs::Parser {
                    if pats.get(idx) == Some(&b'-') {
                        let at = strs.iter().position(|s| s == "--").unwrap();
                        strs.remove(at);
                    }
                } else if pats[idx..idx + n].contains(&b'-') {
                    let at = strs.iter().position(|s| s == "--").unwrap();
                    strs.remove(at);
                }
                idx += n;
                take(self, ai, strs, ns, unrec)?;
            }
            positionals.drain(..counts.len());
            Ok(idx)
        };

        let max_opt = opt_at.keys().next_back().copied();
        let mut start = 0usize;
        if let Some(max_opt) = max_opt {
            while start <= max_opt {
                let mut next = start;
                while next <= max_opt && !opt_at.contains_key(&next) {
                    next += 1;
                }
                if start != next {
                    let end = consume_positionals(start, &mut positionals, ns, &mut unrecognized, &mut take)?;
                    if end > start {
                        start = end;
                        continue;
                    }
                    start = end;
                }
                if !opt_at.contains_key(&start) {
                    extras.extend(args[start..next].iter().cloned());
                    start = next;
                }
                // consume_optional
                let tuples = &opt_at[&start];
                if tuples.len() > 1 {
                    let opts: Vec<&str> = tuples.iter().map(|t| t.1.as_str()).collect();
                    return Err(Exit::Arg(err(None, format!("ambiguous option: {} could match {}", args[start], opts.join(", ")))));
                }
                let (mut action, mut ostr, mut sep, mut explicit) = tuples[0].clone();
                let mut todo: Vec<(usize, Vec<String>)> = Vec::new();
                let stop;
                loop {
                    let ai = match action {
                        None => {
                            extras.push(args[start].clone());
                            stop = start + 1;
                            break;
                        }
                        Some(ai) => ai,
                    };
                    let a = &p.actions[ai];
                    if let Some(ex) = explicit.clone() {
                        let count = match_groups(&[nargs_pattern(a)], b"A").map(|c| c[0]);
                        let second = ostr.chars().nth(1).unwrap_or('-');
                        if count == Some(0) && second != '-' && !ex.is_empty() {
                            if sep.as_deref().is_some_and(|s| !s.is_empty()) || ex.starts_with('-') {
                                return Err(Exit::Arg(err(Some(a), format!("ignored explicit argument {}", py_repr(&ex)))));
                            }
                            todo.push((ai, Vec::new()));
                            let first = ex.chars().next().unwrap();
                            let o = format!("-{}", first);
                            let map = p.option_map();
                            if let Some(&(_, ni)) = map.iter().find(|(m, _)| *m == o) {
                                action = Some(ni);
                                ostr = o;
                                let rest: String = ex.chars().skip(1).collect();
                                if rest.is_empty() {
                                    sep = None;
                                    explicit = None;
                                } else if let Some(r) = rest.strip_prefix('=') {
                                    sep = Some("=".into());
                                    explicit = Some(r.to_string());
                                } else {
                                    sep = Some(String::new());
                                    explicit = Some(rest);
                                }
                            } else {
                                extras.push(format!("-{}", ex));
                                stop = start + 1;
                                break;
                            }
                        } else if count == Some(1) {
                            todo.push((ai, vec![ex]));
                            stop = start + 1;
                            break;
                        } else {
                            return Err(Exit::Arg(err(Some(a), format!("ignored explicit argument {}", py_repr(&ex)))));
                        }
                    } else {
                        let s = start + 1;
                        let count = match match_groups(&[nargs_pattern(a)], &pats[s..]) {
                            Some(c) => c[0],
                            None => {
                                let msg = match a.nargs {
                                    Nargs::One => "expected one argument",
                                    Nargs::Optional => "expected at most one argument",
                                    _ => "expected at least one argument",
                                };
                                return Err(Exit::Arg(err(Some(a), msg.to_string())));
                            }
                        };
                        todo.push((ai, args[s..s + count].to_vec()));
                        stop = s + count;
                        break;
                    }
                }
                for (ai, strs) in todo {
                    take(self, ai, strs, ns, &mut unrecognized)?;
                }
                start = stop;
            }
        }
        let stop = consume_positionals(start, &mut positionals, ns, &mut unrecognized, &mut take)?;
        extras.extend(args[stop..].iter().cloned());

        let missing: Vec<String> = p.actions.iter().enumerate()
            .filter(|(i, a)| !seen.contains(i) && a.required)
            .filter_map(|(_, a)| action_name(a))
            .collect();
        if !missing.is_empty() {
            return Err(Exit::Arg(err(None, format!("the following arguments are required: {}", missing.join(", ")))));
        }
        extras.extend(unrecognized);
        Ok(extras)
    }
}

enum Exit {
    Code(i32),
    Arg(ArgError),
}

// ------------------------------------------------------------- formatting

struct Theme {
    usage: &'static str,
    prog: &'static str,
    heading: &'static str,
    summary_long_option: &'static str,
    summary_short_option: &'static str,
    summary_label: &'static str,
    summary_action: &'static str,
    long_option: &'static str,
    short_option: &'static str,
    label: &'static str,
    action: &'static str,
    reset: &'static str,
}

const COLOR: Theme = Theme {
    usage: "\x1b[1;34m",
    prog: "\x1b[1;35m",
    heading: "\x1b[1;34m",
    summary_long_option: "\x1b[36m",
    summary_short_option: "\x1b[32m",
    summary_label: "\x1b[33m",
    summary_action: "\x1b[32m",
    long_option: "\x1b[1;36m",
    short_option: "\x1b[1;32m",
    label: "\x1b[1;33m",
    action: "\x1b[1;32m",
    reset: "\x1b[0m",
};

const PLAIN: Theme = Theme {
    usage: "",
    prog: "",
    heading: "",
    summary_long_option: "",
    summary_short_option: "",
    summary_label: "",
    summary_action: "",
    long_option: "",
    short_option: "",
    label: "",
    action: "",
    reset: "",
};

/// _colorize.can_colorize() for stdout.
fn theme() -> &'static Theme {
    let env = |k: &str| std::env::var(k).ok();
    let on = match env("PYTHON_COLORS").as_deref() {
        Some("0") => false,
        Some("1") => true,
        _ => {
            if env("NO_COLOR").is_some_and(|v| !v.is_empty()) {
                false
            } else if env("FORCE_COLOR").is_some_and(|v| !v.is_empty()) {
                true
            } else if env("TERM").as_deref() == Some("dumb") {
                false
            } else {
                term::stdout_isatty()
            }
        }
    };
    if on { &COLOR } else { &PLAIN }
}

fn clen(s: &str) -> usize {
    s.chars().count()
}

/// Strip ANSI SGR sequences (_colorize.decolor).
fn decolor(s: &str) -> String {
    let mut out = String::new();
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\x1b' && it.peek() == Some(&'[') {
            it.next();
            for d in it.by_ref() {
                if d == 'm' {
                    break;
                }
            }
        } else {
            out.push(c);
        }
    }
    out
}

fn metavar(a: &Action, default: &str) -> String {
    if let Some(m) = &a.metavar {
        m.clone()
    } else if let Some(c) = &a.choices {
        format!("{{{}}}", c.join(","))
    } else {
        default.to_string()
    }
}

fn format_args(a: &Action, default: &str) -> String {
    let m = metavar(a, default);
    match a.nargs {
        Nargs::One => m,
        Nargs::Optional => format!("[{}]", m),
        Nargs::Star => format!("[{} ...]", m),
        Nargs::Plus => format!("{} [{} ...]", m, m),
        Nargs::Parser => format!("{} ...", m),
        Nargs::Zero => String::new(),
    }
}

fn dest_upper(a: &Action) -> String {
    a.dest.clone().unwrap_or_default().to_uppercase()
}

fn invocation(a: &Action, t: &Theme) -> String {
    if a.opts.is_empty() {
        return format!("{}{}{}", t.action, metavar(a, a.dest.as_deref().unwrap_or("")), t.reset);
    }
    let opts: Vec<String> = a.opts.iter()
        .map(|s| format!("{}{}{}", if clen(s) > 2 { t.long_option } else { t.short_option }, s, t.reset))
        .collect();
    if a.nargs == Nargs::Zero {
        opts.join(", ")
    } else {
        format!("{} {}{}{}", opts.join(", "), t.label, format_args(a, &dest_upper(a)), t.reset)
    }
}

/// textwrap.TextWrapper._split with break_on_hyphens.
fn wrap_chunks(text: &str) -> Vec<String> {
    let c: Vec<char> = text.chars().collect();
    let ws = |x: char| matches!(x, '\t' | '\n' | '\x0b' | '\x0c' | '\r' | ' ');
    let word = |x: char| x.is_alphanumeric() || x == '_';
    let wp = |x: char| word(x) || matches!(x, '!' | '"' | '\'' | '&' | '.' | ',' | '?');
    let letter = |x: char| word(x) && !x.is_numeric();
    let dashes_word = |i: usize| -> bool {
        // (?=-{2,}\w) at i
        let mut j = i;
        while j < c.len() && c[j] == '-' {
            j += 1;
        }
        j - i >= 2 && j < c.len() && word(c[j])
    };
    let mut out = Vec::new();
    let mut i = 0;
    while i < c.len() {
        if ws(c[i]) {
            let mut j = i;
            while j < c.len() && ws(c[j]) {
                j += 1;
            }
            out.push(c[i..j].iter().collect());
            i = j;
            continue;
        }
        if i > 0 && wp(c[i - 1]) && c[i] == '-' {
            let mut j = i;
            while j < c.len() && c[j] == '-' {
                j += 1;
            }
            if j - i >= 2 && j < c.len() && word(c[j]) {
                out.push(c[i..j].iter().collect());
                i = j;
                continue;
            }
        }
        // a word, possibly hyphenated: the shortest run that ends right
        let mut e = i + 1;
        let end = loop {
            // hyphenated: -(?:(?<=lt{2}-)|(?<=lt-lt-))(?=lt-?lt)
            if e < c.len() && c[e] == '-' {
                let behind = (e >= 2 && letter(c[e - 1]) && letter(c[e - 2]))
                    || (e >= 3 && letter(c[e - 1]) && c[e - 2] == '-' && letter(c[e - 3]));
                let ahead = e + 1 < c.len() && letter(c[e + 1])
                    && ((e + 2 < c.len() && letter(c[e + 2]))
                        || (e + 3 < c.len() && c[e + 2] == '-' && letter(c[e + 3])));
                if behind && ahead {
                    break e + 1;
                }
            }
            if e == c.len() || ws(c[e]) {
                break e;
            }
            if wp(c[e - 1]) && dashes_word(e) {
                break e;
            }
            e += 1;
        };
        out.push(c[i..end].iter().collect());
        i = end;
    }
    out
}

/// textwrap.wrap(text, width) (argparse has already collapsed whitespace).
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    let mut chunks: Vec<String> = wrap_chunks(text);
    chunks.reverse();
    let mut lines: Vec<String> = Vec::new();
    let blank = |s: &str| s.trim().is_empty();
    while !chunks.is_empty() {
        let mut cur: Vec<String> = Vec::new();
        let mut cur_len = 0usize;
        let w = width as isize;
        if blank(chunks.last().unwrap()) && !lines.is_empty() {
            chunks.pop();
        }
        while let Some(ch) = chunks.last() {
            let l = clen(ch) as isize;
            if cur_len as isize + l <= w {
                cur_len += l as usize;
                cur.push(chunks.pop().unwrap());
            } else {
                break;
            }
        }
        if let Some(ch) = chunks.last() {
            if clen(ch) as isize > w {
                let space_left = if w < 1 { 1 } else { w - cur_len as isize };
                if space_left > 0 {
                    let chunk: Vec<char> = ch.chars().collect();
                    let sl = space_left as usize;
                    let mut end = sl;
                    if chunk.len() > sl {
                        if let Some(h) = chunk[..sl].iter().rposition(|&x| x == '-') {
                            if h > 0 && chunk[..h].iter().any(|&x| x != '-') {
                                end = h + 1;
                            }
                        }
                    }
                    let end = end.min(chunk.len());
                    cur.push(chunk[..end].iter().collect());
                    *chunks.last_mut().unwrap() = chunk[end..].iter().collect();
                } else if cur.is_empty() {
                    cur.push(chunks.pop().unwrap());
                }
            }
        }
        if cur.last().is_some_and(|s| blank(s)) {
            cur.pop();
        }
        if !cur.is_empty() {
            lines.push(cur.concat());
        }
    }
    lines
}

impl Cli {
    fn width() -> usize {
        term::terminal_size().0.saturating_sub(2)
    }

    /// _format_usage: "usage: ...\n\n".
    fn usage_block(&self, pi: usize, t: &Theme, width: usize) -> String {
        let p = &self.parsers[pi];
        let prefix = "usage: ";
        let prog = p.prog.clone();
        let acts: Vec<usize> = (0..p.actions.len()).filter(|&i| !p.actions[i].help_suppressed).collect();
        // (colored, plain) parts, optionals first
        let mut parts: Vec<(String, String)> = Vec::new();
        let mut done: HashSet<usize> = HashSet::new();
        let group_of = |i: usize| p.mutex.iter().find(|g| g.contains(&i));
        let emit = |group: Vec<usize>, parts: &mut Vec<(String, String)>| {
            let start = parts.len();
            let in_group = group.len() > 1;
            for ai in group {
                let a = &p.actions[ai];
                let (c, plain) = if a.opts.is_empty() {
                    let mut part = format_args(a, a.dest.as_deref().unwrap_or(""));
                    if in_group && part.starts_with('[') && part.ends_with(']') {
                        part = part[1..part.len() - 1].to_string();
                    }
                    (format!("{}{}{}", t.summary_action, part, t.reset), part)
                } else {
                    let o = &a.opts[0];
                    let oc = if clen(o) > 2 { t.summary_long_option } else { t.summary_short_option };
                    let (mut c, mut plain) = if a.nargs == Nargs::Zero {
                        (format!("{}{}{}", oc, o, t.reset), o.clone())
                    } else {
                        let args = format_args(a, &dest_upper(a));
                        (format!("{}{} {}{}{}", oc, o, t.summary_label, args, t.reset), format!("{} {}", o, args))
                    };
                    if !(a.required || in_group) {
                        c = format!("[{}]", c);
                        plain = format!("[{}]", plain);
                    }
                    (c, plain)
                };
                parts.push((c, plain));
            }
            if in_group {
                let last = parts.len() - 1;
                parts[start].0.insert(0, '[');
                parts[start].1.insert(0, '[');
                for part in parts.iter_mut().take(last).skip(start) {
                    part.0.push_str(" |");
                    part.1.push_str(" |");
                }
                parts[last].0.push(']');
                parts[last].1.push(']');
            }
        };
        for &i in &acts {
            if p.actions[i].opts.is_empty() || done.contains(&i) {
                continue;
            }
            let g: Vec<usize> = match group_of(i) {
                Some(g) => g.iter().copied().filter(|x| acts.contains(x) && !done.contains(x)).collect(),
                None => vec![i],
            };
            done.extend(&g);
            emit(g, &mut parts);
        }
        let pos_start = parts.len();
        for &i in &acts {
            if p.actions[i].opts.is_empty() {
                emit(vec![i], &mut parts);
            }
        }
        let mut items = vec![(prog.clone(), prog.clone())];
        items.extend(parts.iter().filter(|(_, pl)| !pl.is_empty()).cloned());
        let plain_usage: Vec<&str> = items.iter().map(|(_, pl)| pl.as_str()).collect();
        let plain_usage = plain_usage.join(" ");
        let mut usage: String = items.iter().map(|(c, _)| c.as_str()).collect::<Vec<_>>().join(" ");
        let text_width = width;
        if clen(prefix) + clen(&plain_usage) > text_width {
            let opt_parts: Vec<(String, String)> = parts[..pos_start].to_vec();
            let pos_parts: Vec<(String, String)> = parts[pos_start..].to_vec();
            let get_lines = |parts: &[(String, String)], indent: &str, prefix: Option<&str>| -> Vec<String> {
                let mut lines: Vec<String> = Vec::new();
                let mut line: Vec<&str> = Vec::new();
                let il = clen(indent);
                let mut line_len = match prefix {
                    Some(p) => clen(p) as isize - 1,
                    None => il as isize - 1,
                };
                for (c, pl) in parts {
                    let part_len = clen(pl) as isize;
                    if line_len + 1 + part_len > text_width as isize && !line.is_empty() {
                        lines.push(format!("{}{}", indent, line.join(" ")));
                        line.clear();
                        line_len = il as isize - 1;
                    }
                    line.push(c);
                    line_len += part_len + 1;
                }
                if !line.is_empty() {
                    lines.push(format!("{}{}", indent, line.join(" ")));
                }
                if prefix.is_some() {
                    lines[0] = lines[0].chars().skip(il).collect();
                }
                lines
            };
            let prog_part = (prog.clone(), prog.clone());
            let lines: Vec<String> = if (clen(prefix) + clen(&prog)) as f64 <= 0.75 * text_width as f64 {
                let indent = " ".repeat(clen(prefix) + clen(&prog) + 1);
                if !opt_parts.is_empty() {
                    let mut v = vec![prog_part];
                    v.extend(opt_parts.iter().cloned());
                    let mut l = get_lines(&v, &indent, Some(prefix));
                    l.extend(get_lines(&pos_parts, &indent, None));
                    l
                } else if !pos_parts.is_empty() {
                    let mut v = vec![prog_part];
                    v.extend(pos_parts.iter().cloned());
                    get_lines(&v, &indent, Some(prefix))
                } else {
                    vec![prog.clone()]
                }
            } else {
                let indent = " ".repeat(clen(prefix));
                let all: Vec<(String, String)> = opt_parts.iter().chain(pos_parts.iter()).cloned().collect();
                let mut l = get_lines(&all, &indent, None);
                if l.len() > 1 {
                    l = get_lines(&opt_parts, &indent, None);
                    l.extend(get_lines(&pos_parts, &indent, None));
                }
                let mut v = vec![prog.clone()];
                v.extend(l);
                v
            };
            usage = lines.join("\n");
        }
        let rest = usage.strip_prefix(prog.as_str()).unwrap_or(&usage).to_string();
        format!("{}{}{}{}{}{}{}\n\n", t.usage, prefix, t.reset, t.prog, prog, t.reset, rest)
    }

    /// format_help's final tidy-up: no runs of blank lines, one newline at the end.
    fn tidy(s: String) -> String {
        let mut out = String::new();
        let mut nl = 0;
        for ch in s.chars() {
            if ch == '\n' {
                nl += 1;
                continue;
            }
            if nl > 0 {
                out.push_str(if nl >= 2 { "\n\n" } else { "\n" });
                nl = 0;
            }
            out.push(ch);
        }
        let out = out.trim_start_matches('\n').to_string();
        if out.is_empty() { out } else { out + "\n" }
    }

    /// `words` naming only command groups (each with subcommands), and
    /// nothing else: that group's help, as -h prints it. Bare `flashpod`
    /// (no words) is the top's.
    pub fn group_help(&self, words: &[String]) -> Option<String> {
        let mut pi = 0;
        for w in words {
            pi = self.parsers[pi].subs.iter().find(|(n, _)| n == w).map(|(_, i)| *i)?;
        }
        self.parsers[pi].sub_action.map(|_| self.format_help(pi))
    }

    pub fn format_usage(&self, pi: usize) -> String {
        Self::tidy(self.usage_block(pi, theme(), Self::width()))
    }

    fn format_help(&self, pi: usize) -> String {
        let t = theme();
        let width = Self::width();
        let p = &self.parsers[pi];
        let mut out = self.usage_block(pi, t, width);
        if let Some(d) = &p.description {
            // RawDescriptionHelpFormatter: kept as written
            out.push_str(d);
            out.push_str("\n\n");
        }
        let max_help_position = 24usize.min((width as isize - 20).max(4) as usize);
        let sections: [(&str, &Vec<usize>); 2] = [("positional arguments", &p.positionals), ("options", &p.optionals)];
        let mut max_len = 0usize;
        for (_, acts) in &sections {
            for &ai in acts.iter() {
                let a = &p.actions[ai];
                if a.help_suppressed {
                    continue;
                }
                max_len = max_len.max(clen(&decolor(&invocation(a, t))) + 2);
                if a.kind == Kind::Sub {
                    for s in &p.sub_help {
                        max_len = max_len.max(clen(&decolor(&invocation(s, t))) + 4);
                    }
                }
            }
        }
        let help_position = (max_len + 2).min(max_help_position);
        let fmt = |a: &Action, indent: usize| -> String {
            let help_width = (width as isize - help_position as isize).max(11) as usize;
            let action_width = help_position as isize - indent as isize - 2;
            let header = invocation(a, t);
            let plain = decolor(&header);
            let help = a.help.clone().unwrap_or_default();
            let (mut out, indent_first) = if help.is_empty() {
                (format!("{}{}\n", " ".repeat(indent), header), 0)
            } else if clen(&plain) as isize <= action_width {
                let pad = " ".repeat((action_width as usize).saturating_sub(clen(&plain)));
                (format!("{}{}{}  ", " ".repeat(indent), header, pad), 0)
            } else {
                (format!("{}{}\n", " ".repeat(indent), header), help_position)
            };
            if !help.trim().is_empty() {
                let collapsed = help.split_whitespace().collect::<Vec<_>>().join(" ");
                let lines = wrap(&collapsed, help_width);
                out.push_str(&format!("{}{}\n", " ".repeat(indent_first), lines[0]));
                for l in &lines[1..] {
                    out.push_str(&format!("{}{}\n", " ".repeat(help_position), l));
                }
            } else if !out.ends_with('\n') {
                out.push('\n');
            }
            out
        };
        for (title, acts) in &sections {
            let mut items = String::new();
            for &ai in acts.iter() {
                let a = &p.actions[ai];
                if a.help_suppressed {
                    continue;
                }
                items.push_str(&fmt(a, 2));
                if a.kind == Kind::Sub {
                    for s in &p.sub_help {
                        items.push_str(&fmt(s, 4));
                    }
                }
            }
            if !items.is_empty() {
                out.push_str(&format!("\n{}{}:{}\n{}\n", t.heading, title, t.reset, items));
            }
        }
        Self::tidy(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_like_textwrap() {
        // textwrap's own docstring example
        assert_eq!(wrap_chunks("Look, goof-ball -- use the -b option!"),
                   vec!["Look,", " ", "goof-", "ball", " ", "--", " ", "use", " ", "the", " ", "-b", " ", "option!"]);
    }

    #[test]
    fn floats_like_python() {
        assert_eq!(py_float(" 1_000.5 "), Some(1000.5));
        assert_eq!(py_float("1__0"), None);
        assert_eq!(py_float("abc"), None);
        assert_eq!(py_float("-inf"), Some(f64::NEG_INFINITY));
        assert_eq!(py_float("1e3"), Some(1000.0));
    }
}

#[cfg(test)]
mod difflib_tests {
    /// Answers from Python 3.14's difflib.get_close_matches(word, choices,
    /// 1) on typo'd command names, ties included.
    #[test]
    fn close_match_is_difflibs() {
        let cases: &[(&str, &[&str], Option<&str>)] = &[
        ("wite", &["show", "write", "backup", "disarm", "artists", "tracks", "extract", "games", "patch"], Some("write")),
        ("dchecbk", &["delete", "add", "inspect"], None),
        ("new", &["ls", "write", "games", "backup", "recovery"], None),
        ("lnit", &["bootrom", "arm", "list", "init", "write", "firmware", "partition", "disarm", "new", "apply", "add", "completion"], Some("list")),
        ("list", &["bootrom", "help", "rm"], None),
        ("exitrat", &["remove", "extract", "help", "inspect", "partition"], Some("extract")),
        ("partition", &["partition", "export"], Some("partition")),
        ("wsite", &["write", "arm"], Some("write")),
        ("adlbubs", &["playlist", "partition", "help", "export", "inspect", "apply", "albums", "list", "recovery", "restore", "new", "bootrom", "find", "extract"], Some("albums")),
        ("bootrom", &["image", "list", "emulate", "bootrom", "help", "rm", "disarm", "add", "rebuild"], Some("bootrom")),
        ("dsride", &["setup", "recovery", "write", "playlist", "extract", "delete", "help", "init", "games", "rm", "find"], None),
        ("remeve", &["help", "extract", "init", "partition", "remove", "rebuild", "disarm", "ls", "image", "playlist", "export", "setup"], Some("remove")),
        ("r", &["tracks", "rm", "playlist", "music", "extract", "apply", "firmware", "rebuild", "write", "new"], Some("rm")),
        ("add", &["remove", "add", "tracks", "bootrom", "setup"], Some("add")),
        ("mularte", &["playlist", "backup", "emulate", "new", "recovery", "add", "ls"], Some("emulate")),
        ("extract", &["extract", "rm", "patch"], Some("extract")),
        ("partition", &["delete", "setup", "add", "playlist", "emulate", "completion", "init", "new", "drive"], None),
        ("recovlry", &["apply", "list", "help", "show", "recovery", "rm", "init", "add", "games"], Some("recovery")),
        ("exporm", &["recovery", "write", "backup", "apply", "init", "disarm", "ls", "find", "patch", "add", "drive", "list", "export"], Some("export")),
        ("palth", &["list", "setup", "patch", "find", "artists", "check"], Some("patch")),
        ("help", &["image", "artists", "restore", "apply", "albums", "help", "music", "bootrom", "write", "check", "rebuild", "rm"], Some("help")),
        ("albums", &["rm", "firmware", "check", "export", "backup", "extract", "apply", "write", "rebuild", "partition", "albums", "artists", "games"], Some("albums")),
        ("tbacmkup", &["backup", "games", "rm", "add", "firmware", "delete", "new", "bootrom", "tracks", "find", "apply"], Some("backup")),
        ("rduild", &["drive", "find", "remove", "export", "backup", "rebuild"], Some("rebuild")),
        ("hblp", &["image", "tracks", "list", "extract", "add", "apply", "music", "playlist", "help"], Some("help")),
        ("lst", &["help", "playlist", "backup", "write", "artists", "list", "recovery", "firmware"], Some("list")),
        ("inet", &["export", "albums", "drive"], None),
        ("conmpletion", &["ls", "completion", "disarm", "init", "add", "backup", "emulate", "firmware", "albums", "help", "playlist", "drive", "list"], Some("completion")),
        ("backup", &["playlist", "extract", "find", "new", "delete", "backup", "arm"], Some("backup")),
        ("dlee", &["music", "backup", "new", "bootrom", "check", "disarm", "image", "find", "emulate", "artists", "restore", "patch", "rebuild", "extract"], None),
        ("debbete", &["delete", "export", "disarm", "list", "help", "ls", "add", "image"], Some("delete")),
        ("etroact", &["write", "partition", "inspect", "firmware", "drive", "extract", "arm", "rm", "check", "albums"], Some("extract")),
        ("image", &["image", "playlist"], Some("image")),
        ("usic", &["find", "music", "list", "bootrom", "games"], Some("music")),
        ("image", &["disarm", "image", "artists"], Some("image")),
        ("check", &["partition", "new", "check", "delete", "albums", "apply", "disarm", "restore"], Some("check")),
        ("new", &["completion", "tracks", "games", "show", "artists", "albums", "new", "backup", "remove", "rm", "export", "extract", "setup"], Some("new")),
        ("lsn", &["partition", "delete", "playlist", "artists", "inspect", "ls", "rebuild", "arm"], Some("ls")),
        ("extract", &["emulate", "disarm", "artists", "rm", "extract", "rebuild", "ls", "arm", "help", "firmware", "patch", "image"], Some("extract")),
        ("rrbuild", &["tracks", "export", "albums", "check", "bootrom"], None),
        ("write", &["rebuild", "games", "inspect", "write", "restore", "disarm", "completion", "albums", "recovery", "patch", "tracks", "list", "apply", "arm"], Some("write")),
        ("check", &["write", "setup", "apply", "inspect", "artists", "check", "rebuild", "image"], Some("check")),
        ("itge", &["inspect", "apply", "artists", "disarm", "emulate", "image", "firmware", "write", "remove", "setup", "backup", "help", "init", "show"], Some("write")),
        ("remove", &["albums", "tracks", "rm", "emulate", "patch", "extract", "rebuild", "list"], None),
        ("tnspect", &["patch", "emulate", "albums", "inspect", "tracks", "rm"], Some("inspect")),
        ("image", &["rebuild", "drive", "image"], Some("image")),
        ("llp", &["artists", "help", "list", "rebuild", "image", "new", "firmware", "backup", "check", "arm", "extract", "disarm", "albums", "partition"], None),
        ("hel", &["help", "extract", "check", "arm"], Some("help")),
        ("patch", &["new", "inspect", "patch", "setup", "recovery", "bootrom"], Some("patch")),
        ("add", &["ls", "list", "drive", "add", "check", "partition"], Some("add")),
        ("extract", &["inspect", "remove", "games", "rm", "music", "restore", "extract"], Some("extract")),
        ("write", &["new", "rebuild"], None),
        ("cimpletion", &["export", "remove", "partition", "write", "albums"], None),
        ("seeu", &["partition", "write", "image", "recovery", "setup", "playlist", "bootrom", "new", "rm", "export", "drive", "remove"], Some("setup")),
        ("brecovery", &["apply", "artists", "firmware", "drive", "recovery", "delete", "new", "backup", "emulate", "ls", "games", "extract"], Some("recovery")),
        ("r", &["delete", "firmware", "rm", "init", "check"], Some("rm")),
        ("export", &["emulate", "arm", "playlist", "rm", "export"], Some("export")),
        ("heop", &["restore", "image", "help", "find", "setup", "delete", "games", "disarm", "firmware"], Some("help")),
        ("games", &["write", "games", "init"], Some("games")),
        ("ind", &["list", "rm", "patch", "albums", "init", "show", "arm", "setup", "help", "completion", "check", "find", "apply", "export"], Some("find")),
        ("recbvry", &["backup", "apply", "firmware", "recovery", "new", "patch", "write"], Some("recovery")),
        ("setu", &["setup", "inspect"], Some("setup")),
        ("tracks", &["recovery", "apply", "tracks", "init"], Some("tracks")),
        ("ew", &["new", "remove", "delete", "check", "apply"], Some("new")),
        ("init", &["delete", "init", "emulate", "apply", "firmware", "rebuild"], Some("init")),
        ("esulatem", &["find", "rebuild", "restore", "drive", "music", "arm", "emulate", "help", "new", "artists", "tracks", "check"], Some("emulate")),
        ("add", &["add", "partition", "completion", "list", "help", "init", "write", "extract", "firmware", "games", "delete"], Some("add")),
        ("helep", &["partition", "find", "drive", "check", "help", "delete", "games", "rebuild"], Some("help")),
        ("write", &["export", "patch", "find", "tracks", "remove", "write", "ls", "bootrom", "drive", "inspect", "arm", "check", "new"], Some("write")),
        ("ini", &["drive", "extract", "disarm", "music", "backup", "tracks", "firmware", "artists", "init", "help", "find"], Some("init")),
        ("haetp", &["emulate", "show", "init", "extract", "music", "inspect", "remove", "rebuild"], None),
        ("arm", &["setup", "backup", "drive", "partition", "check", "delete", "new", "help", "arm"], Some("arm")),
        ("trackn", &["firmware", "new", "delete", "remove", "find", "tracks", "list", "rebuild", "partition", "restore", "rm", "write"], Some("tracks")),
        ("recoaedry", &["extract", "write", "init"], None),
        ("ew", &["init", "backup", "albums", "add", "partition", "arm", "check", "extract", "new", "find", "disarm", "apply"], Some("new")),
        ("inspoect", &["rm", "playlist", "rebuild", "inspect", "write", "export", "backup"], Some("inspect")),
        ("finn", &["write", "list", "albums"], None),
        ("in", &["patch", "emulate", "inspect", "init", "image", "artists", "disarm", "arm", "setup", "remove", "check", "find", "playlist"], Some("init")),
        ("playlist", &["find", "rebuild", "playlist"], Some("playlist")),
        ("artists", &["artists", "inspect", "add", "write"], Some("artists")),
        ("erport", &["add", "check", "export", "restore", "partition"], Some("export")),
        ("eemulate", &["find", "help", "extract", "emulate", "partition"], Some("emulate")),
        ("albusms", &["artists", "emulate", "patch", "backup", "rebuild", "help", "albums"], Some("albums")),
        ("dnletu", &["image", "delete", "patch", "show"], Some("delete")),
        ("playlist", &["apply", "ls", "artists", "init", "playlist"], Some("playlist")),
        ("bnackup", &["setup", "firmware", "write"], None),
        ("find", &["check", "emulate", "find", "export", "list", "bootrom"], Some("find")),
        ("cew", &["apply", "setup", "check", "tracks", "rm", "write", "add", "artists", "init", "inspect", "emulate", "new", "find", "help"], Some("new")),
        ("nenw", &["partition", "write", "add", "find", "new"], Some("new")),
        ("ilag", &["image", "drive", "apply", "emulate"], Some("image")),
        ("appy", &["extract", "completion", "partition"], None),
        ("partition", &["partition", "restore", "export", "show", "new", "emulate", "apply", "albums", "backup"], Some("partition")),
        ("backup", &["help", "games", "patch", "rm", "drive", "export", "backup", "disarm"], Some("backup")),
        ("sretu", &["rebuild", "new", "drive", "delete", "patch", "inspect", "show", "backup", "add"], None),
        ("rebuild", &["partition", "apply", "export", "rm", "rebuild", "playlist", "show", "backup", "firmware", "bootrom", "music", "list", "find"], Some("rebuild")),
        ("new", &["tracks", "rm", "backup", "show", "new", "inspect", "find"], Some("new")),
        ("l", &["patch", "ls"], Some("ls")),
        ("remove", &["extract", "albums", "artists", "bootrom"], None),
        ("resmve", &["completion", "extract", "ls", "tracks", "artists", "drive", "new"], None),
        ("parntition", &["playlist", "emulate", "backup", "partition", "init", "check", "music", "restore"], Some("partition")),
        ("emulateb", &["bootrom", "firmware", "setup", "new", "export", "music", "patch", "backup", "write", "init", "emulate", "delete"], Some("emulate")),
        ("cdd", &["new", "add", "list"], Some("add")),
        ("drive", &["artists", "drive", "tracks", "firmware", "ls", "show"], Some("drive")),
        ("firmware", &["write", "help", "list", "arm", "remove", "tracks", "rm", "firmware", "apply", "games", "disarm"], Some("firmware")),
        ("albums", &["bootrom", "disarm", "new", "delete", "tracks", "check", "export", "patch", "albums", "arm"], Some("albums")),
        ("oarm", &["drive", "image", "arm", "delete", "music", "show", "apply", "ls", "rebuild", "patch", "completion", "extract", "bootrom"], Some("arm")),
        ("eioit", &["restore", "disarm", "backup", "image", "ls", "rm", "export", "write", "init"], Some("init")),
        ("new", &["add", "games", "new", "image", "arm", "init", "check", "artists"], Some("new")),
        ("gcames", &["games", "partition"], Some("games")),
        ("ed", &["write", "new", "add", "list", "drive", "partition", "find", "show", "artists", "firmware", "arm", "apply", "albums"], None),
        ("write", &["drive", "partition", "write", "init", "music", "setup", "patch", "check", "show", "arm"], Some("write")),
        ("emuate", &["emulate", "apply"], Some("emulate")),
        ("albdums", &["help", "remove", "albums", "partition", "completion", "drive", "tracks", "inspect"], Some("albums")),
        ("rm", &["write", "patch", "extract", "backup", "arm", "tracks", "new", "rebuild", "firmware", "artists", "albums"], Some("arm")),
        ("firmware", &["tracks", "image", "disarm", "inspect", "albums", "firmware", "rebuild", "recovery", "ls", "rm"], Some("firmware")),
        ("hmlp", &["drive", "export", "ls", "games", "help", "backup", "init", "restore", "disarm", "artists", "check", "playlist", "new", "image"], Some("help")),
        ("artists", &["artists", "find"], Some("artists")),
        ("deirte", &["patch", "partition", "restore", "extract", "delete"], Some("delete")),
        ("inspect", &["find", "albums", "firmware", "delete", "backup", "inspect", "playlist", "artists"], Some("inspect")),
        ("bootrom", &["bootrom", "setup", "completion"], Some("bootrom")),
        ("upplsy", &["extract", "apply"], Some("apply")),
        ("exort", &["export", "setup", "drive", "image", "init", "restore", "recovery"], Some("export")),
        ("firnware", &["arm", "firmware", "music", "check", "inspect", "artists"], Some("firmware")),
        ("restsrbe", &["emulate", "restore", "find"], Some("restore")),
        ("afirmware", &["rm", "inspect", "firmware", "remove", "help", "arm", "patch"], Some("firmware")),
        ("patch", &["games", "setup", "patch", "image", "tracks", "extract", "help"], Some("patch")),
        ("hrelp", &["albums", "rm", "remove", "delete", "partition", "new", "apply", "playlist", "add", "disarm", "music"], None),
        ("setup", &["firmware", "patch", "arm", "setup", "extract", "ls", "find", "apply", "rebuild", "playlist"], Some("setup")),
        ("eulate", &["list", "delete", "inspect", "recovery", "drive", "arm", "init", "patch", "partition", "firmware"], Some("delete")),
        ("games", &["games", "albums"], Some("games")),
        ("apply", &["check", "emulate", "arm", "apply"], Some("apply")),
        ("rbuid", &["delete", "arm", "drive", "show", "setup", "games", "tracks", "write", "image", "new", "rm", "add"], None),
        ("setup", &["extract", "add", "check", "partition", "recovery", "remove", "completion", "playlist", "artists", "image", "setup", "inspect", "arm", "write"], Some("setup")),
        ("albume", &["games", "tracks", "emulate", "albums", "playlist", "partition", "new", "patch", "setup", "disarm", "arm", "inspect"], Some("albums")),
        ("extrac", &["tracks", "list", "extract", "recovery", "playlist"], Some("extract")),
        ("emove", &["playlist", "remove", "check", "export", "add", "artists", "bootrom", "tracks"], Some("remove")),
        ("sntup", &["extract", "check", "setup", "rm", "patch", "backup", "bootrom", "export", "artists", "show"], Some("setup")),
        ("baockup", &["find", "backup", "tracks", "rebuild", "image", "arm"], Some("backup")),
        ("find", &["find", "firmware", "inspect", "apply", "games", "music", "init", "drive", "rebuild", "tracks", "delete"], Some("find")),
        ("puatch", &["list", "patch", "init"], Some("patch")),
        ("inspuect", &["extract", "add", "partition", "completion", "inspect", "remove", "albums", "firmware"], Some("inspect")),
        ("delete", &["backup", "apply", "remove", "tracks", "ls"], None),
        ("backups", &["rm", "firmware", "completion", "ls", "patch", "recovery", "write", "list", "backup", "rebuild", "add", "tracks"], Some("backup")),
        ("ls", &["inspect", "patch", "delete", "rebuild", "extract", "recovery", "partition", "playlist", "backup"], None),
        ("setupi", &["find", "music", "games", "help", "setup", "disarm", "firmware", "inspect", "tracks", "show", "backup"], Some("setup")),
        ("emculate", &["rebuild", "image", "emulate", "albums", "tracks", "delete", "remove", "setup", "rm", "firmware", "find"], Some("emulate")),
        ("cestore", &["completion", "tracks", "restore", "ls", "list", "inspect", "remove"], Some("restore")),
        ("music", &["recovery", "rebuild", "games", "restore", "extract", "music", "arm", "albums", "help", "completion", "init", "partition", "playlist", "add"], Some("music")),
        ("tracks", &["completion", "new", "list", "find", "help", "remove", "albums", "patch", "extract", "tracks", "music", "recovery", "games", "arm"], Some("tracks")),
        ("disarm", &["write", "restore", "partition"], None),
        ("ab", &["ac", "ad"], None),
        ("ab", &["ad", "ac"], None),
        ("rm", &["ls", "rmm", "rn"], Some("rmm")),
        ("", &["a"], None),
        ("x", &["x"], Some("x")),
        ];
        for (w, cs, want) in cases {
            let cs: Vec<String> = cs.iter().map(|c| c.to_string()).collect();
            assert_eq!(super::close_match(w, &cs).as_deref(), *want, "{:?} in {:?}", w, cs);
        }
    }
}
