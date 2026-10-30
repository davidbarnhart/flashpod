//! The few OS primitives the CLI needs below the Platform layer: the
//! terminal (size, raw key input, a short wait for more input), root,
//! free space, a system-wide sync, and positioned file I/O. Unix through
//! libc; Windows through the console and file APIs.

use std::io;

pub use imp::*;

#[cfg(unix)]
mod imp {
    use std::io;

    /// The size of the terminal on stdout, if there is one.
    pub fn terminal_size() -> Option<(usize, usize)> {
        let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
        if unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) } == 0 {
            Some((ws.ws_col as usize, ws.ws_row as usize))
        } else {
            None
        }
    }

    /// stdin with no echo, no line editing and no signal keys (^C arrives as
    /// the byte) until dropped.
    pub struct RawInput(libc::termios);

    impl RawInput {
        pub fn new() -> io::Result<Self> {
            let mut t: libc::termios = unsafe { std::mem::zeroed() };
            if unsafe { libc::tcgetattr(0, &mut t) } != 0 {
                return Err(io::Error::last_os_error());
            }
            let saved = t;
            t.c_lflag &= !(libc::ECHO | libc::ICANON | libc::ISIG);
            t.c_cc[libc::VMIN] = 1;
            t.c_cc[libc::VTIME] = 0;
            unsafe { libc::tcsetattr(0, libc::TCSAFLUSH, &t) };
            Ok(RawInput(saved))
        }
    }

    impl Drop for RawInput {
        fn drop(&mut self) {
            unsafe { libc::tcsetattr(0, libc::TCSADRAIN, &self.0) };
        }
    }

    /// read(0, buf): the bytes there are, 0 at end of input.
    pub fn read_stdin(buf: &mut [u8]) -> io::Result<usize> {
        let n = unsafe { libc::read(0, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
        if n < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(n as usize)
        }
    }

    /// More input arrives on stdin within `ms` milliseconds.
    pub fn stdin_ready(ms: i32) -> bool {
        let mut p = libc::pollfd { fd: 0, events: libc::POLLIN, revents: 0 };
        unsafe { libc::poll(&mut p, 1, ms) > 0 }
    }

    pub fn is_root() -> bool {
        unsafe { libc::geteuid() == 0 }
    }

    pub fn sync_all() {
        unsafe { libc::sync() };
    }

    /// shutil.disk_usage(path).free.
    pub fn free_bytes(path: &str) -> io::Result<u64> {
        let c = std::ffi::CString::new(path.as_bytes()).map_err(io::Error::other)?;
        let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(c.as_ptr(), &mut st) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(st.f_bavail as u64 * st.f_frsize as u64)
    }

    /// os.access(path, R_OK | X_OK): a directory we may list and enter.
    pub fn can_enter(path: &str) -> bool {
        std::ffi::CString::new(path.as_bytes())
            .map(|c| unsafe { libc::access(c.as_ptr(), libc::R_OK | libc::X_OK) } == 0).unwrap_or(false)
    }

    /// A user's home directory from the password database.
    pub fn home_of(user: &str) -> Option<String> {
        let c = std::ffi::CString::new(user).ok()?;
        let pw = unsafe { libc::getpwnam(c.as_ptr()) };
        if pw.is_null() {
            return None;
        }
        Some(unsafe { std::ffi::CStr::from_ptr((*pw).pw_dir) }.to_string_lossy().into_owned())
    }

    /// This process's user name, '' when the password database has none.
    pub fn user_name() -> String {
        unsafe {
            let pw = libc::getpwuid(libc::getuid());
            if pw.is_null() { String::new() } else { std::ffi::CStr::from_ptr((*pw).pw_name).to_string_lossy().into_owned() }
        }
    }

    pub fn user_ids() -> (u32, u32) {
        unsafe { (libc::getuid(), libc::getgid()) }
    }

    pub fn read_at(f: &std::fs::File, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        std::os::unix::fs::FileExt::read_at(f, buf, offset)
    }

    pub fn write_at(f: &std::fs::File, buf: &[u8], offset: u64) -> io::Result<usize> {
        std::os::unix::fs::FileExt::write_at(f, buf, offset)
    }

    /// mkdir with mode 0700.
    pub fn mkdir_private(path: &std::path::Path) -> io::Result<()> {
        use std::os::unix::fs::DirBuilderExt;
        std::fs::DirBuilder::new().mode(0o700).create(path)
    }
}

#[cfg(windows)]
mod imp {
    use std::io;
    use windows_sys::Win32::Foundation::{HANDLE, WAIT_OBJECT_0};
    use windows_sys::Win32::Storage::FileSystem::{GetDiskFreeSpaceExW, ReadFile};
    use windows_sys::Win32::System::Console::{
        GetConsoleMode, GetConsoleScreenBufferInfo, GetStdHandle, SetConsoleMode, CONSOLE_MODE,
        CONSOLE_SCREEN_BUFFER_INFO, ENABLE_ECHO_INPUT, ENABLE_LINE_INPUT, ENABLE_PROCESSED_INPUT,
        ENABLE_VIRTUAL_TERMINAL_INPUT, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
    };
    use windows_sys::Win32::System::Threading::WaitForSingleObject;

