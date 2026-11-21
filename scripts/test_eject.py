#!/usr/bin/env python3
"""flashpod eject (#123) on fake disks only, never a real device.

    sudo python3 scripts/test_eject.py --rust BIN
    sudo python3 scripts/test_eject.py --rust BIN --faults FAULTS_BIN   # + mutants

Needs root (it attaches and mounts its own disks). Linux: a loop disk made
from this run's own file, with two FAT partitions, both mounted and one of
them mounted a second time (a bind mount); and, where the kernel has the
scsi_debug module, a RAM-backed fake SCSI disk, so the real SCSI STOP UNIT
path runs. macOS: a disk image attached with hdiutil.

What must hold:
- with anything held on the disk (an open file, a working directory, a
  memory map, the partition's device node), eject refuses at step 2, names
  the holder's pid, kills nothing, and unmounts nothing;
- with nothing held, it says "Safe to unplug NODE." with rc 0, and then no
  mount of the disk remains (the kernel's mount table, by device number)
  and the disk is stopped: a loop device detached, a SCSI disk answering
  TEST UNIT READY with NOT READY (sent here, not by flashpod), an hdiutil
  disk gone;
- its own re-checks catch a stand-in umount that unmounts nothing (step 4)
  and a stand-in losetup that detaches nothing (step 5).
Mutants (--faults: a build with `--features eject-faults`, plus a wrapper):
the checks above must fail for a binary that prints the verdict without
doing anything, for one with the open-files check switched off, and for
ones with the step-4 mount re-check or the step-5 detach re-check switched
off (each under the lying tool that re-check exists to catch).

Ends with one PASS/FAIL line; exit 0 only on PASS. A part that couldn't run
here says NOT RUN, and the PASS line names it.
"""
import argparse
import ctypes
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

FAILS = []
NOT_RUN = []
LINUX = sys.platform.startswith("linux")
MAC = sys.platform == "darwin"


def check(cond, what, into=None):
    """A check; `into` collects a mutant run's failures instead of failing."""
    if into is None:
        print("  %s %s" % ("ok  " if cond else "FAIL", what))
        if not cond:
            FAILS.append(what)
    elif not cond:
        into.append(what)
    return cond


def sh(*cmd, check_rc=True):
    r = subprocess.run(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       stdin=subprocess.DEVNULL)
    if check_rc and r.returncode:
        raise RuntimeError("%s: rc %d: %s" % (" ".join(cmd), r.returncode, r.stderr.strip()))
    return r.stdout.strip()


def eject(binary, target, env=None):
    r = subprocess.run(binary + ["eject", target], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, stdin=subprocess.DEVNULL, env=env, timeout=180)
    return r.returncode, r.stdout, r.stderr


# ------------------------------------------------------------------ holders

HOLD = {
    "open file": "f = open(os.path.join(T, 'held.txt'), 'w'); f.write('x'); f.flush()",
    "working directory": "os.chdir(T)",
    "memory map": "import mmap; f = open(os.path.join(T, 'mapped.bin'), 'r+b'); "
                  "m = mmap.mmap(f.fileno(), 0); f.close()",
    "device node": "fd = os.open(T, os.O_RDONLY)",
}


def hold(kind, target):
    """A child process holding `target` the `kind` way, until killed."""
    code = "import os, sys, time\nT = %r\n%s\nprint('ready', flush=True)\ntime.sleep(3600)\n" % (target, HOLD[kind])
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, universal_newlines=True,
                         stdin=subprocess.DEVNULL, cwd="/")
    line = p.stdout.readline()
    if line.strip() != "ready":
        p.kill()
        raise RuntimeError("holder %s on %s didn't start" % (kind, target))
    return p


def release(p):
    p.kill()
    p.wait()


# ------------------------------------------------------------------ Linux disks

def mountinfo_devs():
    """[(major:minor, mountpoint)] of this namespace's mounts."""
    out = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            p = line.split(" ")
            out.append((p[2], p[4]))
    return out


def node_dev(node):
    rd = os.stat(node).st_rdev
    return "%d:%d" % (os.major(rd), os.minor(rd))


