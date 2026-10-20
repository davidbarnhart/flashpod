"""Where flashpod's random ids and "now" come from: one place, so a test can
make a card byte-for-byte reproducible.

By default this is exactly what flashpod always did: Python's ``random``
module and ``time.time()``, looked up at call time (so a harness that seeds
``random`` or freezes ``time.time`` still works). :func:`deterministic` swaps
in :class:`SplitMix64` and a fixed clock. SplitMix64 is a few lines of
integer arithmetic that the Rust port (``rust/flashpod-core``) implements
identically, so the Python reference and the Rust library can be compared
image to image (docs: emupod's FLASHPOD_RUST.md, section 4).
"""
import contextlib
import random
import time

_MASK = (1 << 64) - 1


class SplitMix64(object):
    """A tiny, portable 64-bit generator (Steele, Lea and Flood, 2014). Only
    the methods flashpod uses, each defined so another language can match
    them exactly: ``getrandbits(k)`` is the top ``k`` bits of one output,
    ``randrange(n)`` and ``choice(seq)`` take one output modulo ``n``."""

    def __init__(self, seed):
        self.state = seed & _MASK

    def next(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def getrandbits(self, k):
        return self.next() >> (64 - k) if k else 0

    def randrange(self, n):
        return self.next() % n

    def choice(self, seq):
        return seq[self.next() % len(seq)]


_rng = None          # None: Python's `random` module
_clock = None        # None: time.time


def rng():
    """The id source: anything with getrandbits / randrange / choice."""
    return _rng if _rng is not None else random


def now():
    """The current Unix time in whole seconds."""
    return int(_clock() if _clock is not None else time.time())


@contextlib.contextmanager
def deterministic(seed, unix_time):
    """Within the block, ids come from SplitMix64(seed) and the clock reads
    ``unix_time``. Not thread-safe (nor is anything else in the library)."""
    global _rng, _clock
    saved = _rng, _clock
    _rng, _clock = SplitMix64(seed), (lambda: unix_time)
    try:
        yield
    finally:
        _rng, _clock = saved
