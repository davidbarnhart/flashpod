//! `flashpod music export` (#105, design in #69): every track off the iPod
//! into DEST/Artist/Album/NN Title.ext (flashpod_core::export plans the
//! names), then the files no track points at, on request. Read-only on the
//! iPod. Each file goes to NAME.part and is renamed when complete, so an
//! interrupted run leaves no short file; a run again skips what is already
//! there at the same size (resume), and a different file of the same name
//! gets " (2)", " (3)", ...

use flashpod_core::export::{fat_files, plan_export};
use flashpod_core::fat::Fat32;
use flashpod_core::itunesdb::Library;
use std::path::{Path, PathBuf};

/// Where the music is read from: an OS mount, or the FAT driver over an
/// image or a device.
pub trait Source {
    fn library(&mut self) -> Result<Library, String>;
    /// Every regular file, as ('/' path from the volume root, size).
    fn files(&mut self) -> Result<Vec<(String, u64)>, String>;
    fn read(&mut self, path: &str) -> Result<Vec<u8>, String>;
}

pub struct MountSource(pub String);

impl Source for MountSource {
    fn library(&mut self) -> Result<Library, String> {
        let mut t = crate::mount::MountTarget::new(&self.0);
        flashpod_core::ops::load(&mut t, &mut crate::entropy()).map_err(|e| e.message)
    }
    fn files(&mut self) -> Result<Vec<(String, u64)>, String> {
        let mut out = Vec::new();
        let mut stack = vec![PathBuf::from(&self.0)];
        while let Some(dir) = stack.pop() {
            let rd = std::fs::read_dir(&dir).map_err(|e| format!("{}: {}", dir.display(), e))?;
            for e in rd.flatten() {
                let ft = match e.file_type() {
                    Ok(t) => t,
                    Err(_) => continue,
                };
                if ft.is_dir() {
                    stack.push(e.path());
                } else if ft.is_file() {
                    let rel = e.path().strip_prefix(&self.0).map(|p| p.to_string_lossy().replace('\\', "/")).unwrap_or_default();
                    out.push((rel, e.metadata().map(|m| m.len()).unwrap_or(0)));
                }
            }
        }
        out.sort();
        Ok(out)
    }
    fn read(&mut self, path: &str) -> Result<Vec<u8>, String> {
        std::fs::read(Path::new(&self.0).join(path)).map_err(|e| e.to_string())
    }
}

pub struct FatSource<'a>(pub Fat32<'a>);

impl Source for FatSource<'_> {
    fn library(&mut self) -> Result<Library, String> {
        match flashpod_core::ops::load_library(&mut self.0, &mut crate::entropy()) {
            Ok(Some(l)) => Ok(l),
            Ok(None) => Err("no iTunesDB on it (is this an iPod? run `flashpod music init` on a fresh card)".into()),
            Err(e) => Err(e.message),
        }
    }
    fn files(&mut self) -> Result<Vec<(String, u64)>, String> {
        fat_files(&mut self.0)
    }
    fn read(&mut self, path: &str) -> Result<Vec<u8>, String> {
        self.0.read_file(path)?.ok_or_else(|| format!("{} vanished", path))
    }
}

/// `path`, or the first of "stem (2).ext", "stem (3).ext", ... that is free.
fn free_name(path: &Path) -> PathBuf {
    let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let ext = path.extension().map(|s| format!(".{}", s.to_string_lossy())).unwrap_or_default();
    let mut k = 2;
    loop {
        let cand = path.with_file_name(format!("{} ({}){}", stem, k, ext));
        if !cand.exists() {
            return cand;
        }
        k += 1;
    }
}

enum Put {
    Wrote,
    AlreadyThere,
}

/// Write `data` at `dest`, unless a file of the same size is there already
/// (a resumed run); a different file there moves this one to " (2)".
fn put(dest: &Path, data: &[u8]) -> std::io::Result<Put> {
    let target = match std::fs::metadata(dest) {
        Ok(m) if m.len() == data.len() as u64 => return Ok(Put::AlreadyThere),
        Ok(_) => free_name(dest),
        Err(_) => dest.to_path_buf(),
    };
    if let Some(dir) = target.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut part = target.clone().into_os_string();
    part.push(".part");
    std::fs::write(&part, data)?;
    std::fs::rename(&part, &target)?;
    Ok(Put::Wrote)
}

