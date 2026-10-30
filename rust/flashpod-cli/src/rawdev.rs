//! fatfs.BlockDev's path mode and cli.open_raw_fat: a disk or partition
//! node opened by the userspace FAT driver, O_DIRECT for a block device
//! (page cache bypassed, the FireWire bridge's rule), every transfer capped
//! at FLASHPOD_RAW_MAX_XFER sectors (default 8).

use flashpod_core::block::BlockDevice;
use flashpod_core::fat::{Fat32, Sectors};
use std::io;
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;

const SECTOR: usize = 512;
const ALIGN: usize = 4096;

/// _effective_xfer: the platform's 8, or FLASHPOD_RAW_MAX_XFER.
pub fn effective_xfer() -> u64 {
    match std::env::var("FLASHPOD_RAW_MAX_XFER") {
        Ok(v) => v.trim().parse::<i64>().map(|n| n.max(1) as u64).unwrap_or(8),
        Err(_) => 8,
    }
}

/// A page-aligned buffer for O_DIRECT (BlockDev's mmap bounce).
struct Bounce {
    ptr: *mut u8,
    len: usize,
}

impl Bounce {
    fn new(len: usize) -> Self {
        let len = len.max(ALIGN).div_ceil(ALIGN) * ALIGN;
        let layout = std::alloc::Layout::from_size_align(len, ALIGN).unwrap();
        let ptr = unsafe { std::alloc::alloc_zeroed(layout) };
        Bounce { ptr, len }
    }
    fn buf(&mut self, n: usize) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr, n) }
    }
}

impl Drop for Bounce {
    fn drop(&mut self) {
        unsafe { std::alloc::dealloc(self.ptr, std::alloc::Layout::from_size_align(self.len, ALIGN).unwrap()) };
    }
}

pub struct RawDevice {
    file: std::fs::File,
    bounce: Option<Bounce>,
}

/// An OSError's str(): "[Errno 5] Input/output error".
pub fn py_os(e: &io::Error) -> io::Error {
    match e.raw_os_error() {
        Some(n) => {
            let m = e.to_string();
            io::Error::new(e.kind(), format!("[Errno {}] {}", n, m.split(" (os error").next().unwrap_or(&m)))
        }
        None => io::Error::new(e.kind(), e.to_string()),
    }
}

impl RawDevice {
    /// BlockDev(path, writable): O_DIRECT when it's a block device (and the
    /// kernel takes the flag), else a plain open.
    #[cfg(target_os = "linux")]
    pub fn open(path: &str, writable: bool, max_xfer: u64) -> io::Result<Self> {
        let is_blk = std::fs::metadata(path)?.file_type().is_block_device();
        let mut o = std::fs::OpenOptions::new();
        o.read(true).write(writable);
        if is_blk {
            if let Ok(file) = o.clone().custom_flags(libc::O_DIRECT).open(path) {
                return Ok(RawDevice { file, bounce: Some(Bounce::new(max_xfer as usize * SECTOR)) });
            }
        }
        Ok(RawDevice { file: o.open(path)?, bounce: None })
    }

    /// macOS has no O_DIRECT: the unbuffered node is the raw character
    /// device, so /dev/diskN is opened as /dev/rdiskN (platform.macos
    /// raw_read_node), with F_NOCACHE on whatever is opened.
    #[cfg(target_os = "macos")]
    pub fn open(path: &str, writable: bool, max_xfer: u64) -> io::Result<Self> {
        let node = match path.strip_prefix("/dev/disk") {
            Some(rest) => format!("/dev/rdisk{}", rest),
            None => path.to_string(),
        };
        let raw = std::fs::metadata(&node)?.file_type().is_char_device();
        let file = std::fs::OpenOptions::new().read(true).write(writable).open(&node)?;
        unsafe { libc::fcntl(std::os::fd::AsRawFd::as_raw_fd(&file), libc::F_NOCACHE, 1) };
        let bounce = if raw { Some(Bounce::new(max_xfer as usize * SECTOR)) } else { None };
        Ok(RawDevice { file, bounce })
    }

