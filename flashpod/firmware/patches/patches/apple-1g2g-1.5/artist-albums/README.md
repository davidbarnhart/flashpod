# Read an artist's albums out of the Artists list

## The problem

Opening one artist from **Music > Artists** for the first time pauses, even when the Artists list
itself has just appeared. The firmware sorts the whole library again, into album order, and only
then keeps the tracks of the artist chosen. But the Artists list it has just shown is already
sorted by artist, then album, then disc and track number, so inside one artist the tracks are
already together, album by album.

## The code it targets

Two places in `osos` choose which sorted list to walk:

* `0x28035828` in `fn_28035784`, which builds an artist's menu of albums (`mov fp, #6`: order 6,
  album order: album, disc and track number, artist, title);
* `0x28035a0c` in `fn_28035a0c`, which the track list (`fn_28035a64`) and the play queue
  (`fn_280362e0`) ask for their order (mode 3 gives order 6).

The Artists list is order `0x18`: artist, album, disc and track number, title.

## The fix

Both instructions are diverted to 64 bytes of appended code. Each now gives order `0x18` when an
artist has been chosen (the length of the chosen name, at `[browser + 0x260]`, is not 0) and order
6 as before when none has: **Music > Albums** and **Artists > All** still need album order over
every artist. The firmware is simply asked for a list it already has; the filter and everything
after it are Apple's, and every list holds the same tracks. Order 6 is now built only when one of
those two screens asks for it, so the sort moves there rather than disappearing.

**A case that is worse than stock.** One artist tagged two ways that the artist filter treats as
the same name but the article rule does not ("The X" beside "tHe X", " The X" with a blank in
front, or "Thé X"): the two spellings sit in different places in the Artists list, so the
artist's albums menu lists a shared album twice, and All has one spelling's tracks after the
other's. 3 of 24 kinds of double spelling tried do this.

**Where the two can differ otherwise.** Apple's comparison can go in circles, and the stock
album order then lists one of an artist's albums twice or starts an album at the wrong track (9
artists of 1,283 in a 32,000-track library, 1 of 40 opened in an unsorted 15,488-track one,
emulator, 2026-10-11). For those artists the Artists list is in album order and this fix shows
that. On a library built to make the comparison go in circles both list albums twice,
differently, and this fix is the worse more often than not. `tools/build_artorder.py` gives the
detail.

## Tested

* **Hardware, 2026-10-11, 32,000 tracks, stopwatch**
  ([wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md) §28.4): the first artist opened
  went from 2.19 s to 1.05 s; another artist 1.06 s both ways; Artists itself 3.2 s to 3.24 s.
  A track played from an artist's album ran on into the next one.
* **Emulator, the whole 1.6 image, 2026-10-14:**
  `tools/test_artorder.py build/sort-2000.img --spread 12 --albums 2 --mutants`, 15 checks, 0
  failed, and on the shuffled library 10 of 10. It walks 12 of 82 artists, their albums and play
  queues, Artists > All, Albums and Songs, with and without the fix, and requires the same
  screens, the same rows in every list, and every play queue to be its track list. Every list
  with an artist chosen was the same row for row (70 lists). All four deliberately wrong builds
  were caught. On the shuffled library the first artist opened in 163 ms against 867 ms, and
  Artists > All in 898 ms against 153 ms: that is the album-order sort, moved.
  The disks are the synthetic 2,000-track libraries of `tools/test_fastsort.py`; the
  32,000-track library is private.
