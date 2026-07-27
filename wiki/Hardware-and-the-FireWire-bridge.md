# Hardware and the FireWire bridge

Almost every odd behavior in flashpod traces back to one piece of hardware: the
**gen-1 FireWire-to-ATA bridge** in early iPods. This page collects what we
learned about it the hard way, so you don't have to rediscover it.

## The unit

- 64 GB **flash-modded** iPod (reports 63,996,657,664 bytes), a pre-2007 model.
- Connects over **FireWire** (shows up as an `sbp` transport on Linux,
  "Apple Computer, Inc." / FireWire in macOS `diskutil`). On Linux, lsblk
  reports its device TYPE as `rbc` (SCSI Reduced Block Commands), **not**
  `disk` — filter on either, or the iPod vanishes from your tooling.
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

## Linux: flashpod's raw path is O_DIRECT (queue settings optional for it)

flashpod's own raw driver opens Linux block devices with **O_DIRECT** — the
page-cache bypass, Linux's equivalent of macOS `/dev/rdiskN` — so its capped
transfers reach the hardware exactly as issued, with no kernel readahead or
writeback re-batching. The raw path (`ls`/`add`/`rm`/`init` without a mount)
is therefore bridge-safe on its own, whatever the queue settings say.

What still wants the per-device queue settings pinned:

```
max_sectors_kb = 4      # cap transfers at 4 KiB
read_ahead_kb  = 0      # no prefetch
queue_depth    = 1      # one request at a time
```

- using an **OS mount** of a FireWire iPod (the kernel FAT driver reads big),
- **udev's own blkid probe** — at every (re-)attach, and **again every time a
  writable handle on the device is closed** (udev watches block devices via
  inotify). That second one bites pure-raw workflows: an unpinned post-write
  probe collapsed the bridge to 0 capacity immediately after a successful
  `rm` whose own O_DIRECT I/O had run clean. flashpod therefore pins at the
  start of every raw session too (it's root there, so it's silent),
- any other buffered reader — I/O flashpod doesn't issue and can't intercept.

These **reset on every re-attach**, and unsafe defaults (128/128) are
*data-eating* for buffered access. flashpod checks before every command and
auto-pins them via sudo; it refuses to touch a FireWire iPod that's still
unsafe (`--unsafe-queue` overrides). The udev rule in
`contrib/99-flashpod-firewire-ipod.rules` is optional defense-in-depth that
covers the attach-time window no userspace program can reach; nothing in
flashpod requires it — detection probes disks with flashpod's own driver and
works even when udev's records are blank.

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

### The "disk you inserted was not readable" dialog

Because the mount probe fails, macOS offers **Initialize… / Ignore / Eject**
every time you attach the iPod.

> **Always click Ignore. Never Initialize** — that opens Disk Utility pointed
> at your iPod, one click from erasing the card.

flashpod doesn't care either way: it reads `/dev/rdiskN` itself and never
needs the volume mounted. To stop the panel appearing at all, disable the GUI
agent that draws it (run in Terminal **on the Mac** — over SSH it lands in a
different launchd bootstrap and won't affect your desktop session):

```sh
launchctl unload -w /System/Library/LaunchAgents/com.apple.DiskArbitrationAgent.plist
```

`diskarbitrationd` — the daemon that actually probes and mounts — keeps
running, so ordinary disks still automount as before; only the notification
panels stop. It persists across reboots; `load -w` puts it back. The
trade-off: it silences the panel for *every* unreadable disk, not just the
iPod. (An `/etc/fstab` entry can't help here — fstab matches on volume UUID
or label, and the whole problem is that macOS can't read either one off this
bridge.)

### "It vanished from `diskutil` entirely"

Different symptom, different cause: **the bridge is wedged**, not the card.
Seen live on 10.8 — macOS had the whole driver stack attached
(`IOFireWireSBP2LUN` → `iPodFireWireTransport` → `IOBlockStorageDriver`) but
published **no `IOMedia`**, so no `/dev/diskN` existed and nothing could find
it. That means the OS asked for the medium/capacity and got nothing back —
the same collapse Linux shows as a 0-byte disk.

The bridge is powered by the iPod's own battery, so **its state survives
unplugging** — carrying a wedged iPod to another machine brings the wedge
along, and a cable replug won't clear it. Reset the iPod itself: Hold on,
Hold off, then **Menu + Play/Pause** until the Apple logo appears. The disk
comes back.

Diagnose it with:

```sh
ioreg -c IOFireWireSBP2LUN -r -d 6 | grep -E '\+-o'   # stack attached?
ioreg -c IOMedia | grep -i ipod                       # any media node?
```

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
