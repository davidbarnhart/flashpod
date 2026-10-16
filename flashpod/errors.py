"""Exceptions raised by flashpod's library (:mod:`flashpod.card` and the
modules under it).

Every one is a :class:`FlashpodError` with a stable ``code`` string, which a
frontend maps to its own wording, and a ``details`` dict of JSON-safe facts.
The message (``str(exc)``) is English for logs; it is not an API.
"""


class FlashpodError(Exception):
    code = "error"

    def __init__(self, message="", **details):
        super(FlashpodError, self).__init__(message or self.code)
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


class NotACard(FlashpodError):
    """The path isn't a usable card image (missing, not a regular file, no
    partition table, no FAT32 data partition, ...)."""
    code = "not_a_card"


class CardExists(FlashpodError):
    """create_blank was asked to overwrite an existing file."""
    code = "card_exists"


class CardBusy(FlashpodError):
    """Another process holds a conflicting lock on the image (the emulator, a
    page action, the CLI). Nothing was read or written."""
    code = "card_busy"


class CardTooSmall(FlashpodError):
    code = "card_too_small"


class NotInitialized(FlashpodError):
    """A FAT32 card with no iPod_Control / iTunesDB: run init first."""
    code = "not_initialized"


class CorruptLibrary(FlashpodError):
    """The iTunesDB file is present but won't parse (e.g. a torn write left
    garbage in its clusters). The device is still an iPod -- the database just
    needs rebuilding -- so this is distinct from "no database at all"."""
    code = "corrupt_itunesdb"


class FirmwareInvalid(FlashpodError):
    code = "firmware_invalid"


class NoFirmware(FlashpodError):
    """The card's firmware partition holds no recognizable firmware."""
    code = "no_firmware"


class LayoutMismatch(FlashpodError):
    """flash_firmware: the card isn't laid out the way flashpod lays out a card,
    so replacing only the firmware partition isn't safe."""
    code = "layout_mismatch"


class VerifyFailed(FlashpodError):
    code = "verify_failed"


class NoSpace(FlashpodError):
    code = "no_space"


class DbCeiling(FlashpodError):
    """The batch would grow the iTunesDB past what the firmware loads."""
    code = "db_ceiling"


class NothingToAdd(FlashpodError):
    code = "nothing_to_add"


class NoSuchTrack(FlashpodError):
    code = "no_such_track"


class Cancelled(FlashpodError):
    code = "cancelled"


class AlreadyInitialized(FlashpodError):
    """init: the card already has an iTunesDB (pass overwrite=True)."""
    code = "already_initialized"


class PlanStale(FlashpodError):
    """add(plan=...): the card or the selection changed since plan_add."""
    code = "plan_stale"
