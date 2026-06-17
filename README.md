# flashpod

Command-line tooling for early (1st/2nd/3rd-generation, FireWire-era) iPods:
flash a CompactFlash/SD card with iPod firmware, initialize the music
database, and sync music — no iTunes, no gtkpod. Runs on Linux, macOS, and
Windows (Linux is the most-tested; the macOS/Windows disk backends are newer).

The iTunesDB is read and written natively in pure Python — no libgpod, no
compiled dependencies. Firmware images (the last stock Apple releases for 1G
through 4G models) are downloaded on demand from GitHub and verified by
checksum, or you can supply your own with `--firmware`.

## Requirements

- Python 3.6+ and `mutagen` (tag extraction; installed automatically by
  `pip install`, or use the distro `python3-mutagen` when running from source)
- FAT32 formatting during flash is built in (pure Python) — no external
  filesystem tools needed
- Pre-2007 iPods only — newer models need an iTunesDB checksum/hash that
  these tools don't generate.

## Install

### Download a release binary (no Python needed)

Grab the single self-contained executable for your OS from the
[Releases page](https://github.com/davidbarnhart/flashpod/releases). On
Linux/macOS, `chmod +x` it and run it; on Windows, run the `.exe`. (Firmware
images aren't in the binary — `flashpod flash` downloads the one you pick, or
you supply your own with `--firmware`.) (Building these is documented in
[BUILD.md](BUILD.md), including the manual macOS 10.8 build.)

**Vintage Macs (OS X 10.8):** use `flashpod-macos-10.8`. It has the firmware
**baked in** (no network needed), so `flashpod flash` works offline and
`--firmware` is optional. After downloading, make it runnable and clear the
Gatekeeper quarantine (it's unsigned):

```sh
chmod +x flashpod-macos-10.8
xattr -d com.apple.quarantine flashpod-macos-10.8   # or right-click → Open once
```

### Or install from source with pip

Install from a checkout — this puts a `flashpod` command on your PATH:

```sh
pip install .
```

Or run straight from the source tree without installing:

```sh
python -m flashpod ...        # run from the repo root
```

For development, an editable install keeps the command pointed at your checkout:

```sh
pip install -e .
```

> `flashpod flash` needs root. `sudo` uses root's PATH, so if `sudo flashpod`
> isn't found, run `sudo "$(command -v flashpod)" flash` (or
> `sudo python -m flashpod flash` from source).

## Commands

All commands take `--mount <path>` (before or after the subcommand). Without
it, the tool scans mounted filesystems for something iPod-like and **always
confirms before using its guess**:

- one candidate → `Using iPod mounted at /media/you/IPOD — continue? [Y/n]`
- several candidates → a numbered chooser, most probable first
- nothing iPod-like mounted → scans attached disks for an unmounted iPod
  partition (FAT-family; label/FireWire/removable heuristics) and offers to
  mount it via udisks — no root needed
- not running on a terminal → refuses; pass `--mount` explicitly

### `flashpod ls` (alias: `flashpod list`)

```
$ flashpod ls                 # artist → album tree with track counts
iPod "iPod": 68 tracks, 1 artists, 5 albums
New Order
  Power, Corruption & Lies (8 tracks)
  Substance (12 tracks)

$ flashpod ls all             # same tree + every track (id, track no., duration)
New Order
  Substance
        52   1. Ceremony                 4:25
        53   2. Everything's Gone Green  5:31

$ flashpod ls artist          # flat per-artist track counts (or `artists`)
$ flashpod ls album           # flat per-album track counts (or `albums`)
```

Track ids shown by `ls all` are what `flashpod rm <id>` takes.

### `flashpod add [path ...]`

Add audio files and/or directories. Directories are scanned recursively in
sorted order; macOS `._*` AppleDouble files and non-audio files are skipped.
Recognized extensions: `.mp3 .m4a .m4b .aac .wav .aif .aiff`. Tags, duration,
and bitrate are read automatically (mutagen).

Files already on the iPod are skipped, so you can safely re-point `add` at an
overlapping set — e.g. add a single, then later add the whole album folder
that contains it, and only the new tracks are copied. A track is considered a
duplicate when its size, duration, and title all match one already present
(this also de-duplicates within a single batch). Re-encoded or re-tagged
copies have a different size and are added as new.

```
$ flashpod add ~/music/Some\ Album            # a directory
$ flashpod add a.mp3 b.mp3 ~/music/More/      # mix files and directories
$ flashpod add                                # no args: prompts, with tab completion
```

Progress is one line per track, shown in a scrolling 4-line window so a big
batch doesn't flood the terminal history (when output is piped or redirected,
every line is printed instead). Skips and failures stay visible above the
window, are counted in the summary, and don't stop the batch:

```
[3/14] skipping ._cover.mp3: not a recognized audio file
[4/14] skipping 01 Come Together.mp3: already on iPod
[11/14] Adding: Pet Cemetery — Relic Pop...        ⌝
[12/14] Adding: Rarely Seen Violence — Relic Pop... | last 4 only;
[13/14] Adding: Sister Sky — Relic Pop...           | older lines scroll away
[14/14] Adding: Thick as Thieves — Relic Pop...    ⌟
12 tracks added, 1 skipped (already on iPod), 1 failed in 1m02s
```

