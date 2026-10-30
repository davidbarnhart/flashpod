# Top up a long track in memory before it runs out

## The problem

Every few minutes, a long track went silent for about as long as the card takes to wake up.
It was heard with FLAC files, but it is not about FLAC: the emulator shows the same silence in
a WAV.

The iPod does not stream a track from the card. It reads as much of the file as fits into
memory and then puts the drive to sleep. Stock firmware fetches more only when the player has
used up everything it had: the drive is asleep, the thread that feeds the decoder has to wait
for it to wake, and the sound stops. About 40 ms of decoded sound is queued ahead of the
speaker, so the gap is the drive's wake time less that.

## The code it targets

In `osos`: `TrackCacheReadTask` (`0x280784d8`) loads the file in pieces of 262,128 bytes, as
many as memory allows (a "window": 25,164,288 bytes in the emulator with one song on the disk).
The stream's read, `fn_280b2c0c`, is called 4,096 bytes at a time; only when a read ends at or
past the last byte loaded does it mark the window (`stream + 0x152`) and fetch those bytes
itself. The player's loop (`fn_280782b8`, run twice a second) acts on the mark: it keeps the
pieces still ahead of the reader, frees the rest and has the task load new ones. That is a
correct sliding window; stock only starts it too late.

## The fix

Three instructions are retargeted to 84 bytes appended at `0x28379aac`, in two halves.

1. **The mark is set early.** At `0x280b2c88`, a read the cache can serve marks the window when
   it ends within 2 MiB of the last byte loaded and the file goes on beyond the window. Apple's
   own slide then does the rest while 2 MiB are still to play. It acts only if the window is
   8 MiB or larger, which keeps the reader clear of a path in the player's loop that misbehaves
   when the reader is still in the first piece; a smaller window keeps Apple's behaviour.
2. **The slide lets go of the stream before it waits for the drive.** At `0x28078464` and
   `0x28078474`, the player's loop releases the stream's two locks and then asks for the drive,
   instead of holding them through the wake; otherwise the reader, which takes one of those locks
   on every read, would stand still for the wake with 2 MiB in front of it.

This is the second version of the part (`refill2`, `tools/build_refill2.py`). The first
(`refill`, `tools/build_refill.py`) marked early while a window was still being loaded, which
is always true just after a seek. Each such mark made the player's loop take the locks and do
nothing, holding up the wheel: in the emulator (2026-10-12) a scrub through a FLAC became 7
seeks for 3 scrubs. `refill2` marks early only when the window is full, the one state in which
the slide acts. It replaces `refill` and cannot be combined with it.

## Tested

* **Emulator, 2026-10-14, on the whole 1.6 image** (`build/gate16/refill.log`):
  `tools/test_refill.py` with `OPENPOD_REFILL_PART=refill2`, a drive that takes 2 s to wake:
  47 checks, all pass. Without the fix a large FLAC had 1.99 s of silence and a large WAV
  2.01 s; with it, none, the window slid once with 2,097,024 bytes still loaded, and the sound is
  the file frame for frame. Mutants 1, 2, 3, 4, 5 and 7 are caught.
* **Emulator, 2026-10-14:** `tools/test_flacseek_refill.py`, scrubbing while the window
  refills: 61 checks, none failed; mutant 6 (the first version's test) is caught, with 7 seeks
  reaching the decoder for 3 scrubs.
* **Hardware:** the first version, `refill`, removed the silence, confirmed by ear (commit
  `43ca41a`). `refill2` has been on one iPod since 2026-10-13 in the image that also carries the
  new FLAC decoder; on its first session that iPod froze once, 26 s into a FLAC, and restarted.
  Which of the three new parts, if any, caused it is not known (commit `f50c032`, and
  [flac](flac.md)).
