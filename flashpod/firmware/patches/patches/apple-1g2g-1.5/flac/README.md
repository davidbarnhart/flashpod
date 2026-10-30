# Play FLAC files

## The problem

Apple's firmware plays MP3, AAC, WAV, AIFF and Audible files. A FLAC file on the disk is not
music to it: it is never listed and cannot be played.

The first FLAC decoder added here played, but on hardware fast-forward and rewind decoded
everything between the old position and the new one, so a seek took a long time to land. The
first faster seek had a fault of its own: near the end of a track a fast-forward could take 12
seconds. And a FLAC that followed another track gave 54 ms of sound, then 92 ms of nothing,
then played.

## The code it targets

The firmware chooses a decoder in four steps, each a table or a branch in `osos`: a file
extension becomes a codec id, the id becomes a three-letter tag, the tag selects a routine that
binds a decoder, and the decoder is an object with eleven entry points. Only codec id 8 is free.
The eleven edits below (240 bytes) are those tables and the one branch that calls the binder.
There is no FLAC decoder anywhere in Apple's image, so one is added.

The decoder in this file is the second one. Of the eleven edits, one word differs from the
first decoder's: a call at `0x281dac84` into the appended code, whose target moved when the
decoder was rebuilt.

## The fix

A FLAC decoder (dr_flac, compiled for ARMv4T by `tools/build_flac_codec.py`) is appended above
Apple's image at `0x28315aa8`, in `flac.hex`. `tools/build_flac_firmware.py` registers it: the
`.flac` extension, id 8, the tag, and a binder `flac_bind` that hands the firmware an object
shaped like Apple's own decoders. The decoder's pointers use the low address of memory
(`0x00…`), not the `0x28…` address it is loaded at: on hardware the `0x28…` address is about
three times slower, and a decoder linked there stutters.

It is built with two switches on (`OPENPOD_FLAC_SEEK=1`, `OPENPOD_FLAC_START=1`). The code is
360,604 bytes, padded with zeros to the 363,916 of the first decoder so that nothing after it
moves.

* **Seeking.** The decoder looks for a FLAC frame header near the target and starts there, and
  sets the elapsed time itself through the firmware's own routine. The 12-second fast-forward
  was a race between the two processors over one byte: the firmware's loader, on the main
  processor, ends a stream whenever its status byte is not zero, and the decoder cleared that
  byte only after its seek had returned. It is now cleared before the seek as well. The old
  workaround (seek to the start of the file and try again) stays as a safety net; the gate
  checks that it is never needed.
* **Starting.** The firmware loads the file in a burst of drive reads on the same thread that
  accepts decoded sound, and that burst fell just after the sound had started. The decoder now
  asks the stream to hold 63 of its 64 blocks before it hands over its first frame, so the burst
  comes first, and opening a file reads 16 KB with 2 seeks where it read 1 MB with 6. It also
  plays FLAC files smaller than 256 KB, which the first decoder opened and never decoded.

The first decoder is [wiki page 25](../../../docs/wiki/25-flac-the-whole-fix-and-porting-it.md);
its bytes are explained in `docs/FLAC-PATCH.md`. Neither covers the seek or the start fix yet;
those are in commits `43ca41a` and `f50c032` and in `src/decoders/flac_codec.c`.

## Tested

* **Hardware, 2026-10-08 (first decoder):** FLAC files with 4,608-sample blocks, as they come
  off a CD rip, were played on one 1st-generation iPod and judged by ear ("sounds perfect").
* **Hardware, 2026-10-13 (this decoder, with `flac-refill`), by recording, one recording each:**
  FLAC to FLAC with 1,152-sample blocks, a gap of 672 ms and no dropout (the day before: 784 ms,
  then 54 ms of sound, then a 92 ms dropout). With 4,608-sample blocks, music chosen from the
  menu and noise both by automatic transition and from the menu: clean.
  The faster seek, before its status-byte fix, was confirmed by ear on the iPod (commit
  `43ca41a`); both fixes in this decoder were on the iPod on 2026-10-13 (commit `f50c032`).
* **Open (hardware, 2026-10-13):** once, on its first session, the iPod froze 26 s into a 31 s
  FLAC and then restarted. As recorded in `f50c032`, it had not happened again, on hardware or
  in 24 emulator runs of that stretch. Three things were new on hardware at once (the seek fix, the start fix and
  `flac-refill`); which of them, if any, caused it is not known.
* **Emulator, 2026-10-14, on the whole 1.6 image** (`build/gate16/`):
  * `tools/test_flac_seek.py --dr-flac DIR --static`, `--unit`, `--emu --with-refill`: 9, 131
    and 154 checks, none failed. Mutant 10 (the status byte left alone) is caught. On the run of
    2026-10-13, `--emu` failed 1 of 154 (one scrub landed at the very end of the track); it failed
    the same way on an image without the newest parts, and is thought to be the test script's
    timing, not the decoder (inferred). It passed on 2026-10-14.
  * `tools/test_flac_start.py MEDIA --dr-flac DIR --emu`: 439 checks, none failed, five mutants
    caught; track started to first sample on an automatic transition 0.448 s before, 0.163 s
    after. One allowed exception is reported: a decode call 7.8 ms late at the start of one
    4,608-block file, which the emulator cannot say is audible. On hardware the 4,608-block cases
    above were clean.
  * The seek gate does not play on through further tracks after a seek (as of `f50c032`); the
    session that froze did.
* `tools/test_flac_end_to_end.py` and `tools/test_flac_decode.py` test the first decoder and were
  not run against this image.
* Not tested: high-resolution files (above 16 bits or 48 kHz).
