"""`flashpod burn IMAGE [DEVICE]`: write a card image to a real SD/CF card.

The emulator's web app keeps cards as image files and only SHOWS this
command; David runs it. Everything that makes writing a device safe lives
here, on the CLI side -- flashpod.card never writes a device.

Checks, all before a byte is written:

* the image is a regular file holding a partition table (card.inspect; a
  card that isn't fully "ready" -- no firmware, no iTunesDB -- is named in
  the plan, not refused);
* the target is a whole-disk block device, not a partition, not a disk
  backing the running system, and REMOVABLE (USB / MMC / hot-plug) per lsblk
  -- the same list `flashpod flash` offers;
* not on FireWire: the gen-1 bridge crashes on big writes (use a USB reader);
* the card reports a size (an empty reader slot reports 0) at least the
  image's size;
* past 128 GiB the plan warns that only LBA48 firmware reaches the data;
* a typed ERASE confirmation (--yes skips it), as `flashpod flash` asks.

Then: unmount the card's partitions, write the image in 4 MiB chunks with a
progress line, flush, read the whole card range back and compare (--no-verify
skips that), re-read the partition table, eject. The image is held under a
SHARED flock for the duration, so nothing else can write it mid-burn.

Linux only for now (the web app runs on Linux); other systems are refused
with a message rather than run without the removable-disk check.
"""
import os
import stat
import sys
import time

from . import card
from . import ipod_flash
from . import platform as _plat

CHUNK = 4 << 20


def say(msg, c=None):
    print(ipod_flash.color(msg, c) if c else msg, file=sys.stderr)


def fail(msg):
    sys.exit(ipod_flash.color("flashpod burn: " + msg, ipod_flash.C_RED))


def removable_disks():
    """{kernel name: lsblk entry} of the disks `flashpod flash` would offer."""
    return {d["name"]: d for d in ipod_flash.list_candidates()}


def is_block_device(dev):
    try:
        return stat.S_ISBLK(os.stat(dev).st_mode)
    except OSError:
        return False


def check_target(dev, image_bytes, plat):
    """Refuse (sys.exit) anything but a removable whole disk at least as big
    as the image. Returns the device's size in bytes."""
    if not os.path.exists(dev):
        fail("no such device: %s" % dev)
    if not is_block_device(dev):
        fail("%s is not a block device. To copy an image to a file, use cp."
             % dev)
    plat.validate_target(dev, False)          # partition node, system disk
    name = os.path.basename(os.path.realpath(dev))
    entry = removable_disks().get(name)
    if entry is None:
        fail("%s is not a removable disk (USB, MMC or hot-plug, per lsblk); "
             "flashpod burn only writes those. `lsblk -o NAME,SIZE,TRAN,RM` "
             "shows what is attached." % dev)
    if (entry.get("tran") or "") in ("sbp", "ieee1394"):
        fail("%s is on FireWire; early iPod FireWire bridges crash on large "
             "writes. Put the card in a USB reader instead." % dev)
    size = plat.device_sectors(dev) * ipod_flash.SECTOR
    if size <= 0:
        fail("%s reports 0 bytes: is a card in the slot? (An empty slot of a "
             "multi-slot reader still shows up as a disk.)" % dev)
    if size < image_bytes:
        fail("%s is %s; the image is %s. Use a card at least as big as the "
             "image." % (dev, ipod_flash.fmt_size(size),
                         ipod_flash.fmt_size(image_bytes)))
    return size, entry


