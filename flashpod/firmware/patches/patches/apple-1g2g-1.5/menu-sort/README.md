# Open Artists, Albums and Songs without the pause

## The problem

With a few thousand songs on the iPod, choosing **Artists** pauses before the list appears. So
does choosing an artist, and so does **Songs**. The second visit to each is instant: the pause is
the firmware sorting every track the first time one of those screens is opened after a start.

## The code it targets

`list_sort` in `osos`. Its sort is a heapsort, whose loop begins at `0x2806a8e8`. A heapsort does
the same work whether the list arrives shuffled or already in order. Each comparison goes through
Apple's comparator (`0x2806a7f8`), which finds both tracks and looks up every field again, two
name-table lookups a name, before a character is compared.

## The fix

One instruction, the first of the heapsort loop, is diverted to a merge sort
(`src/patches/fastsort_keys.c`, 4,320 bytes appended):

* before every merge it checks whether the two halves are already in order across their
  boundary, so a list that arrives in order costs one comparison per neighbouring pair;
* each track's fields are looked up once, by Apple's own getters, into a scratch record, and the
  comparison walks the records. Names are still compared by Apple's routine (`0x28034248`), so
  the order means what it meant before; two names that are the same table entry are equal
  without asking;
* a key list it does not know, an item that is not a track, or memory it cannot get, and the
  list is sorted as before: by Apple's comparator, or by the stock loop when there is no memory
  at all.

**This replaces the first version.** The sort flashed on 2026-10-08, and described here until
now, was `src/patches/fastsort.c`, 1,312 bytes at `0x2836e834`, which called Apple's comparator
for every pair. `tools/build_fastsort.py` still builds it byte for byte; `OPENPOD_FASTSORT=keys`
builds this one. [Wiki page 24](../../../docs/wiki/24-menu-lag-the-sort.md) §24.11.

One visible difference, from the first version on: the order of **Songs** is not always the one
stock gives. Apple's comparison is not consistent (it can say A before B, B before C and C before
A), so two correct sorts can disagree. See wiki page 24 §24.7.

## Tested

* **Hardware, 2026-10-10, 15,488 tracks, database re-saved in the firmware's order, stopwatch,
  one run each** (`build/iPod_1.1.5-1.6+lba48+keysort.ipsw`, from
  `tools/build_gen1_keysort_firmware.py`): first open of Artists / an artist / Songs 3.04 / 2.02
  / 1.30 s with the first version, 1.57 / 1.2 / about 1.2 s with this one. The Artists list was
  scrolled and was in order. Another artist took 1.86 s on the same firmware, so that screen
  depends on the artist (wiki 24 §24.11).
* **Hardware, 2026-10-08, 6,137 tracks (first version):** about 6 / 4 / 4 s with stock, about
  2 s each with the merge sort (hand timed, wiki 24 §24.6).
* **Emulator, stock 1.5 plus this part alone, 2026-10-14:**
  `OPENPOD_FASTSORT=keys tools/test_fastsort.py`, 18 checks, 0 failed: on two 2,000-track
  libraries every sort leaves every track where the stock sort leaves it.
  `OPENPOD_FASTSORT=keys tools/test_fastsort_compare.py --pairs 20000 --mutants`: 275,976 pairs
  against Apple's comparator, 0 disagreements, and all five deliberately wrong builds caught.
* **Emulator, the whole 1.6 image: these gates cannot test it unchanged, by design.** They walk
  to an artist and expect the sort into album order ("order 6"). In 1.6 the
  [artist-albums](artist-albums.md) fix reads an artist from the Artists list instead, so order
  6 is never built on that walk. On 2026-10-14 `test_fastsort_compare.py` stopped on the missing
  order 6, as both gates did on 2026-10-13; `test_fastsort.py` was not run that day (the run
  lacked `OPENPOD_FASTSORT=keys` and was refused). On 1.6 without artist-albums (2026-10-13) the
  comparator gate passed and `test_fastsort.py` failed 6 of 18, all checks that compare track
  positions across two images; the gate summary *infers* a different heap layout caused by other
  fixes, which is not proven.
* Not covered by any test: the records allocated and the merge's pointer array then refused.
