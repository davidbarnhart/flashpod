# flashpod-core: API changes for library users

Newest first. Each entry says what moved and whether emupod (pinned at
4b62594) is affected. The JSON command API — `api::run_json` and its
commands — is the stable contract and has not changed.

## 1.0 clean-up (#93), on branch claude/api-cleanup

### Names emupod uses today: all unchanged

| Name | Used by | Status |
| --- | --- | --- |
| `api::run_json` | offline app (flashpod-wasm) | unchanged; also re-exported as `flashpod_core::run_json` |
| `api::Host` | flashpod-wasm | unchanged; also `flashpod_core::Host` |
| `block::BlockDevice` | flashpod-wasm, server | unchanged; also `flashpod_core::BlockDevice` |
| `library::Hooks` | flashpod-wasm | unchanged |
| `tags::ReadAt` | flashpod-wasm | unchanged |
| `firmware::{sha, CATALOG_JSON, catalog}` | server | unchanged |
| `fat::open_image_fat` | server (3 uses) | unchanged |
| `card::{flash, FlashOptions, firmware_bytes}` | server | unchanged (see below for FlashOptions) |
| `layout::{partitions, layout_facts, LBA28_MAX}` | server | unchanged |

`card::FlashOptions` gains two fields on branch claude/hfsplus (#104):
`mac: bool` (default `false`: an MBR + FAT32 card, as before) and
`hfs_time: u32` (default `0`; only read when `mac` is true, as the HFS+
volume's dates in seconds since 1904). FlashOptions derives `Default`, so
a struct literal keeps today's behaviour with `..Default::default()`
after its four fields. emupod's server (cards.rs:933) names all four
fields and no default, so it needs that one addition at the bump.

### Library gains user playlists (#106)

`itunesdb::Library` has a new field `playlists: Vec<Playlist>` (the user
playlists; `itunesdb::Playlist { name, ids }`). Code that builds a
Library with a struct literal must add `playlists: Vec::new()`;
`Library::new` sets it. emupod does not build one. A database with no
user playlists serializes to the same bytes as before; one with them now
keeps them through add and rm (rm also takes removed tracks out of every
playlist) instead of dropping them; rebuild still starts without them.
New module `playlist` edits them: `create`, `add`, `take`, `delete`,
`forget_tracks`.

### Now crate-private (no consumer named them)

`api::{run, firmware_of}`;
`fat::{SECTOR, ATTR_VOLUME_ID, ATTR_LFN, FAT32_EOC_MIN, FAT_MASK, find_fat_partition, EOC, FREE}`;
`fat32fmt::{SECTOR, FAT32_MIN_CLUSTERS, FAT32_MAX_CLUSTERS, fat_size_sectors, geometry, write_zeros}`;
`firmware::{FIXES_JSON, PP5002_OSOS_ADDR, VERSION_WORD_OFFSET, version_word_means, known_osos, known_aupd, fixes_table, sha256_hex, walk_directory, aupd_armed, fixes_present, describe_installed, unwrap_container}`;
`inspect::{DB_PATH, IPOD_DB_SIZE_LIMIT, IPOD_PATCHED_TRACK_LIMIT, LIBRARY_FIX}`;
`itunesdb::{MHOD_TITLE, MHOD_LOCATION, MHOD_ALBUM, MHOD_ARTIST, MHOD_GENRE, MHOD_FILETYPE, MHOD_COMPOSER, MHOD_PLAYLIST_POS, MAC_EPOCH_OFFSET, MARKER_MP3, MARKER_FLAC, parse_bytes}`;
`layout::FAT_TYPES`;
`library::{ADD_HEADROOM, track_from_audio, track_key, Batch, read_batch, IPOD_PATCHED_TRACK_LIMIT, select_for_removal, fingerprint}`;
`ops::{DB, save_library, probe}`;
`text::{latin1, utf16le_replace, py_rstrip, u32_at, u64_at, u16_at, slice}`.

If you need one of these, say so: it goes back to `pub` with a reason.

### Removed (used nowhere)

`entropy::{Clock, FixedClock}`, `library::NoHooks`.
