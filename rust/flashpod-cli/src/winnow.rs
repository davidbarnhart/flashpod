//! cli._winnow and its two selectors (_select_cursor, _select_lines): trim
//! an add batch that's bigger than the free space, whole artists or albums
//! at a time.

use crate::data::fmt_size;
use crate::term;
use flashpod_core::itunesdb::Track;
use flashpod_core::text::casefold;
use std::collections::HashSet;
use std::io::Write;

const NAME_W: usize = 48;

fn orunknown(s: Option<&str>) -> String {
    match s {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => "(unknown)".to_string(),
    }
}

fn ellipsize(s: &str, width: usize) -> String {
    if s.chars().count() <= width {
        s.to_string()
    } else {
        s.chars().take(width - 1).collect::<String>() + "\u{2026}"
    }
}

type Checked = HashSet<usize>;

fn total(sizes: &[i64], checked: &Checked) -> i64 {
    checked.iter().map(|&i| sizes[i]).sum()
}

/// _select_cursor: up/down move, space toggles, a/n all/none, Enter
/// confirms when it fits, q/Esc cancels. Rewrites only the changed lines.
fn select_cursor(names: &[String], sizes: &[i64], mut checked: Checked, budget: i64) -> Option<Checked> {
    let n_rows = names.len();
    let mut cursor = 0usize;
    let mut msg = String::new();
    let row_text = |i: usize, cursor: usize, checked: &Checked| {
        let s = format!(" {} [{}]  {:<w$} {:>10}", if i == cursor { "\u{203a}" } else { " " },
                        if checked.contains(&i) { "x" } else { " " }, ellipsize(&names[i], NAME_W),
                        fmt_size(sizes[i]), w = NAME_W);
        if i == cursor { format!("\x1b[7m{}\x1b[0m", s) } else { s }
    };
    let total_text = |checked: &Checked| {
        let t = total(sizes, checked);
        let over = t - budget;
        let s = format!("       total: {:>10}", fmt_size(t));
        if over > 0 {
            format!("\x1b[31m{}   over by {}\x1b[0m", s, fmt_size(over))
        } else {
            format!("{}   ({} to spare)", s, fmt_size(-over))
        }
    };
    let help_text = |msg: &str| {
        format!("\x1b[2m  \u{2191}/\u{2193} move \u{b7} space toggle \u{b7} a all \u{b7} n none \u{b7} enter add \u{b7} q cancel\x1b[0m{}",
                if msg.is_empty() { String::new() } else { format!("   {}", msg) })
    };
    // the cursor parks on the line below the block; `up` rows above it
    let at = |up: usize, text: String| print!("\x1b[{}A\r\x1b[2K{}\x1b[{}B\r", up, text, up);

    let guard = term::Cbreak::new();
    let mut lines: Vec<String> = (0..n_rows).map(|i| row_text(i, cursor, &checked)).collect();
    lines.push(total_text(&checked));
    lines.push(help_text(&msg));
    print!("{}\n", lines.join("\n"));
    let _ = std::io::stdout().flush();
    let result = loop {
        let key = term::read_key();
        let had_msg = !msg.is_empty();
        msg.clear();
        match key {
            Some(k @ ("up" | "down")) => {
                let prev = cursor;
                cursor = if k == "down" { (cursor + 1) % n_rows } else { (cursor + n_rows - 1) % n_rows };
                at(n_rows + 2 - prev, row_text(prev, cursor, &checked));
                at(n_rows + 2 - cursor, row_text(cursor, cursor, &checked));
            }
            Some("space") => {
                if !checked.remove(&cursor) {
                    checked.insert(cursor);
                }
                at(n_rows + 2 - cursor, row_text(cursor, cursor, &checked));
                at(2, total_text(&checked));
            }
            Some(k @ ("all" | "none")) => {
                checked = if k == "all" { (0..n_rows).collect() } else { HashSet::new() };
                for i in 0..n_rows {
                    at(n_rows + 2 - i, row_text(i, cursor, &checked));
                }
                at(2, total_text(&checked));
            }
            Some("accept") => {
                if total(sizes, &checked) <= budget {
                    break Some(checked);
                }
                msg = "\x1b[31mstill over budget\x1b[0m".to_string();
            }
            Some("cancel") => break None,
            _ => {}
        }
        if had_msg || !msg.is_empty() {
            at(1, help_text(&msg));
        }
        let _ = std::io::stdout().flush();
    };
    drop(guard);
    println!();
    let _ = std::io::stdout().flush();
    result
}

/// re.split(r"[,\s]+", s).
fn split_tokens(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut in_sep = false;
    for (i, c) in s.char_indices() {
        let sep = c == ',' || c.is_whitespace();
        if sep && !in_sep {
            out.push(&s[start..i]);
        }
        if !sep && in_sep {
            start = i;
        }
        in_sep = sep;
    }
    out.push(if in_sep { "" } else { &s[start..] });
    out
}

