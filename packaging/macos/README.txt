flashpod for macOS (built for OS X 10.8, runs on 10.8+)
=======================================================

This archive contains:

  flashpod      the program — self-contained (no Python needed). Unlike the
                Linux/Windows builds, the firmware images are baked in, so
                `flashpod flash` works with no network.
  README.txt    this file
  LICENSE       MIT license for flashpod itself

Run it
------
This binary is unsigned, so macOS quarantines downloads. Clear that and make
it executable:

  chmod +x flashpod
  xattr -d com.apple.quarantine flashpod      # or right-click -> Open once

Then run it (optionally move it onto your PATH, e.g. /usr/local/bin):

  ./flashpod --help
  sudo ./flashpod flash      # writing a card needs root

Firmware
--------
The firmware images are bundled in this build (Apple's copyright, not covered
by flashpod's MIT license). You can still override the bundled image with
`--firmware <file>`.

Project: https://github.com/davidbarnhart/flashpod
