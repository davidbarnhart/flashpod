# Do not clear all of free memory at every start

## The problem

About a second and a half of every start is spent writing zeros.

## The code it targets

`arena_init` at `0x28003d40` in `osos`, which sets up the pool that all memory is later handed
out from. Before it does, it writes zero to every word of the pool: 7,006,234 stores, 26.7 MB.

## The fix

One byte. The branch at `0x28003db8` that already skips the loop when there is nothing to clear
becomes unconditional. The two marker words at the ends of the pool are written before it and
still are. Nothing is appended. [Wiki page 27](../../../docs/wiki/27-why-the-ipod-starts-slowly.md) §27.5.

The risk is code that takes memory from the pool and assumes it is zero without clearing it.

## Tested

* **Hardware, 2026-10-08, 8,412 tracks, hand timed:** about 23 s to about 21.5 s. Since then it
  has been in every firmware on the iPod, through starts, menus and playing music.
* **Emulator:** with the pool filled beforehand with `0xDEADBEEF`, the start reaches the same
  screen as stock.
* Not tested: every path through the firmware. A routine that depends on zeroed memory and has
  not yet been run would not have shown itself.