/// _select_lines: numbered toggles typed at a prompt.
fn select_lines(names: &[String], sizes: &[i64], mut checked: Checked, budget: i64) -> Option<Checked> {
    loop {
        for (i, n) in names.iter().enumerate() {
            println!("  {:>2}  [{}]  {:<w$} {:>10}", i + 1, if checked.contains(&i) { "x" } else { " " },
                     ellipsize(n, NAME_W), fmt_size(sizes[i]), w = NAME_W);
        }
        let t = total(sizes, &checked);
        let over = t - budget;
        let mut line = format!("              total: {:>10}", fmt_size(t));
        if over > 0 {
            line += &format!("   over by {}", fmt_size(over));
            if term::stdout_isatty() {
                line = format!("\x1b[31m{}\x1b[0m", line);
            }
        } else {
            line += &format!("   ({} to spare)", fmt_size(-over));
        }
        println!("{}", line);
        let ans = match term::input_line(&format!("Toggle 1-{}, (a)ll, (n)one, Enter=add, q=quit: ", names.len())) {
            Some(a) => a.trim().to_lowercase(),
            None => {
                println!();
                return None;
            }
        };
        match ans.as_str() {
            "q" | "quit" => return None,
            "a" => {
                checked = (0..names.len()).collect();
                continue;
            }
            "n" => {
                checked.clear();
                continue;
            }
            "" => {
                if t > budget {
                    println!("  still over budget \u{2014} trim more, or q to cancel.");
                    continue;
                }
                return Some(checked);
            }
            _ => {}
        }
        for tok in split_tokens(&ans) {
            let k = match tok.parse::<usize>() {
                Ok(k) if tok.bytes().all(|b| b.is_ascii_digit()) && (1..=names.len()).contains(&k) => k - 1,
                _ => {
                    println!("  ?  didn't understand '{}'", tok);
                    break;
                }
            };
            if !checked.remove(&k) {
                checked.insert(k);
            }
        }
    }
}

/// _winnow: the kept subset of `pending` (indices, in group order), empty
/// if the user cleared the selection, None if they quit or nothing fits.
pub fn winnow(pending: &[(String, Track)], budget: i64) -> Option<Vec<usize>> {
    let artists: HashSet<String> = pending.iter().map(|(_, t)| casefold(&orunknown(t.artist.as_deref()))).collect();
    let by_artist = artists.len() >= 2;
    let label_of = |t: &Track| orunknown(if by_artist { t.artist.as_deref() } else { t.album.as_deref() });
    let mut groups: Vec<(String, Vec<usize>)> = Vec::new();
    for (i, (_, t)) in pending.iter().enumerate() {
        let l = label_of(t);
        match groups.iter_mut().find(|(n, _)| *n == l) {
            Some((_, v)) => v.push(i),
            None => groups.push((l, vec![i])),
        }
    }
    groups.sort_by_cached_key(|(n, _)| casefold(n));
    let names: Vec<String> = groups.iter().map(|(n, _)| n.clone()).collect();
    let sizes: Vec<i64> = groups.iter().map(|(_, v)| v.iter().map(|&i| pending[i].1.size as i64).sum()).collect();
    let unit = if by_artist { "artist" } else { "album" };

    let min = *sizes.iter().min().unwrap();
    if min > budget {
        let smallest = sizes.iter().position(|&s| s == min).unwrap();
        eprintln!("flashpod music add: even your smallest {} \"{}\" ({}) is bigger than the {} free. Nothing fits.",
                  unit, names[smallest], fmt_size(min), fmt_size(budget));
        return None;
    }

    // artists smallest-first (fit the most); albums alphabetical, until one
    // doesn't fit
    let mut checked = HashSet::new();
    let mut room = budget;
    if by_artist {
        let mut order: Vec<usize> = (0..names.len()).collect();
        order.sort_by_key(|&i| sizes[i]);
        for i in order {
            if sizes[i] <= room {
                checked.insert(i);
                room -= sizes[i];
            }
        }
    } else {
        for i in 0..names.len() {
            if sizes[i] > room {
                break;
            }
            checked.insert(i);
            room -= sizes[i];
        }
    }
    let batch: i64 = sizes.iter().sum();
    println!("flashpod music add: this batch is {} but only {} is free. I can fit {} of your {} {}s \u{2014} pick what to keep:",
             fmt_size(batch), fmt_size(budget), checked.len(), names.len(), unit);

    let fits = names.len() + 3 <= term::terminal_size().1;
    let cursor = term::stdin_isatty() && term::stdout_isatty() && fits;
    let kept = if cursor { select_cursor(&names, &sizes, checked, budget) } else { select_lines(&names, &sizes, checked, budget) }?;
    Some((0..names.len()).filter(|i| kept.contains(i)).flat_map(|i| groups[i].1.iter().copied()).collect())
}