    /// platform.windows open_raw: \\.\PhysicalDriveN shared for read and
    /// write (the volumes on it are locked separately), sector-aligned
    /// transfers through the bounce buffer; an image file opens plainly.
    #[cfg(windows)]
    pub fn open(path: &str, writable: bool, max_xfer: u64) -> io::Result<Self> {
        use std::os::windows::fs::OpenOptionsExt;
        let physical = path.starts_with(r"\\.\");
        let mut o = std::fs::OpenOptions::new();
        o.read(true).write(writable);
        if physical {
            o.share_mode(1 | 2); // FILE_SHARE_READ | FILE_SHARE_WRITE
        }
        let file = o.open(path)?;
        let bounce = if physical { Some(Bounce::new(max_xfer as usize * SECTOR)) } else { None };
        Ok(RawDevice { file, bounce })
    }
}

impl BlockDevice for RawDevice {
    fn len(&self) -> io::Result<u64> {
        #[cfg(windows)]
        if self.bounce.is_some() {
            return crate::sys::disk_length(&self.file);
        }
        let mut f = &self.file;
        io::Seek::seek(&mut f, io::SeekFrom::End(0))
    }
    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        match self.bounce.as_mut() {
            None => crate::sys::read_at(&self.file, buf, offset).map_err(|e| py_os(&e)),
            Some(b) => {
                let n = buf.len().min(b.len);
                let got = crate::sys::read_at(&self.file, b.buf(n), offset).map_err(|e| py_os(&e))?;
                buf[..got].copy_from_slice(&b.buf(got)[..]);
                Ok(got)
            }
        }
    }
    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()> {
        match self.bounce.as_mut() {
            None => crate::sys::write_all_at(&self.file, buf, offset).map_err(|e| py_os(&e)),
            Some(b) => {
                let mut done = 0;
                while done < buf.len() {
                    let n = (buf.len() - done).min(b.len);
                    b.buf(n).copy_from_slice(&buf[done..done + n]);
                    let w = crate::sys::write_at(&self.file, b.buf(n), offset + done as u64).map_err(|e| py_os(&e))?;
                    if w != n {
                        return Err(io::Error::other(format!("short write at LBA {} ({}/{})", (offset as usize + done) / SECTOR, w, n)));
                    }
                    done += n;
                }
                Ok(())
            }
        }
    }
    /// fsync. On macOS not File::sync_all, which is F_FULLFSYNC there and
    /// refused by a disk node (ENOTTY); the rdisk node is unbuffered anyway.
    fn flush(&mut self) -> io::Result<()> {
        #[cfg(target_os = "macos")]
        {
            use std::os::fd::AsRawFd;
            if unsafe { libc::fsync(self.file.as_raw_fd()) } != 0 {
                let e = io::Error::last_os_error();
                if !matches!(e.raw_os_error(), Some(libc::ENOTTY) | Some(libc::ENOTSUP) | Some(libc::EINVAL)) {
                    return Err(py_os(&e));
                }
            }
            return Ok(());
        }
        #[cfg(not(target_os = "macos"))]
        self.file.sync_all().map_err(|e| py_os(&e))
    }
    fn set_len(&mut self, _len: u64) -> io::Result<()> {
        Err(io::Error::other("a device can't be resized"))
    }
}

/// open_raw_fat after the open: the FAT at LBA 0 (a partition node), or the
/// MBR's 0x0b/0x0c partition (a whole disk).
pub fn open_raw_fat(dev: &mut RawDevice, max_xfer: u64) -> Result<Fat32<'_>, String> {
    let mut sec = Sectors { dev, part_start: 0, max_xfer };
    let boot = sec.read(0, 1)?;
    let is_fat = &boot[82..85] == b"FAT" && boot[510..512] == [0x55, 0xaa];
    if !is_fat && boot[510..512] == [0x55, 0xaa] {
        for poff in [446usize, 462, 478, 494] {
            if matches!(boot[poff + 4], 0x0b | 0x0c) {
                let start = u32::from_le_bytes(boot[poff + 8..poff + 12].try_into().unwrap()) as u64;
                if start != 0 {
                    sec.part_start = start;
                    break;
                }
            }
        }
    }
    Fat32::open(sec)
}
