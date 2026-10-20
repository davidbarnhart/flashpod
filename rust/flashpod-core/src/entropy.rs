//! Ids and "now", injectable, matching the Python `flashpod.entropy`:
//! SplitMix64 with the same `getrandbits` / `randrange` / `choice`
//! definitions, so a deterministic run makes the same bytes in both.

pub struct SplitMix64 {
    state: u64,
}

impl SplitMix64 {
    pub fn new(seed: u64) -> Self {
        SplitMix64 { state: seed }
    }
    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    /// The top `k` bits of one output (Python: `getrandbits(k)`).
    pub fn getrandbits(&mut self, k: u32) -> u64 {
        if k == 0 {
            0
        } else {
            self.next_u64() >> (64 - k)
        }
    }
    /// One output modulo `n` (Python: `randrange(n)`).
    pub fn randrange(&mut self, n: u64) -> u64 {
        self.next_u64() % n
    }
    /// An index for `choice(seq)`: one output modulo `len`.
    pub fn choice_index(&mut self, len: usize) -> usize {
        (self.next_u64() % len as u64) as usize
    }
}

/// Seconds since the Unix epoch, supplied by the caller.
pub trait Clock {
    fn now(&self) -> i64;
}

pub struct FixedClock(pub i64);

impl Clock for FixedClock {
    fn now(&self) -> i64 {
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reference_vectors() {
        // published SplitMix64 outputs for seed 0
        let mut r = SplitMix64::new(0);
        assert_eq!(r.next_u64(), 0xe220a8397b1dcdaf);
        assert_eq!(r.next_u64(), 0x6e789e6aa1b965f4);
        assert_eq!(r.next_u64(), 0x06c45d188009454f);
    }
}
