# Use disks larger than 128 GiB

## The problem

The 1st-generation iPod addresses its disk with 28-bit sector numbers: 2^28 sectors of 512
bytes, 128 GiB. Given a larger card formatted to its full size, stock firmware does not mount
the volume at all: it shows no music. In the emulator it sends the drive 7 commands during the
start and lists 0 tracks.

## The code it targets

In `osos`, the ATA driver builds each read or write command in one of four routines
(`0x280ce324`, `0x280ce4c8`, `0x280ce5c0`, `0x280ce728`); each packs the sector number into the
28-bit form and drops bits 31..28. The command is sent later, by the issue routine at
`0x280d1814`. The disk's size is read from the drive's IDENTIFY data in two places
(`0x280cece8..0x280ced1c` and `0x280b4384`), both from the 28-bit words 60/61; the partition
scan at `0x2803a7c4` skips a partition that is not smaller than the first of those, which is why
the volume is never mounted. [Wiki page 17](../../../docs/wiki/17-lba48-in-1.1.5.md) is the
map.

## The fix

Seven instructions are diverted to 516 bytes appended at `0x283731cc`.

* **The four command builders:** for a command that touches any sector at or past 2^28 (one that
  starts below and runs across included), the 28-bit address bits are dropped and the high bytes
  are kept aside. The builder otherwise runs whole, so its own choice between DMA and PIO stands.
* **The issue routine** (`0x280d1820`): if a command was kept aside, its opcode becomes the
  48-bit ("EXT") twin of whichever the builder chose, and the high bytes are written to the drive
  before the stock code writes the low ones and starts the command.
* **The two capacities** (`0x280ced18`, `0x280b4394`): if the drive says it supports 48-bit
  addressing (IDENTIFY word 83, bit 10), its size is taken from words 100/101. That is 32 bits of
  sectors: 2 TiB.

A command wholly below 2^28 is built and sent as before. The patch's one word of state is addressed at the fast
low alias of memory, as the FLAC decoder's are. An earlier patch (2026-07) is withdrawn: it
never mounted a large volume and would have crashed on its first command past the line; wiki
§17.8 says why.

**Not covered: FireWire disk mode.** It runs from the iPod's flash chip, not from the firmware on
the card, and with Apple's chip it reported the 28-bit maximum (268,435,455 sectors) for a
256 GB card. A separate patch for the chip exists (wiki page 29, commits `5aaf0f8`, `2727a74`);
it is not in this folder. Do not use disk mode on a card over 128 GiB with Apple's chip.

## Tested

* **Emulator, 2026-10-14, on the whole 1.6 image** (`build/gate16/lba48.log`):
  `tools/test_lba48_1_1_5.py`, a 160 GiB disk: 12 of 13 checks pass. Stock does not mount the
  volume; the patched image does (3 tracks), plays the song that runs across the line and is the
  right song (440 Hz), with 28 EXT commands and no error; with every file past the line, the
  library is read from there and the firmware's own writes land there (74 EXT write commands).
  Check 5 shows each piece is needed: without the driver's capacity the volume is not
  mounted, and with the capacities alone the song past the line cannot be played.
* **Check 6 fails, as expected on this image.** It asks that on an ordinary small disk the
  patched firmware send exactly the commands stock sends, in order. It was written for stock
  against stock with this one fix, and on the 1.6 image the other fixes change the disk traffic
  by themselves (in the 2026-10-13 diagnosis: stock 1,046 commands, 1.6 without this fix 880).
  Against 1.6 without this fix, 1.6 with it reads the same 1,556 sectors and writes the same, but
  re-reads one sector more often (17 times against 10, in 11 more commands). Why is inferred, not proven: the extra
  instructions per command move the timing of a loop that re-reads it
  (`build/gate16/SUMMARY.md` in `run-2026-10-13-before-recshrink/`). The same check on stock
  against stock with this fix gave different answers on two builds of the small disk (wiki
  §17.8, 2026-10-08).
* **Hardware, 2026-10-08:** one iPod, a 250 GB card formatted to its full size, a song moved so
  that it lies across the 128 GiB line: it played through one SD-to-CF adapter. Through another,
  it was listed and skipped; that the adapter drops the high address bits is inferred (wiki
  §17.9).
* **Hardware, 2026-10-11/12:** a 256 GB card filled to about 231 GiB plays tracks from end to end
  (stopwatch and ear, one iPod; [wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md)).
* **Not known:** whether the iPod used DMA or PIO for the commands past the line (the emulator
  uses PIO); and, as of wiki §17.9, writes past the line by the iPod itself on hardware.
