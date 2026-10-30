# Remember what the article rule said about the last artist

## The problem

The iPod files "The Sines" under S, as "Sines, The". To do that, the start-up checks every
track's artist against every leading article of the iPod's language: 72 of them on an English
iPod, about 1,050 instructions a track, a tenth of reading the library
(`tools/boot_profile.py`, 2026-10-10). Yet 96% of the time the artist is the one the previous
track had, and the answer is the same.

## The code it targets

`article_prefix_rewrite` at `0x2806fd00` in `osos`, run once for every track the library load
reads. Its walk over the (article, rewrite) pairs runs from `0x2806fd40` to the loop branch at
`0x2806fddc`; `0x2806fda0` is where it lands when an article fits. The pairs are filled in by
`article_tables_load` at `0x2803176c` when a language is loaded.

## The fix

Four instructions are diverted to `src/patches/artmemo.c` (820 bytes appended). It keeps one
answer: "article k is the first that fits" or "none fits". That answer depends only on the list
of articles and on the start of the name, as many characters as the longest article. So the
memory is keyed by exactly those characters, and by which article list was used, never by the
track or by a name's number, which can be handed out again.

* Before the walk: a name that starts like the remembered one gets the remembered answer, and
  the walk is skipped. Everything after the walk (building "Sines, The", filing it, the reference
  counts) is still Apple's code, entered where the walk would have left it.
* When the walk runs, its answer is remembered.
* When a language is loaded the articles may change, so the memory is forgotten.

If the longest article is more than eight characters, nothing is remembered and every track takes
the stock walk. The memory is read and written so that two tasks using it at once cannot see a
half-written answer.

## Tested

* **Emulator, 2026-10-14:** `tools/test_loadfix.py --mutants` boots with and without the fix on
  three disks and requires, call by call, the same artist and sort-artist given to every track,
  the same artist tables byte for byte, the same sorted lists, and every screen the same. Two of
  the disks are 2,000-track libraries (the memory skipped 1,997 walks and ran 3 on each); the
  third has 3,000 tracks with artists chosen to trip the rule: every English article in every
  capitalisation, names that only look like they start with one, names shorter than an article,
  leading blanks, no artist at all (2,132 walks skipped, 863 run). Three deliberately wrong
  builds (keyed on two characters only; "The" without its space; the article before the right
  one) were each caught on that disk.
* On the build the gate was written for (`tools/build_gen1_keysort_firmware.py`) it passes, 54 of
  54. On the whole 1.6 image it fails 9 of 54, **by design**: one check also requires at least
  four sorted lists, and the [artist-albums](artist-albums.md) fix leaves 1.6 with three. The
  archived gate summary of 2026-10-13 records that the lists equal the base's in all nine.
* **Hardware:** it is in the build timed with 32,000 tracks on 2026-10-11
  ([wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md) §28.1). That build was not timed
  without it, so its own effect on hardware has not been measured.
