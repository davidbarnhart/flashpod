# Build the Artists menu without comparing a name with itself

## The problem

**Music > Artists** still took over two seconds on a large library even when its sorted list was
already built. Building the menu walks every track in artist order and starts a new row whenever
a track's artist differs from the row above, by fetching the name and comparing it. Nearly every
track has the same artist as the one before, and Apple's comparison is at its slowest on equal
names: nothing differs, so it goes on to the full, weighted comparison. On a 32,000-track library
the Artists menu spent 25 of its 34 million instructions outside the sort there
(`tools/test_artrun.py --profile`, emulator, 2026-10-11).

## The code it targets

`fn_28035784` in `osos`, which builds the menus of artists, albums, genres and composers. Its walk
starts at `0x28035878`; `0x280358b0` is in the loop for each track, before the track's name is
fetched and compared with the row's (`fn_2806e340`); `0x280358f8` is the branch taken when the
names compared equal.

## The fix

Three instructions are diverted to 164 bytes of appended code. For the length of one walk it
remembers the name-table number of the name that last compared equal to the current row. A track
with that same number is the same row: Apple's code would fetch the same name, compare it with the
same copy and get the same answer, so neither is done and the walk moves to the next track.

The number used is the one the menu's rows are made of: the album's for albums, the artist's for
artists (and the sort-artist's as well, because the firmware's artist-name getter reads that
one when it is set), the genre's, the composer's. The number is forgotten at every track that does not match and
at the start of every walk, so it never outlives the row it was equal to.

## Tested

* **Hardware, 2026-10-11, 32,000 tracks, stopwatch**, added on top of
  [artist-albums](artist-albums.md) and [list-filter](list-filter.md)
  ([wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md) §28.4): Artists the first time went
  from 3.11 s to 1.73 s; the first artist opened from 0.73 s to 0.67 s, another from 0.72 s to
  0.56 s. Artists opened again took 0.71 s, against 2.32 s on the build without any of the three.
* **Emulator, the whole 1.6 image, 2026-10-14:**
  `tools/test_artrun.py build/sort-2000.img --spread 8 --albums 2 --mutants` walks the menus with
  and without this fix and [list-filter](list-filter.md) and requires nothing to differ. On the
  ordered and shuffled 2,000-track libraries and on a 217-artist library built to be awkward,
  every screen and every list was the same, row for row.
* **Two of the four deliberately wrong builds were not caught, on either library they were run
  on, so the gate reports FAIL.** Mutants 2 (an albums menu keyed on the artist) and 3 (never a
  new row after a comparison) were caught. Mutant 4 keys a composers menu on nothing; the gate
  summary of 2026-10-13 *infers* it escapes because the test libraries have no composers, which
  has not been checked. Mutant 1 never forgets at the start of a walk; the sources do not explain
  why it escapes. Until both are explained, the claim that this gate would catch those mistakes
  is not established.
