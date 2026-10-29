//! One JSON command in, one JSON answer out: the library's operations for a
//! host that isn't Rust (emupod's offline app through WebAssembly, a server,
//! the parity harness). The host supplies the card (a [`BlockDevice`]) and a
//! [`Host`]: the music files an add reads, progress, cancel.
//!
//! A command is `{"op": ..., ...}`; the answer is `{"ok": true, "value": ...}`
//! or `{"ok": false, "error": {"code", "message", "details"}}`, the same
//! values and errors as the Python library's `to_dict()`. Ids and "now" come
//! from the command's "seed" and "now" (see [`Entropy`]); a host passes
//! random ones. A firmware file, where an operation takes one, is the `blob`.
//!
//! Operations and their fields (all optional unless marked):
//!
//! | op | fields | value |
//! |---|---|---|
//! | `min_card_bytes` | | bytes |
//! | `catalog` | | firmware.json's entries |
//! | `check_firmware` | blob* | `{"firmware_bytes"}` |
//! | `inspect` | `path` (the name it reports), `db_limit` | CardInfo |
//! | `tracks` | | `{"name", "tracks"}` |
//! | `create` | `size`* | CardInfo of the new blank card (the host made an empty file) |
//! | `flash` | blob*, `lba48`, `max_data_gb`, `format` (true), `arm` | FlashResult |
//! | `flash_firmware` | blob* | FirmwareResult |
//! | `format_data` | | FormatResult |
//! | `init` | `name` ("iPod"), `overwrite` | Result |
//! | `remove` | `what`* (ids, or `["artist"|"album", words...]`) | removed tracks |
//! | `plan_add` | `files`*, `expand` (true), `assess` (true), `db_limit` | AddPlan |
//! | `add` | `files`*, `full` ("stop"/"fit"), `ceiling` ("refuse"/"fit"/"force"), `db_limit` | AddReport |
//! | `rebuild` | `name` ("iPod"), `save` (true) | Result |
//!
//! The firmware blob may be a raw image, a `.bin.gz`, an `.ipsw`, or (an
//! addition over the Python library, for uploads) a zip holding an `.ipsw`.

use crate::block::BlockDevice;
use crate::entropy::Entropy;
use crate::inspect::Options;
use crate::library::{Hooks, TagResult, TagSource};
use crate::tags::{self, ReadAt};
use crate::{card, firmware, inspect, ops, Error, ErrorKind};
use serde_json::{json, Value};
use std::io;

/// What the library asks of its host during a command.
pub trait Host: Hooks {
    /// The music file `path` an add names, or None when there is none.
    fn source(&mut self, path: &str) -> Option<Box<dyn ReadAt + '_>>;
}

/// Run one command.
pub(crate) fn run(cmd: &Value, blob: &[u8], dev: &mut dyn BlockDevice, host: &mut dyn Host) -> Value {
    match run_op(cmd, blob, dev, host) {
        Ok(v) => json!({"ok": true, "value": v}),
        Err(e) => json!({"ok": false, "error": e.to_json()}),
    }
}

/// Run one command given as JSON text.
pub fn run_json(cmd: &[u8], blob: &[u8], dev: &mut dyn BlockDevice, host: &mut dyn Host) -> Value {
    match serde_json::from_slice::<Value>(cmd) {
        Ok(c) => run(&c, blob, dev, host),
        Err(e) => json!({"ok": false, "error": {"code": "error", "message": format!("bad command: {}", e), "details": {}}}),
    }
}

/// The host, shared by the roles the library asks for separately (tags,
/// progress, the source bytes). The library never uses two at once, so each
/// call borrows it in turn.
type Shared<'h> = std::cell::RefCell<&'h mut dyn Host>;

/// The tags of the host's files (the Rust reader).
struct HostTags<'a, 'h>(&'a Shared<'h>);

impl TagSource for HostTags<'_, '_> {
    fn read(&mut self, path: &str) -> TagResult {
        let mut h = self.0.borrow_mut();
        let r = match h.source(path) {
            Some(mut f) => tags::audio_info(path, f.as_mut()),
            None => TagResult::Unreadable(format!("[Errno 2] No such file or directory: '{}'", path)),
        };
        r
    }
    fn read_stream(&mut self, path: &str, file: &mut dyn ReadAt) -> TagResult {
        tags::audio_info(path, file)
    }
}

/// The host's progress and cancel.
struct HostHooks<'a, 'h>(&'a Shared<'h>);

impl Hooks for HostHooks<'_, '_> {
    fn progress(&mut self, stage: &str, done: u64, total: u64, item: &str) {
        self.0.borrow_mut().progress(stage, done, total, item)
    }
    fn cancelled(&mut self) -> bool {
        self.0.borrow_mut().cancelled()
    }
}

