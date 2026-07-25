#!/usr/bin/env python3
"""macOS backend smoke test — exercises the real `diskutil` on this machine.

Read-only: it enumerates disks and runs the target-validation guards, but
never opens a device for writing. Safe to run on any Mac, including CI.

    PYTHONPATH=. python3 scripts/test_macos_platform.py

Exists because the macOS backend is the one platform CI never executed, so
`diskutil` argument-order bugs shipped undetected. The regressions guarded
here both come from `-plist` being appended after the operand instead of
following the verb (`diskutil list physical -plist` -> "Could not find disk
for -plist"):

  * `flash` crashed outright in device enumeration; and, worse,
  * every `_diskutil_info` call raised, so the "never the boot disk" guard in
    validate_target -- which swallows OSError -- silently failed OPEN and
    would accept the running system disk as a flash target.

A plain import or --self-test cannot catch either: both need a live diskutil.
"""
import os
import sys

if sys.platform != "darwin":
    print("not macOS -- skipping")
    sys.exit(0)

from flashpod.platform import macos

fails = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001 - report, keep going
        fails.append("%s: %s: %s" % (name, type(exc).__name__, exc))
        print("  FAIL  %s: %s" % (name, exc))
    else:
        print("  ok    %s" % name)


# -- the plist plumbing every other call sits on -------------------------------
def t_list_physical():
    pl = macos._diskutil_plist(["list", "physical"])
    assert isinstance(pl, dict), "expected a dict, got %r" % type(pl)
    assert "WholeDisks" in pl, "no WholeDisks key in `diskutil list physical`"


def t_list_bare():
    # worked even with the old argument order (no operand to be confused with);
    # pinned so a fix for the operand form can't regress it
    assert "WholeDisks" in macos._diskutil_plist(["list"])


def t_info_device():
    disks = macos._diskutil_plist(["list"]).get("WholeDisks") or []
    assert disks, "no whole disks at all -- cannot test `diskutil info`"
    info = macos._diskutil_info("/dev/" + disks[0])
    assert isinstance(info, dict) and info, "empty info for /dev/%s" % disks[0]


def t_info_root():
    # feeds the boot-disk guard below; this is the call that used to raise
    info = macos._diskutil_info("/")
    assert isinstance(info, dict) and info, "empty `diskutil info /`"


# -- enumeration ---------------------------------------------------------------
# Both of these cross-check against diskutil rather than just iterating what
# comes back: an empty list passes any for-loop trivially, and empty is exactly
# what these return when the underlying diskutil call is broken. Every
# assertion below has to be one that a silent [] cannot satisfy.
def t_external_disks():
    phys = macos._diskutil_plist(["list", "physical"]).get("WholeDisks") or []
    assert phys, "diskutil reports no physical disks at all -- cannot test"
    for d, info in macos.MacOSPlatform()._external_disks():
        assert d in phys, "%r is not in `diskutil list physical` (%r)" % (d, phys)
        assert isinstance(info, dict) and info, "empty info dict for %r" % d
        assert info.get("Internal", info.get("DeviceInternal", True)) is False, \
            "%r is not external but was returned anyway" % d


def t_fat_candidates():
    """Must offer every whole disk EXCEPT the boot disk.

    fat_disk_candidates swallows OSError and returns [], so it reported
    "success" all through the -plist bug while actually enumerating nothing.
    Pinning the exact expected set is what makes that detectable: derive the
    answer independently from diskutil and demand an exact match.
    """
    whole = macos._diskutil_plist(["list"]).get("WholeDisks") or []
    assert whole, "diskutil reports no whole disks at all -- cannot test"
    boot = macos._whole_disk(macos._diskutil_info("/").get("ParentWholeDisk", ""))
    assert boot, "could not resolve the boot disk"

    want = set("/dev/r" + d for d in whole if d != boot)
    got = dict(macos.MacOSPlatform().fat_disk_candidates())

    assert set(got) == want, "candidates %r != expected %r" % (sorted(got), sorted(want))
    assert "/dev/r" + boot not in got, "boot disk %r offered as a flash target" % boot
    for node, desc in got.items():
        assert isinstance(desc, str) and desc, "empty description for %r" % node


# -- the safety regression that matters ---------------------------------------
def t_refuses_boot_disk():
    """validate_target must REFUSE the disk backing the running system.

    It swallows OSError around the diskutil lookup, so a broken _diskutil_info
    turns this guard into a no-op instead of an error -- exactly the failure
    mode that made it worth a test.
    """
    parent = macos._diskutil_info("/").get("ParentWholeDisk", "")
    boot = macos._whole_disk(parent)
    assert boot, "could not resolve the boot disk (ParentWholeDisk=%r)" % parent
    dev = "/dev/" + boot
    if not os.path.exists(dev):
        print("      (boot node %s absent -- skipping)" % dev)
        return
    try:
        macos.MacOSPlatform().validate_target(dev, dry_run=True)
    except SystemExit:
        return                                     # refused, as it must be
    raise AssertionError("validate_target ACCEPTED the boot disk %s" % dev)


def t_refuses_partition():
    try:
        macos.MacOSPlatform().validate_target("/dev/disk0s1", dry_run=True)
    except SystemExit:
        return
    raise AssertionError("validate_target accepted a partition")


print("macOS backend test (live diskutil)")
for name, fn in [
    ("diskutil list physical -plist", t_list_physical),
    ("diskutil list -plist", t_list_bare),
    ("diskutil info -plist <device>", t_info_device),
    ("diskutil info -plist /", t_info_root),
    ("_external_disks()", t_external_disks),
    ("fat_disk_candidates()", t_fat_candidates),
    ("validate_target refuses the boot disk", t_refuses_boot_disk),
    ("validate_target refuses a partition", t_refuses_partition),
]:
    check(name, fn)

if fails:
    print("\nmacOS backend test: %d FAILED" % len(fails), file=sys.stderr)
    for f in fails:
        print("  - " + f, file=sys.stderr)
    sys.exit(1)
print("\nmacOS backend test: OK")