    fn stdin() -> HANDLE {
        unsafe { GetStdHandle(STD_INPUT_HANDLE) }
    }

    pub fn terminal_size() -> Option<(usize, usize)> {
        let mut info: CONSOLE_SCREEN_BUFFER_INFO = unsafe { std::mem::zeroed() };
        if unsafe { GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &mut info) } == 0 {
            return None;
        }
        let w = info.srWindow;
        Some(((w.Right - w.Left + 1) as usize, (w.Bottom - w.Top + 1) as usize))
    }

    /// The console with no echo, no line editing and ^C as a key; arrows
    /// arrive as the same VT escape sequences a Unix terminal sends.
    pub struct RawInput(CONSOLE_MODE);

    impl RawInput {
        pub fn new() -> io::Result<Self> {
            let mut mode: CONSOLE_MODE = 0;
            if unsafe { GetConsoleMode(stdin(), &mut mode) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let raw = (mode & !(ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT)) | ENABLE_VIRTUAL_TERMINAL_INPUT;
            if unsafe { SetConsoleMode(stdin(), raw) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(RawInput(mode))
        }
    }

    impl Drop for RawInput {
        fn drop(&mut self) {
            unsafe { SetConsoleMode(stdin(), self.0) };
        }
    }

    pub fn read_stdin(buf: &mut [u8]) -> io::Result<usize> {
        let mut got: u32 = 0;
        if unsafe { ReadFile(stdin(), buf.as_mut_ptr(), buf.len() as u32, &mut got, std::ptr::null_mut()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(got as usize)
    }

    pub fn stdin_ready(ms: i32) -> bool {
        unsafe { WaitForSingleObject(stdin(), ms.max(0) as u32) == WAIT_OBJECT_0 }
    }

    pub fn is_root() -> bool {
        unsafe { windows_sys::Win32::UI::Shell::IsUserAnAdmin() != 0 }
    }

    /// Windows has no system-wide sync; writers flush their own handles.
    pub fn sync_all() {}

    pub fn free_bytes(path: &str) -> io::Result<u64> {
        let wide: Vec<u16> = path.encode_utf16().chain(std::iter::once(0)).collect();
        let mut free: u64 = 0;
        if unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut free, std::ptr::null_mut(), std::ptr::null_mut()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(free)
    }

    pub fn can_enter(path: &str) -> bool {
        std::fs::read_dir(path).is_ok()
    }

    pub fn home_of(_user: &str) -> Option<String> {
        None
    }

    pub fn user_name() -> String {
        std::env::var("USERNAME").unwrap_or_default()
    }

    /// No uid/gid on Windows; the sudo-mount path that uses them is Unix-only.
    pub fn user_ids() -> (u32, u32) {
        (0, 0)
    }

    pub fn read_at(f: &std::fs::File, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        std::os::windows::fs::FileExt::seek_read(f, buf, offset)
    }

    pub fn write_at(f: &std::fs::File, buf: &[u8], offset: u64) -> io::Result<usize> {
        std::os::windows::fs::FileExt::seek_write(f, buf, offset)
    }

    pub fn mkdir_private(path: &std::path::Path) -> io::Result<()> {
        std::fs::create_dir(path)
    }

    /// DeviceIoControl with no input; the output bytes it returned.
    pub fn ioctl(f: &std::fs::File, code: u32, out_size: usize) -> io::Result<Vec<u8>> {
        use std::os::windows::io::AsRawHandle;
        let mut out = vec![0u8; out_size];
        let mut got: u32 = 0;
        let ok = unsafe {
            windows_sys::Win32::System::IO::DeviceIoControl(
                f.as_raw_handle() as HANDLE, code, std::ptr::null(), 0,
                if out_size == 0 { std::ptr::null_mut() } else { out.as_mut_ptr() as *mut _ },
                out_size as u32, &mut got, std::ptr::null_mut())
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        out.truncate(got as usize);
        Ok(out)
    }

    /// IOCTL_DISK_GET_LENGTH_INFO: a physical drive's size in bytes.
    pub fn disk_length(f: &std::fs::File) -> io::Result<u64> {
        let raw = ioctl(f, 0x0007_405C, 8)?;
        Ok(u64::from_le_bytes(raw[..8].try_into().map_err(io::Error::other)?))
    }
}

/// write_at until all of `buf` is written.
pub fn write_all_at(f: &std::fs::File, mut buf: &[u8], mut offset: u64) -> io::Result<()> {
    while !buf.is_empty() {
        match write_at(f, buf, offset) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::WriteZero, "failed to write whole buffer")),
            Ok(n) => {
                buf = &buf[n..];
                offset += n as u64;
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