/// A source file's bytes (an add's copy).
fn read_all(shared: &Shared, path: &str) -> io::Result<Vec<u8>> {
    let mut h = shared.borrow_mut();
    let mut f = h
        .source(path)
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "No such file or directory"))?;
    let n = f.size()? as usize;
    let mut v = vec![0u8; n];
    let mut got = 0;
    while got < n {
        let k = f.read_at(got as u64, &mut v[got..])?;
        if k == 0 {
            break;
        }
        got += k;
    }
    v.truncate(got);
    Ok(v)
}

fn entropy(c: &Value) -> Entropy {
    Entropy::new(c["seed"].as_u64().unwrap_or(1), c["now"].as_i64().unwrap_or(0))
}

fn strings(c: &Value, key: &str) -> Vec<String> {
    c[key].as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default()
}

fn flag(c: &Value, key: &str, default: bool) -> bool {
    c[key].as_bool().unwrap_or(default)
}

fn options(c: &Value) -> Options {
    let mut o = Options::default();
    if let Some(n) = c["db_limit"].as_i64() {
        o.db_limit = n;
        o.db_limit_set = true;
    }
    o
}

/// card._firmware_bytes, and a zip holding an .ipsw (the largest one).
pub(crate) fn firmware_of(blob: &[u8]) -> Result<Vec<u8>, Error> {
    match card::firmware_bytes(blob, false) {
        Ok(fw) => Ok(fw),
        Err(first) => {
            use std::io::Read;
            if let Ok(mut z) = zip::ZipArchive::new(io::Cursor::new(blob)) {
                let mut best: Option<(usize, u64)> = None;
                for i in 0..z.len() {
                    if let Ok(f) = z.by_index(i) {
                        if !f.is_dir()
                            && f.name().to_lowercase().ends_with(".ipsw")
                            && best.map_or(true, |(_, s)| f.size() > s)
                        {
                            best = Some((i, f.size()));
                        }
                    }
                }
                if let Some((i, _)) = best {
                    let mut inner = Vec::new();
                    if let Ok(mut f) = z.by_index(i) {
                        if f.read_to_end(&mut inner).is_ok() {
                            return card::firmware_bytes(&inner, false);
                        }
                    }
                }
            }
            Err(first)
        }
    }
}

fn to_value<T: serde::Serialize>(v: T) -> Value {
    serde_json::to_value(v).unwrap()
}

fn run_op(c: &Value, blob: &[u8], dev: &mut dyn BlockDevice, host: &mut dyn Host) -> Result<Value, Error> {
    let path = c["path"].as_str().unwrap_or("card.img");
    match c["op"].as_str().unwrap_or("") {
        "min_card_bytes" => Ok(json!(card::min_card_bytes())),
        "catalog" => Ok(Value::Array(firmware::catalog())),
        "check_firmware" => firmware_of(blob).map(|fw| json!({"firmware_bytes": fw.len()})),
        "inspect" => inspect::inspect(dev, path, options(c)).map(to_value),
        "tracks" => inspect::tracks(dev),
        "create" => {
            let size = card::blank_size(c["size"].as_u64().unwrap_or(0))?;
            dev.set_len(size)?;
            inspect::inspect(dev, path, options(c)).map(to_value)
        }
        "flash" => {
            let o = card::FlashOptions {
                lba48: flag(c, "lba48", false),
                max_data_gb: c["max_data_gb"].as_f64(),
                format: flag(c, "format", true),
                arm: flag(c, "arm", false),
            };
            card::flash(dev, firmware_of(blob)?, o).map(to_value)
        }
        "flash_firmware" => card::flash_firmware(dev, firmware_of(blob)?).map(to_value),
        "format_data" => card::format_data(dev).map(to_value),
        "init" => ops::init(dev, c["name"].as_str().unwrap_or("iPod"), flag(c, "overwrite", false), &mut entropy(c)),
        "remove" => ops::remove(dev, &strings(c, "what"), &mut entropy(c)),
        "plan_add" => {
            let shared = Shared::new(host);
            ops::plan_add(dev, &strings(c, "files"), &mut HostTags(&shared), &mut entropy(c), options(c),
                          flag(c, "expand", true), flag(c, "assess", true), &mut HostHooks(&shared))
                .map(|p| p.json)
        }
        "add" => {
            let shared = Shared::new(host);
            ops::add(dev, &strings(c, "files"), c["full"].as_str().unwrap_or("stop"),
                     c["ceiling"].as_str().unwrap_or("refuse"), &mut HostTags(&shared),
                     &mut |p: &str| read_all(&shared, p), &mut entropy(c), options(c), &mut HostHooks(&shared))
        }
        "rebuild" => {
            let shared = Shared::new(host);
            ops::rebuild(dev, c["name"].as_str().unwrap_or("iPod"), flag(c, "save", true), &mut HostTags(&shared),
                         &mut entropy(c), &mut HostHooks(&shared))
        }
        other => Err(Error::new(ErrorKind::Error, format!("no such operation: {}", other))),
    }
}

