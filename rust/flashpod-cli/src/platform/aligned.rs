//! base.AlignedRawIO for a raw disk node (\\.\PhysicalDriveN, /dev/rdiskN):
//! whole, sector-aligned transfers only.

use flashpod_core::block::BlockDevice;
use std::io;

const SECTOR: u64 = 512;

/// base.AlignedRawIO: a physical drive takes only whole, sector-aligned
/// transfers, so unaligned edges are read, patched and written back.
pub struct Aligned {
    pub file: std::fs::File,
    /// The disk's size in bytes, taken when it was opened.
    pub len: u64,
}

impl Aligned {
    fn sectors(&self, first: u64, count: u64) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; (count * SECTOR) as usize];
        let mut got = 0;
        while got < buf.len() {
            let n = crate::sys::read_at(&self.file, &mut buf[got..], first * SECTOR + got as u64)?;
            if n == 0 {
                break;
            }
            got += n;
        }
        Ok(buf)
    }
}

impl BlockDevice for Aligned {
    fn len(&self) -> io::Result<u64> {
        Ok(self.len)
    }

    fn read_at(&mut self, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        let first = offset / SECTOR;
        let last = (offset + buf.len() as u64).div_ceil(SECTOR);
        let data = self.sectors(first, last - first)?;
        let skip = (offset - first * SECTOR) as usize;
        let n = buf.len().min(data.len().saturating_sub(skip));
        buf[..n].copy_from_slice(&data[skip..skip + n]);
        Ok(n)
    }

    fn write_at(&mut self, offset: u64, buf: &[u8]) -> io::Result<()> {
        if buf.is_empty() {
            return Ok(());
        }
        let end = offset + buf.len() as u64;
        if offset % SECTOR == 0 && end % SECTOR == 0 {
            return crate::sys::write_all_at(&self.file, buf, offset);
        }
        let first = offset / SECTOR;
        let last = end.div_ceil(SECTOR);
        let mut data = self.sectors(first, last - first)?;
        data.resize(((last - first) * SECTOR) as usize, 0);
        let skip = (offset - first * SECTOR) as usize;
        data[skip..skip + buf.len()].copy_from_slice(buf);
        crate::sys::write_all_at(&self.file, &data, first * SECTOR)
    }

    /// Windows: FlushFileBuffers. macOS: a plain fsync -- File::sync_all is
    /// F_FULLFSYNC there, which a disk node refuses (ENOTTY); writes to the
    /// unbuffered rdisk node are already on the device.
    fn flush(&mut self) -> io::Result<()> {
        #[cfg(windows)]
        return self.file.sync_all();
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            if unsafe { libc::fsync(self.file.as_raw_fd()) } != 0 {
                let e = io::Error::last_os_error();
                if !matches!(e.raw_os_error(), Some(libc::ENOTTY) | Some(libc::ENOTSUP) | Some(libc::EINVAL)) {
                    return Err(e);
                }
            }
            Ok(())
        }
    }

    fn set_len(&mut self, _len: u64) -> io::Result<()> {
        Err(io::Error::other("a device can't be resized"))
    }
}
