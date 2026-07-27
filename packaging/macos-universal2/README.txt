flashpod for macOS (modern Macs — Intel and Apple Silicon)
==========================================================

This is the build for MODERN Macs: one universal binary containing both
the Intel (x86_64) and Apple Silicon (arm64) versions; macOS runs the
right one automatically. It needs macOS 10.13 or newer (the embedded
Python's floor). If you are setting up a vintage FireWire-era Mac
(OS X 10.8), this build will NOT launch there ("Symbol not found:
___sincos_stret") — use flashpod-macos-10.8.tar.gz instead: firmware
baked in, built for that hardware.

This archive contains:

  flashpod      the program — self-contained (no Python needed)
  README.txt    this file
  LICENSE       MIT license for flashpod itself

Run it
------
This binary is unsigned, so macOS quarantines downloads. Clear that and
make it executable:

  chmod +x flashpod
  xattr -d com.apple.quarantine flashpod      # or right-click -> Open once

Then run it (optionally move it onto your PATH, e.g. /usr/local/bin):

  ./flashpod --help
  sudo ./flashpod flash      # writing a card needs root

Prefer pip? `pip install flashpod` (or pipx) gives you the same tool as
a normal Python package, on any Mac with Python 3.

Firmware
--------
At flash time the chosen firmware image is downloaded from the project's
GitHub releases (checksum-verified) and cached, or supply your own with
`--firmware <file>`. Firmware images are Apple's copyright and are not
covered by flashpod's MIT license.

Project: https://github.com/davidbarnhart/flashpod
