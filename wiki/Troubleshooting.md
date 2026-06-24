# Troubleshooting

Symptom → cause → fix. Most of these come back to the
[FireWire bridge](Hardware-and-the-FireWire-bridge).

## macOS won't mount the iPod — "Invalid BS_jmpBoot in boot block: 000000"

**Cause:** not a format bug. macOS's read-ahead prefetch over the FireWire
bridge corrupts the boot-sector read into zeros, so the mount probe rejects the
volume. The card is fine. ([details](Hardware-and-the-FireWire-bridge#macos-cant-mount-over-firewire-no-fix-available))

**Fix:** don't mount it over FireWire. Either:
- Manage it with flashpod's raw path — `flashpod ls` (it scans, finds the iPod,
  elevates with sudo, reads it directly), or
- Pull the card into a **USB reader** (mounts fine over USB), or
- Manage it on **Linux** with the block queue pinned.

## "no iTunesDB on <device> … run `flashpod init` first?"

**Cause:** the FAT mounted but `iPod_Control/iTunes/iTunesDB` wasn't found. The
read path works (good news!) — the database just isn't there.

**Fix:** flashpod prints what it *did* find:
- *"has no iPod_Control directory — freshly flashed/formatted"* → it's a blank
  card. Run `flashpod init`, then `add` music.
- *"iPod_Control is present but … iTunesDB is missing"* → if this iPod should
  have music, that's unexpected — otherwise `init`.

## Adds/edits are very slow over FireWire

**Cause:** raw writes over the bridge are **bandwidth-limited at ~270 KiB/s** —
a hardware ceiling, not a configuration problem. Transfer-size tuning can't help
(larger transfers don't speed it up, and corrupt above the safe size).
([details](Hardware-and-the-FireWire-bridge#write-bandwidth))

**Fix:** for bulk loads, pull the card into a **USB reader** and `add` over the
mount (~10–50× faster). Use the raw FireWire path only for quick edits.

## Linux: flashpod refuses to touch the iPod (unsafe queue settings)

**Cause:** the FireWire iPod is attached with default block-queue settings,
which are *data-eating* for this bridge. They reset on every re-attach.

**Fix:** flashpod auto-pins safe settings via sudo. To make it permanent,
install the udev rule:

```sh
sudo cp contrib/99-flashpod-firewire-ipod.rules /etc/udev/rules.d/
sudo udevadm control --reload
```

`--unsafe-queue` overrides the check (don't, unless you know why).

## "<mount> is a stale mount — its device is gone"

**Cause:** the iPod disconnected while mounted; the mountpoint is a dead handle
and touching it raises EIO.

**Fix:** `sudo umount -l <mount>`, reconnect the iPod, retry.

## A mounted iPod isn't readable (left mounted by root)

**Cause:** an earlier `sudo` run left it mounted under `/media/root` (0700).

**Fix:** flashpod offers to unmount and remount it as you. Accept, or
`sudo umount <mount>` and re-run.

## Bogus capacity (e.g. 707 GB) or empty filesystem probes

**Cause:** usually a **loose connector**, not a dead card.

**Fix:** reseat the card/connector before suspecting the hardware.

## `fsck.vfat` reports errors on the iPod

**Cause:** `dosfstools` chokes on iPod boot sectors; its verdicts are unreliable
here.

**Fix:** ignore it — the kernel (and flashpod) read the volume fine.

## udisks Format calls abort after ~25 s

**Cause:** the D-Bus Format call's default timeout.

**Fix:** `gdbus call --timeout 300 …`.

## Verifying the FAT driver itself

If you suspect a read/write bug (rather than hardware), run the self-test — it
cross-checks against mtools and, as root, the Linux kernel:

```sh
python3 -m flashpod.fatfs --self-test          # vs mtools (as a normal user)
sudo python3 -m flashpod.fatfs --self-test     # + Linux kernel loop mount
```