class Disk:
    """A fake disk with FAT partitions, some mounted."""

    def __init__(self, node, parts, mounts, work, kind):
        self.node, self.parts, self.mounts, self.work, self.kind = node, parts, mounts, work, kind
        self.devs = {node_dev(n) for n in [node] + parts}

    def mounted(self):
        return sorted(mp for d, mp in mountinfo_devs() if d in self.devs)

    def attached(self):
        name = os.path.basename(self.node)
        if self.kind == "loop":
            return os.path.exists("/sys/block/%s/loop/backing_file" % name)
        return os.path.exists("/sys/block/%s" % name)

    def teardown(self):
        for mp in sorted(self.mounted(), key=lambda m: -m.count("/")):
            sh("umount", mp, check_rc=False)
        if self.kind == "loop" and self.attached():
            sh("losetup", "-d", self.node, check_rc=False)


def part_nodes(node, n):
    """The partition nodes of `node`; mknod them if devtmpfs hasn't yet."""
    name = os.path.basename(node)
    nodes = []
    for i in range(1, n + 1):
        pname = ("%sp%d" if name[-1].isdigit() else "%s%d") % (name, i)
        path = "/dev/" + pname
        sysdev = "/sys/block/%s/%s/dev" % (name, pname)
        for _ in range(50):
            if os.path.exists(path) or os.path.exists(sysdev):
                break
            time.sleep(0.1)
        if not os.path.exists(path) and os.path.exists(sysdev):
            maj, mn = open(sysdev).read().strip().split(":")
            os.mknod(path, 0o600 | stat.S_IFBLK, os.makedev(int(maj), int(mn)))
        if not os.path.exists(path):
            raise RuntimeError("no partition node %s" % path)
        nodes.append(path)
    return nodes


def format_and_mount(node, nparts, work, tag, bind=True):
    layout = "label: dos\n" + (",24M,c\n,,c\n" if nparts == 2 else ",,c\n")
    r = subprocess.run(["sfdisk", "--quiet", "--wipe", "always", node], input=layout, universal_newlines=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode:
        raise RuntimeError("sfdisk %s: %s" % (node, r.stderr.strip()))
    sh("blockdev", "--rereadpt", node, check_rc=False)
    if shutil.which("udevadm"):
        sh("udevadm", "settle", "--timeout=10", check_rc=False)
    parts = part_nodes(node, nparts)
    mounts = []
    for i, p in enumerate(parts):
        sh("mkfs.vfat", "-n", "EJ%s%d" % (tag, i), p)
        mp = os.path.join(work, "%s-m%d" % (tag, i))
        os.makedirs(mp, exist_ok=True)
        sh("mount", p, mp)
        mounts.append(mp)
        with open(os.path.join(mp, "mapped.bin"), "wb") as f:
            f.write(b"\0" * 8192)
    if bind:
        b = os.path.join(work, "%s-bind" % tag)
        os.makedirs(b, exist_ok=True)
        sh("mount", "--bind", mounts[-1], b)
        mounts.append(b)
    return parts, mounts


def loop_disk(work, tag):
    img = os.path.join(work, "%s.img" % tag)
    with open(img, "wb") as f:
        f.truncate(64 << 20)
    node = sh("losetup", "--find", "--show", "--partscan", img)
    if not node.startswith("/dev/loop") or not stat.S_ISBLK(os.stat(node).st_mode):
        raise RuntimeError("losetup gave %r" % node)
    try:
        parts, mounts = format_and_mount(node, 2, work, tag)
    except Exception:
        sh("losetup", "-d", node, check_rc=False)
        raise
    return Disk(node, parts, mounts, work, "loop")


# ------------------------------------------------------------------ SCSI (TEST UNIT READY, sent here)

class SgIoHdr(ctypes.Structure):
    _fields_ = [("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int), ("cmd_len", ctypes.c_ubyte),
                ("mx_sb_len", ctypes.c_ubyte), ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
                ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p), ("sbp", ctypes.c_void_p),
                ("timeout", ctypes.c_uint), ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
                ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte), ("masked_status", ctypes.c_ubyte),
                ("msg_status", ctypes.c_ubyte), ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
                ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int), ("duration", ctypes.c_uint),
                ("info", ctypes.c_uint)]


def test_unit_ready(node):
    """(status, sense key) for TEST UNIT READY on `node`."""
    cdb = (ctypes.c_ubyte * 6)()
    sense = (ctypes.c_ubyte * 32)()
    h = SgIoHdr(interface_id=ord("S"), dxfer_direction=-1, cmd_len=6, mx_sb_len=32,
                cmdp=ctypes.addressof(cdb), sbp=ctypes.addressof(sense), timeout=10000)
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, 0x2285, h)
    finally:
        os.close(fd)
    s = bytes(sense[:h.sb_len_wr])
    key = None
    if s and (s[0] & 0x7f) in (0x70, 0x71) and len(s) >= 3:
        key = s[2] & 0x0f
    elif s and (s[0] & 0x7f) in (0x72, 0x73) and len(s) >= 2:
        key = s[1] & 0x0f
    return h.status, key


