# Managing music: ls / add / rm / init

The everyday commands. Each works **either** over an OS mount **or** over the
[raw device](Raw-device-access-(no-OS-mount)) — flashpod picks automatically: an
already-mounted iPod is used as-is; otherwise it scans attached disks for one to
read/write directly (and elevates with sudo for raw access).

You can force a source: `--mount <path>` or `--raw <device>` (before or after
the subcommand).

## `flashpod ls` — list the library

```
$ flashpod ls            # artist → album tree with track counts
$ flashpod ls all        # + every track, with IDs (needed for `rm`) and times
$ flashpod ls artist     # flat per-artist counts (also: artists / album / albums)
```

## `flashpod add [path ...]` — add files/folders

Recurses directories (sorted; `._*` junk and non-audio skipped). Tags and
duration come from mutagen. The whole batch is committed in one DB write.

```
$ flashpod add ~/Music/Album/
$ flashpod add song1.mp3 song2.mp3 ~/more/
$ flashpod add                       # prompts for a path (tab-completes)
```

**Dedup:** an incoming file already on the iPod is skipped, keyed on
`(size, duration, title)` — derived from metadata, so it never reads the iPod's
stored copies back (which is slow, and fatal over FireWire). Re-encoded copies
have a different size and count as new.

The summary reports throughput, e.g. `12 tracks added in 4m41s (73.4 MiB at
267 KiB/s)` — handy for spotting when you're hitting the
[FireWire bandwidth ceiling](Hardware-and-the-FireWire-bridge#write-bandwidth).

## `flashpod rm` — remove tracks

```
$ flashpod rm 52 53                  # by track id (see `flashpod ls all`)
$ flashpod rm artist Relic Pop       # every track by an artist
$ flashpod rm album Thick As Thieves # every track in an album
```

Names are case-insensitive; multiword names need no quotes. (`remove`/`delete`/
`erase` are accepted aliases.)

## `flashpod init [name]` — set up a fresh card

Creates the `iPod_Control` directory structure and an empty database. Use after
flashing/formatting a card, or after a wipe. Destroys an existing **database**
but not music files already in the `F##` folders.

Over raw (no mount), `init` can't scan for an existing iPod database (a fresh
card has none), so it lists candidate FAT disks, labels each
(*empty / already an iPod*), and asks before writing.

## Fast bulk loading: use a USB reader

Writing over **FireWire** is slow (~270 KiB/s — a
[hardware limit](Hardware-and-the-FireWire-bridge#write-bandwidth)). For a whole
album or library, pull the card out of the iPod, put it in a **USB card reader**,
and `add` over the normal mount — USB bypasses the bridge and is far faster.
Keep the raw FireWire path for quick incremental edits where pulling the card
isn't worth it.

## A typical Linux session (over a mount)

```sh
udisksctl mount -b /dev/sdX2          # find X via: lsblk -o NAME,TRAN,LABEL
flashpod add /path/to/music/
flashpod ls
sync && udisksctl unmount -b /dev/sdX2
```

> Close **Rhythmbox** before syncing/ejecting — it auto-grabs the iPod mount
> (its own libgpod plugin) and blocks unmount.
