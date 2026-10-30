# Do not list every music file at every start

## The problem

The more music is on the disk, the longer the iPod takes to start, even with a small library:
5.8 seconds with 8,412 files.

## The code it targets

`fn_28069600` in `osos`, called once at every start. It looks for factory test scripts: files
ending `.amx` in `iPod_Control\Testing`. The routine that lists the folder for it
(`fn_280694fc`) hands it every file below `iPod_Control`, which is the whole `Music` folder, and
reads the disk one directory sector per command to do so.

## The fix

The two calls in `fn_28069600`, the one that chooses the path and the one that lists it, become
no-ops. The routine then returns what it returns on any iPod without a `Testing` folder: no test
script. Nothing is appended.

What is lost: a test script placed in `iPod_Control\Testing` is no longer found.
[Wiki page 27](../../../docs/wiki/27-why-the-ipod-starts-slowly.md) §27.3.

## Tested

* **Hardware, 2026-10-08, hand timed:** one card holding 8,412 music files and a 20-track
  library started in 19.4 s without this fix and 13.6 s with it.
* **Emulator:** a disk whose music folders hold files reproduces it (732 drive commands in the
  start against 61). The emulator does not show it with empty music folders, which is how it was
  missed for a day.
