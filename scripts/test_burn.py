#!/usr/bin/env python3
"""Logic test for `flashpod burn`: every check, and the write + verify, with a
regular file standing in for the card.

    PYTHONPATH=. python3 scripts/test_burn.py

No device is touched. The device-only probes (is it a block device, is it in
lsblk's removable list, its size, unmount / re-read / flush / eject) are
replaced by stand-ins; the test checks that each refusal happens BEFORE any
byte is written, that a burn leaves the target byte-identical to the image,
that verify catches a card that reads back wrong, and that the image is held
under a lock the whole time.

What this can't prove: the real-device path (O_DIRECT is not used; the OS
writes through its page cache to a USB reader). That is David's to run.
"""
import fcntl
import hashlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import burn, card, platform  # noqa: E402

fails = []
calls = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class FakePlat(object):
    """The Linux backend's device steps, recorded instead of run."""
    def __init__(self, sectors):
        self.sectors = sectors
        self.corrupt_reads = False

    def validate_target(self, dev, dry):
        calls.append("validate")

    def device_sectors(self, dev):
        return self.sectors

    def device_mountpoints(self, dev):
        return []

    def unmount_all(self, dev, dry):
        calls.append("unmount")

    def flush_buffers(self, dev):
        calls.append("flush")

    def reread_partition_table(self, dev):
        calls.append("reread")

    def eject(self, dev, dry):
        calls.append("eject")

    def choose_device(self):
        raise AssertionError("chooser must not run when a device is given")

    def open_raw(self, dev, mode):
        f = open(dev, mode)
        if "r" == mode[0] and "+" not in mode and self.corrupt_reads:
            data = bytearray(f.read())
            f.close()
            data[70000] ^= 0xFF
            return io.BytesIO(bytes(data))
        return f


def run_burn(img, dev, plat, entry, stdin=None, **kw):
    """burn.burn with the device probes stubbed; returns (exit, message)."""
    del calls[:]
    platform._cached = plat
    burn.is_block_device = lambda d: True
    name = os.path.basename(dev)
    burn.removable_disks = lambda: ({name: entry} if entry else {})
    saved_in = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        return burn.burn(img, dev, **kw), ""
    except SystemExit as exc:
        return "exit", str(exc.code)
    finally:
        sys.stdin = saved_in


def main():
    if not sys.platform.startswith("linux"):
        print("not Linux -- burn is Linux-only; skipping")
        return 0
    tmp = tempfile.mkdtemp(prefix="fp-burn-")
    img = os.path.join(tmp, "card.img")
    card.create_blank(img, 96 << 20)
    card.flash(img, synth.write_firmware(os.path.join(tmp, "fw.bin")))
    card.init(img, "Burned")
    music = synth.make_music(os.path.join(tmp, "music"))
    card.add(img, [music])
    size = os.path.getsize(img)
    usb = {"name": "sdz", "tran": "usb", "vendor": "Generic", "model": "Reader"}

    def fresh_dev(nbytes):
        dev = os.path.join(tmp, "sdz")
        with open(dev, "wb") as f:
            f.write(b"\xaa" * nbytes)      # old contents a burn must replace
        return dev

    print("refusals write nothing")
    cases = [
        ("not removable", size, None, "not a removable disk"),
        ("FireWire", size, dict(usb, tran="sbp"), "FireWire"),
        ("empty slot", 0, usb, "reports 0 bytes"),
        ("card smaller than the image", size - 512, usb, "at least as big"),
    ]
    for what, nbytes, entry, msg in cases:
        dev = fresh_dev(max(nbytes, 512))
        before = sha(dev)
        rc, out = run_burn(img, dev, FakePlat(nbytes // 512), entry,
                           assume_yes=True)
        check(rc == "exit" and msg in out and sha(dev) == before
              and "unmount" not in calls,
              "%s: refused (%r), target untouched" % (what, out[:60]))
    dev = fresh_dev(size)
    before = sha(dev)
    rc, out = run_burn(img, dev, FakePlat(size // 512), usb,
                       stdin="ERASE sdb\n")
    check(rc == "exit" and "did not match" in out and sha(dev) == before,
          "wrong typed confirmation: aborted, untouched")
    rc, out = run_burn(img, dev, FakePlat(size // 512), usb, dry_run=True)
    check(rc == 0 and sha(dev) == before and not calls[1:],
          "dry run: checks pass, nothing written")
    holder = open(img, "rb")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    rc, out = run_burn(img, dev, FakePlat(size // 512), usb, assume_yes=True)
    holder.close()
    check(rc == "exit" and "in use" in out and sha(dev) == before,
          "image locked by another program: refused, untouched")

    print("a burn")
    dev = fresh_dev(size + (8 << 20))        # a bigger card
    rc, out = run_burn(img, dev, FakePlat(os.path.getsize(dev) // 512), usb,
                       stdin="ERASE sdz\n")
    with open(dev, "rb") as f:
        head = f.read(size)
        tail = f.read()
    check(rc == 0, "typed ERASE sdz: burned (rc %r %s)" % (rc, out))
    check(hashlib.sha256(head).hexdigest() == sha(img),
          "the card's first %d bytes are the image, byte for byte" % size)
    check(tail == b"\xaa" * (8 << 20), "the rest of the bigger card untouched")
    check(calls == ["validate", "unmount", "flush", "reread", "eject"],
          "unmount before writing, flush before verify, then re-read + eject "
          "(%r)" % calls)
    lib = card.tracks(img)
    check(len(lib["tracks"]) == 5, "the image still reads fine afterwards")

    print("verify catches a bad card")
    dev = fresh_dev(size)
    plat = FakePlat(size // 512)
    plat.corrupt_reads = True
    rc, out = run_burn(img, dev, plat, usb, assume_yes=True)
    check(rc == "exit" and "VERIFY FAILED at byte 70000" in out
          and "eject" not in calls, "mismatch reported at its byte, no eject")

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
