# Keep a track's file information only for tracks that have been played

## The problem

A loaded library keeps one record for every track, and most of each record is room for file
information that is filled in only when a track is about to be played. For every other track it
stays zeros for ever. On a large library that is megabytes of memory holding nothing, which the
player then cannot use for its track cache.

## The code it targets

The records come from a pool in `osos` created by `pool_create(0x1b8, 100)` at `0x2806ee8c`: each
record is asked for as 0x1b8 = 440 bytes, which the pool rounds to 448. 276 of those bytes,
record+0x94..+0x1a7, are one block of file information: a word, the file's path as bytes (room
for 260), a flag at block+0x108 and two words. For a library read from iTunesDB the block is
filled only when a track is "resolved" (`0x28031228`, on the way to playing it). After it come the
record's path table (+0x1a8) and the path's id in it (+0x1ac).

The layout was read from the code by the decompilation session, and every access at or beyond
+0x94 was then traced in the emulator through a load, Shuffle Songs, menus and play
(2026-10-13); the two lists agree.

## The fix

The 276-byte block becomes one pointer, at +0x94, to a buffer of 0x114 bytes from the heap that
exists only once the track has been resolved (or cloned into). The path table and id move down to
+0x98 and +0x9c, and the record is 0xa0 = 160 bytes. 36 instructions change and 1,200 bytes of
code are appended. By kind:

* the twelve instructions that use +0x1a8 / +0x1ac use +0x98 / +0x9c;
* where the block was handed on, a call hands on the buffer, or a block of zeros if there is none;
  where the callee writes it, the buffer is allocated first; the flag read gives 0 with no buffer;
* the five stores made when a track is resolved go into the buffer;
* freeing a record, and "clear the database", free the buffers first;
* the pool's size becomes 0xa0;
* cloning a record copies 0xa0 bytes and gives the clone its own buffer. Left alone, the clone
  would have written 0x118 bytes past the end of a 160-byte record and shared the source's buffer.
  This was found by reading the code, not by a run: no traced action clones.

Every added routine keeps the flags and every register it is not asked to set. One site
(`0x2806f12c`) is in a routine that leaves by a tail branch, so it becomes a branch to a stub
rather than a call.

[shuffle-audiobooks](shuffle-audiobooks.md) reads the two moved fields and is built for this
layout in 1.6.

Known limits, from the builder:

* **If the heap refuses the 276 bytes**, the record is pointed at one shared scratch block.
  Nothing is written through a null pointer, but that track's file information is then whatever
  was last written there, and a later reader of an earlier record on the scratch block sees
  whichever track was resolved last. Accepted for now: it needs a heap that is already all but gone.
* **A record cloned into another list** is not on the database's list, so "clear the database"
  does not free its buffer.

Added to 1.6 by the owner's decision on 2026-10-14.

## Tested

All in the emulator, 2026-10-13, one run each. **Not yet run on hardware.**

* **Memory, 31,958 tracks, the whole of 1.6 with and without this fix:** heap in use once the
  library is loaded 21,414,220 → 12,198,220 bytes. The difference, 9,216,000, is exactly
  (448 − 160) × 32,000: the bytes saved a record, times the pool's capacity in 100-record chunks.
  The track cache gets 46 blocks instead of 6; Shuffle Songs shows "1 of 31958" after 9.1 s, Next
  twice with sound, Artists lists 1,208, nothing refused. At 4,000 tracks: 3,109,204 → 1,957,204
  bytes, cache 90 blocks instead of 86.
* **Access trace** on the new layout (load, Shuffle, menus, Next, On-The-Go): beyond +0x93 only
  +0x94, +0x98 and +0x9c are touched, and only by the patched sites, the pool's zeroing and this
  fix's own code.
* **Leaks:** Shuffle Songs and three Nexts allocate 34 buffers; when the cable goes active the
  library is torn down and all 35 are freed. With "clear" made to free nothing
  (`OPENPOD_RECSHRINK_MUTANT=1`) 35 are left.
* **A missed site is loud:** with the tail-branch site left as it was (`MUTANT=2`) there is no
  sound at all, every track skipped.
* **Freeing a record and cloning one**, called directly (nothing in the UI reached either): the
  buffer is freed and the pointer cleared; a clone gets its own copy of the buffer, and freeing
  it leaves the source intact. With the clone left as it was (`MUTANT=5`) freeing the clone
  destroys the source's file information.
* **Every allocation refused** (4,000 tracks): the 45 records resolved by Shuffle Songs and two
  Nexts all point at the scratch block, and the screens and sound match the run with nothing
  refused. Only that one sequence was tried.
* **Rewind near the end of a 270 s MP3, 31,958 tracks, with shuffle-reserve (since withdrawn, see the README):**
  the larger cache holds the whole song, so the rewind reads no sectors (18,122 without this fix).
  Sound starts 0.88 s after Select either way, but the read-ahead takes 31.9 s to fill instead of
  6.8 s. Menu response during those seconds was not measured.
* **Not tested:** anything that calls the clone, five routines nothing calls, a long session.
  `MUTANT=3` (the size not changed) is caught only by the memory measurement.
* **Not re-runnable from the repository:** the measurements above. Their disks
  (`build/shuffle-analysis`, `build/jog-analysis`) are built from a private library and are not
  committed. On 2026-10-14 the 1.6 image with this fix was put through the earlier gates
  (`build/gate16/`), with no unexplained failure.
* **Re-runnable, synthetic (2026-10-15): `tools/test_recshrink.py --dr-flac DIR`.** It runs on
  `tools/fw_dblimit.py make --tracks 31958 --playable`, made-up tracks naming the emulator
  disk's tone files. It boots the release (osos `109ed777…`) and mutants 1, 4 and 5, then runs a
  leak, a free and a clone job in forks of each. The release passes all three, and each mutant is
  caught by its own job only. **All checks pass.** On this disk only two records get resolved
  (the private library's runs resolved 34), so the jobs work on those two.
