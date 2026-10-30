# Enlarge the name tables in big steps

## The problem

The same slow start as `name-lookup`, from a second cause in the same routine.

## The code it targets

`string_table_add` at `0x28033848` in `osos`. Its tables start small and are enlarged as names
arrive: 50 entries at a time, and 8 KB of text at a time. Every enlargement copies and clears the
whole table, so the copying grows with the square of the library.

## The fix

Six instructions change their constants: the entry tables grow by 1,024 entries instead of 50,
and the text space grows by a step that doubles, up to 256 KB, instead of a fixed 8 KB. Nothing
is appended. [Wiki page 26](../../../docs/wiki/26-how-many-tracks-and-how-long-to-start.md).

The cost is memory: each table can be up to 1,023 entries, or 256 KB of text, larger than it
needs to be, and a library has about seventeen of these tables (one for titles, ten for file
paths, one each for artists, albums, genres and so on).

It also shrinks the leak that `library-leak` removes, without fixing it: a table that grows in
bigger steps is copied, and its old copy lost, far less often. In the emulator (2026-10-10,
`tools/fw_dbedge.py`), stock with this fix alone loads 7,600 made-up tracks with 7.0 MB in use
and 12,000 with 11.9 MB; stock alone stops listing that kind of track at 7,526.

## Tested

* **Hardware, 2026-10-08, 8,412 tracks:** timed together with `library-leak` and `name-lookup` (33.5 s to about
  29 s); not timed alone.
* **Emulator, 2026-10-08:** with `name-lookup`, the library part of a 32,000-track start goes from about
  100 seconds of emulated time to about 37 (wiki page 26); not measured alone.
