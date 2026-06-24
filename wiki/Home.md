# flashpod

Minimal command-line iPod sync + card-flashing tooling, in pure Python.

flashpod manages music on a classic (pre-2007) iPod and flashes iPod firmware
onto CF/SD cards. It grew out of a simple goal — get MP3s onto a flash-modded
FireWire iPod — and ended up solving a much harder problem: **managing an iPod
that the operating system refuses to mount at all.**

## Start here

- **[Managing music](Managing-music-(ls-add-rm-init))** — `ls`, `add`, `rm`,
  `init`: the everyday commands.
- **[Hardware and the FireWire bridge](Hardware-and-the-FireWire-bridge)** —
  why these old iPods are finicky, and the bridge defect that drives almost
  every quirk below. *Read this if anything behaves strangely.*
- **[Raw device access](Raw-device-access-(no-OS-mount))** — how flashpod reads
  and writes an iPod with no OS mount (a pure-Python FAT driver). This is what
  makes the unmountable case work.
- **[Troubleshooting](Troubleshooting)** — symptom → cause → fix.
- **[Building](Building)** — building the binaries, including the macOS 10.8 trap.

## The one-paragraph story

Stock 1st/2nd-gen iPods mount fine. **This** unit is a 64 GB flash-modded
FireWire iPod whose gen-1 bridge corrupts large/queued transfers. On Linux you
work around it by pinning the block queue (flashpod does this automatically). On
**macOS there is no such knob**, so the OS can't even mount the volume — its
read-ahead turns the boot sector into zeros. flashpod's answer is to skip the OS
filesystem entirely: a pure-Python FAT32 driver talks to the raw device in
small, bridge-safe transfers, finds the iPod by its actual iTunes database, and
reads/writes it directly. The result: full `ls`/`add`/`rm`/`init` on a Mac that
flatly refuses to mount the iPod.

## Two ways flashpod reaches an iPod

| | OS mount | Raw device (no mount) |
|---|---|---|
| How | normal filesystem via `udisks`/Finder | flashpod's own FAT driver on `/dev/rdiskN` |
| Speed | fast | slow on FireWire (~270 KiB/s — a [hardware ceiling](Hardware-and-the-FireWire-bridge#write-bandwidth)) |
| Needs root | no (Linux udisks) | yes (auto-elevates via sudo) |
| When | Linux (queue pinned), or any card in a **USB reader** | macOS/FireWire (can't mount), or by choice |

**Rule of thumb:** bulk loads → put the card in a **USB reader** and use the
mount (fast). Quick edits on the iPod itself → the raw path (convenient, slow).
