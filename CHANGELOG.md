# Changelog

## 0.5.2 (2026-10-20)

- `.aac` files can be added. Adding one failed with "required argument is
  not an integer" (and took the rest of the batch with it).
- WAV and AIFF files keep their tags. Their title, artist, album, genre,
  composer, track number and year were ignored: the title was the file name
  and the rest empty. Files added with earlier versions keep the old entries
  until a `flashpod rebuild`, which re-reads every file's tags.
- `flashpod rebuild` skips a file on the card it can't read and names it,
  as `add` does, instead of stopping with an error.

## 0.5.1 (2026-10-20)

- FLAC tracks keep their type through later changes. Reading the iTunesDB
  ignored each track's type marker and time added, so every save after a
  load (any `add`, `rm` or `rebuild`) rewrote the FLAC tracks already on
  the card as MP3 ones and dated every track "now". Both are now read back.
  A card already changed by 0.5.0 or earlier still has MP3 markers on its
  older FLAC tracks; `flashpod rebuild` re-derives each track's type from
  its file and restores them (it also renumbers the tracks).

## 0.5.0 (2026-10-17)

### flashpod 1.6 firmware for the 1st and 2nd generation

The firmware catalog has its first firmware that isn't Apple's: **flashpod
1.6**, Apple's last release (1.5) with 23 fixes, built from the
[openpod](https://github.com/davidbarnhart/openpod) project's fix folder.
`flashpod flash` offers it above 1.5, marked `(flashpod)` and NOT AN APPLE
RELEASE, and Apple's 1.5 stays the default. Over 1.5 it gives:

- FLAC playback, with seeking that lands at once and no dropouts;
- cards over 128 GiB (flash with `--lba48`);
- big libraries: 1.5 shows zero songs past a ~3.8 MB iTunesDB (about 5,300
  tracks), and 1.6 has run with 31,958 songs. `flashpod add` sees the fix on
  the card (it reads a few sectors of firmware once a batch would pass 1.5's
  limit) and lets the library grow to the 32,000-track playlist cap;
- Artists, an artist and Songs open in about 2 s instead of 4-6, and start-up
  is faster;
- characters the font lacks show as `?` instead of vanishing;
- FireWire disk mode past 128 GiB, once the iPod's flash chip holds 1.6's
  disk-mode program: flash with `--arm` (below), or use openpod's tooling.

It was tested on a real 1st-generation iPod with a 250 GB card of 31,958 songs
and a 128 GB FLAC card. Known limit, as on 1.5: Shuffle Songs doesn't start on
a library near 32,000 songs. It hasn't been tested on a 2G. See the README.

### `flash --arm`: arm the card for a disk-mode update

Optionally, a flash can leave the card's disk-mode update pending, so that on
its next start on FireWire power the iPod reprograms its own flash chip from
the card. An interactive flash asks, then warns and asks again (each
defaulting to no); `--arm` arms without asking
and `--no-arm` never arms. Only flashpod 1.6's and Apple 1.5's disk-mode
programs can be armed. A power cut early in the programming leaves an iPod
that needs an external chip programmer; the README explains the risk.

### flashpod as a library

`flashpod.card` is a Python API for iPod card image files, for programs such
as the emulator's web app: create a blank card, inspect it (layout; which
firmware, by name and version, with any openpod fixes; songs, artists and
albums), flash, swap only the firmware, format, init, add and remove songs,
rebuild, and fetch catalog firmware. It never prints, prompts, exits or asks
for root, and it locks each image while it works. See "Using flashpod as a
library" in the README.

The command line now runs this same code for every card operation (add, rm,
init, rebuild, and the flash sequence), so the logic lives in one place. A
test pins it there, and a second test runs 91 commands in both the old and
the new code and checks that the output and the resulting card are identical,
apart from the fixes below.

### `flashpod burn`

`flashpod burn IMAGE [DEVICE]` writes a card image to a real SD or CF card
(Linux). Before writing, it refuses non-removable, system, FireWire,
empty-slot and too-small targets. Afterwards it checks the card by reading it
back.

### Fixes

- A damaged iTunesDB was read as an empty library, so the next `add` wrote a
  database of only the new songs: every song already on the card dropped out
  and its file was orphaned. A damaged database is now refused, and a rebuild
  offered.
- `add` on a card with no iTunesDB exited without a word; it now says so.
- Flashing a card too small for a data partition crashed with a traceback;
  it's now refused in words.
- After `rebuild`, re-adding an untagged song copied it to the card again.
- An accepted rebuild during `add` was saved only if a song was then added.
- The flash summary said "@ 0 KiB" for 512-byte clusters.
- `flash --no-format` said it would ERASE the card and format it, though it
  keeps the data partition and its music. It now says what it does, and it
  refuses a card whose data partition isn't where the new partition table
  points (before, it flashed such a card and said "Done").

### Firmware names

The first three 1G releases are named as Apple named them, 1.0, 1.0.2 and
1.0.4, instead of 0.0, 0.2 and 0.4. They're still listed oldest to newest.
