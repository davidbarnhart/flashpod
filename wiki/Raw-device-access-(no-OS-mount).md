# Raw device access (no OS mount)

flashpod can read and write an iPod **without mounting it**, by talking to the
raw block device with its own pure-Python FAT32 driver. This is what makes an
iPod work on a Mac that [can't mount it](Hardware-and-the-FireWire-bridge#macos-cant-mount-over-firewire-no-fix-available).

## Why it exists

macOS can't mount this iPod's FAT over FireWire — the bridge corrupts the OS's
read-ahead. But the bridge handles **small, direct** transfers fine. So flashpod
does every filesystem access itself, in tiny chunks, straight to the device —
no OS mount, no read-ahead.

## How it finds the iPod (no labels, no guessing)

Volume labels and bus types proved too fragile (a mis-parsed bus on 10.8 made
the iPod invisible once). So flashpod identifies an iPod by its **actual iTunes
database**:

1. Enumerate attached disks worth probing (every external disk on macOS via
   `diskutil`; FAT partitions on removable/USB/FireWire disks on Linux).
2. Open each with the FAT driver and check whether its filesystem contains
   `iPod_Control/iTunes/iTunesDB`.
3. The one(s) that do are iPods. One hit → use it; several → pick from a list.

This is robust: a random USB stick is read and rejected; the iPod is found by
what's *on* it.

## The two things that make raw access correct

1. **Unbuffered node.** On macOS, reads must go through `/dev/rdiskN`
   (unbuffered), never `/dev/diskN` (buffered) — the buffered device
   re-introduces the read-ahead that corrupts the bridge. flashpod maps
   `disk`→`rdisk` automatically, so even `--raw /dev/disk1` is safe.
2. **Tiny transfers.** Every read and write is capped at a bridge-safe size:
   **1 sector on macOS, 8 (4 KiB) on Linux.** macOS is single-sector because
   this bridge corrupts anything larger in *both* directions (proven on
   hardware). `FLASHPOD_RAW_MAX_XFER=<sectors>` overrides it — raise it on a USB
   reader, which has no bridge.

## Root and sudo

Reading/writing a raw device needs root. You don't type `sudo` — flashpod
**re-execs itself under sudo** the first time it needs the device, so:

```
$ flashpod add ~/Music/album
flashpod: add over the iPod's raw device needs root — elevating via sudo...
Password:
Found iPod on /dev/rdisk2 — 31 tracks.
```

Any `FLASHPOD_*` environment variables you set are carried through the sudo
boundary (sudo otherwise wipes them).

## Using it explicitly

Auto-detection covers the common case, but you can name the device:

```
$ flashpod ls  --raw /dev/rdisk1s2     # macOS: the data partition…
$ flashpod ls  --raw /dev/rdisk1       # …or the whole disk (MBR auto-walked)
$ flashpod add --raw /dev/sdb2 ~/Music # Linux works too
```

`--raw` accepts the data **partition** node or the **whole disk** (it reads the
MBR and seeks to the FAT partition itself).

## How it's validated

The FAT driver — directories, long filenames, multi-cluster files, allocation,
overwrite, delete — is cross-checked three independent ways in
`python3 -m flashpod.fatfs --self-test`:

- flashpod's own reader re-reads what it wrote,
- **mtools** (an independent FAT implementation) reads our writes and vice
  versa,
- the **Linux kernel's** own FAT driver mounts our hand-written filesystem
  (run the self-test as root for this one).

> Test note: under `sudo`, root's mtools can mangle binary reads or hang on
> stdin, so the self-test uses mtools only as non-root and the kernel mount only
> as root — each an independent oracle.

## Speed

Over FireWire the raw path is **slow** (~270 KiB/s — a
[hardware ceiling](Hardware-and-the-FireWire-bridge#write-bandwidth), not
tunable). The driver's own overhead was optimized away (it no longer scans the
multi-MB FAT to count free clusters, and batches FAT updates), so it's steady,
not stalled — but the bridge's bandwidth is the limit. For bulk loads, use a
**USB reader** and the normal mount.

## Status

- **Read:** done (`ls`).
- **Write:** done (`add`, `rm`, `init`) via a pure-Python FAT writer.
