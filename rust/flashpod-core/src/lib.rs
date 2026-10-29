//! flashpod's card library in Rust: the port of the Python `flashpod.card`
//! (the reference). Same rules as the Python library: no printing, no
//! prompts, no exit, no global state; all I/O through [`block::BlockDevice`],
//! so it builds for `wasm32-unknown-unknown`. Results serialize to the same
//! JSON as the Python results' `to_dict()`, and errors carry the same stable
//! codes. Parity is proved by `scripts/rust_parity.py` in the flashpod repo.

pub mod api;
pub mod block;
pub mod card;
pub mod entropy;
pub mod error;
pub mod export;
pub mod extract;
pub mod fat;
pub mod fat32fmt;
pub mod hfsplus;
pub mod firmware;
pub mod inspect;
pub mod itunesdb;
pub mod layout;
pub mod library;
pub mod ops;
pub mod tags;
pub mod text;

// The public surface, in three tiers (#93):
//  1. The stable contract: api::run_json and its JSON commands. emupod's
//     offline app talks only through it; it changes only with emupod.
//  2. The typed card API: card, ops, inspect, firmware's catalog -- what
//     the CLI and emupod's server call directly; renames are listed in
//     rust/MIGRATION.md.
//  3. The building blocks the CLI drives (fat, itunesdb, layout, tags,
//     library, text). Anything no consumer names is pub(crate).
pub use api::{run_json, Host};
pub use block::{BlockDevice, FileDevice, MemDevice};
pub use error::{Error, ErrorKind};
