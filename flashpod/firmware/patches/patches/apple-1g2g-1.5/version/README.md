# Say 1.6, not 1.5

## The problem

With these fixes applied the firmware still calls itself 1.5, on the iPod's **Settings > About**
screen and in the `SysInfo` file it writes for the computer to read.

## The code it targets

Apple's 1.5 holds its version number twice in `osos`, in Apple's packed form (`0x01508000`:
major 1, then 5.0, then a byte that marks a release):

* `0x28007d38`, `mov r1, #0x50` in `firmware_version_string` (`0x28007d18`), which formats it for
  **Settings > About** ("Version 1.5");
* `0x2803174c`, the word `0x01508000` read by `sysinfo_write` (`0x28031604`), which writes it to
  `iPod_Control\Device\SysInfo` as the `buildID:` line.

## The fix

Both become 1.6 (`0x60`, `0x01608000`), so the screen and the file agree: 8 bytes changed in
two places, nothing appended (`tools/build_version16.py`).

## Tested

* **Emulator, 2026-10-14, on the whole 1.6 image** (`build/gate16/version16.log`):
  `OPENPOD_OSOS=<osos> OPENPOD_DISK=<disk> tools/test_version16.py OUTDIR --expect 1.6` starts
  the firmware, walks Language > English > Settings > About, and reads the string the firmware
  formatted there. Every formatting of the About version gave "1.6", the saved About screen shows
  "Version 1.6", 0 faults, 0 reboots. The `SysInfo` formatting at the start gave "1.6"; the hook
  also recorded six later calls of that routine that formatted "0.0 0", which the sources do not
  explain.
* `OPENPOD_VERSION16_MUTANT=1` builds 1.7 on purpose for the gate; whether it was run in the
  2026-10-14 run is not recorded.
* **Hardware:** this fix is in the image that has been on one iPod since 2026-10-13. No reading of
  the About screen or of `SysInfo` from that iPod is recorded.
