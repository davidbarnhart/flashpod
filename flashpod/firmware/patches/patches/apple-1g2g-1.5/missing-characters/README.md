# Draw a '?' for a character the font does not have

## The problem

A title in a script the iPod's font does not have is drawn as nothing: the characters the font
lacks simply vanish, and a title made only of them is an empty line in the list.

## The code it targets

Every character drawn or measured goes through the glyph lookup `0x2805394c` in `osos` (two
callers: the draw at `0x280534b0` and the character width at `0x28052de4`). It tries a list of
fallback fonts for characters from U+3000 up, one special font for U+2500..U+25CA, and last of
all the string's own font, with the call at `0x28053ab4` to `0x28052ca4`. When that finds no glyph
it returns 0; the draw then returns before copying anything or moving the pen, and the width is
0. (Read from the disassembly by the decompilation session, 2026-10-13, and seen in the emulator.)

## The fix

That last call goes to 100 bytes of appended code: the same lookup, and if it finds nothing, the
lookup of `?` in the same font. Drawing and measuring use the one routine, so a row's width
still matches what is drawn.

Characters that are meant to be invisible are left as they were — nothing: control characters
(below U+0020, and U+007F..U+009F), the block U+2000..U+206F when the font lacks one (spaces,
zero-width and direction marks), the variation selectors U+FE00..U+FE0F and the byte-order mark
U+FEFF.

A companion fix, [missing-characters-sort](missing-characters-sort.md), sorts the same characters
after every letter.

## Tested

* **Emulator, 2026-10-13:** two titles on the Songs list, one of a snowman (U+2603), a Cyrillic
  letter (U+0416), a Chinese character (U+4E2D) and é (U+00E9); the other of a tab, U+FEFF, U+200B
  and `|`. A hook at `0x28053ac0` counts lookups that end with no glyph, and the screen is
  rendered.
  * without the fix: no glyph for U+2603, U+0416, the tab, U+FEFF and U+200B; the first title is
    drawn with its first two characters missing;
  * with it: no glyph only for the tab, U+FEFF and U+200B; the first title is drawn `??` and then
    the two characters that have glyphs;
  * `OPENPOD_NOGLYPH_MUTANT=2` (substitute for controls too): no misses at all, the tab drawn as
    `?`.

  U+4E2D and U+00E9 have glyphs in stock 1.5 and are untouched.
* **Not re-runnable from the repository:** the disk (`build/shuffle-analysis/mkglyph.py` +
  `job_glyph.py`) is built from a private library and is not committed. There is no committed gate
  for this fix.
* **Not looked at:** Now Playing, the scrolling title, a cut-off row that ends in a `?`.
* **Not on hardware.**
