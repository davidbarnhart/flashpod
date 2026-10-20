# flashpod in Rust

The port of flashpod's card library (`flashpod/card.py` and the modules
under it) to Rust, so one implementation can serve the command line,
emupod's server and emupod's offline phone app (compiled to WebAssembly).
The brief is emupod's `docs/FLASHPOD_RUST.md`.

Decisions (David, 2026-10-20): the crates live here, in `rust/`, until the
Rust command line takes over; the Python library stays the reference and
the shipped program until the port reaches parity; the OS X 10.8 build stays
Python (current Rust targets macOS 10.12 and later).

- `flashpod-core`: the library. No printing, prompts or exit; all I/O
  through `BlockDevice` (u64 offsets), so it builds for
  `wasm32-unknown-unknown`. Results serialize to the Python `to_dict()` JSON,
  errors carry the same codes.
- `fpcore` (a binary in `flashpod-core`): the harness's handle on the
  library, JSON in and out. Not the flashpod CLI.

## Proof

`scripts/rust_parity.py` runs the same calls through Python and Rust on a
corpus of cards (synthetic, plus one per cached catalog firmware) and
compares every field Rust produces; fields it doesn't produce yet are listed
as PENDING. Ids and time come from `flashpod/entropy.py`, whose SplitMix64
and fixed clock the Rust side reproduces, so written images can be compared
byte for byte as the port reaches the writers.

```sh
cd rust && cargo test && cargo build --lib --target wasm32-unknown-unknown
python3 scripts/rust_parity.py
```

## Progress

- M0: harness, entropy, block device, error codes. Done.
- M1 (read-only): `inspect`'s layout fields (size, partitions, layout_ok,
  problems, past_128gib) match on every card in the corpus. Firmware,
  filesystem and iTunesDB fields: pending.
