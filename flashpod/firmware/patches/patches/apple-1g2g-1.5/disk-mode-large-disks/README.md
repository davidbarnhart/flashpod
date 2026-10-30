# Disk mode: use disks larger than 128 GiB, reading and writing

## The problem

[large-disks](large-disks.md) lets the player use a card past 128 GiB. FireWire **disk mode** is a
different program, and it is 28-bit too: on a 250 GB card a computer saw only 268,435,455 sectors
(128 GiB), and could not reach anything stored past them (2026-10-11, Linux).

## The code it targets

Disk mode is the "stand-alone disk mode" program inside `aupd` (file offset `0xf2750`, linked at
`0x40000000`; [wiki page 4](../../../docs/wiki/04-firewire-disk-mode.md)). It takes the disk's size
from IDENTIFY words 60/61 (`0x400017d0`), refuses any command at or past that (`0x400097fc`), and
sends every ATA command through one routine (`0x400090e0`) that writes a 28-bit task file.
`tools/build_aupd_lba48.py` has the full reading.

## The fix

Four places in disk mode, and the checksum of the program in `aupd`'s `flsh` directory, 356 bytes
in all. No code is appended: the new code is written over two routines stock disk mode never
reaches (an unreferenced WRITE SECTORS and its sibling).

* **Capacity** (`0x400017d0`): when the drive says it is 48-bit, its size comes from words
  100/101, so a computer sees the whole card.
* **Commands** (`0x400090e0`): READ DMA, WRITE DMA and READ VERIFY that touch a sector at or past
  2^28 go out as their 48-bit twins (0x25, 0x35, 0x42), the task file written the ATA-6 way.
  Every other command, and every command wholly below 2^28, runs stock's instructions.

`aupd` goes from sha256 `b32e141d…` (Apple's 1.5) to `dc68f0fc5d9aae12…`.

**It does nothing on its own.** Disk mode does not run from the card: it runs from the iPod's
flash chip, which holds the copy of `aupd`'s programs the chip was last programmed with. Copying
this firmware onto a card changes the chip only if Apple's updater is made to run from that card,
which reprograms the chip from the card's `aupd`. `tools/flash_rom_update.py --payload rw` is the
tool that does that, and it says what can go wrong: Apple's updater has no checks, and a power cut
in the first part of the programming leaves an iPod that does not start.

## Tested

* **The bytes are the ones on the author's iPod.** This fix's `aupd` is byte for byte the `aupd`
  inside the firmware `tools/flash_rom_update.py` pins for its `rw` payload (`.ipsw` sha256
  `f6c0c52c…`). That firmware was rehearsed in the emulator against a dump of the iPod's chip
  (`tools/rom_rehearse.py`, 2026-10-13: the updater runs to its reset with 21 erases, the boot block
  untouched, the chip left as sha256 `50751455…`), then programmed into the author's 1st-generation
  iPod, and the chip read back afterwards is that image (2026-10-13).
* **On that iPod, disk mode past 128 GiB:** a full read-write session over FireWire on the 250 GB
  card (2026-10-13): 44 tracks deleted and 2 added, `fsck` clean afterwards, the new files' hashes
  right. Whether those writes landed past the 128 GiB line was not established.
* **One iPod.** The update has been run on one iPod, from one chip. `tools/flash_rom_update.py`
  refuses a firmware or a chip dump it has not rehearsed, on purpose.
