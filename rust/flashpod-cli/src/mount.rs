//! target.MountTarget: an iPod reached through an OS mount, or any
//! directory given as `--mount`, with the operations of itunesdb.py's mount
//! helpers (load, save, init_ipod, copy_to_ipod) and the mount rebuild.

use crate::window;
use flashpod_core::entropy::Entropy;
use flashpod_core::error::{Error, ErrorKind};
use flashpod_core::itunesdb::{self, Library};
use flashpod_core::library::{self, Hooks, TagSource};
use flashpod_core::ops::Target;
use flashpod_core::tags::os_error;
use std::io::{Read, Write};

/// os.path.join for relative parts.
pub fn join(a: &str, b: &str) -> String {
    if a.is_empty() || a.ends_with('/') {
        format!("{}{}", a, b)
    } else {
        format!("{}/{}", a, b)
    }
}

fn os(e: &std::io::Error, path: &str) -> Error {
    Error::new(ErrorKind::Error, os_error(e, path)).detail("errno", e.raw_os_error().unwrap_or(0))
}

/// The errno of a target error, when it was an OSError.
pub fn errno(e: &Error) -> Option<i32> {
    e.details.get("errno").and_then(|v| v.as_i64()).map(|v| v as i32)
}

fn listdir(path: &str) -> Result<Vec<String>, Error> {
    let mut v: Vec<String> = std::fs::read_dir(path)
        .map_err(|e| os(&e, path))?
        .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
        .collect();
    v.sort();
    Ok(v)
}

/// os.makedirs(path, exist_ok=True).
fn makedirs(path: &str) -> Result<(), Error> {
    let p = std::path::Path::new(path);
    if let Some(head) = p.parent() {
        let head = head.to_string_lossy();
        if !head.is_empty() && !std::path::Path::new(&*head).exists() {
            makedirs(&head)?;
        }
    }
    match std::fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && p.is_dir() => Ok(()),
        Err(e) => Err(os(&e, path)),
    }
}

pub struct MountTarget {
    pub mount: String,
}

impl MountTarget {
    pub fn new(mount: &str) -> Self {
        MountTarget { mount: mount.to_string() }
    }

    fn db(&self) -> String {
        join(&self.mount, "iPod_Control/iTunes/iTunesDB")
    }

    pub fn music_dir_exists(&self) -> bool {
        std::path::Path::new(&join(&self.mount, "iPod_Control/Music")).is_dir()
    }

    /// itunesdb.save: through a .new file, fsynced, then renamed over.
    fn save(&self, lib: &Library, ent: &mut Entropy) -> Result<(), Error> {
        let db = self.db();
        let tmp = format!("{}.new", db);
        let data = itunesdb::serialize(lib, ent);
        let mut f = std::fs::File::create(&tmp).map_err(|e| os(&e, &tmp))?;
        f.write_all(&data).map_err(|e| os(&e, &tmp))?;
        f.sync_all().map_err(|e| os(&e, &tmp))?;
        std::fs::rename(&tmp, &db).map_err(|e| {
            let m = os_error(&e, &tmp);
            Error::new(ErrorKind::Error, format!("{} -> {}", m, flashpod_core::text::py_repr(&db)))
                .detail("errno", e.raw_os_error().unwrap_or(0))
        })
    }
}

impl Target for MountTarget {
    fn load_library(&mut self, ent: &mut Entropy) -> Result<Option<Library>, Error> {
        let db = self.db();
        let data = match std::fs::read(&db) {
            Ok(d) => d,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(os(&e, &db)),
        };
        itunesdb::parse_bytes_with(&data, ent).map(Some).map_err(|e| Error::new(ErrorKind::CorruptLibrary, e))
    }

    fn db_bytes(&mut self) -> Option<Vec<u8>> {
        std::fs::read(self.db()).ok()
    }

    fn save_library(&mut self, lib: &Library, ent: &mut Entropy) -> Result<(), Error> {
        self.save(lib, ent)?;
        // the kernel's FAT keeps directory entries in the page cache until a
        // system-wide sync (CLAUDE.md, SYNC LESSON)
        unsafe { libc::sync() };
        Ok(())
    }

