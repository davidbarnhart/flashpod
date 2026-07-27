flashpod for macOS (modern Macs — Intel and Apple Silicon)
==========================================================

This is the build for MODERN Macs: one universal binary containing both
the Intel (x86_64) and Apple Silicon (arm64) versions; macOS runs the
right one automatically. It needs macOS 10.13 or newer (the embedded
Python's floor). If you are setting up a vintage FireWire-era Mac
(OS X 10.8), this build will NOT launch there ("Symbol not found:
___sincos_stret") — use flashpod-macos-vintage-no-internet.tar.gz
instead: firmware baked in, built for that hardware.

This archive contains:

  flashpod      the program — self-contained (no Python needed)
  README.txt    this file
  LICENSE       MIT license for flashpod itself

Run it
------
Just run it (optionally move it onto your PATH, e.g. /usr/local/bin) --
the archive already carries the executable bit:

  ./flashpod --help
  sudo ./flashpod flash      # writing a card needs root

In the unlikely event macOS refuses ("cannot be opened because the
developer cannot be verified", or it is simply killed), this build is
unsigned and got flagged on download. Clear the flag:

  xattr -d com.apple.quarantine flashpod      # or right-click -> Open once

Prefer pip? `pip install flashpod` (or pipx) gives you the same tool as
a normal Python package, on any Mac with Python 3.

Attaching an iPod over FireWire
------------------------------
macOS cannot mount these iPods' FAT volume (the early FireWire bridge
corrupts the OS's read-ahead), so on attach you get a "disk you inserted
was not readable" panel. ALWAYS CLICK IGNORE -- "Initialize" opens Disk
Utility pointed at your iPod and is one click from erasing it. flashpod
doesn't need the mount; it reads the raw device itself.

If the iPod disappears from `diskutil` completely, the bridge is wedged:
reset the iPod (Hold on, Hold off, then Menu + Play/Pause until the Apple
logo). The bridge runs off the iPod's battery, so replugging the cable
does not reset it.

Firmware
--------
At flash time the chosen firmware image is downloaded from the project's
GitHub releases (checksum-verified) and cached, or supply your own with
`--firmware <file>`. Firmware images are Apple's copyright and are not
covered by flashpod's MIT license.

Project: https://github.com/davidbarnhart/flashpod
