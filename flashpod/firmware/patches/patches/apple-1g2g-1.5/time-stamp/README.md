# Work out the time once a second, not once a track

## The problem

Part of the time spent reading the song list is spent on the calendar: about 0.8 seconds with
8,412 tracks.

## The code it targets

`fn_28074788` in `osos`, called once for every track read from the library, to stamp it with the
current time. It reads the clock as a calendar date and converts it to a day number, back to a
date, and to a day number again, dividing in software each time: about 1,600 instructions a
track.

## The fix

The routine's first instruction is diverted to 224 bytes of appended code. It reads the clock the
way the firmware does and, if the second is the one it saw last time, returns the answer from
last time. Otherwise the stock routine runs and its answer is kept.

One difference: the stock routine reads the clock itself, so if the second turns over between
the two readings, the answer kept for that second is one second ahead.
[Wiki page 27](../../../docs/wiki/27-why-the-ipod-starts-slowly.md) §27.6.

## Tested

* **Hardware, 2026-10-08, 8,412 tracks, hand timed:** about 20.3 s to about 19.5 s.
* **Emulator:** `tools/build_nowcache.py --test [disk]`, which needs a disk image with a library
  on it.
