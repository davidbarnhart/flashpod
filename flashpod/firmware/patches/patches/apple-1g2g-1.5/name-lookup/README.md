# Look up artist, album and genre names without searching the whole table

## The problem

Starting an iPod with a large library is slow, and gets slower faster than the library grows.
Most of that is one search.

## The code it targets

`string_table_add` at `0x28033848` in `osos`. For each track the firmware looks for its artist,
album and genre in a table of every distinct name seen so far, by walking the whole
table (`0x280338d4`..`0x28033940`). Tracks of one album arrive together, so the answer is nearly
always a name used a moment ago; and when the name is new, the whole walk is spent finding that
out.

## The fix

Three instructions of `string_table_add` are diverted to `src/patches/stfast3.c` (17,856 bytes
appended, most of it tables):

* each table remembers the entry it last found or added, and that one is tried first. A track's
  artist, album and genre are nearly always the last track's, so the usual lookup is one
  comparison;
* when that misses, the eight most recently used entries of any table are tried;
* every name a table holds is marked in a table of 131,072 bits. A name whose bit is clear
  cannot be in the table, so the walk is skipped and the firmware goes straight to adding it.

A remembered entry is never trusted: the name is compared with it before it is used. When no
shortcut applies the stock walk runs, so the worst case is stock's. The names are compared a
word at a time where both are aligned for it, a halfword or a byte at a time where they are not;
the code never makes an unaligned load, which the iPod's processor would not fault on but would
get wrong.

**This is the second version.** The first (`src/patches/stfast.c`, 17,232 bytes, eight shared
notes and a byte-at-a-time comparison) is what was timed on hardware on 2026-10-08.
`tools/build_stfast.py` still builds it byte for byte; `OPENPOD_STFAST=v3` builds this one.
[Wiki page 26](../../../docs/wiki/26-how-many-tracks-and-how-long-to-start.md),
[page 27](../../../docs/wiki/27-why-the-ipod-starts-slowly.md) §27.6.

## Tested

* **Hardware, first version, 2026-10-08, 8,412 tracks, hand timed:** with `library-leak` and
  `name-table-growth`, the start went from 33.5 s to about 29 s; adding the bit table took a later
  build from about 19.5 s to 18.7 s. The Artists list was scrolled end to end and showed no name
  twice.
* **Hardware, this version:** it is in the build timed with 32,000 tracks on 2026-10-11 (start
  37.48 s, [wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md) §28.1). That build was not
  timed without it, so hardware has not measured what this version adds over the first.
* **Emulator, the whole 1.6 image, 2026-10-14:** `tools/test_stfast3.py --mutants`, 22 checks, 0
  failed. It swaps the first version for this one inside the 1.6 stack, on two 2,000-track
  libraries and one of 1,200 tracks whose names often start with the one before: every call of
  `string_table_add` returns the same result and entry (12,001 calls on each 2,000-track
  library), every sorted list is the same, and every skipped walk was checked against the table.
  The lookup cost 55.3% fewer instructions on the ordered library and 14.7% fewer on the
  shuffled one. Four deliberately wrong builds were each caught.
  `tools/test_stfast_eq.py --mutants`: the hand-written comparison, 48,000 calls over every
  length 0..600 and pair of alignments, 0 wrong answers and 0 unaligned loads; both wrong builds
  caught.
* **Emulator, stock 1.5 plus this part, 2026-10-14:** `OPENPOD_STFAST=v3 tools/test_stfast.py`,
  24 checks, 0 failed (same answers as stock on all 12,001 calls; 7.4x fewer instructions in
  `string_table_add` on the ordered library, 1.1x on the shuffled one). This gate cannot run on
  the whole 1.6 image unchanged, by design: it checks the stock bytes of `name-table-growth`,
  which 1.6 already changes.
