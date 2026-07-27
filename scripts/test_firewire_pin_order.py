#!/usr/bin/env python3
"""Logic test: the FireWire queue must be pinned BEFORE the disk is mounted.

The 2026-07-26 failure: flashpod offered to mount a freshly attached FireWire
iPod, mounted it (= big buffered kernel reads at the data-eating default queue
settings), and only THEN pinned the queue — the bridge was already EIO'ing.
These tests pin the corrected order: ensure_firewire_disk_safe() gates
mount_device(), an unpinnable disk is refused, and a non-FireWire disk is
untouched. All sysfs/lsblk/udisks access is mocked; runs on any OS.

    PYTHONPATH=. python3 scripts/test_firewire_pin_order.py
"""
import sys

sys.argv = ["flashpod"]
from flashpod import cli                    # noqa: E402


def check(name, ok, detail=""):
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))
    assert ok, detail


calls = []


def fake_problem_factory(answers):
    """firewire_disk_problem stub returning scripted answers per call."""
    it = iter(answers)
    def problem(disk):
        calls.append("check:" + disk)
        return next(it)
    return problem


def fake_pin(disk):
    calls.append("pin:" + disk)
    return True


real = (cli.firewire_disk_problem, cli.pin_firewire_queue, cli.subprocess.run,
        cli._disk_of_dev)
cli.pin_firewire_queue = fake_pin
cli._disk_of_dev = lambda dev: "sdb"        # no /sys on the test box
try:
    # Not a FireWire disk: safe immediately, nothing pinned.
    del calls[:]
    cli.firewire_disk_problem = fake_problem_factory([None])
    check("non-FireWire disk -> safe, no pin",
          cli.ensure_firewire_disk_safe("/dev/sdb2") is True
          and calls == ["check:sdb"], repr(calls))

    # Unsafe FireWire disk: pinned, re-verified, then safe.
    del calls[:]
    cli.firewire_disk_problem = fake_problem_factory(
        [("sdb", ["read_ahead_kb=128 (need 0)"]), None])
    check("unsafe disk -> pin then re-verify",
          cli.ensure_firewire_disk_safe("/dev/sdb2") is True
          and calls == ["check:sdb", "pin:sdb", "check:sdb"], repr(calls))

    # Pinning didn't take: report unsafe.
    del calls[:]
    bad = ("sdb", ["max_sectors_kb=128 (need 4)"])
    cli.firewire_disk_problem = fake_problem_factory([bad, bad])
    check("pin fails -> reported unsafe",
          cli.ensure_firewire_disk_safe("/dev/sdb2") is False, repr(calls))

    # mount_device must refuse BEFORE any mount attempt when unsafe.
    del calls[:]
    cli.firewire_disk_problem = fake_problem_factory([bad, bad])
    def no_mount_allowed(argv, **kw):
        raise AssertionError("mount attempted on an unsafe FireWire disk: %r"
                             % (argv,))
    cli.subprocess.run = no_mount_allowed
    check("mount_device refuses unsafe disk without mounting",
          cli.mount_device("/dev/sdb2", "IPOD") is None, repr(calls))

    # RAW sessions must pin too — before the device is opened. Not for our
    # own O_DIRECT I/O (safe at any settings) but because closing a writable
    # handle makes udev re-probe the disk with big buffered reads; at
    # default settings that probe collapsed the bridge right after a
    # successful `rm` (2026-07-27).
    del calls[:]
    cli.firewire_disk_problem = fake_problem_factory(
        [("sdb", ["read_ahead_kb=128 (need 0)"]), None])
    real_orf = cli.open_raw_fat
    cli.open_raw_fat = lambda device, writable=False: \
        calls.append("open:" + device) or "FAKE-FS"
    try:
        target = cli.open_raw_target("/dev/sdb2")
        check("open_raw_target pins the queue before opening",
              calls == ["check:sdb", "pin:sdb", "check:sdb", "open:/dev/sdb2"]
              and target is not None, repr(calls))
    finally:
        cli.open_raw_fat = real_orf
finally:
    (cli.firewire_disk_problem, cli.pin_firewire_queue, cli.subprocess.run,
     cli._disk_of_dev) = real

print("\nALL ASSERTIONS PASSED")
