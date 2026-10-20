//! Errors with the Python library's stable codes (flashpod/errors.py).

use serde_json::{Map, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Error,
    NotACard,
    CardExists,
    CardBusy,
    CardTooSmall,
    NotInitialized,
    CorruptLibrary,
    FirmwareInvalid,
    NoFirmware,
    LayoutMismatch,
    VerifyFailed,
    NoSpace,
    DbCeiling,
    NothingToAdd,
    NoSuchTrack,
    Cancelled,
    AlreadyInitialized,
    PlanStale,
}

impl ErrorKind {
    pub fn code(self) -> &'static str {
        use ErrorKind::*;
        match self {
            Error => "error",
            NotACard => "not_a_card",
            CardExists => "card_exists",
            CardBusy => "card_busy",
            CardTooSmall => "card_too_small",
            NotInitialized => "not_initialized",
            CorruptLibrary => "corrupt_itunesdb",
            FirmwareInvalid => "firmware_invalid",
            NoFirmware => "no_firmware",
            LayoutMismatch => "layout_mismatch",
            VerifyFailed => "verify_failed",
            NoSpace => "no_space",
            DbCeiling => "db_ceiling",
            NothingToAdd => "nothing_to_add",
            NoSuchTrack => "no_such_track",
            Cancelled => "cancelled",
            AlreadyInitialized => "already_initialized",
            PlanStale => "plan_stale",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
    pub details: Map<String, Value>,
}

impl Error {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Error { kind, message: message.into(), details: Map::new() }
    }
    pub fn detail(mut self, key: &str, value: impl Into<Value>) -> Self {
        self.details.insert(key.to_string(), value.into());
        self
    }
    /// The Python `FlashpodError.to_dict()`.
    pub fn to_json(&self) -> Value {
        let message = if self.message.is_empty() { self.kind.code().to_string() } else { self.message.clone() };
        serde_json::json!({"code": self.kind.code(), "message": message, "details": self.details})
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind.code(), self.message)
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::new(ErrorKind::Error, e.to_string())
    }
}
