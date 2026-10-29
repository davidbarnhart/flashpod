#!/usr/bin/env python3
"""The Rust flashpod drives a machine's disks the way the Python one does:
run both on the same FAKE HOST and compare everything they do.

    sudo python3 scripts/test_cli_fakehost.py --rust rust/target/release/flashpod
    python3 scripts/test_cli_fakehost.py --rust ... --mode user   # no root

A fake host is a private mount namespace in which flashpod sees:

* /dev holding only the fake disks (sdb, sdc, ...) beside null/zero/tty/pts;
* /sys/block, /sys/module and /sys/bus/firewire built by this script: sizes,
  queue settings, the USB device behind each disk, firewire_sbp2 loaded or
  not, FireWire devices on the bus;
* a PATH of stand-in tools (lsblk, udisksctl, mount, umount, eject, wipefs,
  partprobe, blockdev, modprobe, sync, sudo) that answer from the fake
  host's state and log every call -- the log is compared too;
* FLASHPOD_MOUNTS_FILE pointing at the fake host's mount table.

No real disk is reachable: the namespace's /dev and /sys/block hold nothing
else, and the stand-ins never call the real tools.

Modes. As root (--mode root, the default under sudo) each fake disk is a
LOOP DEVICE over a synthetic card image, so flashpod sees real block
devices, partition nodes included, and `sudo` is a set-uid stand-in that
really elevates (policy per scenario). Without root (--mode user) the disks
are plain image files bind-mounted into /dev: flashpod then takes some of
them for disk images, there are no partition nodes, and sudo always says no
-- enough to exercise detection, listings and messages, not the device-only
steps. Each scenario says which modes it runs in.

Time: both builds run on a virtual clock that sleeping advances (Python's
time.monotonic/time.sleep patched by the runner; Rust under FLASHPOD_NOW),
so waits are instant and their outcomes deterministic.

All inputs are synthetic (scripts/synth_fixtures.py): no real card, music,
iTunesDB or Apple firmware is used.
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

import layout1

LAYOUT_1 = False           # set by --layout-1
NORMALIZED = [0]           # output lines / call words read back from 1.0 names

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import synth_fixtures as synth          # noqa: E402
import test_cli_unchanged as tcu        # noqa: E402  (run_pty, sha)

SECTOR = 512
USER_UID = int(os.environ.get("SUDO_UID") or os.getuid() or 1000)
USER_GID = int(os.environ.get("SUDO_GID") or os.getgid() or 1000)
USER_NAME = os.environ.get("SUDO_USER") or os.environ.get("USER") or "user"

RUNNER = r'''
import os, random, sys, time
sys.path.insert(0, os.environ["FP_TREE"])
random.seed(1234)
time.time = lambda: 1760000000.0
# a virtual clock: sleeping advances it, nothing waits
_now = [1000.0]
time.monotonic = lambda: _now[0]
def _sleep(s):
    _now[0] += max(0.0, s)
time.sleep = _sleep
# as an installed console script sees itself: by its absolute path
sys.argv = [os.path.join(os.environ["FP_HOST"], "bin", "flashpod")] + sys.argv[1:]
from flashpod import cli
from flashpod import entropy
with entropy.deterministic(int(os.environ["FLASHPOD_SEED"]),
                           int(os.environ["FLASHPOD_NOW"])):
    sys.exit(cli.main())
'''

# ---------------------------------------------------------------------------
# the stand-in tools: one script, called by the name it was invoked as
# ---------------------------------------------------------------------------
STUB = r'''#!/usr/bin/env python3
import json, os, struct, subprocess, sys
HOST = os.environ.get("FP_HOST") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(HOST, "state.json")
SECTOR = 512

def load():
    with open(STATE) as f:
        return json.load(f)

def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)
    write_mounts(st)

def write_mounts(st):
    with open(os.path.join(HOST, "mounts"), "w") as f:
        for m in st["mounts"]:
            f.write("%s %s %s rw 0 0\n" % (m["dev"], m["mnt"].replace(" ", "\\040"), m["fstype"]))

def log(name, argv):
    with open(os.path.join(HOST, "calls.log"), "a") as f:
        caller = int(os.environ.pop("FP_CALLER_UID", os.geteuid()))
        f.write(json.dumps({"tool": name, "argv": argv, "uid": os.getuid(), "euid": caller}) + "\n")

def disk_of(st, node):
    name = os.path.basename(node)
    for d in st["disks"]:
        if name == d["name"] or (name.startswith(d["name"]) and name[len(d["name"]):].isdigit()):
            return d
    return None

def read_node(path, off, n):
    try:
        with open(path, "rb") as f:
            f.seek(off)
            return f.read(n)
    except OSError:
        return b""

def parts_of(st, d):
    """The partitions the disk's current MBR describes (what udev would
    have probed), with their filesystem and label."""
    node = "/dev/" + d["name"]
    out = []
    if not d["size"]:
        return out                     # a collapsed bridge reads nothing
    mbr = read_node(node, 0, SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa" or (mbr[82:85] == b"FAT"):
        return out
    for i, poff in enumerate((446, 462, 478, 494), 1):
        ptype = mbr[poff + 4]
        lba, cnt = struct.unpack_from("<II", mbr, poff + 8)
        if not ptype and not (lba and cnt):
            continue
        if not (lba and cnt):
            continue
        fstype, label = "", ""
        boot = read_node(node, lba * SECTOR, SECTOR)
        if len(boot) == SECTOR and boot[82:87] == b"FAT32" and boot[510:512] == b"\x55\xaa":
            fstype = "vfat"
            label = boot[71:82].decode("latin1").strip()
        if d.get("blind_udev"):
            fstype, label = "", ""
        out.append({"name": "%s%d" % (d["name"], i), "size": cnt * SECTOR,
                    "fstype": fstype, "label": label})
    return out

def human(n):
    for u in "BKMGTP":
        if n < 1024 or u == "P":
            v = ("%.1f" % n).rstrip("0").rstrip(".") if u != "B" else str(int(n))
            return v + ("" if u == "B" else u)
        n /= 1024.0

def mountpoint(st, name):
    for m in st["mounts"]:
        if m["dev"] == "/dev/" + name:
            return m["mnt"]
    return None

def lsblk(argv):
    st = load()
    cols, devs, j, b, d_only, noheadings = [], [], False, False, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o":
            cols = argv[i + 1].split(","); i += 2; continue
        if a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                if ch == "J": j = True
                elif ch == "b": b = True
                elif ch == "d": d_only = True
                elif ch == "n": noheadings = True
                elif ch == "o":
                    cols = argv[i + 1].split(","); i += 1
            i += 1; continue
        devs.append(a); i += 1
    def size_of(n):
        return n if b else human(n)
    def entry(d, p=None):
        e = {}
        for c in cols:
            k = c.lower()
            if p is not None:
                v = {"name": p["name"], "type": "part", "fstype": p["fstype"] or None,
                     "label": p["label"] or None, "size": size_of(p["size"]),
                     "mountpoint": mountpoint(st, p["name"]), "pkname": d["name"],
                     "tran": None, "rm": bool(d.get("rm")), "hotplug": bool(d.get("hotplug")),
                     "model": None, "vendor": None, "serial": None, "pttype": "dos"}.get(k)
            else:
                v = {"name": d["name"], "type": d.get("type", "disk"), "fstype": d.get("fstype") or None,
                     "label": None, "size": size_of(d["size"]), "mountpoint": mountpoint(st, d["name"]),
                     "pkname": None, "tran": d.get("tran") or None, "rm": bool(d.get("rm")),
                     "hotplug": bool(d.get("hotplug")), "model": d.get("model"), "vendor": d.get("vendor"),
                     "serial": d.get("serial"), "pttype": "dos" if parts_of(st, d) else None}.get(k)
            e[k] = v
        return e
    disks = [d for d in st["disks"] if not d.get("gone")]
    if devs:
        sel = []
        for dv in devs:
            dd = disk_of(st, dv)
            if dd is None or dd.get("gone"):
                sys.stderr.write("lsblk: %s: not a block device\n" % dv)
                sys.exit(32)
            sel.append(dd)
        disks = sel
    if j:
        out = []
        for d in disks:
            e = entry(d)
            if not d_only:
                ch = [entry(d, p) for p in parts_of(st, d)]
                if ch:
                    e["children"] = ch
            out.append(e)
        print(json.dumps({"blockdevices": out}, indent=3))
        return 0
    for d in disks:
        e = entry(d)
        vals = []
        for c in cols:
            v = e[c.lower()]
            vals.append("" if v is None else ("1" if v is True else "0" if v is False else str(v)))
        print(" ".join(vals))
    return 0

def find_part(st, node):
    d = disk_of(st, node)
    return d

def udisksctl(argv):
    st = load()
    verb = argv[0] if argv else ""
    dev = argv[argv.index("-b") + 1] if "-b" in argv else None
    pol = st.get("udisks", "ok")
    if pol == "absent":
        sys.stderr.write("Error connecting to the udisks daemon\n")
        return 1
    if verb == "mount":
        if pol == "deny":
            sys.stderr.write("Error mounting %s: GDBus.Error:org.freedesktop.UDisks2.Error.NotAuthorizedCanObtain: Not authorized to perform operation\n" % dev)
            return 1
        name = os.path.basename(dev)
        label = None
        d = disk_of(st, dev)
        for p in (parts_of(st, d) if d else []):
            if p["name"] == name:
                label = p["label"]
        mnt = "/media/%s/%s" % (st["user"], label or name)
        if not real_mount(st, dev, mnt):
            sys.stderr.write("Error mounting %s: wrong fs type\n" % dev)
            return 1
        st["mounts"].append({"dev": dev, "mnt": mnt, "fstype": "vfat"})
        save(st)
        print("Mounted %s at %s" % (dev, mnt))
        return 0
    if verb == "unmount":
        for m in list(st["mounts"]):
            if m["dev"] == dev:
                real_umount(st, m["mnt"])
                st["mounts"].remove(m)
                save(st)
                print("Unmounted %s." % dev)
                return 0
        sys.stderr.write("Error unmounting %s: GDBus.Error:org.freedesktop.UDisks2.Error.NotMounted: Device `%s' is not mounted\n" % (dev, dev))
        return 1
    if verb == "power-off":
        d = disk_of(st, dev)
        if d is None:
            return 1
        if any(disk_of(st, m["dev"]) is d for m in st["mounts"]):
            sys.stderr.write("Error powering off drive: The drive in use: Device %s is mounted\n" % dev)
            return 1
        d["gone"] = True
        save(st)
        return 0
    return 1

def real_mount(st, dev, mnt):
    """Root mode: mount the loop partition for real (kernel vfat, inside the
    namespace). User mode can't, and says so."""
    if st["mode"] != "root" or os.geteuid() != 0:
        return False
    os.makedirs(mnt, exist_ok=True)
    return subprocess.call(["/usr/bin/mount", "-t", "vfat", "-o", "uid=%d,gid=%d" % (st["uid"], st["gid"]), dev, mnt],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

def real_umount(st, mnt):
    if st["mode"] == "root" and os.geteuid() == 0:
        subprocess.call(["/usr/bin/umount", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def mount(argv):
    st = load()
    if os.geteuid() != 0:
        sys.stderr.write("mount: only root can do that\n")
        return 1
    args = [a for a in argv if not a.startswith("-")]
    if len(args) < 2:
        return 1
    dev, mnt = args[0], args[1]
    if not real_mount(st, dev, mnt):
        sys.stderr.write("mount: %s: wrong fs type, bad option, bad superblock on %s\n" % (mnt, dev))
        return 32
    st["mounts"].append({"dev": dev, "mnt": mnt, "fstype": "vfat"})
    save(st)
    return 0

def umount(argv):
    st = load()
    target = [a for a in argv if not a.startswith("-")]
    if not target:
        return 1
    t = target[0]
    if os.geteuid() != 0:
        sys.stderr.write("umount: %s: must be superuser to unmount.\n" % t)
        return 32
    for m in list(st["mounts"]):
        if m["dev"] == t or m["mnt"] == t:
            real_umount(st, m["mnt"])
            st["mounts"].remove(m)
            save(st)
            return 0
    sys.stderr.write("umount: %s: not mounted.\n" % t)
    return 32

def blockdev(argv):
    st = load()
    dev = argv[-1]
    d = disk_of(st, dev)
    if "--getsize64" in argv:
        if d is None:
            return 1
        print(d["size"])
        return 0
    if "--getss" in argv:
        print(512)
        return 0
    if "--rereadpt" in argv:
        return reread(st, dev)
    return 0

def reread(st, dev):
    if st["mode"] == "root" and os.geteuid() == 0:
        # BLKRRPART on the loop device, then expose its partition nodes
        import fcntl
        try:
            fd = os.open(dev, os.O_RDONLY)
            fcntl.ioctl(fd, 0x125f)
            os.close(fd)
        except OSError:
            return 1
        d = disk_of(st, dev)
        if d is not None:
            for i in range(1, 5):
                src = "%sp%d" % (d["loop"], i)
                dst = "/dev/%s%d" % (d["name"], i)
                host = os.path.join(HOST, "hostdev", os.path.basename(src))
                if os.path.exists(host) and not os.path.ismount(dst):
                    if not os.path.exists(dst):
                        open(dst, "w").close()
                    subprocess.call(["/usr/bin/mount", "--bind", host, dst])
    return 0

def partprobe(argv):
    st = load()
    return reread(st, argv[-1]) if argv else 0

def modprobe(argv):
    st = load()
    if os.geteuid() != 0:
        sys.stderr.write("modprobe: ERROR: could not insert 'firewire_sbp2': Operation not permitted\n")
        return 1
    os.makedirs(os.path.join(HOST, "sys", "module", "firewire_sbp2"), exist_ok=True)
    st["sbp2"] = True
    for d in st["disks"]:
        if d.get("needs_sbp2"):
            d.pop("gone", None)
    save(st)
    return 0

def sudo(argv):
    """The set-uid helper hands us root (root mode); user mode never can."""
    st = load()
    pol = st.get("sudo", "allow")
    n = False
    user = None
    while argv and argv[0].startswith("-"):
        a = argv.pop(0)
        if a == "-n":
            n = True
        elif a == "-u":
            user = argv.pop(0)
        elif a == "--":
            break
    if pol == "absent":
        sys.stderr.write("sudo: command not found\n")
        return 127
    if pol == "deny" or os.geteuid() != 0:
        sys.stderr.write("sudo: a password is required\n" if n else "Sorry, try again.\nsudo: 3 incorrect password attempts\n")
        return 1
    # env_reset, as sudo does -- except PATH: real sudo's secure_path would
    # hand the elevated flashpod the host's real udisksctl/mount/eject
    keep = {k: os.environ[k] for k in ("TERM", "LANG", "LC_ALL") if k in os.environ}
    env = dict(keep, PATH=os.path.join(HOST, "bin"), HOME=os.path.join(HOST, "home-root"),
               USER="root", LOGNAME="root", SHELL="/bin/sh",
               SUDO_USER=st["user"], SUDO_UID=str(st["uid"]), SUDO_GID=str(st["gid"]),
               SUDO_COMMAND=" ".join(argv))
    while argv and argv[0] == "env":
        argv.pop(0)
        while argv and "=" in argv[0] and not argv[0].startswith("/"):
            k, v = argv.pop(0).split("=", 1)
            env[k] = v
    os.setgid(0)
    os.setuid(0)
    if user and user != "root":
        os.setgid(st["gid"])
        os.setuid(st["uid"])
    os.execvpe(argv[0], argv, env)

def main():
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    if name == "_stub.py":
        name, argv = argv[0], argv[1:]
    log(name, argv)
    fn = {"lsblk": lsblk, "udisksctl": udisksctl, "mount": mount, "umount": umount,
          "blockdev": blockdev, "partprobe": partprobe, "modprobe": modprobe,
          "sudo": sudo}.get(name)
    if fn is None:
        return 0                                   # sync, eject, wipefs: logged only
    return fn(list(argv))

sys.exit(main() or 0)
'''

SUDO_C = r'''
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* set-uid root, for the tools that elevate (sudo) or act for the user with
   privilege (udisks): note who called, become root for real, then let the
   Python stand-in, named by argv[0], decide */
int main(int argc, char **argv) {
    char **v = calloc(argc + 3, sizeof(char *));
    char uid[32];
    const char *name = strrchr(argv[0], '/') ? strrchr(argv[0], '/') + 1 : argv[0];
    int i;
    snprintf(uid, sizeof uid, "%d", (int)getuid());
    setenv("FP_CALLER_UID", uid, 1);
    if (setgid(0) || setuid(0)) { /* not set-uid: the stand-in will refuse */ }
    v[0] = "python3"; v[1] = STUB; v[2] = (char *)name;
    for (i = 1; i < argc; i++) v[i + 2] = argv[i];
    execv(PYTHON, v);
    perror("execv");
    return 126;
}
'''


# ---------------------------------------------------------------------------
# building a host
# ---------------------------------------------------------------------------
def make_cards(root, fw, music):
    """Synthetic card images, made with flashpod's own image path: an iPod
    with music, a freshly flashed card with no database, a blank card."""
    py = [sys.executable, "-m", "flashpod"]
    env = dict(os.environ, PYTHONPATH=TREE, HOME=root)
    cards = {}

    def run(*args, ok=(0,)):
        rc = subprocess.run(py + list(args), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc not in ok:
            raise RuntimeError("making a card: %s -> %d" % (" ".join(args), rc))

    for kind in ("ipod", "nodb", "fresh", "blank"):
        path = os.path.join(root, "card-%s.img" % kind)
        with open(path, "wb") as f:
            f.truncate(80 << 20)
        if kind != "blank":
            run("flash", path, "--firmware", fw, "--yes", "--no-arm")
        if kind in ("ipod", "nodb"):
            run("init", "--raw", path)
        if kind == "ipod":
            run("add", "--raw", path, music, ok=(0, 1))    # one file is meant to fail
        if kind == "nodb":
            # iPod_Control without its database (deleted, or never written)
            sys.path.insert(0, TREE)
            from flashpod import fatfs
            with open(path, "r+b") as f:
                dev = fatfs.BlockDev(part_start=0, max_xfer=2048, fileobj=f)
                boot = dev.read(0, 1)
                dev.part_start = int.from_bytes(boot[446 + 16 + 8:446 + 16 + 12], "little")
                fs = fatfs.Fat32(dev)
                fs.remove("iPod_Control/iTunes/iTunesDB")
                fs.sync()
        cards[kind] = path
    # for burn: the iPod card plus 1 MiB + 7 sectors of non-zero bytes past
    # its partitions, so the image ends off a 4 MiB chunk boundary and its
    # last chunk isn't zeros (a dropped tail must show)
    tail = os.path.join(root, "card-burntail.img")
    shutil.copyfile(cards["ipod"], tail)
    with open(tail, "ab") as f:
        f.write(pattern((1 << 20) + 7 * 512, 0x5A))
    cards["burntail"] = tail
    return cards


def pattern(n, seed):
    """n bytes that are never zero and never repeat within a sector."""
    one = bytes(((i * 7 + seed) % 255) + 1 for i in range(512 * 3 + 1))
    return (one * (n // len(one) + 1))[:n]


def build_host(run, spec, cards, mode, music):
    """Lay out the fake host's files under `run`."""
    os.makedirs(run)
    os.chmod(run, 0o755)
    for sub in ("dev", "media", "bin", "home", "home-root", "sys/block", "sys/module", "sys/bus/firewire/devices",
                "sys/devices"):
        os.makedirs(os.path.join(run, sub), exist_ok=True)
    st = {"mode": mode, "user": USER_NAME, "uid": USER_UID, "gid": USER_GID,
          "sudo": spec.get("sudo", "allow"), "udisks": spec.get("udisks", "ok"),
          "sbp2": spec.get("sbp2", True), "disks": [], "mounts": []}
    if st["sbp2"]:
        os.makedirs(os.path.join(run, "sys/module/firewire_sbp2"))
    for i, fwdev in enumerate(spec.get("firewire_bus", [])):
        d = os.path.join(run, "sys/bus/firewire/devices", "fw%d" % i)
        os.makedirs(d)
        with open(os.path.join(d, "is_local"), "w") as f:
            f.write("1\n" if fwdev == "local" else "0\n")
    for d in spec["disks"]:
        d = dict(d)
        img = os.path.join(run, "disk-%s.img" % d["name"])
        shutil.copyfile(cards[d["card"]], img)
        if d.get("size_mb"):
            with open(img, "r+b") as f:
                f.truncate(d["size_mb"] << 20)
        if d.get("fill"):
            # a used card: old data everywhere, nothing the burn can leave as zeros
            size = os.path.getsize(img)
            with open(img, "r+b") as f:
                f.write(pattern(size, 0x33))
        d["image"] = img
        d["size"] = 0 if d.get("zero") else os.path.getsize(img)
        if d.get("needs_sbp2") and not st["sbp2"]:
            d["gone"] = True
        st["disks"].append(d)
        sysd = os.path.join(run, "sys/block", d["name"])
        os.makedirs(os.path.join(sysd, "queue"))
        q = d.get("queue", {})
        for k, v in (("size", d["size"] // SECTOR), ("removable", int(bool(d.get("rm"))))):
            with open(os.path.join(sysd, k), "w") as f:
                f.write("%d\n" % v)
        for k, v in (("max_sectors_kb", q.get("msk", 128)), ("read_ahead_kb", q.get("rak", 128))):
            with open(os.path.join(sysd, "queue", k), "w") as f:
                f.write("%d\n" % v)
        if d.get("tran") == "usb":
            usb = os.path.join(run, "sys/devices/pci0/usb1/1-%s" % d["name"])
            os.makedirs(usb)
            for k, v in (("idVendor", d.get("idvendor", "05e3")), ("product", d.get("product", "USB Storage"))):
                with open(os.path.join(usb, k), "w") as f:
                    f.write(v + "\n")
            scsi = os.path.join(usb, "host0/target0:0:0/0:0:0:0")
        else:
            scsi = os.path.join(run, "sys/devices/pci0/fw0/fw1/host0/target0:0:0/0:0:0:%s" % d["name"])
        os.makedirs(scsi)
        with open(os.path.join(scsi, "queue_depth"), "w") as f:
            f.write("%d\n" % q.get("qd", 1))
        for k in ("vendor", "model"):
            with open(os.path.join(scsi, k), "w") as f:
                f.write((d.get(k) or "") + "\n")
        os.symlink(scsi, os.path.join(sysd, "device"))
        for p in range(1, 5):
            os.makedirs(os.path.join(sysd, "%s%d" % (d["name"], p)))
    for m in spec.get("mounts", []):
        st["mounts"].append(dict(m))
    with open(os.path.join(run, "state.json"), "w") as f:
        json.dump(st, f, indent=1)
    with open(os.path.join(run, "mounts"), "w") as f:
        for m in st["mounts"]:
            f.write("%s %s %s rw 0 0\n" % (m["dev"], m["mnt"], m["fstype"]))
    open(os.path.join(run, "calls.log"), "w").close()
    # the tools
    stub = os.path.join(run, "bin", "_stub.py")
    with open(stub, "w") as f:
        f.write(STUB)
    os.chmod(stub, 0o755)
    for tool in ("lsblk", "mount", "umount", "eject", "wipefs", "partprobe",
                 "blockdev", "modprobe", "sync") + (() if mode == "root" else ("udisksctl",)):
        os.symlink("_stub.py", os.path.join(run, "bin", tool))
    if mode == "root":
        os.chown(run, USER_UID, USER_GID)
        for dirpath, dirs, files in os.walk(run):
            for x in dirs + files:
                p = os.path.join(dirpath, x)
                if not os.path.islink(p):
                    os.chown(p, USER_UID, USER_GID)
        src = os.path.join(run, "sudo.c")
        with open(src, "w") as f:
            f.write(SUDO_C)
        for tool in ("sudo", "udisksctl"):
            subprocess.check_call(["gcc", "-O1", "-o", os.path.join(run, "bin", tool),
                                   '-DSTUB="%s"' % stub, '-DPYTHON="%s"' % sys.executable, src])
            os.chmod(os.path.join(run, "bin", tool), 0o4755)
    else:
        os.symlink("_stub.py", os.path.join(run, "bin", "sudo"))
    for real in ("env", "sh", "cat", "dd", "id", "python3", "true", "sleep"):
        path = shutil.which(real) if real != "python3" else sys.executable
        if path:
            os.symlink(path, os.path.join(run, "bin", real))
    # loop devices behind the disks (root mode)
    if mode == "root":
        for d in st["disks"]:
            d["loop"] = subprocess.check_output(["losetup", "-P", "-f", "--show", d["image"]],
                                                universal_newlines=True).strip()
        with open(os.path.join(run, "state.json"), "w") as f:
            json.dump(st, f, indent=1)
    return st


def teardown(st):
    for d in st["disks"]:
        if d.get("loop"):
            subprocess.call(["losetup", "-d", d["loop"]])


# ---------------------------------------------------------------------------
# entering a host (this script re-invoked inside the new mount namespace)
# ---------------------------------------------------------------------------
def _bind(src, dst):
    subprocess.check_call(["/usr/bin/mount", "--bind", src, dst])


def enter(run, as_root, argv):
    with open(os.path.join(run, "state.json")) as f:
        st = json.load(f)
    dev = os.path.join(run, "dev")
    subprocess.check_call(["/usr/bin/mount", "-t", "tmpfs", "tmpfs", dev])
    for n in ("null", "zero", "full", "random", "urandom", "tty", "ptmx"):
        p = os.path.join(dev, n)
        open(p, "w").close()
        _bind("/dev/" + n, p)
    os.makedirs(os.path.join(dev, "pts"))
    _bind("/dev/pts", os.path.join(dev, "pts"))
    for d in st["disks"]:
        p = os.path.join(dev, d["name"])
        open(p, "w").close()
        if st["mode"] == "root":
            _bind(d["loop"], p)
            for i in range(1, 5):
                lp = "%sp%d" % (d["loop"], i)
                if os.path.exists(lp):
                    pp = os.path.join(dev, "%s%d" % (d["name"], i))
                    open(pp, "w").close()
                    _bind(lp, pp)
        else:
            _bind(d["image"], p)
    if st["mode"] == "root":
        # the real /dev, for the partprobe stand-in to find the loop
        # partitions a flash creates; flashpod itself never looks here
        hostdev = os.path.join(run, "hostdev")
        os.makedirs(hostdev, exist_ok=True)
        _bind("/dev", hostdev)
    # --rbind: the disk and null/tty nodes are bind mounts inside it
    subprocess.check_call(["/usr/bin/mount", "--rbind", dev, "/dev"])
    _bind(os.path.join(run, "sys/block"), "/sys/block")
    _bind(os.path.join(run, "sys/module"), "/sys/module")
    subprocess.check_call(["/usr/bin/mount", "-t", "tmpfs", "tmpfs", "/sys/bus"])
    os.makedirs("/sys/bus/firewire")
    _bind(os.path.join(run, "sys/bus/firewire"), "/sys/bus/firewire")
    if os.path.isdir("/media"):
        _bind(os.path.join(run, "media"), "/media")
    env = {
        "PATH": os.path.join(run, "bin"), "HOME": os.path.join(run, "home"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "xterm",
        "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "FP_HOST": run, "FP_TREE": TREE,
        "FLASHPOD_MOUNTS_FILE": os.path.join(run, "mounts"),
        "FLASHPOD_SEED": "1234", "FLASHPOD_NOW": "1760000000",
        "USER": USER_NAME if not as_root else "root",
    }
    for k, v in os.environ.items():
        if k.startswith("FPX_"):
            env[k[4:]] = v
    if not as_root:
        if st["mode"] == "root":
            os.setgroups([])
            os.setgid(USER_GID)
            os.setuid(USER_UID)
        else:
            argv = ["/usr/bin/unshare", "--map-user=%d" % USER_UID, "--map-group=%d" % USER_GID] + argv
    os.execvpe(argv[0], argv, env)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
USB_IPOD = {"name": "sdb", "card": "ipod", "tran": "usb", "rm": 1, "hotplug": 1,
            "model": "Card Reader", "vendor": "Generic"}

SCENARIOS = {
    # flash with no device named: the chooser over removable disks (a
    # generic-class model, an empty reader slot), then a dry run on the pick
    "flash_chooser": {
        "modes": ("user", "root"),
        "host": {"disks": [dict(USB_IPOD, model="MassStorageClass", vendor="Generic"),
                           dict(USB_IPOD, name="sdc", card="blank", zero=True, model="Card Reader",
                                idvendor="0bda", product="USB3.0-CRW")],
                 "mounts": [{"dev": "/dev/sdb2", "mnt": "/media/david/IPOD", "fstype": "vfat"}]},
        "steps": [
            {"argv": ["flash", "--dry-run", "--firmware", "{fw}", "--no-arm"],
             "pty": [(r"to quit\): ", "9\r"), (r"to quit\): ", "1\r"), (r"to quit\): ", "r\r"),
                     (r"to quit\): ", "0\r")]},
            {"argv": ["flash", "--dry-run", "--firmware", "{fw}", "--no-arm"],
             "pty": [(r"to quit\): ", "q\r")]},
            # end of input at the chooser
            {"argv": ["flash", "--dry-run", "--firmware", "{fw}", "--no-arm"]},
            # --no-format keeps a matching data partition; a dry run without
            # root can't read the card to check, and says so
            {"argv": ["flash", "--dry-run", "--no-format", "--firmware", "{fw}", "--no-arm"],
             "pty": [(r"to quit\): ", "0\r")]},
        ],
    },
    # one iPod card in a USB reader, not mounted: ls finds it by its database
    "usb_ls_scan": {
        "modes": ("user", "root"),
        "host": {"disks": [dict(USB_IPOD, blind_udev=True)]},
        "steps": [
            {"argv": ["ls"], "root": True},
            {"argv": ["ls", "all"], "root": True},
            {"argv": ["ls", "--raw", "/dev/sdb"], "root": True},
        ],
    },
    # as the user: ls wants root to scan, and sudo is refused
    "usb_ls_sudo_denied": {
        "modes": ("user", "root"),
        "host": {"disks": [dict(USB_IPOD, blind_udev=True)], "sudo": "deny"},
        "steps": [
            {"argv": ["ls"]},
            # a plain file in user mode, so an image: root mode only
            {"argv": ["ls", "--raw", "/dev/sdb"], "modes": ("root",)},
            {"argv": ["rm", "--raw", "/dev/sdb", "52"], "modes": ("root",)},
        ],
    },
    # an iPod beside a card that has iPod_Control but no database, and a
    # freshly flashed one: only the iPod is found
    "usb_three_cards": {
        "modes": ("user", "root"),
        "host": {"disks": [dict(USB_IPOD, name="sdb", card="nodb", blind_udev=True),
                           dict(USB_IPOD, name="sdc", blind_udev=True),
                           dict(USB_IPOD, name="sdd", card="fresh", blind_udev=True)]},
        "steps": [
            {"argv": ["ls"], "root": True},
            {"argv": ["init"], "root": True, "pty": [(r"Initialize which\? \[none\] ", "x\r")]},
            {"argv": ["init"], "root": True},
            {"argv": ["rm", "52"], "root": True},
        ],
    },
    # a legacy-named FireWire disk (tran "ieee1394"), udev able to see it
    "firewire_ieee1394": {
        "modes": ("user", "root"),
        "host": {"disks": [{"name": "sdb", "card": "ipod", "tran": "ieee1394", "type": "disk",
                            "blind_udev": True, "queue": {"msk": 4, "rak": 0, "qd": 1}}]},
        "steps": [{"argv": ["ls"], "root": True}, {"argv": ["ls", "artists"], "root": True}],
    },
    # ROOT MODE ONLY from here: real block devices behind the fake disks.
    # As the user, sudo allowed: ls elevates itself and the root run scans.
    "usb_sudo_allowed": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD)]},
        "steps": [
            {"argv": ["ls"], "pty": []},
            {"argv": ["ls", "--raw", "/dev/sdb2"], "pty": []},
            {"argv": ["rm", "--raw", "/dev/sdb", "53"],
             "pty": [(r"Eject the iPod now\? \[Y/n\] ", "n\r")]},
        ],
    },
    # add/rm with no --mount on a USB card: found by its database, then
    # MOUNTED (udisks) and driven through the kernel's FAT; eject offered
    "usb_mount_preference": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD)]},
        "steps": [
            {"argv": ["rm", "54"], "root": True,
             "pty": [(r"Eject the iPod now\? \[Y/n\] ", "n\r")]},
            {"argv": ["ls"], "pty": [(r"continue\? \[Y/n\] ", "\r")]},
            {"argv": ["rm", "55"], "pty": [(r"continue\? \[Y/n\] ", "\r"),
                                           (r"Eject the iPod now\? \[Y/n\] ", "\r")]},
        ],
    },
    # a FireWire iPod at the kernel's default queue settings: pinned first
    "firewire_pin": {
        "modes": ("root",),
        "host": {"disks": [{"name": "sdb", "card": "ipod", "tran": "sbp", "type": "rbc",
                            "queue": {"msk": 128, "rak": 128, "qd": 1}}]},
        "steps": [
            {"argv": ["ls"], "root": True},
            {"argv": ["rm", "52"], "root": True},
        ],
    },
    # ROOT MODE: flashing a real block device. Elevates through sudo,
    # writes, verifies, re-reads the table, offers init on the new data
    # partition (mounted at a temp dir), then ejects.
    "flash_device": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD, card="blank")]},
        "steps": [
            {"argv": ["flash", "/dev/sdb", "--firmware", "{fw}", "--yes", "--no-arm"],
             "pty": [(r"Run init on /dev/sdb2 now\? \[Y/n\] ", "\r"),
                     (r"Load music onto the card now\? \[Y/n\] ", "n\r")]},
        ],
    },
    # the typed confirmation, a mounted card unmounted first, init declined
    "flash_device_typed": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD)],
                 "mounts": [{"dev": "/dev/sdb2", "mnt": "/media/david/IPOD", "fstype": "vfat"}]},
        "steps": [
            {"argv": ["flash", "/dev/sdb", "--firmware", "{fw}", "--no-arm"], "root": True,
             "pty": [(r"to proceed: ", "ERASE sdb\r"),
                     (r"Run init on /dev/sdb2 now\? \[Y/n\] ", "n\r")]},
        ],
    },
    # --no-format on an iPod card: the data partition is checked and kept,
    # no init offer; and on a blank card it is refused
    "flash_device_keep": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD), dict(USB_IPOD, name="sdc", card="blank")]},
        "steps": [
            {"argv": ["flash", "/dev/sdc", "--no-format", "--firmware", "{fw}", "--yes", "--no-arm"], "root": True},
            {"argv": ["flash", "/dev/sdb", "--no-format", "--firmware", "{fw}", "--yes", "--no-arm"], "root": True,
             "pty": []},
        ],
    },
    # what flash refuses before writing anything
    "flash_device_refusals": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD), dict(USB_IPOD, name="sdc", card="blank", zero=True)]},
        "steps": [
            {"argv": ["flash", "/dev/sdb1", "--firmware", "{fw}", "--yes", "--no-arm"], "root": True},
            {"argv": ["flash", "/dev/sdz", "--firmware", "{fw}", "--yes", "--no-arm"], "root": True},
            {"argv": ["flash", "/dev/sdc", "--firmware", "{fw}", "--yes", "--no-arm"], "root": True},
            # no root and no terminal: the sudo command to run instead
            {"argv": ["flash", "/dev/sdb", "--firmware", "{fw}", "--yes", "--no-arm"]},
            {"argv": ["flash", "--self-test"]},
        ],
    },
    # a terminal, sudo refused: the resolved choices were carried in its argv
    "flash_sudo_denied": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD)], "sudo": "deny"},
        "steps": [
            {"argv": ["flash", "/dev/sdb", "--firmware", "{fw}", "--yes", "--max-data-gb", "0.5", "--no-arm"],
             "pty": []},
        ],
    },
    # burn's checks, all before a byte is written. In user mode /dev/sdb is a
    # plain file, so "not a block device"; in root mode a dry run's plan
    "burn_checks": {
        "modes": ("user", "root"),
        "host": {"disks": [dict(USB_IPOD, card="blank")]},
        "steps": [
            {"argv": ["burn", "{ipod_img}", "/dev/sdb", "--dry-run"]},
            {"argv": ["burn", "{blank_img}", "/dev/sdb", "--dry-run"]},
            {"argv": ["burn", "/nonexistent.img", "/dev/sdb", "--dry-run"]},
            {"argv": ["burn", "{ipod_img}", "--dry-run"], "pty": [(r"to quit\): ", "0\r")]},
            # no root, no terminal: the sudo command to run instead
            {"argv": ["burn", "{ipod_img}", "/dev/sdb", "--yes", "--no-verify"]},
        ],
    },
    # ROOT MODE: an image burned onto a card, read back, ejected
    "burn_device": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD, card="blank", size_mb=96)]},
        "steps": [
            {"argv": ["burn", "{ipod_img}", "/dev/sdb", "--yes"], "pty": []},
        ],
    },
    # a used card (old data everywhere) and an image whose last chunk is
    # short and non-zero: every byte of the image must reach the card
    "burn_device_tail": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD, card="blank", size_mb=96, fill=True)]},
        "steps": [
            {"argv": ["burn", "{burntail_img}", "/dev/sdb", "--yes"], "pty": []},
        ],
    },
    # the typed confirmation: a mismatch, then the real thing without verify
    "burn_device_typed": {
        "modes": ("root",),
        "host": {"disks": [dict(USB_IPOD, card="blank")],
                 "mounts": [{"dev": "/dev/sdb2", "mnt": "/media/david/IPOD", "fstype": "vfat"}]},
        "steps": [
            {"argv": ["burn", "{ipod_img}", "/dev/sdb"], "root": True, "pty": [(r"to proceed: ", "ERASE sdc\r")]},
            {"argv": ["burn", "{ipod_img}", "/dev/sdb", "--no-verify"], "root": True,
             "pty": [(r"to proceed: ", "ERASE sdb\r")]},
        ],
    },
    # what burn refuses: a partition, FireWire, a fixed disk, too small,
    # an empty slot, nothing there
    "burn_refusals": {
        "modes": ("root",),
        "host": {"disks": [{"name": "sdb", "card": "blank", "tran": "sbp", "type": "disk", "rm": 1},
                           {"name": "sdc", "card": "blank", "tran": "sata", "type": "disk", "rm": 0, "hotplug": 0},
                           dict(USB_IPOD, name="sdd", card="blank", size_mb=40),
                           dict(USB_IPOD, name="sde", card="blank", zero=True),
                           dict(USB_IPOD, name="sdf")]},
        "steps": [
            {"argv": ["burn", "{ipod_img}", "/dev/sdf1", "--yes"], "root": True},
            {"argv": ["burn", "{ipod_img}", "/dev/sdb", "--yes"], "root": True},
            {"argv": ["burn", "{ipod_img}", "/dev/sdc", "--yes"], "root": True},
            {"argv": ["burn", "{ipod_img}", "/dev/sdd", "--yes"], "root": True},
            {"argv": ["burn", "{ipod_img}", "/dev/sde", "--yes"], "root": True},
            {"argv": ["burn", "{ipod_img}", "/dev/sdz", "--yes"], "root": True},
        ],
    },
    # nothing attached at all
    "nothing": {
        "modes": ("user", "root"),
        "host": {"disks": []},
        "steps": [
            {"argv": ["ls"], "root": True},
            {"argv": ["add", "x"], "root": True},
            {"argv": ["init"], "root": True},
            # flash with no device: the chooser has nothing to offer
            {"argv": ["flash", "--dry-run", "--firmware", "{fw}", "--no-arm"], "pty": []},
        ],
    },
    # a disk reporting 0 bytes on FireWire: the bridge-collapse hint
    "firewire_collapsed": {
        "modes": ("user", "root"),
        "host": {"disks": [{"name": "sdb", "card": "ipod", "tran": "sbp", "type": "rbc",
                            "zero": True}]},
        "steps": [{"argv": ["ls"], "root": True}],
    },
    # a FireWire device on the bus, firewire_sbp2 not loaded
    "firewire_sbp2_missing": {
        "modes": ("user", "root"),
        "host": {"disks": [{"name": "sdb", "card": "ipod", "tran": "sbp", "type": "rbc",
                            "needs_sbp2": True, "blind_udev": True}],
                 "sbp2": False, "firewire_bus": ["local", "remote"]},
        "steps": [{"argv": ["ls"]}, {"argv": ["ls"], "root": True}],
    },
}


# ---------------------------------------------------------------------------
# running and comparing
# ---------------------------------------------------------------------------
def run_step(run, step, cmd, timeout=120):
    me = [sys.executable, os.path.abspath(__file__), "--enter", run,
          "root" if step.get("root") else "user", "--"]
    extra = {"FPX_" + k: v for k, v in (step.get("env") or {}).items()}
    if os.geteuid() == 0:
        outer = ["unshare", "--mount", "--propagation", "private"] + me
    else:
        outer = ["unshare", "-rm", "--propagation", "private"] + me
    env = dict(os.environ, **extra)
    full = outer + cmd + step["argv"]
    if "pty" in step:
        rc, out, err = tcu.run_pty(full, env, run, step["pty"], timeout=timeout)
    else:
        p = subprocess.run(full, env=env, cwd=run, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        rc, out, err = p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    return rc, out, err


def normalize(text, run, selfcmd):
    """What may differ between the builds and the runs: the scratch path,
    and how flashpod re-invokes itself (python -m flashpod + PYTHONPATH vs
    the binary's path)."""
    import re
    # the self command first: Python's wrapper lives under the scratch path
    for s in selfcmd:
        text = text.replace(s, "<flashpod>")
    text = text.replace(run, "<host>")
    # the post-flash init's mount point: tempfile.mkdtemp's random name
    text = re.sub(r"/tmp/flashpod-init-[a-z0-9_]+", "<initdir>", text)
    # as a JSON list element ("PYTHONPATH=...", ) or in plain text
    text = re.sub(r'"PYTHONPATH=[^"]*", ', "", text)
    text = re.sub(r"PYTHONPATH=\S+ ?", "", text)
    return text


def disk_digest(image, mounted):
    """The disk's sha256, or, once the kernel has written it through a mount,
    a digest of what its FAT holds (every path, directory flag, size and
    content): the kernel stamps entries with the real clock, which neither
    build controls, so the raw bytes differ between any two runs."""
    if not mounted:
        return tcu.sha(image)
    if TREE not in sys.path:
        sys.path.insert(0, TREE)
    from flashpod import fatfs, target
    h = hashlib.sha256()
    with open(image, "rb") as f:
        fat = target.open_image_fat(f)

        def walk(path):
            for e in sorted(fat.listdir(path), key=lambda e: e.name):
                p = path + "/" + e.name if path else e.name
                isdir = bool(e.attr & fatfs.ATTR_DIRECTORY)
                h.update(("%s %d %d\n" % (p, isdir, e.size)).encode())
                if isdir:
                    walk(p)
                else:
                    h.update(hashlib.sha256(fat.read_file(p)).digest())
        walk("")
    return "fat:" + h.hexdigest()


def run_scenario(name, sc, mode, cards, build, rust, root):
    run = os.path.join(root, "%s-%s-%s" % (name, mode, build))
    st = build_host(run, sc["host"], cards, mode, None)
    if build == "python":
        runner = os.path.join(run, "runner.py")
        with open(runner, "w") as f:
            f.write(RUNNER)
        wrapper = os.path.join(run, "bin", "flashpod")
        with open(wrapper, "w") as f:
            # the runner's own settings, which sudo's env_reset would drop
            f.write("#!/bin/sh\nexec env FP_HOST=%s FP_TREE=%s PYTHONDONTWRITEBYTECODE=1 %s %s \"$@\"\n"
                    % (run, TREE, sys.executable, runner))
        os.chmod(wrapper, 0o755)
        selfcmd = [os.path.join(run, "bin", "flashpod")]
    else:
        os.symlink(os.path.abspath(rust), os.path.join(run, "bin", "flashpod"))
        selfcmd = [os.path.realpath(rust)]
    recs = []
    mounted = False

    def back(text):
        if build != "rust" or not LAYOUT_1:
            return text
        text, n = layout1.normalize_calls(text)
        NORMALIZED[0] += n
        return text
    try:
        for i, step in enumerate(sc["steps"]):
            if mode not in step.get("modes", (mode,)):
                continue
            open(os.path.join(run, "calls.log"), "w").close()
            step = dict(step, argv=[a.format(fw=FIXTURE_FW, ipod_img=cards["ipod"], blank_img=cards["blank"],
                                             burntail_img=cards["burntail"])
                                    for a in step["argv"]])
            if build == "rust" and LAYOUT_1:
                step = dict(step, argv=layout1.argv_1_0(step["argv"]))
            rc, out, err = run_step(run, step, ["flashpod"])
            if build == "rust" and LAYOUT_1:
                (out, n1), (err, n2) = layout1.normalize(out), layout1.normalize(err)
                NORMALIZED[0] += n1 + n2
            with open(os.path.join(run, "calls.log")) as f:
                calls = [json.loads(l) for l in f if l.strip()]
            with open(os.path.join(run, "state.json")) as f:
                state = json.load(f)
            mounted = mounted or any(c["tool"] == "mount" or c["tool"] == "udisksctl" and c["argv"][:1] == ["mount"]
                                     for c in calls)
            recs.append({
                "step": i, "argv": step["argv"], "rc": rc,
                "stdout": normalize(out, run, selfcmd), "stderr": normalize(err, run, selfcmd),
                "calls": ["%s %s uid=%d" % (c["tool"], back(normalize(json.dumps(c["argv"]), run, selfcmd)), c["euid"])
                          for c in calls],
                "mounts": [(m["dev"], m["mnt"]) for m in state["mounts"]],
                "gone": [d["name"] for d in state["disks"] if d.get("gone")],
                "disks": {d["name"]: disk_digest(d["image"], mounted) for d in state["disks"]},
            })
    finally:
        teardown(st)
    return recs


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--enter":
        run, who = sys.argv[2], sys.argv[3]
        argv = sys.argv[sys.argv.index("--") + 1:]
        enter(run, who == "root", argv)
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--layout-1", action="store_true",
                    help="the Rust side runs the 1.0 command names (music ls, firmware write, ...); "
                         "1.0 names in its output and recorded calls are read back first")
    ap.add_argument("--mode", choices=("root", "user"),
                    default="root" if os.geteuid() == 0 else "user")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--mutants", metavar="DIR",
                    help="after the comparison, run every binary in DIR (each a deliberately broken "
                         "build) against the same Python results; each must differ somewhere")
    opts = ap.parse_args()
    global LAYOUT_1
    LAYOUT_1 = opts.layout_1
    if opts.mode == "root" and os.geteuid() != 0:
        sys.exit("--mode root needs root (sudo)")
    base = "/var/tmp" if os.path.isdir("/var/tmp") else None
    root = tempfile.mkdtemp(prefix="fp-fakehost-", dir=base)
    # 0750 root:<user's group>: the set-uid sudo stand-in lives in here, so
    # no other account may reach it while the run lasts
    if os.geteuid() == 0:
        os.chown(root, 0, USER_GID)
    os.chmod(root, 0o750)
    try:
        fx = os.path.join(root, "fixtures")
        os.makedirs(fx)
        music = synth.make_music(os.path.join(fx, "music"))
        fw = synth.write_firmware(os.path.join(fx, "fw.bin"))
        global FIXTURE_FW
        FIXTURE_FW = fw
        cards = make_cards(fx, fw, music)
        chosen = [(name, sc) for name, sc in SCENARIOS.items()
                  if (not opts.only or name in opts.only) and opts.mode in sc["modes"]]
        ref = {}

        def compare(rust, label, dump=False):
            bad, n = [], 0
            for name, sc in chosen:
                # progress on stderr: a step waiting at a prompt takes its full timeout
                sys.stderr.write("%s%s ...\n" % (label, name))
                sys.stderr.flush()
                if name not in ref:
                    ref[name] = run_scenario(name, sc, opts.mode, cards, "python", None, root)
                a = ref[name]
                b = run_scenario(name, sc, opts.mode, cards, "rust", rust, root)
                shutil.rmtree(os.path.join(root, "%s-%s-rust" % (name, opts.mode)), ignore_errors=True)
                for ra, rb in zip(a, b):
                    n += 1
                    if dump:
                        print("==== %s step %d %s rc=%s" % (name, ra["step"], ra["argv"], ra["rc"]))
                        print(ra["stdout"] + ra["stderr"])
                        print("calls:", *ra["calls"], sep="\n  ")
                    for key in ("rc", "stdout", "stderr", "calls", "mounts", "gone", "disks"):
                        if ra[key] != rb[key]:
                            bad.append((name, ra["step"], ra["argv"], key, ra[key], rb[key]))
                            break
            return bad, n

        bad, n = compare(opts.rust, "", opts.dump)
        missed = []
        if opts.mutants:
            for m in sorted(os.listdir(opts.mutants)):
                mbad, _ = compare(os.path.join(opts.mutants, m), "[mutant %s] " % m)
                if mbad:
                    name, i, argv, key = mbad[0][:4]
                    print("mutant %s: CAUGHT (%d steps differ; first %s step %d, %s)" % (m, len(mbad), name, i, key))
                else:
                    print("mutant %s: MISSED" % m)
                    missed.append(m)
        if not bad:
            print("IDENTICAL: %d steps on the fake host (%s mode)" % (n, opts.mode))
            if LAYOUT_1:
                print("  layout 1.0: the Rust side ran the 1.0 command names; %d output line(s) or "
                      "recorded call(s) had their 1.0 names read back" % NORMALIZED[0])
            return 1 if missed else 0
        print("DIFFERENT: %d of %d steps (%s mode)" % (len(bad), n, opts.mode))
        for name, i, argv, key, x, y in bad:
            print("  %s step %d %s: %s differs" % (name, i, " ".join(argv), key))
            if isinstance(x, str):
                import difflib
                for l in difflib.unified_diff(x.splitlines(), y.splitlines(), "python", "rust", lineterm="", n=1):
                    print("    " + l)
            else:
                print("    python: %r\n    rust:   %r" % (x, y))
        return 1
    finally:
        if not opts.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