fn mib(n: u64) -> String {
    format!("{:.1} MiB", n as f64 / (1 << 20) as f64)
}

/// The export, given an open source. `others`: Some(answer) from
/// --other-files / --no-other-files, None to ask on a terminal (and not
/// copy them off one).
pub fn export(src: &mut dyn Source, from: &str, dest: Option<&str>, others: Option<bool>) -> i32 {
    let lib = match src.library() {
        Ok(l) => l,
        Err(e) => {
            eprintln!("flashpod music export: {}: {}", from, e);
            return 1;
        }
    };
    // #69: by default ./<the iPod's name> (not ~/Music, iTunes' own place)
    let default = format!("./{}", flashpod_core::export::sanitize(&lib.name, "iPod"));
    let dest = dest.unwrap_or(&default);
    let files = match src.files() {
        Ok(f) => f,
        Err(e) => {
            eprintln!("flashpod music export: couldn't list the files on {}: {}", from, e);
            return 1;
        }
    };
    let plan = plan_export(&lib, &files);
    let total: u64 = plan.tracks.iter().map(|t| t.size).sum();
    println!("Exporting {} tracks ({}) from {} to {}", plan.tracks.len(), mib(total), from, dest);
    let (mut wrote, mut already, mut failed, mut bytes) = (0, 0, 0, 0u64);
    let t0 = std::time::Instant::now();
    for (k, item) in plan.tracks.iter().enumerate() {
        let data = match src.read(&item.source) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("  [{}/{}] couldn't read {}: {}", k + 1, plan.tracks.len(), item.source, e);
                failed += 1;
                continue;
            }
        };
        match put(&Path::new(dest).join(&item.dest), &data) {
            Ok(Put::Wrote) => {
                wrote += 1;
                bytes += data.len() as u64;
            }
            Ok(Put::AlreadyThere) => already += 1,
            Err(e) => {
                eprintln!("  [{}/{}] couldn't write {}: {}", k + 1, plan.tracks.len(), item.dest, e);
                failed += 1;
            }
        }
    }
    let secs = t0.elapsed().as_secs_f64().max(0.001);
    println!("{} exported ({} at {:.1} MiB/s), {} already there, {} failed.",
             wrote, mib(bytes), bytes as f64 / (1 << 20) as f64 / secs, already, failed);
    if !plan.missing.is_empty() {
        println!("{} track(s) in the database have no file on the iPod (ids {}).", plan.missing.len(),
                 plan.missing.iter().map(|(id, _)| id.to_string()).collect::<Vec<_>>().join(", "));
    }
    if !plan.others.is_empty() {
        let size: u64 = plan.others.iter().map(|(_, n)| n).sum();
        println!("{} other file(s) on the iPod ({}) aren't in its database.", plan.others.len(), mib(size));
        let go = match others {
            Some(a) => a,
            None if crate::term::stdin_isatty() => crate::term::ask_yes("Export these too, to \"Other Files\"? [Y/n] ", true),
            None => {
                println!("(--other-files exports them too)");
                false
            }
        };
        if go {
            let (mut n, mut skip) = (0, 0);
            for (p, _) in &plan.others {
                match src.read(p).and_then(|d| put(&Path::new(dest).join("Other Files").join(p), &d).map_err(|e| e.to_string())) {
                    Ok(Put::Wrote) => n += 1,
                    Ok(Put::AlreadyThere) => skip += 1,
                    Err(e) => {
                        eprintln!("  couldn't export {}: {}", p, e);
                        failed += 1;
                    }
                }
            }
            println!("{} other file(s) exported to {}, {} already there.", n, Path::new(dest).join("Other Files").display(), skip);
        }
    }
    if failed > 0 { 1 } else { 0 }
}
