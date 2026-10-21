//! Test only: `flashpod_core::api` as a WebAssembly module behind a plain C
//! interface, driven by scripts/wasm_check.mjs under Node, so the parity
//! harness can show the wasm build answers and writes exactly as the native
//! one. The host imports (module "host"): the card (`dev_*`), the music
//! files by name (`src_*`), `progress` and `cancelled`.
#![cfg(target_arch = "wasm32")]

use flashpod_core::api::{self, Host};
use flashpod_core::block::BlockDevice;
use flashpod_core::library::Hooks;
use flashpod_core::tags::ReadAt;
use std::io;

#[link(wasm_import_module = "host")]
extern "C" {
    fn dev_len() -> f64;
    fn dev_read(off: f64, ptr: *mut u8, len: u32) -> i32;
    fn dev_write(off: f64, ptr: *const u8, len: u32) -> i32;
    fn dev_flush() -> i32;
    fn dev_set_len(len: f64) -> i32;
    fn src_size(name: *const u8, name_len: u32) -> f64;
    fn src_read(name: *const u8, name_len: u32, off: f64, ptr: *mut u8, len: u32) -> i32;
    fn progress(stage: *const u8, stage_len: u32, done: f64, total: f64, item: *const u8, item_len: u32);
    fn cancelled() -> i32;
}

fn bad(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::Other, what.to_string())
}

struct Dev;
impl BlockDevice for Dev {
    fn len(&self) -> io::Result<u64> {
        Ok(unsafe { dev_len() } as u64)
    }
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> io::Result<usize> {
        match unsafe { dev_read(off as f64, buf.as_mut_ptr(), buf.len() as u32) } {
            n if n < 0 => Err(bad("card read failed")),
            n => Ok(n as usize),
        }
    }
    fn write_at(&mut self, off: u64, buf: &[u8]) -> io::Result<()> {
        if unsafe { dev_write(off as f64, buf.as_ptr(), buf.len() as u32) } < 0 {
            return Err(bad("card write failed"));
        }
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        if unsafe { dev_flush() } < 0 {
            return Err(bad("card flush failed"));
        }
        Ok(())
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        if unsafe { dev_set_len(len as f64) } < 0 {
            return Err(bad("card resize failed"));
        }
        Ok(())
    }
}

struct Src(String);
impl ReadAt for Src {
    fn size(&mut self) -> io::Result<u64> {
        match unsafe { src_size(self.0.as_ptr(), self.0.len() as u32) } {
            n if n < 0.0 => Err(bad("no such file")),
            n => Ok(n as u64),
        }
    }
    fn read_at(&mut self, off: u64, buf: &mut [u8]) -> io::Result<usize> {
        match unsafe { src_read(self.0.as_ptr(), self.0.len() as u32, off as f64, buf.as_mut_ptr(), buf.len() as u32) } {
            n if n < 0 => Err(bad("read failed")),
            n => Ok(n as usize),
        }
    }
}

struct JsHost;
impl Hooks for JsHost {
    fn progress(&mut self, stage: &str, done: u64, total: u64, item: &str) {
        unsafe { progress(stage.as_ptr(), stage.len() as u32, done as f64, total as f64, item.as_ptr(), item.len() as u32) }
    }
    fn cancelled(&mut self) -> bool {
        unsafe { cancelled() != 0 }
    }
}
impl Host for JsHost {
    fn source(&mut self, path: &str) -> Option<Box<dyn ReadAt + '_>> {
        let mut s = Src(path.to_string());
        s.size().ok().map(|_| Box::new(s) as Box<dyn ReadAt>)
    }
}

static mut RESULT: Vec<u8> = Vec::new();

/// Run one command; read the answer from result_ptr/result_len.
#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn run(json_ptr: *const u8, json_len: u32, blob_ptr: *const u8, blob_len: u32) -> u32 {
    let cmd = unsafe { std::slice::from_raw_parts(json_ptr, json_len as usize) };
    let blob: &[u8] = if blob_len == 0 { &[] } else { unsafe { std::slice::from_raw_parts(blob_ptr, blob_len as usize) } };
    let answer = api::run_json(cmd, blob, &mut Dev, &mut JsHost);
    unsafe { RESULT = serde_json::to_vec(&answer).unwrap() };
    0
}

#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn result_ptr() -> *const u8 {
    unsafe { RESULT.as_ptr() }
}

#[no_mangle]
#[allow(static_mut_refs)]
pub extern "C" fn result_len() -> u32 {
    unsafe { RESULT.len() as u32 }
}

#[no_mangle]
pub extern "C" fn alloc(n: u32) -> *mut u8 {
    let mut v = vec![0u8; n as usize];
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}
