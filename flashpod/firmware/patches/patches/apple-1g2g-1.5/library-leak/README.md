# Stop losing memory while the song list is read

## The problem

A 1st-generation iPod with too many songs starts normally and shows no music, with no error
message. Where the edge falls depends on the library; on one real iPod, 5,297 tracks worked and
5,298 did not.

## The code it targets

`handle_grow` at `0x28034d90` in `osos`, which enlarges a block of memory by making a bigger copy.
While the song list is read, the tables of text (titles, artists, file names) are enlarged a
little at a time. Each time, `handle_grow` frees the old block's 16-byte record and keeps the old
block itself. The waste grows with the square of the library.

## The fix

60 bytes of `handle_grow` are rewritten in place so that the old block is freed too. The room
comes from four instructions in it that do nothing (calls to lock routines that are empty), so
nothing is appended. [Wiki page 23](../../../docs/wiki/23-music-library-limit.md).

## Tested

* **Emulator:** `tools/test_library_leak_fix.py`. With the user's real library scaled up
  (2026-10-08): without the fix 9,000 tracks load and 10,000 do not; with it the limit is exactly
  32,000, which is a different limit and not a memory one.
* **Hardware:** it is in the firmware timed on 2026-10-08 with 8,412 tracks, which listed and
  played. That library is below where the unfixed firmware stops for it, so hardware has not yet
  shown the fix itself making the difference.
