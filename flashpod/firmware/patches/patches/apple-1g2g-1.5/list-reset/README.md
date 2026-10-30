# Clear only the sorted lists that exist when a track is added

## The problem

While the song list is read at start-up, the firmware does some work for every track that has
no effect: each time a track is added it asks to throw away every sorted list it may have kept
for the menus. During a start there are none, so it throws away nothing, 31 times a track.
`tools/boot_profile.py` (2026-10-10) put it at about 950 instructions a track.

## The code it targets

`fn_280699a4` in `osos`, called every time a track is added to the library. It calls
`fn_28069914` once for each of the 31 list orders. `fn_28069914` frees whatever is kept for one
order: four slots, at `[db + order*4 + 0x110 / 0x190 / 0x210 / 0x290]`, and for order 4 a fifth
at `[db + 0x310]`. When they are all empty it does nothing.

## The fix

The first instruction of `fn_280699a4` is diverted to 100 bytes of appended code that makes the
same 31 visits in the same order, reads the slots itself, and calls `fn_28069914` only for an
order with a slot that is not empty. `fn_28069914` is not changed and still does all the
freeing.

One difference, which no caller sees: the routine returns nothing its callers read. Stock leaves
in the return register whatever the last free returned; the fix leaves the last slot it looked
at.

## Tested

* **Emulator, 2026-10-14:** `tools/test_fastinval.py --mutants`. The stock routine and the
  diverted one run on the same made-up database block, 4,000 times, with the slots filled at
  random from all empty to all full, and the calls to the two routines that actually free are
  recorded. 135,691 frees by the stock routine; 0 blocks where the two differ in the calls, their
  arguments, their order or the memory left behind. Three deliberately wrong builds (one slot not
  read; the fifth slot checked for the wrong order; the last order skipped) were caught in
  1,645, 86 and 1,599 blocks.
* **Emulator, a whole start:** `tools/test_loadfix.py --mutants` boots with and without the fix
  and requires the article calls, the artist tables, the sorted lists and every screen to be the
  same. A start never has a list to free, so a boot cannot tell a wrong version from a right one;
  that is why the gate above exists. On the build the gate was written for
  (`tools/build_gen1_keysort_firmware.py`, 2026-10-14) it passes, 54 of 54. On the whole 1.6
  image it fails 9 of 54, **by design**: one check also requires that at least four sorted lists
  were built, and in 1.6 the [artist-albums](artist-albums.md) fix leaves out one of the sorts,
  so there are three. The archived gate summary of 2026-10-13 records that the lists themselves
  equal the base's in all nine.
* **Hardware:** it is in the build timed with 32,000 tracks on 2026-10-11
  ([wiki page 28](../../../docs/wiki/28-a-full-256gb-card.md) §28.1). That build was not timed
  without it, so its own effect on hardware has not been measured.
