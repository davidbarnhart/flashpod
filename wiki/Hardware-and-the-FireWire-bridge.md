# Hardware and the FireWire bridge

Almost every odd behavior in flashpod traces back to one piece of hardware: the
**gen-1 FireWire-to-ATA bridge** in early iPods. This page collects what we
learned about it the hard way, so you don't have to rediscover it.

## The unit

- 64 GB **flash-modded** iPod (reports 63,996,657,664 bytes), a pre-2007 model.
- Connects over **FireWire** (shows up as an `sbp` transport on Linux,
  "Apple Computer, Inc." / FireWire in macOS `diskutil`).
- Pre-2007 ⇒ **no iTunesDB checksum/hash** needed — plain library writes work.
- Partition layout written by flashpod: a 32 MiB firmware partition (MBR type
  0x00, hidden from macOS `diskutil`) followed by a FAT32 data partition (type
  0x0b). The FAT volume is labeled `IPOD`.

## The defect: the bridge corrupts large/queued transfers

The bridge reliably handles **small, direct** transfers but corrupts **large or
read-ahead** ones — they come back as zeros. This single fact explains:

- Why a fresh attach can wipe out reads until the OS is told to throttle.
- Why macOS can't mount the volume (below).
- Why flashpod's raw driver caps every transfer at a tiny size.

### Proven transfer limits (measured on real hardware)

| Transfer | Reads | Writes |
|---|---|---|
| 1 sector (512 B) | ✅ works | ✅ works |
| 8 sectors (4 KiB) | ❌ returns zeros | ❌ corrupts |

So over FireWire, **single-sector is the only proven-safe size in both
directions.** (On Linux the kernel queue cap of 4 KiB happens to be safe for
the *kernel's* access pattern, but the macOS raw device has no such cap and
corrupts at 4 KiB — see below.)

## Linux: pin the block queue (flashpod does this for you)

On Linux the fix is per-device queue settings:

```
max_sectors_kb = 4      # cap transfers at 4 KiB
read_ahead_kb  = 0      # no prefetch
queue_depth    = 1      # one request at a time
```

These **reset on every re-attach**, and unsafe defaults (128/128) are
*data-eating*. flashpod checks before every command and auto-pins them via
sudo; refuses to touch a FireWire iPod that's still unsafe (`--unsafe-queue`
overrides). For a permanent fix, install the udev rule in
`contrib/99-flashpod-firewire-ipod.rules`.

> **Incident (the cautionary tale):** an afternoon attach ran with default
> queue settings, an `ls` triggered EIO on the iTunesDB, the device collapsed
> to 0 capacity, and the in-flight FAT writes corrupted the filesystem. Root
> cause was *not* the card or the format — it was the unpinned queue. Lesson:
> never let a FireWire iPod run with default queue settings.

## macOS: can't mount over FireWire (no fix available)

macOS Disk Utility shows **"Invalid BS_jmpBoot in boot block: 000000"** and
won't mount the volume. This is **not** a format bug — the card is perfect:

- A freshly-flashed card reads back byte-perfect over a **USB reader**.
- It *also* reads back byte-perfect over FireWire via the **raw** char device:
  `sudo dd if=/dev/rdiskN bs=512 count=1 skip=65599` → `eb 58 90 "MSWIN4.1"`.
- The **same** sector through the **buffered** block device (`/dev/diskN`)
  comes back all zeros.

The only variable is read-ahead: macOS's buffer cache prefetches a large read,
the bridge mangles it, and the mount probe sees a zeroed boot sector. macOS 10.8
exposes no per-disk read-ahead/transfer throttle (unlike Linux), and even a
forced mount would corrupt every buffered file read. **Conclusion:** don't
target Mac/FireWire mounting for this unit. Manage it on Linux (queue pinned),
or pull the card into a USB reader, or use flashpod's
[raw device path](Raw-device-access-(no-OS-mount)) which sidesteps the mount.

This is specific to this flaky bridge + flash mod; stock 1G/2G iPods mount on
Macs over FireWire fine.

## Write bandwidth

Raw writes over the bridge are **bandwidth-limited at ~270 KiB/s** (a hard
ceiling), not
transaction-limited. Measured: 1024 B writes → 267 KiB/s, 2048 B writes → 281
KiB/s. A 4× larger transfer bought ~5% — i.e. ~3.6 µs/byte with no
per-transaction overhead to amortize, and anything above the safe size corrupts
anyway. **So tuning the transfer size is futile**; flashpod just uses the
single proven-safe size. This is a property of the old bridge (likely PIO-mode
ATA), nothing software can fix. For bulk loads, use a **USB reader** — it
bypasses the bridge and is ~10–50× faster.

## "Insane capacity" = a seating/connector symptom

A bogus capacity reading (e.g. 707 GB) or empty fs-probes usually means a
**loose connector**, not a dead card. On a 3G, a loose IDE connector also
produced a "folder with !" boot icon. Reseat before suspecting the card.

## Other format facts worth knowing

- **FAT32 needs ≥ 65525 clusters.** Below that, the spec says the volume is
  FAT16, and the iPod firmware reads it as FAT16 and crashes the same way a
  missing filesystem would. `mkfs.fat` only *warns*. flashpod sizes clusters
  adaptively to stay above the floor.
- **The FAT32 `hidden_sectors` field is not required** (the iPod reads FAT fine
  with it 0). An earlier "requirement" was a red herring caused by a missing
  `os.sync()`.
- **`os.sync()` is required after writing** over a mount — fsyncing the
  iTunesDB file alone isn't enough; the kernel's FAT dir entries / tables stay
  in page cache until a system-wide sync, and the iPod reads a stale FS (0
  tracks) without it.
- **`dosfstools`/`fsck.vfat` chokes on iPod boot sectors** but the kernel mounts
  them fine — don't trust its verdicts on this device.
