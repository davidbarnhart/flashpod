# flashpod

flashpod is both an iPod flash-card imager and a tool for syncing and
organizing the music on it.

It handles the whole setup without iTunes, so you can revive a vintage iPod
and run it from a modern desktop: it writes the firmware to the card, creates
the initial music database, and loads the card with music. Then, once you pop
the card into an iPod and connect it over USB or FireWire, flashpod manages
the library on the device too — adding and removing songs right on the iPod.

## Requirements

- **iPod:** Gen 1, 2, or 3 (tested successfully); Gen 4 should work but isn't
  tested yet. Later models (2007 and newer) aren't supported — they need an
  iTunesDB checksum/hash flashpod doesn't generate.
- **Adapters:** CF to 1.8" IDE adapter (common on eBay); CF to SD card adapter
  (if using an SD card).
- **Flash cards:** CompactFlash card, or SD card with a CompactFlash adapter.
- **Card reader:** USB CompactFlash reader (to flash the card and load the bulk
  of your music).
- **Operating system:** Linux and macOS (tested). Windows has a backend but
  isn't tested yet.

flashpod was originally written to run from a modern Linux desktop. The
one-time flashing operation only needs a working USB card reader. For a 3rd- or
4th-gen iPod, a modern machine can flash the card and manage the iPod. For a 1st-
or 2nd-gen iPod, you can still flash the card from a modern machine, but you'll
need a FireWire interface to manage the music on the iPod afterward.

## FireWire

A FireWire interface is a rarity these days. On a desktop, the easiest option
is to add a FireWire card — Linux still supports FireWire well, so a Linux
machine with a FireWire card lets flashpod manage a 1st- or 2nd-gen iPod.

That's not the only option, though. Older Macs shipped with FireWire built in,
and flashpod was deliberately written with as few external dependencies as
possible — and against a relatively old Python — to stay runnable on vintage
Mac hardware. It's been tested on a MacBook running OS X 10.8 so far. Since old
Macs often can't get online, the macOS release can be copied to a USB drive on
a modern machine and installed on the MacBook from there.

## Loading music

There are two complementary ways to get music onto a flash-modded iPod:

1. **Over FireWire, once the card is installed in the iPod** — convenient, but
   slower.
2. **A one-time bulk load onto the flash card while it's still in the card
   reader** — fast, but only possible before the card goes into the iPod.

flashpod supports both. We suggest loading the majority of your library onto the
card while it's still in the (much faster) card reader, and saving the FireWire
sync for occasionally adding new albums later.

## Install

If you already have the **pip** Python package manager (or **pipx**), install
with a single command:

```sh
pip install flashpod       # or: pipx install flashpod
```

