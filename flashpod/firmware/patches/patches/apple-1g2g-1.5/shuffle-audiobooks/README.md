# Keep audiobooks out of Shuffle Songs without looking every track up

## The problem

Choosing Shuffle Songs on a large library is a long wait before anything plays: 15.3 s at
31,958 tracks in the emulator, 17.1 s on hardware (2026-10-13). About 9.5 s of it is a check
that keeps audiobooks out of the shuffle, made by looking up every track's file one at a time.

## The code it targets

`fn_280361d0` in `osos` builds the playback queue. While the byte `[player+0x898]` is 1 — the UI
script sets it around Shuffle Songs and one other "play everything" action (read from the
decompiled script, not run) — it calls `resolve` (`0x2806eb14`) at `0x28036238` for every
track, which writes the file's path into the track's record, and then asks `0x28074c4c` (via
`0x28074bcc`) whether that path ends in `.aa` or `.m4b`, either case: an Audible book. If so the
track is left out.

## The fix

The path is already in memory: it is in the record's path table, two bytes a character, with
`:` where the resolved path has a backslash. The four instructions at `0x28036238` are rewritten
as a call to 140 bytes of appended code, which reads that string (`string_table_get_u16`,
`0x28033f20`, into 512 bytes of stack) and makes the same `.aa` / `.m4b` test, followed by the same
"leave the track out" on its answer. `resolve` is not called in this loop at all.

Two differences from Apple's code, both deliberate:

* A track whose lookup fails is kept by Apple's code whatever its name; here its name decides.
  What makes a lookup fail was not established.
* The 40 bytes of file information are no longer written into every record here. Whatever plays
  a track looks it up itself, as it does when playback starts from a menu, where this test is off.

**This fix depends on [track-record](track-record.md).** That fix moves the two record fields this
one reads (the path table and the path's id). In 1.6 this fix is built for the moved layout
(`OPENPOD_RECSHRINK=1`, set by `tools/build_gen1_16_firmware.py`), so the bytes below are only
right in a firmware that also has track-record. The patcher block says so
(`"requires": ["track-record"]`), and `tools/fw_fix.py` refuses a choice of fixes that has this
one without track-record.

The alternative was to drop the check altogether (`patches/shuffle_skip_audiobook_check.json`):
8.4 s instead of 9.1 s at 31,958 tracks, with audiobooks shuffled in with the music. The owner
chose this fix over it on 2026-10-13; that patch is kept, marked not used.

## Tested

All in the emulator, 2026-10-13, built for the stock record layout. Not on hardware.

* **4,000 tracks, 14 with audiobook names and 6 near misses:** Now Playing counts 3,986 with this
  fix, so all 14 are left out and the near misses kept. Apple's loop counts 3,995: it drops the
  five `.m4b` and keeps the nine `.aa`, because its lookup fails for an `.aa` file that holds MP3
  data — the first difference above. A real Audible file was not available. The three mutants
  (`OPENPOD_SHUFBOOKS_MUTANT=1..3`) give 3,995, 4,000 and 3,991.
* **31,958 tracks:** the wait before Now Playing 15.3 s → 9.1 s; Next twice gives "2 of" and
  "3 of" with sound; Artists lists 1,208 afterwards; nothing refused.
* **For all 4,000 tracks** the string this fix reads is the path the lookup writes, with `:` for
  the backslash and a leading `:`.
* **Not re-runnable from the repository:** the disks (`build/shuffle-analysis`) are built from a
  private library and are not committed. There is no committed gate for this fix.
* **On the moved layout** it has been run only as part of the whole of 1.6 (emulator, 2026-10-13,
  31,958 tracks): Shuffle Songs "1 of 31958" after 9.1 s, Next twice with sound — see
  [track-record](track-record.md).
