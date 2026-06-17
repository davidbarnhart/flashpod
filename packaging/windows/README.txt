flashpod for Windows
====================

This archive contains:

  flashpod.exe    the program — a single self-contained executable (no Python
                  or other dependencies needed)
  README.txt      this file
  LICENSE         MIT license for flashpod itself

There's no installer. To use it:

  1. Move flashpod.exe somewhere convenient.
  2. Open a terminal (PowerShell or Command Prompt) in that folder
     (or add the folder to your PATH so you can run `flashpod` anywhere).
  3. Run, e.g.:
        flashpod.exe ls

Writing firmware to a card (`flashpod.exe flash`) needs an **Administrator**
terminal and is, frankly, the least-tested path on Windows — back up the card
first.

Firmware
--------
Firmware images are NOT bundled. `flashpod.exe flash` downloads the image you
choose from GitHub, verifies its checksum, and caches it — or pass your own
with `--firmware <file>`. The firmware is Apple's copyright, not covered by
flashpod's MIT license.

Project: https://github.com/davidbarnhart/flashpod