### `flashpod rm`

```
$ flashpod rm 52 53           # by track id (see `flashpod ls all`)
$ flashpod rm artist Relic Pop      # every track by the artist
$ flashpod rm album Thick As Thieves # every track in the album
```

Artist/album matching is case-insensitive exact match; multiword names need
no quotes. `remove`, `delete`, and `erase` all work as synonyms for `rm`.
**Removal is immediate — there is no confirmation prompt.**

### `flashpod init [name]`

Create the `iPod_Control` directory structure and an empty music database.
Use on a freshly flashed/formatted card or after a wipe. Destroys any
existing database, but not music files already in the `F##` folders.

### `flashpod flash [/dev/sdX]`

Write the iPod firmware and partition layout to a CF/SD card. **Erases the
card.** Needs root (`sudo flashpod flash`).

```
$ sudo flashpod flash                  # interactive: pick from removable disks
$ sudo flashpod flash /dev/sdb         # direct
$ flashpod flash /dev/sdb --dry-run    # print the plan, write nothing (no root)
$ flashpod flash --self-test           # validate layout logic, no hardware
```

**Firmware:** with no `--firmware`, an interactive chooser lists the images
from the bundled catalog (`flashpod/firmware/firmware.json`) by iPod
generation, version, and description (the default entry is preselected;
non-interactive runs use it outright). The chosen `.ipsw` is then **downloaded
from GitHub**, cached under `~/.cache/flashpod/`, and **verified against its
SHA-256** before use; later flashes reuse the cached copy (no network). The
images aren't bundled with flashpod — they're Apple's copyright, hosted as
[release assets](https://github.com/davidbarnhart/flashpod/releases/tag/firmware).

To use a firmware flashpod doesn't host (or to work fully offline), download
an `.ipsw` yourself and pass it with `--firmware <file>` — that path never
touches the network. To add an image to the catalog, upload it to the firmware
release and add a manifest entry (`file`, `url`/`base_url`, `sha256`,
`generation`, `version`, `description`).

Options:

| Flag | Meaning |
|------|---------|
| `--firmware <file>` | use a local `.ipsw` (bring-your-own; no download). Default: pick from the catalog and download it |
| `--yes`             | skip the typed `ERASE sdX` confirmation |
| `--no-format`       | don't format the data partition |
| `--dry-run`         | show the plan only |
| `--self-test`       | check layout-building logic and exit |

Safety: only removable/USB disks are offered, the disk backing the running
system is always refused, partition nodes (`/dev/sdb1`) are rejected, the
target is unmounted first, and an explicit typed confirmation is required.
After writing, the firmware region is read back and compared byte-for-byte
before the card is ejected. Cards larger than 128 GiB are capped at the
iPod's LBA28 addressing limit.

After a successful interactive flash, flashpod offers to run init on the new
card right away, and after that to load music onto it too — answer Y (the
default) to both and the card comes out of the flash step ready to play. The
offers are skipped for `--dry-run`, `--no-format`, and non-interactive runs.

## Typical workflows

**Build a new card from scratch:**

```sh
sudo flashpod flash                 # flash + FAT32 format (label IPOD);
                                      # answer Y to the init offer, then Y to
                                      # load music — done in one sitting
# put the card in the iPod and play
```

(If you declined the offers — or flashed non-interactively — mount the card
and run `flashpod init`, then `flashpod add`, as separate steps.)

**Sync music to an existing iPod:**

```sh
udisksctl mount -b /dev/sdX2      # find X via: lsblk -o NAME,TRAN,LABEL
flashpod add ~/music/Some\ Album
flashpod ls
sync && udisksctl unmount -b /dev/sdX2
```

## Files

| Path | Role |
|------|------|
| `flashpod/cli.py` | the command-line interface (entry point `flashpod`) |
| `flashpod/itunesdb.py` | pure-Python classic iTunesDB reader/writer |
| `flashpod/ipod_flash.py` | flashing engine (firmware + partition layout) |
| `flashpod/fat32.py` | pure-Python FAT32 formatter |
| `flashpod/platform/` | per-OS backends (disk enumerate / unmount / raw I/O / privilege) |
| `flashpod/firmware/firmware.json` | firmware catalog (URLs + checksums; images are downloaded) |
| `flashpod/contrib/` | the Linux FireWire udev rule |
| `pyproject.toml` | packaging + `flashpod` entry point |
| `ipodctl.c` | legacy libgpod C helper — kept only as a test oracle |

## Notes

- Close Rhythmbox before syncing/ejecting — its libgpod plugin grabs the
  iPod mount and blocks unmount.
- Each `flashpod add` rewrites the whole iTunesDB per track; fine at hundreds of
  tracks, slow for huge libraries.
- Don't trust `fsck.vfat` on iPod cards: dosfstools chokes on iPod boot
  sectors that the kernel mounts fine.

## License

flashpod is released under the [MIT License](LICENSE). The firmware `.ipsw`
images that `flashpod flash` downloads are Apple's copyright, not covered by
that license and not part of this source tree — they are hosted separately for
convenience, and you may supply your own instead.
