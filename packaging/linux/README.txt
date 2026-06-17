flashpod — command-line iPod sync + card flashing for early (1G-4G) iPods
========================================================================

This archive contains:

  flashpod      the program — a single self-contained executable (no Python
                or other dependencies needed)
  install.sh    installer that puts `flashpod` on your PATH
  README.txt    this file
  LICENSE       MIT license for flashpod itself

Install
-------
  ./install.sh          install to ~/.local/bin   (no root needed)
  sudo ./install.sh     install to /usr/local/bin (system-wide)

…or don't install at all — just run it where it is:

  chmod +x flashpod
  ./flashpod --help

Usage
-----
  flashpod ls                  show what's on the iPod
  flashpod add <files/dirs>    add music (directories are scanned recursively)
  flashpod rm <id>             remove tracks (ids from `flashpod ls all`)
  sudo flashpod flash          write firmware to a CF/SD card (ERASES it)

Run `flashpod --help` or any subcommand with `--help` for details.

Firmware
--------
Firmware images are NOT bundled in this build. When you run `flashpod flash`,
it downloads the image you choose from GitHub, verifies it against a checksum,
and caches it under ~/.cache/flashpod. To work offline or use your own image,
pass it with `--firmware <file>`.

The firmware images are Apple's copyright and are not covered by flashpod's
MIT license.

Project: https://github.com/davidbarnhart/flashpod