Otherwise, download the archive for your OS from the
[Releases page](https://github.com/davidbarnhart/flashpod/releases) — each holds
a single self-contained executable (no Python or anything else to install).

**Linux** (`flashpod-linux-x86_64.tar.gz`):
```sh
tar xzf flashpod-linux-x86_64.tar.gz
cd flashpod-linux-x86_64
./install.sh            # to ~/.local/bin (no root); or: sudo ./install.sh
flashpod --help
```

**Windows** (`flashpod-windows-x86_64.zip`): unzip it and run `flashpod.exe`
from a terminal (an Administrator terminal for `flash`).

The **Linux and Windows** builds don't bundle firmware — `flashpod flash`
downloads the image you pick (verified by checksum), or you supply your own with
`--firmware`. (The **macOS 10.8** build is different — see below.) Building the
binaries yourself is documented in [BUILD.md](BUILD.md).

**Vintage Macs (OS X 10.8):** use `flashpod-macos-10.8.tar.gz`. Extract it,
then make the binary runnable and clear the Gatekeeper quarantine (it's unsigned):

```sh
tar xzf flashpod-macos-10.8.tar.gz && cd flashpod-macos-10.8
chmod +x flashpod
xattr -d com.apple.quarantine flashpod   # or right-click → Open once
./flashpod --help
```

## Typical workflow

**Set the card up in a USB reader first — transfers are far faster there than
over FireWire.** Flash it, then let flashpod initialize the database and load
your music, all in one sitting:

```sh
flashpod flash    # flash firmware + format the card; then answer Y to
                  # init the database, and Y to load your music onto it
```

<details>
<summary><strong>See a full <code>flashpod flash</code> session</strong> — model and firmware pick, card selection, erase, verify, init, and first music load</summary>

```
$ flashpod flash
flashpod flash: writing to a disk needs root — elevating via sudo...
[sudo] password for david:
Which iPod are you flashing for?

  #  iPod            Years    How to identify it
  -  --------------  -------  ------------------------------------------------
  0  1st generation  2001-02  scroll wheel physically turns
  1  2nd generation  2002     wheel does not move (touch-sensitive)
  2  3rd generation  2003     four buttons in a row above the wheel
  3  4th generation  2004     click wheel, buttons on the wheel, grayscale
  4  iPod photo      2004-05  click wheel, color display

Select model: 0
Firmware for 1st generation (newest first):

  #  Version  Built       Size  Notes
  -  -------  ----------  ----  ----------------------------------------------
  0  1.5 *    2005-02-18  2.0M  Last stock Apple-released firmware for 1G/2G
                                iPods
  1  1.4      2004-04-23  1.9M  Adds 20+ EQ presets, shuffle by album, track
                                scrubbing, Contacts/Calendar/Clock, Korean and
                                Chinese; iTunes 4.5 compatibility
  2  0.4      2001-12-23  0.7M  Adds Italian, Dutch, Spanish, Brazilian
                                Portuguese, Danish, Finnish, Norwegian and
                                Swedish; 255-character filenames; fixes low-
                                battery sleep wake and blue-and-white Power
                                Macintosh G3 compatibility
  3  0.0      2001-11-01  0.7M  Initial release for the original 1G iPod

  * recommended default

Select firmware [0]: 0
flashpod flash: downloading iPod_1.1.5_2005_02_18.bin.gz
  from https://github.com/davidbarnhart/flashpod/releases/download/firmware/iPod_1.1.5_2005_02_18.bin.gz
  91% (1.8/2.0 MiB)
flashpod flash: verified and cached iPod_1.1.5_2005_02_18.bin.gz
firmware OK: iPod_1.1.5_2005_02_18.bin.gz (5068800 bytes, structure validated)
Attached removable storage:

  #  Device         Size  Reader
  -  --------  ---------  ------------------------------------------
  0  /dev/sdb  119.1 GiB  USB3.0 Card Reader (Genesys Logic), slot 1
                          MBR: unformatted 32 MiB, vfat 119.1 GiB 'IPOD'  <-
                          looks like an iPod card
  1  /dev/sdc        0 B  USB3.0 Card Reader (Genesys Logic), slot 2
                          no card inserted

Select device number (or 'q' to quit): 0

  PLAN — this will ERASE /dev/sdb
    layout      : MBR + FAT32
    device size : 119.1 GiB (249737216 sectors)
    firmware     : sectors 63..65598   (32 MiB)
    data (FAT32) : sectors 65599..249735167  type 0x0B  (119.1 GiB)

  Type "ERASE sdb" to proceed: ERASE sdb
  wrote MBR partition table + firmware
  formatted data partition (FAT32, 7798364 clusters @ 16 KiB, label 'IPOD')
  verifying firmware on the card byte-for-byte (5068800 bytes @ sector 63) ...
  verify OK: 5068800 firmware bytes on the card match the image exactly.

The card still needs the iPod database before it can take music ("flashpod init").
Run init on /dev/sdb2 now? [Y/n] Y
Initialized iPod directory structure on /dev/sdb2

Music can be loaded onto the card now, or later when it is in the iPod.
Load music onto the card now? [Y/n] Y
File or directory to add (TAB to complete): /mnt/homestore/sound/mp3/New Order/Technique/
flashpod add: this batch is 54.8 MiB. You've got 118.94 GiB more than you need, Dude. That's gnarly!
[6/9] Adding: Run — New Order... 100% (6.2/6.2 MiB)
[7/9] Adding: Mr. Disco — New Order... 100% (6.0/6.0 MiB)
[8/9] Adding: Vanishing Point — New Order... 100% (7.3/7.3 MiB)
[9/9] Adding: Dream Attack — New Order... 100% (7.2/7.2 MiB)
9 tracks added in 42s (54.8 MiB at 1.3 MiB/s)
  flushing + ejecting /dev/sdb

Done. /dev/sdb is ready — insert it into the iPod.
```

</details>

Load the **bulk** of your library now, while the card is in the reader. Once the
card is in the iPod, music transfers over FireWire are much slower — fine for a
song or two, painful for a discography. So front-load it here.

Now pop the card into the iPod and connect the iPod to your computer. flashpod
finds it on its own — no mounting, no device paths, no `sudo` to type:

```
$ flashpod list
flashpod: looking for an iPod means reading attached disks, which needs root — elevating via sudo...
[sudo] password for david:
Found iPod on /dev/sdb2 (IPOD FireWire 119.1G) — 25 tracks.
iPod "iPod": 25 tracks, 2 artists, 3 albums
Can
  Tago Mago (7 tracks)
New Order
  Brotherhood (9 tracks)
  Technique (9 tracks)
```

Add or remove the odd track right on the device:

```
$ flashpod add ~/music/New\ Order\ -\ Blue\ Monday.mp3
Found iPod on /dev/rdisk1 — 82 tracks.
[1/1] Adding: Blue Monday — New Order... 100% (6.8/6.8 MiB)
1 track added in 26s (6.8 MiB at 268 KiB/s)

$ flashpod rm album Substance
Found iPod on /dev/rdisk1 — 83 tracks.
Removed: New Order - Ceremony
Removed: New Order - Temptation
…
Removed 12 tracks
```

## Commands

The easiest way to use flashpod is to run a simple command — like `flashpod
flash` or `flashpod add` — and let it walk you through the options. Many
commands accept extra flags, but you don't need to supply any.

**flashpod finds your iPod for you.** With no flags, it uses one that's already
mounted; otherwise it scans the attached disks and picks out the iPod by the
iTunes database on it (no guessing from volume labels), then reads and writes it
**directly over the raw device** with its own FAT driver — no OS mount required.

That raw path is what lets flashpod manage an iPod the OS *can't* mount, like a
flash-modded FireWire iPod on a Mac (macOS's read-ahead corrupts the boot
sector, so it refuses the volume). Raw access needs root, so flashpod re-runs
itself under sudo and prompts for your password — you never type `sudo`
yourself.

If more than one iPod is attached — say a freshly-flashed card sitting in a
reader while a FireWire iPod is also plugged in — flashpod lists them and asks
which to use rather than picking one for you. And on Linux, if a FireWire iPod
is plugged in but the kernel hasn't attached it as a disk yet, flashpod loads
the driver it needs first, so the iPod shows up instead of silently going
missing.

All library commands — `list`, `add`, `rm`, `init`, `rebuild` — work this way.

### `flashpod list`

```
$ flashpod list               # artist → album tree with track counts
iPod "iPod": 25 tracks, 2 artists, 3 albums
Can
  Tago Mago (7 tracks)
New Order
  Brotherhood (9 tracks)
  Technique (9 tracks)

$ flashpod list all           # same tree + every track (id, track no., duration)
$ flashpod list artist        # flat per-artist track counts (or `artists`)
$ flashpod list album         # flat per-album track counts (or `albums`)
```

<details>
<summary><strong>See full <code>flashpod list all</code> output</strong> — every track with its id, track number, and duration</summary>

```
$ flashpod list all
flashpod: looking for an iPod means reading attached disks, which needs root — elevating via sudo...
Found iPod on /dev/sdb2 (IPOD FireWire 119.1G) — 25 tracks.
iPod "iPod": 25 tracks, 2 artists, 3 albums
Can
  Tago Mago
        70   1. Paperhouse                            7:28
        71   2. Mushroom                              4:03
        72   3. Oh Yeah                               7:24
        73   4. Halleluhwah                          18:28
        74   5. Aumgn                                17:33
        75   6. Peking O                             11:38
        76   7. Bring Me Coffee or Tea                6:46
New Order
  Brotherhood
        61   1. Paradise                              3:50
        62   2. Weirdo                                3:52
        63   3. As It Is When It Was                  3:46
        64   4. Broken Promise                        3:47
        65   5. Way Of Life                           4:05
        66   6. Bizarre Love Triangle                 4:21
        67   7. All Day Long                          5:12
        68   8. Angel Dust                            3:43
        69   9. Every Little Counts                   4:25
  Technique
        52   1. Fine Time                             4:42
        53   2. All The Way                           3:24
        54   3. Love Less                             3:04
        55   4. Round And Round                       4:31
        56   5. Guilty Partner                        4:48
        57   6. Run                                   4:31
        58   7. Mr. Disco                             4:21
        59   8. Vanishing Point                       5:17
        60   9. Dream Attack                          5:12
```

</details>

Track ids shown by `list all` are what `flashpod rm <id>` takes.

### `flashpod add [path ...]`

Add audio files and/or directories. Directories are scanned recursively in
sorted order. Recognized extensions: `.mp3 .m4a .m4b .aac .wav .aif .aiff`.
Tags, duration, and bitrate are read automatically.

Files already on the iPod are skipped, so you can safely re-point `add` at an
overlapping set — e.g. add a single, then later add the whole album folder
that contains it, and only the new tracks are copied. A track is considered a
duplicate when its size, duration, and title all match one already present
(this also de-duplicates within a single batch). Re-encoded or re-tagged
copies have a different size and are added as new.

```
$ flashpod add                                # ← the usual way: prompts, with tab completion
$ flashpod add ~/music/Some\ Album            # a directory
$ flashpod add a.mp3 b.mp3 ~/music/More/      # mix files and directories
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

<details>
<summary><strong>See a full <code>flashpod add</code> session</strong> — adding an album to the iPod over FireWire</summary>

```
$ flashpod add
flashpod: add over the iPod's raw device needs root — elevating via sudo...
Found iPod on /dev/sdb2 (IPOD FireWire 119.1G).
File or directory to add (TAB to complete): /mnt/homestore/sound/mp3/New Order/Brotherhood/
flashpod add: this batch is 51.0 MiB. You've got 118.89 GiB more than you need, Dude. That's gnarly!
[6/9] Adding: Bizarre Love Triangle — New Order... 100% (6.0/6.0 MiB)
[7/9] Adding: All Day Long — New Order... 100% (7.2/7.2 MiB)
[8/9] Adding: Angel Dust — New Order... 100% (5.1/5.1 MiB)
[9/9] Adding: Every Little Counts — New Order... 100% (6.1/6.1 MiB)
9 tracks added in 2m38s (51.0 MiB at 330 KiB/s)
```

Run with no arguments, `add` finds the iPod itself and prompts for a path (with
tab completion). Only the last four progress lines stay on screen — tracks 1–5
scrolled away above.

</details>

> **Adding over FireWire is slow** (~270 KiB/s — a hardware limit of these early
> bridges, not something a setting can fix). For **bulk** loads, pull the card
> into a USB reader and `add` over the normal mount — USB bypasses the bridge
> and is far faster. Keep the raw FireWire path for quick incremental edits.

### `flashpod rm`

```
$ flashpod rm 52 53           # by track id (see `flashpod list all`)
$ flashpod rm artist Relic Pop      # every track by the artist
$ flashpod rm album Thick As Thieves # every track in the album
```

Artist/album matching is case-insensitive exact match; multiword names need
no quotes. `remove`, `delete`, and `erase` all work as synonyms for `rm`.
**Removal is immediate — there is no confirmation prompt.**

### `flashpod init [name]`

Create the `iPod_Control` directory structure and an **empty** music database.
Use on a freshly flashed/formatted card or after a wipe. Destroys any
existing database, but not music files already in the `F##` folders. To
*recover* an iPod whose music files are intact but whose database is
missing/corrupt, use `rebuild` (below) — it keeps the music; `init` does not
re-index it.

### `flashpod rebuild [name]`

Rebuild the iTunes database **from the music files already on the iPod**. Walks
`iPod_Control/Music/F##`, reads each track's tags, and writes a fresh database
pointing at the existing files — so a corrupt or missing database is recovered
without re-copying or losing music. (It reads every track to sniff its tags, so
it's slow over FireWire; fine for a one-off recovery.) `flashpod list` points you
here when it finds an iPod with an unparseable database, and `flashpod add` onto
such an iPod offers to rebuild first, then adds your new files on top.

### `flashpod flash [/dev/sdX]`

Write the iPod firmware and partition layout to a CF/SD card. **Erases the
card.** Writing needs root, so flashpod re-runs itself under sudo (prompting for
your password) — you launch it as a regular user.

**Just run `flashpod flash` with no arguments.** That's the normal way: it lists
the removable disks, walks you through picking the right one, writes the
firmware, and then offers to initialize the database and load your music — the
whole card set up in one sitting. The forms below are only for when you want to
name the device yourself or preview the plan.

```
$ flashpod flash                       # ← the usual way: pick a disk interactively
$ flashpod flash /dev/sdb              # name the device yourself
$ flashpod flash /dev/sdb --dry-run    # print the plan, write nothing (no root)
$ flashpod flash --self-test           # validate layout logic, no hardware
```

**Firmware:** with no `--firmware`, flashpod first asks **which iPod you're
flashing for** (or takes `--model 1G|2G|3G|4G|photo`), then lists only the
firmware that fits it, from the bundled catalog
(`flashpod/firmware/firmware.json`), with version, build date, and description
(the default entry is preselected; non-interactive runs use it outright). The
chosen image is then **downloaded from GitHub**, cached under
`~/.cache/flashpod/`, and **verified against its SHA-256** before use; later
flashes reuse the cached copy (no network). The images aren't bundled with
flashpod — they're Apple's copyright, hosted as
[release assets](https://github.com/davidbarnhart/flashpod/releases/tag/firmware).

The model question isn't cosmetic: firmware is not interchangeable across
models. 2nd-generation (touch wheel) support first appeared in firmware 1.1.2,
so the 2001 images (1G's 0.0 and 0.4) would leave a 2G unusable — asking first
means they're never offered to one.

These early images have no low disk-size ceiling — 1.0 and 1.0.4 were tested on
a 128 GB card in full (the broken-folder icon on such a card means the data
partition wasn't initialized, since 1.0 doesn't auto-create `iPod_Control`; run
`flashpod init`). The only size limit is the 128 GiB LBA28 cap that binds all of
this firmware; see `--lba48`. A manifest entry *may* carry a `max_data_gb`
ceiling if some firmware ever needs one, and `--max-data-gb` sets it by hand.

Catalog images ship as `.bin.gz` (a gzipped raw `Firmware-*` image). To use a
firmware flashpod doesn't host (or to work fully offline), pass it with
`--firmware <file>` — that path never touches the network, and accepts a raw
`Firmware-*` image, a `.bin.gz`, or an Apple `.ipsw` (the format is detected
from the file's magic bytes, not its name). To add an image to the catalog,
upload it to the firmware release and add a manifest entry (`file`,
`url`/`base_url`, `sha256`, `size`, `generation`, `version`, `description`, and
`build_date` if known — it can't be recovered from the file later, since git
and release downloads both reset mtimes).

Options:

| Flag | Meaning |
|------|---------|
| `--firmware <file>` | use a local image — raw `Firmware-*`, `.bin.gz`, or `.ipsw` (bring-your-own; no download). Default: pick from the catalog and download it |
| `--model <id>`      | iPod model to flash for (`1G`, `2G`, `3G`, `4G`, `photo`); decides which firmware is offered. Default: ask |
| `--yes`             | skip the typed `ERASE sdX` confirmation |
| `--no-format`       | don't format the data partition |
| `--lba48`           | **experimental**: use the whole card for data, past the 128 GiB cap (LBA48-patched iPods only) |
| `--max-data-gb <N>` | cap the data partition to N decimal GB (e.g. to make a smaller partition, or probe a firmware's disk-size ceiling) |
| `--dry-run`         | show the plan only |
| `--self-test`       | check layout-building logic and exit |

Safety: only removable/USB disks are offered, the disk backing the running
system is always refused, partition nodes (`/dev/sdb1`) are rejected, the
target is unmounted first, and an explicit typed confirmation is required.
After writing, the firmware region is read back and compared byte-for-byte
before the card is ejected. Cards larger than 128 GiB are capped at the
iPod's LBA28 addressing limit — pass `--lba48` to use the whole card instead,
but only on an iPod running an LBA48 patch (an unpatched iPod can't address the
extra sectors and will misread the card).

After a successful interactive flash, flashpod offers to run init on the new
card right away, and after that to load music onto it too — answer Y (the
default) to both and the card comes out of the flash step ready to play. The
offers are skipped for `--dry-run`, `--no-format`, and non-interactive runs.


## Notes

- Close Rhythmbox before syncing/ejecting — its libgpod plugin grabs the
  iPod mount and blocks unmount.
- A batch `flashpod add` writes the database **once**, at the end — not per
  track. If a batch is interrupted, you may be left with orphaned music files
  but an unchanged database; just re-run the same `add` (files already present
  are skipped).
- Don't trust `fsck.vfat` on iPod cards: dosfstools chokes on iPod boot
  sectors that the kernel mounts fine.

## License

flashpod is released under the [MIT License](LICENSE). The firmware `.ipsw`
images that `flashpod flash` downloads are Apple's copyright, not covered by
that license and not part of this source tree — they are hosted separately for
convenience, and you may supply your own instead.