def scsi_debug_disk(work):
    """A scsi_debug disk, or None (with the reason in NOT_RUN)."""
    if os.path.exists("/sys/module/scsi_debug"):
        NOT_RUN.append("scsi_debug (already loaded by someone else; left alone)")
        return None
    before = set(os.listdir("/sys/block"))
    r = subprocess.run(["modprobe", "scsi_debug", "dev_size_mb=64", "removable=1"], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode:
        NOT_RUN.append("scsi_debug (modprobe: %s)" % r.stderr.strip())
        return None
    def model(n):
        try:
            with open("/sys/block/%s/device/model" % n) as f:
                return f.read().strip()
        except OSError:
            return ""
    name = None
    for _ in range(100):
        new = [n for n in set(os.listdir("/sys/block")) - before if model(n) == "scsi_debug"]
        if new:
            name = new[0]
            break
        time.sleep(0.1)
    if not name:
        sh("modprobe", "-r", "scsi_debug", check_rc=False)
        NOT_RUN.append("scsi_debug (no disk appeared)")
        return None
    node = "/dev/" + name
    for _ in range(50):
        if os.path.exists(node):
            break
        time.sleep(0.1)
    parts, mounts = format_and_mount(node, 1, work, "sd", bind=False)
    return Disk(node, parts, mounts, work, "scsi")


def unload_scsi_debug(disk):
    disk.teardown()
    for _ in range(20):
        if subprocess.run(["modprobe", "-r", "scsi_debug"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.5)
    print("  note: scsi_debug wouldn't unload; `modprobe -r scsi_debug` when it's free")


# ------------------------------------------------------------------ the checks

def refusal_checks(binary, disk, kind, target, holder_target, into=None, env=None):
    """Held `kind` way: eject on `target` must refuse at step 2, naming the pid,
    and change nothing."""
    before = disk.mounted()
    p = hold(kind, holder_target)
    try:
        rc, out, err = eject(binary, target, env=env)
        alive = p.poll() is None
    finally:
        release(p)
    head = "%s held (%s), eject %s" % (kind, os.path.basename(holder_target), target)
    check(rc == 1, "%s: refused (rc %d)" % (head, rc), into)
    check("FAIL  2 open files" in out, "%s: step 2 is the one that failed" % head, into)
    check(re.search(r"\bpid %d\b" % p.pid, out) is not None, "%s: names the holder, pid %d" % (head, p.pid), into)
    check(alive, "%s: the holder is still running (nothing killed)" % head, into)
    check(disk.mounted() == before, "%s: every mount is still there (%d)" % (head, len(before)), into)
    check(disk.attached(), "%s: the disk is still attached" % head, into)
    if into is None and rc != 1:
        print(out + err)


def success_checks(binary, disk, target, into=None, env=None, stopped_word=None):
    rc, out, err = eject(binary, target, env=env)
    head = "nothing held, eject %s" % target
    check(rc == 0, "%s: rc 0 (got %d)" % (head, rc), into)
    check("Safe to unplug %s." % disk.node in out.split("\n"), "%s: says \"Safe to unplug %s.\"" % (head, disk.node), into)
    check(("Target: %s (drive)" % disk.node) in err.split("\n"), "%s: names its disk on stderr" % head, into)
    left = disk.mounted()
    check(not left, "%s: no mount of the disk left (kernel mount table)%s" % (head, "" if not left else ": %s" % left), into)
    if disk.kind == "loop":
        check(not disk.attached(), "%s: the loop device is detached" % head, into)
    elif disk.kind == "scsi":
        try:
            status, key = test_unit_ready(disk.node)
            check(status == 2 and key == 2, "%s: the disk answers TEST UNIT READY with NOT READY (status %#x, sense key %s)"
                  % (head, status, key), into)
        except OSError as e:
            check(False, "%s: TEST UNIT READY: %s" % (head, e), into)
    if stopped_word:
        check(stopped_word in out, "%s: reports %r" % (head, stopped_word), into)
    if into is None and rc != 0:
        print(out + err)


def stub_dir(work, name, script="#!/bin/sh\nexit 0\n"):
    d = tempfile.mkdtemp(prefix="stub-%s-" % name, dir=work)
    with open(os.path.join(d, name), "w") as f:
        f.write(script)
    os.chmod(os.path.join(d, name), 0o755)
    return dict(os.environ, PATH=d + os.pathsep + os.environ.get("PATH", ""))


def lying_umount_checks(binary, d, work, into=None, fault=None):
    env = stub_dir(work, "umount")
    if fault:
        env["FLASHPOD_EJECT_FAULT"] = fault
    rc, out, _ = eject(binary, d.node, env=env)
    check(rc == 1 and "FAIL  4 flush" in out and "still mounted" in out,
          "a umount that unmounts nothing: step 4 fails, \"still mounted\" (rc %d)" % rc, into)
    check(len(d.mounted()) == 3, "  ...and the mounts really are all still there", into)


def lying_losetup_checks(binary, d, work, into=None, fault=None):
    """Expects `d` with its mounts in place; the eject unmounts them."""
    env = stub_dir(work, "losetup")
    if fault:
        env["FLASHPOD_EJECT_FAULT"] = fault
    rc, out, _ = eject(binary, d.node, env=env)
    check(rc == 1 and "FAIL  5 stop" in out, "a losetup that detaches nothing: step 5 fails (rc %d)" % rc, into)
    check(d.attached() and not d.mounted(), "  ...unmounted, and really still attached", into)
    check("Safe to unplug" not in out, "  ...and it never says safe", into)


def linux(binary, faults, work):
    print("a loop disk: two FAT partitions, the second also bind-mounted")
    d = loop_disk(work, "a")
    try:
        check(len(d.mounted()) == 3, "three mounts to start with (%d)" % len(d.mounted()))
        refusal_checks(binary, d, "open file", d.mounts[1], d.mounts[1])          # by mountpoint
        refusal_checks(binary, d, "working directory", d.parts[0], d.mounts[0])  # by partition
        refusal_checks(binary, d, "memory map", d.mounts[2], d.mounts[2])        # by the bind mount
        refusal_checks(binary, d, "device node", d.node, d.parts[0])             # by the whole disk
        success_checks(binary, d, d.mounts[0], stopped_word="loop device detached")
    finally:
        d.teardown()

    print("its own re-checks catch stand-in tools that lie")
    d = loop_disk(work, "b")
    try:
        lying_umount_checks(binary, d, work)
        lying_losetup_checks(binary, d, work)
    finally:
        d.teardown()

    print("a scsi_debug disk (RAM-backed fake SCSI): the real STOP UNIT")
    s = scsi_debug_disk(work)
    if s:
        try:
            refusal_checks(binary, s, "open file", s.mounts[0], s.mounts[0])
            success_checks(binary, s, s.mounts[0], stopped_word="reports not ready")
        finally:
            unload_scsi_debug(s)

    if faults:
        print("mutants: the checks must fail for a broken eject")
        d = loop_disk(work, "c")
        try:
            wrap = os.path.join(work, "lying-eject")
            with open(wrap, "w") as f:
                f.write("#!/bin/sh\necho 'Safe to unplug %s.'\necho 'Target: %s (drive)' >&2\nexit 0\n" % (d.node, d.node))
            os.chmod(wrap, 0o755)
            caught = []
            success_checks([wrap], d, d.mounts[0], into=caught)
            check(bool(caught), "a binary that just prints the verdict: caught (%s)" % "; ".join(caught[:2]))
        finally:
            d.teardown()
        d = loop_disk(work, "d")
        try:
            caught = []
            env = dict(os.environ, FLASHPOD_EJECT_FAULT="no-holder-check")
            refusal_checks(faults, d, "device node", d.node, d.parts[0], into=caught, env=env)
            check(bool(caught), "the open-files check switched off: caught (%s)" % "; ".join(caught[:2]))
        finally:
            d.teardown()
        d = loop_disk(work, "e")
        try:
            caught = []
            lying_umount_checks(faults, d, work, into=caught, fault="no-mount-recheck")
            check(bool(caught), "the step-4 mount re-check switched off, under a lying umount: caught (%s)"
                  % "; ".join(caught[:2]))
        finally:
            d.teardown()
        d = loop_disk(work, "f")
        try:
            caught = []
            lying_losetup_checks(faults, d, work, into=caught, fault="no-stop-recheck")
            check(bool(caught), "the step-5 detach re-check switched off, under a lying losetup: caught (%s)"
                  % "; ".join(caught[:2]))
        finally:
            d.teardown()
    else:
        NOT_RUN.append("mutants (no --faults binary)")


# ------------------------------------------------------------------ macOS

class MacDisk:
    def __init__(self, node, mount, img):
        self.node, self.mount, self.img, self.kind = node, mount, img, "hdiutil"
        self.parts = []

    def mounted(self):
        out = sh("mount")
        base = os.path.basename(self.node)
        return sorted(l for l in out.splitlines() if re.match(r"/dev/%s(s\d+)? on " % re.escape(base), l))

    def attached(self):
        return os.path.exists(self.node)

    def teardown(self):
        if self.attached():
            sh("hdiutil", "detach", "-force", self.node, check_rc=False)


def mac_disk(work, tag):
    img = os.path.join(work, "%s.dmg" % tag)
    sh("hdiutil", "create", "-size", "32m", "-fs", "MS-DOS", "-volname", "EJ%s" % tag.upper(), "-layout", "MBRSPUD", img)
    out = sh("hdiutil", "attach", "-nobrowse", img)
    node = mount = None
    for line in out.splitlines():
        f = line.split("\t")
        dev = f[0].strip()
        if re.match(r"/dev/disk\d+$", dev):
            node = dev
        if len(f) >= 3 and f[-1].strip().startswith("/"):
            mount = f[-1].strip()
    if not node or not mount:
        raise RuntimeError("hdiutil attach: %r" % out)
    with open(os.path.join(mount, "mapped.bin"), "wb") as f:
        f.write(b"\0" * 8192)
    return MacDisk(node, mount, img)


def mac(binary, faults, work):
    print("an hdiutil-attached disk image")
    d = mac_disk(work, "a")
    try:
        before = d.mounted()
        check(len(before) == 1, "one mount to start with (%d)" % len(before))
        p = hold("open file", d.mount)
        try:
            rc, out, err = eject(binary, d.mount)
            alive = p.poll() is None
        finally:
            release(p)
        check(rc == 1 and "FAIL  2 open files" in out, "open file held: refused at step 2 (rc %d)" % rc)
        check(re.search(r"\bpid %d\b" % p.pid, out) is not None, "open file held: names pid %d" % p.pid)
        check(alive and d.mounted() == before and d.attached(), "open file held: nothing killed, unmounted or ejected")
        if rc != 1:
            print(out + err)
        rc, out, err = eject(binary, d.mount)
        check(rc == 0 and ("Safe to unplug %s." % d.node) in out.split("\n"), "nothing held: \"Safe to unplug %s.\" (rc %d)" % (d.node, rc))
        check(not d.mounted(), "nothing held: no mount of it left")
        check(not d.attached(), "nothing held: %s is gone (ejected)" % d.node)
        if rc != 0:
            print(out + err)
    finally:
        d.teardown()
    NOT_RUN.append("mutants on macOS (they run on Linux)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True, help="the flashpod binary")
    ap.add_argument("--faults", help="a flashpod built with --features eject-faults (enables the mutants)")
    o = ap.parse_args()
    if os.geteuid() != 0:
        print("test_eject needs root: it attaches and mounts its own fake disks.\nFAIL: not run")
        return 2
    binary = [os.path.abspath(o.rust)]
    faults = [os.path.abspath(o.faults)] if o.faults else None
    work = tempfile.mkdtemp(prefix="fp-eject-")
    try:
        if LINUX:
            linux(binary, faults, work)
        elif MAC:
            mac(binary, faults, work)
        else:
            print("FAIL: Linux and macOS only")
            return 2
    finally:
        if any(mp.startswith(work) for _, mp in (mountinfo_devs() if LINUX else [])):
            print("  note: something is still mounted under %s; left in place" % work)
        elif not (MAC and sh("mount").count(work)):
            shutil.rmtree(work, ignore_errors=True)
    for n in NOT_RUN:
        print("  NOT RUN: %s" % n)
    tail = "" if not NOT_RUN else " (NOT RUN: %s)" % "; ".join(NOT_RUN)
    if FAILS:
        print("FAIL: %d failed%s" % (len(FAILS), tail))
        return 1
    print("PASS: 0 failed%s" % tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
