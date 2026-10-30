# Sort a character the iPod cannot draw after every letter

## The problem

The iPod sorts names by a weight it gives each character, and for most characters outside
Western European ones the weight puts them before the digits or among the letters. So a title in
Cyrillic or Greek comes first in Songs. Almost none of those characters can be drawn on this iPod,
so the top of the list is titles drawn as gaps or, with
[missing-characters](missing-characters.md), as `?`.

## The code it targets

Names are compared by weight (`0x28034248` → `0x28007648` → `0x28020aec` in `osos`; [wiki page
24](../../../docs/wiki/24-menu-lag-the-sort.md) §24.10). One character's weight comes from
`0x28020a84`: below U+0100 from a table; from U+0100 up the character's own value, by four
instructions starting at `0x28020a8c`. Letters and digits weigh 0x6c5..0x944, so a character from
U+0945 up already sorts after Z, and one in U+0100..U+0944 sorts before the digits or among the
letters.

Of U+0100..U+0944 the iPod can draw only fifteen (emulator, 2026-10-13, every code point put
through the glyph lookup): U+0131, U+0152, U+0153, U+0178, U+0192, U+02C6, U+02C7,
U+02D8..U+02DD, U+03A9 and U+03C0.

## The fix

The first of the four instructions becomes a branch to 112 bytes of appended code. A character in
U+0100..U+0944 that is not one of the fifteen weighs its value + 0xF000, which is after every
letter and keeps such characters in their own order among themselves. Everything else weighs
what it did, and the fifteen keep Apple's places, odd as some are (U+0152 before the digits).

Known limits, from the builder:

* An accented Latin letter the font lacks (A with macron) goes after Z, not beside A: the
  firmware has no weight for it to share.
* The private-use characters U+F100..U+F944 now tie with the moved ones.
* Characters from U+0945 up without a glyph are not touched (they already sort after Z), and below
  U+0100 nothing changes.
* flashpod keeps its own copy of this rule (`flashpod/itunesdb.py`). It must change with this fix,
  or its order hints stop matching and the iPod sorts Songs itself again: correctly, but slowly.

## Tested

* **Emulator, against the firmware's own comparison, 2026-10-14:** `tools/test_fw_collate.py` runs
  the 1.6 firmware's comparison under Unicorn on 200,000 random pairs and on 336,200 pairs of single
  characters, and compares it with the model in `tools/fw_collate.py`. With the model set to this
  rule (`OPENPOD_SORTUNK=1`): 0 disagreements on both (`build/gate16/fw_collate-sortunk1.log`).
  With the model left at stock: 2,181 and 76,232 disagreements (`fw_collate-sortunk0.log`) — the
  order really did change. Stock firmware against the stock model: 0 and 0
  (`fw_collate-stock.log`). The logs do not record their command lines; the command is:

      OPENPOD_SORTUNK=1 .venv/bin/python3 tools/test_fw_collate.py <1.6 osos.bin>

* **On screen, emulator, 2026-10-13:** a 40-track library with eight test titles, Songs scrolled to
  the end. Without the fix the list starts with A-macron, Greek, Omega and Cyrillic titles before
  the one starting with a digit; with it, it starts with Omega and then the digit, and A-macron,
  Greek and Cyrillic come last, after the snowman. The scripts that build that disk and read the
  screen (`build/shuffle-analysis/mksortdisk.py`, `job_sortlist.py`) are not committed, so this
  part cannot be re-run from the repository.
* **Not on hardware.**
