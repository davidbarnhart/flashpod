# Do not ask twice about the same artist while building a list

## The problem

Opening an artist or an album builds its list by walking every track in the library and asking,
of each one, whether it belongs: fetch the track, fetch its artist's name, compare it with the
name chosen. In a sorted library one artist's tracks sit together, so almost every question is
the one just asked about the track before. At 32,000 tracks that is 9.4 million instructions for
every artist and every album opened (`tools/test_artrun.py`, emulator, 2026-10-11).

## The code it targets

The walks that build a list and ask the filter `fn_280355b8` about each track: `fn_28035784`
(the menus of albums, artists, genres and composers, call at `0x28035890`), `fn_28035a64` (a
track list, `0x28035ae8`) and `fn_280361d0` (the play queue, `0x28036214`). Each calls
`fn_2806aedc`, "item N of this order", about 68 instructions, and the filter then fetches and
compares the track's artist name, about 200 more. Inside the filter, `0x2803567c` is where the
artist name differed and `0x28035724` where the album name was tested.

## The fix

Five instructions are diverted to 372 bytes of appended code. For the length of one walk it
remembers the name-table number of the last artist the filter refused, and of the last album. A
track whose artist or album is that same table entry is refused without its name being fetched
or compared, and the item is marked exactly as the filter would have marked it. Nothing is
remembered about names that pass, and the walk still visits every track in the same order. Both
are forgotten when a walk starts.

Separately, "item N" is read straight out of the order's list when the list is there; anything
unusual (no list yet, N past the end, an order the firmware maps elsewhere) goes to Apple's
routine. The play queue uses only this half: nothing in it is skipped.

Why the answer cannot change: the filter's artist test reads the track's artist number and looks
that number up in one name table, so the name compared depends on the number alone, and the
chosen name does not change during a walk. A refusal by either test is a refusal by the filter.

## Tested

* **Hardware, 2026-10-11, 32,000 tracks, stopwatch**, added on top of
  [artist-albums](artist-albums.md) ([wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md)
  §28.4): the first artist opened went from 1.05 s to 0.73 s, another artist from 1.06 s to
  0.72 s, Artists itself from 3.24 s to 3.11 s.
* **Emulator, the whole 1.6 image, 2026-10-14:**
  `tools/test_artrun.py build/sort-2000.img --spread 8 --albums 2 --mutants` walks artists,
  albums, play queues, Albums, Songs, Composers and Genres with and without this fix and
  [artist-menu](artist-menu.md), and requires nothing to differ: the same screens, every list
  the same row for row, and the filter's marks left in the items the same. On the ordered
  2,000-track library (118 screens, 5,240 rows), the shuffled one, and a 217-artist library built
  to be awkward (`build/artorder-awkward.img`, 120 screens, 4,594 rows) all of that held, and all
  five deliberately wrong builds of this fix were caught on both libraries they were run on.
  The gate as a whole reports FAIL, because two of the deliberately wrong builds of
  [artist-menu](artist-menu.md) were not caught; that file says why.
* A control run on the older stack the gate was written for (without the rest of 1.6) failed its
  equality checks on 2026-10-13 and again on 2026-10-14, at a play queue. The gate summary of
  2026-10-13 calls that control inconclusive; why it fails has not been worked out.
