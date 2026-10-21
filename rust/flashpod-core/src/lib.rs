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
pub mod fat;
pub mod fat32fmt;
pub mod firmware;
pub mod inspect;
pub mod itunesdb;
pub mod layout;
pub mod library;
pub mod ops;
pub mod tags;
pub mod text;

pub use block::BlockDevice;
pub use error::{Error, ErrorKind};