def burn(image, device=None, assume_yes=False, dry_run=False, verify=True):
    if not sys.platform.startswith("linux"):
        fail("burn is Linux-only for now. Elsewhere, write the image with "
             "your system's disk imager.")
    plat = _plat.current()
    try:
        info = card.inspect(image)
    except card.FlashpodError as exc:
        fail("%s: %s" % (image, exc))
    if info.partition_table is None:
        fail("%s has no partition table: a blank card. Flash it first "
             "(flashpod flash %s)." % (image, image))
    image_bytes = info.size_bytes
    dev = device or plat.choose_device()
    dev_bytes, entry = check_target(dev, image_bytes, plat)

    fw = info.firmware
    db = info.itunesdb
    say("\n  PLAN -- this will ERASE %s and write %s onto it"
        % (dev, image), ipod_flash.C_RED)
    say("    card        : %s  %s %s" % (
        ipod_flash.fmt_size(dev_bytes), (entry.get("vendor") or "").strip(),
        (entry.get("model") or "").strip()))
    say("    image       : %s" % ipod_flash.fmt_size(image_bytes))
    if dev_bytes > image_bytes:
        say("    (the last %s of the card stay unused)"
            % ipod_flash.fmt_size(dev_bytes - image_bytes))
    say("    firmware    : %s" % (fw.get("build") if fw.get("present")
                                  else "NONE (%s)" % fw.get("reason")))
    say("    iTunesDB    : %s" % (
        "%d tracks, %d artists" % (db["tracks"], db["artists"])
        if db.get("state") == "ok" else db.get("state")))
    for p in info.problems:
        say("    ⚠ %s" % p, ipod_flash.C_YEL)
    if not info.ready:
        say("    ⚠ the image isn't a ready-to-play card (see above); it is "
            "written as it is.", ipod_flash.C_YEL)
    if info.past_128gib:
        say("    ⚠ the data partition reaches past 128 GiB: only firmware "
            "with the LBA48 fix can address it.", ipod_flash.C_YEL)
    mps = plat.device_mountpoints(dev)
    if mps:
        say("    mounted now : " + ", ".join("%s@%s" % m for m in mps),
            ipod_flash.C_YEL)
    if dry_run:
        say("  [dry-run] nothing written.", ipod_flash.C_DIM)
        return 0
    if not assume_yes:
        want = "ERASE %s" % ipod_flash.device_label(dev)
        got = input(ipod_flash.color('\n  Type "%s" to proceed: ' % want,
                                     ipod_flash.C_RED)).strip()
        if got != want:
            sys.exit("Confirmation did not match. Aborted.")

    with open(image, "rb") as src:
        # hold the image still while we copy it
        card._lock(src, False, image)
        plat.unmount_all(dev, False)
        _copy(src, dev, image_bytes, plat)
        if verify:
            _verify(src, dev, image_bytes, plat)
    plat.reread_partition_table(dev)
    plat.eject(dev, False)
    say("\nDone. %s now holds %s -- put it in the iPod." % (dev, image),
        ipod_flash.C_GRN)
    return 0


def _progress(label, done, total, t0):
    if not sys.stderr.isatty():
        return
    rate = done / max(time.monotonic() - t0, 1e-6)
    sys.stderr.write("\r  %s %3d%%  %s of %s  (%.1f MiB/s)   " % (
        label, done * 100 // max(total, 1), ipod_flash.fmt_size(done),
        ipod_flash.fmt_size(total), rate / (1 << 20)))
    sys.stderr.flush()


def _copy(src, dev, total, plat):
    say("  writing %s ..." % ipod_flash.fmt_size(total), ipod_flash.C_DIM)
    t0 = time.monotonic()
    done = 0
    src.seek(0)
    with plat.open_raw(dev, "r+b") as out:
        while done < total:
            chunk = src.read(min(CHUNK, total - done))
            if not chunk:
                fail("short read from the image at %d of %d bytes" % (done, total))
            out.write(chunk)
            done += len(chunk)
            _progress("writing", done, total, t0)
        out.flush()
        os.fsync(out.fileno())
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    say("  wrote %s in %.0fs" % (ipod_flash.fmt_size(total),
                                time.monotonic() - t0), ipod_flash.C_GRN)


def _verify(src, dev, total, plat):
    say("  verifying: reading the card back ...", ipod_flash.C_DIM)
    plat.flush_buffers(dev)                   # read the card, not the cache
    t0 = time.monotonic()
    done = 0
    src.seek(0)
    with plat.open_raw(dev, "rb") as card_f:
        while done < total:
            want = src.read(min(CHUNK, total - done))
            got = card_f.read(len(want))
            if got != want:
                bad = next((i for i in range(min(len(got), len(want)))
                            if got[i] != want[i]), min(len(got), len(want)))
                fail("VERIFY FAILED at byte %d (sector %d): the card does not "
                     "hold what was written. Do NOT use it; re-seat it or try "
                     "another card." % (done + bad, (done + bad) // 512))
            done += len(want)
            _progress("verifying", done, total, t0)
    if sys.stderr.isatty():
        sys.stderr.write("\n")
    say("  verify OK: all %s on the card match the image."
        % ipod_flash.fmt_size(total), ipod_flash.C_GRN)
