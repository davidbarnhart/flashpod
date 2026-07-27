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

Attaching an iPod over FireWire
------------------------------
macOS cannot mount these iPods' FAT volume -- the early FireWire bridge
corrupts the OS's read-ahead -- so every attach brings up a "disk you
inserted was not readable" panel offering Initialize / Ignore / Eject.

  ALWAYS CLICK IGNORE. "Initialize" opens Disk Utility pointed at your
  iPod and is one click away from erasing the card.

flashpod is unaffected: it reads the raw device (/dev/rdiskN) in small
transfers the bridge handles fine, and never needs the volume mounted.
So `flashpod ls`, `add`, and `rm` all work with the volume unmounted.

To stop the panel appearing at all, disable the GUI agent that draws it
(run this in Terminal on this Mac; normal disks still automount, since
the daemon that mounts them is separate):

  launchctl unload -w /System/Library/LaunchAgents/com.apple.DiskArbitrationAgent.plist

(`load -w` puts it back. It silences the panel for every unreadable
disk, not just the iPod.)

If the iPod vanishes from `diskutil list` entirely, its bridge is wedged
-- reset the iPod: Hold on, Hold off, then Menu + Play/Pause until the
Apple logo appears. The bridge runs off the iPod's own battery, so
unplugging the cable does NOT reset it.

Firmware
--------
The firmware images are bundled in this build (Apple's copyright, not covered
by flashpod's MIT license). You can still override the bundled image with
`--firmware <file>`.

Project: https://github.com/davidbarnhart/flashpod
