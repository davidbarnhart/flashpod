# Clear the firmware's variables eight words at a time

## The problem

For about two seconds after the firmware takes over from the boot ROM, the processor is still on
its slow clock with its cache off, and nearly all of that time goes on one loop.

## The code it targets

The helper at `0x28125248` in `osos`, used by the start-up code to clear 1.8 MB of variables. It
spends four instructions on every word: 1.87 million of the 2.67 million instructions run before
the cache is turned on.

## The fix

The helper's first instruction is diverted to 72 bytes of appended code that store eight zero
words with one instruction, and finish the last few words the stock way. Same inputs, same
outputs; it borrows 32 bytes of stack and gives them back.
[Wiki page 27](../../../docs/wiki/27-why-the-ipod-starts-slowly.md) §27.4.

## Tested

* **Hardware, 2026-10-08, 8,412 tracks, hand timed:** about 21.5 s to about 20.3 s.
* **Emulator:** `tools/build_fastzero.py --test`. At the moment the cache is turned on, every
  4 KB page of memory holds what stock's holds, except the two pages the fix itself is in; and
  the start ends on the same screen. Two places are left out of the comparison and named in the
  tool: a clock reading, and the dead start-up stack.
