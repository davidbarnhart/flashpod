//! The one way the library touches a card: a block device addressed in
//! bytes with u64 offsets (wasm32's usize is 32 bits; cards are bigger).
//! Implemented for files here; a browser file handle implements it in the
//! wasm binding.

use crate::error::{Error, ErrorKind};
use std::io;

pub trait BlockDevice {
    /// Size in bytes.
    fn len(&self) -> io::Result<u64>;
    /// Fill `buf` from `offset`; a read past the end returns the bytes there
    /// are (like a file read), so callers check the count.
    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize>;
    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()>;
    fn flush(&mut self) -> io::Result<()>;
    fn set_len(&mut self, len: u64) -> io::Result<()>;

    /// `n` bytes at `offset`, shorter at the end of the device.
    fn read_vec(&mut self, offset: u64, n: usize) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; n];
        let mut got = 0;
        while got < n {
            let k = self.read_at(offset + got as u64, &mut buf[got..])?;
            if k == 0 {
                break;
            }
            got += k;
        }
        buf.truncate(got);
        Ok(buf)
    }
}

/// A card image file.
pub struct FileDevice {
    file: std::fs::File,
}

impl FileDevice {
    pub fn open(path: &std::path::Path, write: bool) -> io::Result<Self> {
        let file = std::fs::OpenOptions::new().read(true).write(write).open(path)?;
        Ok(FileDevice { file })
    }

    /// Open a card IMAGE the way card._Image does, with its NotACard
    /// reasons: missing, not_a_file (a directory, a device), unopenable.
    /// (Locking, card._lock, is not ported yet.)
    pub fn open_image(path: &str, write: bool) -> Result<Self, Error> {
        let p = std::path::Path::new(path);
        let not_a_card = |msg: String, reason: &str| {
            Error::new(ErrorKind::NotACard, msg).detail("reason", reason)
        };
        if std::fs::symlink_metadata(p).is_err() {
            return Err(not_a_card(format!("{} does not exist", path), "missing"));
        }
        if p.is_dir() {
            return Err(not_a_card(format!("{} is a directory", path), "not_a_file"));
        }
        let dev = FileDevice::open(p, write)
            .map_err(|e| not_a_card(format!("can't open {}: {}", path, e), "unopenable"))?;
        let meta = dev.file.metadata()?;
        if !meta.is_file() {
            return Err(not_a_card(
                format!("{} is not a regular file (a device?) -- flashpod's library only works on image files", path),
                "not_a_file",
            ));
        }
        Ok(dev)
    }
}

impl BlockDevice for FileDevice {
    fn len(&self) -> io::Result<u64> {
        Ok(self.file.metadata()?.len())
    }
    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        use std::io::{Read, Seek, SeekFrom};
        self.file.seek(SeekFrom::Start(offset))?;
        self.file.read(buf)
    }
    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()> {
        use std::io::{Seek, SeekFrom, Write};
        self.file.seek(SeekFrom::Start(offset))?;
        self.file.write_all(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        use std::io::Write;
        self.file.flush()?;
        self.file.sync_all()
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.file.set_len(len)
    }
}

/// An in-memory card (tests, and the browser before a handle exists).
#[derive(Default)]
pub struct MemDevice {
    pub data: Vec<u8>,
}

impl BlockDevice for MemDevice {
    fn len(&self) -> io::Result<u64> {
        Ok(self.data.len() as u64)
    }
    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        let len = self.data.len() as u64;
        if offset >= len {
            return Ok(0);
        }
        let n = buf.len().min((len - offset) as usize);
        let o = offset as usize;
        buf[..n].copy_from_slice(&self.data[o..o + n]);
        Ok(n)
    }
    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()> {
        let end = offset as usize + buf.len();
        if end > self.data.len() {
            self.data.resize(end, 0);
        }
        self.data[offset as usize..end].copy_from_slice(buf);
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.data.resize(len as usize, 0);
        Ok(())
    }
}
