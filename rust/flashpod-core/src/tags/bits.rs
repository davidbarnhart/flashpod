//! mutagen._util.BitReader over a [`Stream`]: MSB first, reads whole bytes
//! lazily, `skip` seeks (past the end too, failing only on a later read).

use super::{Stream, TagError};

pub enum BitErr {
    /// BitReaderError: not enough data
    Short,
    Io(TagError),
}

impl From<TagError> for BitErr {
    fn from(e: TagError) -> Self {
        BitErr::Io(e)
    }
}

pub type BR<T> = Result<T, BitErr>;

pub struct BitReader<'a, S: Stream + ?Sized> {
    pub f: &'a mut S,
    buffer: u64,
    nbits: u32,
    pos: u64,
}

impl<'a, S: Stream + ?Sized> BitReader<'a, S> {
    pub fn new(f: &'a mut S) -> Self {
        let pos = f.tell();
        BitReader { f, buffer: 0, nbits: 0, pos }
    }

    pub fn bits(&mut self, count: u32) -> BR<u64> {
        assert!(count <= 56);
        if count > self.nbits {
            let n = (count - self.nbits + 7) / 8;
            let data = self.f.read(n as u64)?;
            if data.len() as u32 != n {
                return Err(BitErr::Short);
            }
            for b in data {
                self.buffer = (self.buffer << 8) | b as u64;
            }
            self.nbits += n * 8;
        }
        self.nbits -= count;
        let v = self.buffer >> self.nbits;
        self.buffer &= (1u64 << self.nbits) - 1;
        Ok(v)
    }

    pub fn bytes(&mut self, count: u64) -> BR<Vec<u8>> {
        if self.nbits == 0 {
            let d = self.f.read(count)?;
            if d.len() as u64 != count {
                return Err(BitErr::Short);
            }
            return Ok(d);
        }
        let mut out = Vec::new();
        for _ in 0..count {
            out.push(self.bits(8)? as u8);
        }
        Ok(out)
    }

    pub fn skip(&mut self, count: u64) -> BR<()> {
        if count <= self.nbits as u64 {
            self.bits(count as u32)?;
        } else {
            let count = count - self.align() as u64;
            let n_bytes = count / 8;
            self.f.seek_rel(n_bytes as i64);
            self.bits((count - n_bytes * 8) as u32)?;
        }
        Ok(())
    }

    pub fn get_position(&self) -> i64 {
        (self.f.tell() as i64 - self.pos as i64) * 8 - self.nbits as i64
    }

    pub fn align(&mut self) -> u32 {
        let b = self.nbits;
        self.buffer = 0;
        self.nbits = 0;
        b
    }
}