    fn init_structure(&mut self, name: &str, ent: &mut Entropy) -> Result<(), Error> {
        let mut subs = vec!["iTunes".to_string(), "Device".to_string()];
        subs.extend((0..50).map(|i| format!("Music/F{:02}", i)));
        for s in subs {
            makedirs(&join(&self.mount, &format!("iPod_Control/{}", s)))?;
        }
        self.save(&Library::new(name, ent), ent)
    }

    fn remove_location(&mut self, location: &Option<String>) -> Result<(), Error> {
        let loc = match location.as_deref() {
            Some(l) if !l.is_empty() => l,
            _ => return Ok(()),
        };
        let path = loc.trim_start_matches(':').split(':').fold(self.mount.clone(), |acc, p| join(&acc, p));
        if std::path::Path::new(&path).exists() {
            std::fs::remove_file(&path).map_err(|e| os(&e, &path))?;
        }
        Ok(())
    }

    /// itunesdb.copy_to_ipod: a random F## folder, a collision-free name,
    /// 64 KiB at a time with progress at most 4 times a second, fsynced.
    fn copy(&mut self, src: &str, _read_source: &mut dyn FnMut(&str) -> std::io::Result<Vec<u8>>,
            ent: &mut Entropy, progress: &mut dyn FnMut(u64, u64)) -> Result<String, String> {
        let music = join(&self.mount, "iPod_Control/Music");
        let fdirs: Vec<String> = listdir(&music).map_err(|e| e.message)?.into_iter().filter(|d| d.starts_with('F')).collect();
        if fdirs.is_empty() {
            return Err("no Music/F## directories (run init?)".into());
        }
        let fdir = fdirs[ent.rng.choice_index(fdirs.len())].clone();
        let (_, ext) = library::splitext(src);
        let ext = if ext.is_empty() { ".mp3" } else { ext };
        let (name, dst) = loop {
            let name = format!("fp{:06}{}", ent.rng.randrange(1_000_000), ext.to_lowercase());
            let dst = join(&join(&music, &fdir), &name);
            if !std::path::Path::new(&dst).exists() {
                break (name, dst);
            }
        };
        let total = std::fs::metadata(src).map_err(|e| os_error(&e, src))?.len();
        let mut fin = std::fs::File::open(src).map_err(|e| os_error(&e, src))?;
        let mut fout = std::fs::File::create(&dst).map_err(|e| os_error(&e, &dst))?;
        let mut buf = vec![0u8; 1 << 16];
        let (mut done, mut last) = (0u64, 0.0f64);
        loop {
            let n = fin.read(&mut buf).map_err(|e| os_error(&e, src))?;
            if n == 0 {
                break;
            }
            fout.write_all(&buf[..n]).map_err(|e| os_error(&e, &dst))?;
            done += n as u64;
            let now = window::monotonic();
            if now - last >= 0.25 {
                progress(done, total);
                last = now;
            }
        }
        progress(done, total);
        fout.sync_all().map_err(|e| os_error(&e, &dst))?;
        Ok(format!(":iPod_Control:Music:{}:{}", fdir, name))
    }

    /// shutil.disk_usage(mount).free. FP_DISK_FREE stands in for it under
    /// scripts/test_cli_unchanged.py, which pins Python's the same way.
    fn free_bytes(&mut self) -> Result<i64, String> {
        if let Some(v) = std::env::var("FP_DISK_FREE").ok().and_then(|v| v.parse().ok()) {
            return Ok(v);
        }
        let c = std::ffi::CString::new(self.mount.as_bytes()).map_err(|e| e.to_string())?;
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            return Err(os_error(&std::io::Error::last_os_error(), &self.mount));
        }
        Ok(st.f_bavail as i64 * st.f_frsize as i64)
    }

    /// cli._mount_firmware_read: the card's firmware partition through the
    /// whole disk behind the mount, opened directly as root, else through
    /// `sudo dd iflag=direct` (sudo -n off a terminal). A directory that
    /// isn't a mountpoint has no device: unreadable.
    fn probe(&mut self, memo: &mut Option<Option<bool>>) -> Option<bool> {
        if let Some(v) = memo {
            return *v;
        }
        let v = crate::detect::device_for_mount(&self.mount).and_then(|node| {
            let disk = format!("/dev/{}", crate::host::disk_of_dev(&node));
            firmware_fix(&disk)
        });
        *memo = Some(v);
        v
    }

    fn rebuild_library(&mut self, name: &str, src: &mut dyn TagSource, ent: &mut Entropy, hooks: &mut dyn Hooks,
                       report: &mut dyn FnMut(String)) -> Result<Library, Error> {
        let music = join(&self.mount, "iPod_Control/Music");
        let mut files = Vec::new();
        for d in listdir(&music)? {
            let ddir = join(&music, &d);
            if !(d.to_uppercase().starts_with('F') && std::path::Path::new(&ddir).is_dir()) {
                continue;
            }
            for f in listdir(&ddir)? {
                let fp = join(&ddir, &f);
                if std::path::Path::new(&fp).is_file() {
                    files.push((fp, format!(":iPod_Control:Music:{}:{}", d, f)));
                }
            }
        }
        let total = files.len();
        let mut located = Vec::new();
        for (nr, (fp, location)) in files.into_iter().enumerate() {
            hooks.progress("rebuild", nr as u64 + 1, total as u64, &fp);
            if let Some(t) = library::make_track(&fp, nr + 1, total, src, ent, report) {
                located.push((location, t));
            }
        }
        // _library_from_located: the library is made after the tags are read
        let mut lib = Library::new(name, ent);
        for (loc, mut t) in located {
            t.location = Some(loc);
            t.id = lib.next_track_id();
            lib.tracks.push(t);
        }
        Ok(lib)
    }
}

/// _sector_reader + target.firmware_reader + firmware.fix_on_card.
fn firmware_fix(disk: &str) -> Option<bool> {
    let xfer = crate::rawdev::effective_xfer();
    let mut direct = crate::rawdev::RawDevice::open(disk, false, xfer).ok();
    let sudo: Vec<String> = if crate::term::stdin_isatty() { vec!["sudo".into()] } else { vec!["sudo".into(), "-n".into()] };
    if direct.is_none() {
        if !crate::host::have("sudo") || !crate::host::have("dd") {
            return None;
        }
        if crate::term::stdin_isatty() {
            eprintln!("flashpod: reading the iPod's firmware (to see how large a library it can load) needs root \u{2014} elevating via sudo...");
        }
    }
    let mut sectors = |lba: u64, count: u64| -> Option<Vec<u8>> {
        match direct.as_mut() {
            Some(d) => {
                let mut sec = flashpod_core::fat::Sectors { dev: d, part_start: 0, max_xfer: xfer };
                sec.read(lba, count).ok()
            }
            None => {
                let mut cmd = sudo.clone();
                cmd.extend(["dd".to_string(), format!("if={}", disk), "bs=512".into(), format!("skip={}", lba), format!("count={}", count), "iflag=direct".into()]);
                let out = std::process::Command::new(&cmd[0]).args(&cmd[1..]).stderr(std::process::Stdio::null()).output().ok()?;
                (out.status.success() && out.stdout.len() as u64 == count * 512).then_some(out.stdout)
            }
        }
    };
    let mbr = sectors(0, 1)?;
    if mbr.len() < 512 || mbr[510..512] != [0x55, 0xaa] {
        return None;
    }
    let mut start = None;
    for off in [446usize, 462, 478, 494] {
        let lba = u32::from_le_bytes(mbr[off + 8..off + 12].try_into().unwrap()) as u64;
        let cnt = u32::from_le_bytes(mbr[off + 12..off + 16].try_into().unwrap());
        if mbr[off + 4] == 0 && lba != 0 && cnt != 0 {
            start = Some(lba);
            break;
        }
    }
    let start = start?;
    let mut read = |offset: u64, n: usize| -> Option<Vec<u8>> {
        if n == 0 {
            return Some(Vec::new());
        }
        let first = offset / 512;
        let last = (offset + n as u64 - 1) / 512;
        let data = sectors(start + first, last - first + 1)?;
        let skip = (offset - first * 512) as usize;
        Some(data[skip..skip + n].to_vec())
    };
    flashpod_core::firmware::fix_on_card(&mut read, flashpod_core::library::LIBRARY_FIX_NAME)
}
