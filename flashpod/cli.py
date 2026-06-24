#!/usr/bin/env python3
"""flashpod - manage music on the iPod.

Subcommands:
  flashpod ls (or: list)          artist/album tree with track counts
  flashpod ls all                 same tree, with every track listed (ids shown)
  flashpod ls artist|album        flat per-artist or per-album track counts
  flashpod add [path1 path2 ...]  add audio files; directories are scanned
                                  recursively (tags read via mutagen);
                                  with no paths, prompts for one
  flashpod rm id [id ...]         remove tracks by id (see `flashpod ls`)
  flashpod rm artist|album <name> remove all tracks by an artist / in an album
  flashpod init [name]            create iPod_Control structure + empty DB
  flashpod flash [/dev/sdX]       write iPod firmware + partition layout to a
                                  CF/SD card (1G/2G iPod; needs sudo)

The mountpoint is auto-detected from mounted filesystems (FAT-family
mounts under /media, ranked by iPod_Control presence), and a detected
mount is always confirmed first: Y/n for a single candidate, a numbered
chooser for several. Non-interactive runs must pass --mount.
The iTunesDB is read/written natively (itunesdb.py) — no libgpod.
"""

import argparse
import collections
import errno
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

import mutagen

from . import fatfs
from . import ipod_flash
from . import itunesdb
from . import platform
from . import resources

FIRMWARE_MANIFEST = resources.firmware_manifest()


def _load_manifest():
    """Parse the bundled firmware catalog, or print why we can't."""
    try:
        with open(FIRMWARE_MANIFEST) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        print(f"flashpod flash: firmware manifest unusable ({exc}); pass --firmware",
              file=sys.stderr)
        return None


def choose_firmware(manifest):
    """Pick a firmware entry (dict) from the manifest. Interactive chooser on
    a tty (default preselected); non-tty uses the default outright. The .ipsw
    itself is fetched later by ensure_firmware()."""
    entries = manifest.get("firmwares") or []
    if not entries:
        print("flashpod flash: no firmware listed in the manifest; pass --firmware",
              file=sys.stderr)
        return None
    default = next((i for i, e in enumerate(entries) if e.get("default")), 0)
    if not sys.stdin.isatty():
        e = entries[default]
        print(f"flashpod flash: using default firmware {e['file']} "
              f"({e['generation']}, {e['version']})", file=sys.stderr)
        return e
    print("Available firmware:")
    for i, e in enumerate(entries):
        mark = "  [default]" if i == default else ""
        size = " (~%.1f MiB)" % (e["size"] / (1 << 20)) if e.get("size") else ""
        print(f"  [{i}] {e['generation']} — version {e['version']}{mark}\n"
              f"      {e['description']}{size}")
    try:
        choice = input(f"Select firmware [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        choice = str(default)
    if not (choice.isdigit() and int(choice) < len(entries)):
        print("flashpod flash: invalid selection", file=sys.stderr)
        return None
    return entries[int(choice)]


def _firmware_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or \
        os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "flashpod", "firmware")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dst, total=None):
    """Stream `url` to `dst` with a progress line. Raises on network/IO error."""
    req = urllib.request.Request(url, headers={"User-Agent": "flashpod"})
    with urllib.request.urlopen(req) as resp, open(dst, "wb") as out:
        total = total or int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if sys.stderr.isatty():
                now = time.monotonic()
                if now - last >= 0.2:
                    pct = "%d%% " % (done * 100 // total) if total else ""
                    sys.stderr.write("\r  %s(%.1f/%.1f MiB)   "
                                     % (pct, done / (1 << 20),
                                        (total or done) / (1 << 20)))
                    sys.stderr.flush()
                    last = now
        if sys.stderr.isatty():
            sys.stderr.write("\n")


def ensure_firmware(entry, base_url):
    """Resolve a manifest entry to a local .ipsw path: prefer a copy shipped
    inside the build, then the verified cache, else download + verify + cache.
    Returns the path or None (with an actionable message) on any failure."""
    name = entry["file"]
    want = entry.get("sha256")

    # Shipped inside the binary? A "heavy" build bundles the .ipsw next to the
    # manifest for fully-offline use — e.g. the macOS 10.8 release, where the
    # HTTPS download can't negotiate the TLS that GitHub requires.
    bundled = os.path.join(resources.firmware_dir(), name)
    if os.path.exists(bundled) and (not want or _sha256(bundled) == want):
        return bundled

    dst = os.path.join(_firmware_cache_dir(), name)
    url = entry.get("url") or (base_url.rstrip("/") + "/" + name if base_url else None)

    if os.path.exists(dst):
        if not want or _sha256(dst) == want:
            return dst
        print(f"flashpod flash: cached {name} failed checksum; re-downloading",
              file=sys.stderr)
        try:
            os.remove(dst)
        except OSError:
            pass

    if not url:
        print(f"flashpod flash: no download URL for {name}; pass --firmware",
              file=sys.stderr)
        return None
    try:
        os.makedirs(_firmware_cache_dir(), exist_ok=True)
    except OSError as exc:
        print(f"flashpod flash: cannot create cache dir: {exc}", file=sys.stderr)
        return None

    print(f"flashpod flash: downloading {name}\n  from {url}", file=sys.stderr)
    tmp = dst + ".part"
    try:
        _download(url, tmp, entry.get("size"))
    except (urllib.error.URLError, OSError) as exc:
        _rm(tmp)
        print(f"flashpod flash: download failed ({exc}).\n"
              f"  Download it yourself from {url} and pass it with --firmware.",
              file=sys.stderr)
        return None
    if want and _sha256(tmp) != want:
        _rm(tmp)
        print(f"flashpod flash: {name} failed checksum verification — refusing "
              f"to use it.\n  Re-run to retry, or download from {url} and pass "
              f"--firmware.", file=sys.stderr)
        return None
    os.replace(tmp, dst)
    print(f"flashpod flash: verified and cached {name}", file=sys.stderr)
    return dst


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def load_library(mount):
    """Parse the iTunesDB, or print why we can't and return None."""
    try:
        return itunesdb.load(mount)
    except FileNotFoundError:
        if not os.path.isdir(mount):
            print(f"flashpod: {mount} does not exist — is the iPod "
                  "mounted?", file=sys.stderr)
        else:
            print(f"flashpod: no iTunesDB on {mount} "
                  "(run `flashpod init` first?)", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(f"flashpod: failed to read iTunesDB: {exc}", file=sys.stderr)
    return None


def _effective_xfer():
    """The transfer size (in sectors) that will actually be used for reads and
    writes, after the platform default and the FLASHPOD_RAW_MAX_XFER override."""
    plat = platform.current()
    return max(1, int(os.environ.get("FLASHPOD_RAW_MAX_XFER", plat.raw_max_xfer())))


def open_raw_fat(device, writable=False):
    """Open `device` with the userspace FAT driver and return a Fat32. Accepts
    either the data PARTITION node (/dev/rdisk1s2, /dev/sdb2 — boot sector at
    LBA 0) or the whole DISK (/dev/rdisk1, /dev/sdb, /dev/disk1), in which case
    we read the MBR and seek to the FAT (type 0x0b/0x0c) partition ourselves.
    On macOS a /dev/diskN path is mapped to the unbuffered /dev/rdiskN — reading
    the buffered node re-introduces the read-ahead the FireWire bridge corrupts.
    Transfer size (reads and writes) defaults to the platform's safe ceiling
    (8 sectors on Linux, 1 on macOS where the bridge corrupts anything larger in
    BOTH directions); FLASHPOD_RAW_MAX_XFER overrides it (raise on a USB reader,
    which has no bridge)."""
    plat = platform.current()
    max_xfer = _effective_xfer()
    node = plat.raw_read_node(device)
    dev = fatfs.BlockDev(node, part_start=0, max_xfer=max_xfer, writable=writable)
    boot = dev.read(0, 1)
    is_fat = boot[82:85] == b"FAT" and boot[510:512] == b"\x55\xaa"
    if not is_fat and boot[510:512] == b"\x55\xaa":
        # whole-disk MBR: locate the FAT data partition and seek into it
        for poff in (446, 462, 478, 494):
            if boot[poff + 4] in (0x0b, 0x0c):
                start = int.from_bytes(boot[poff + 8:poff + 12], "little")
                if start:
                    dev.part_start = start
                    break
    return fatfs.Fat32(dev)


def _self_cmd():
    """Argv prefix that re-invokes this same flashpod, however it was started:
    a PyInstaller binary, `python -m flashpod`, or an installed console script."""
    if getattr(sys, "frozen", False):              # PyInstaller one-file binary
        return [sys.executable]
    if os.path.basename(sys.argv[0] or "") == "__main__.py":   # python -m flashpod
        return [sys.executable, "-m", "flashpod"]
    return [sys.argv[0]]                            # ./flashpod / installed script


def _sudo_reexec(extra):
    """Re-exec this same flashpod under sudo with ``extra`` args appended
    (prompting for the password on a terminal). REPLACES the process and never
    returns on success; returns only if it can't elevate (non-tty / no sudo)."""
    if os.name == "nt" or not sys.stdin.isatty():
        return                                     # can't prompt — caller handles it
    # sudo resets the environment, so the FLASHPOD_* tuning knobs the user set
    # would be lost across elevation. Re-assert them in the child via `env`.
    passthru = ["%s=%s" % (k, v) for k, v in sorted(os.environ.items())
                if k.startswith("FLASHPOD_")]
    # A `pip install --user` puts the flashpod package under ~/.local/lib/...,
    # which is NOT importable once sudo re-runs the script as root: sudo resets
    # HOME to /root, so that user's site-packages drops off sys.path and the
    # child dies with ModuleNotFoundError. Hand the package's parent directory
    # (its site-packages) to the child via PYTHONPATH so the import survives
    # elevation. The frozen PyInstaller binary bundles its own modules and needs
    # none of this.
    if not getattr(sys, "frozen", False):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        existing = os.environ.get("PYTHONPATH", "")
        passthru.append("PYTHONPATH=" + (pkg_dir + os.pathsep + existing
                                         if existing else pkg_dir))
    prefix = ["sudo"] + (["env"] + passthru if passthru else [])
    cmd = prefix + _self_cmd() + extra
    try:
        os.execvp("sudo", cmd)                      # replaces this process
    except OSError as exc:
        print(f"flashpod: couldn't run sudo ({exc}); re-run it yourself:\n"
              f"  {' '.join(cmd)}", file=sys.stderr)




def _diagnose_missing_db(fs, device):
    """The FAT mounted but iPod_Control/iTunes/iTunesDB didn't resolve. Walk the
    path one component at a time and report exactly where it breaks, so we can
    tell a freshly-flashed (empty) card from a real directory-traversal problem."""
    def names(path):
        try:
            entries = fs.listdir(path)
        except (OSError, ValueError):
            return None
        return None if entries is None else [e.name for e in entries]

    root = names("")
    if root is not None and not any(n.casefold() == "ipod_control" for n in root):
        print(f"flashpod: {device} has no iPod_Control directory — it looks "
              f"freshly flashed/formatted, not yet set up.", file=sys.stderr)
        print("  root contains: " + (", ".join(root) if root else "(empty)"),
              file=sys.stderr)
        print("  Initialize it (on Linux: `flashpod init`), then add music.",
              file=sys.stderr)
        return
    # iPod_Control exists — find which deeper component is missing.
    for path in ("iPod_Control", "iPod_Control/iTunes"):
        listing = names(path)
        if listing is None:
            print(f"flashpod: {device}: '{path}' didn't resolve as a directory "
                  f"— unexpected; this may be a read bug. Please report.",
                  file=sys.stderr)
            return
        print(f"  {path}/ contains: " +
              (", ".join(listing) if listing else "(empty)"), file=sys.stderr)
    print(f"flashpod: no iTunesDB on {device} (iPod_Control is present but "
          f"iPod_Control/iTunes/iTunesDB is missing). If this iPod should have "
          f"music, that's unexpected — otherwise run `flashpod init`.",
          file=sys.stderr)


def load_library_raw(device):
    """Read the iTunesDB straight off `device` via the userspace FAT driver,
    no OS mount — for iPods the OS can't mount (e.g. over the flaky FireWire
    bridge on macOS). Returns a Library, or prints why and returns None."""
    try:
        fs = open_raw_fat(device)
        data = fs.read_file("iPod_Control/iTunes/iTunesDB")
    except PermissionError:
        print(f"flashpod: need root to read {device}.\n"
              f"  sudo {' '.join(_self_cmd())} ls --raw {device}",
              file=sys.stderr)
        return None
    except (OSError, ValueError) as exc:
        print(f"flashpod: couldn't read a FAT filesystem on {device}: {exc}\n"
              "(pass the iPod's data partition, e.g. /dev/rdisk1s2, or its "
              "whole disk /dev/rdisk1)", file=sys.stderr)
        return None
    if data is None:
        _diagnose_missing_db(fs, device)
        return None
    try:
        return itunesdb.parse_bytes(data)
    except (ValueError, IndexError, struct.error) as exc:
        print(f"flashpod: failed to parse iTunesDB from {device}: {exc}",
              file=sys.stderr)
        return None


class RawTarget:
    """An iPod managed DIRECTLY over its raw device with the userspace FAT
    driver — no OS mount. Provides the read/write operations the data commands
    need (init/add/rm/ls), so the same command cores work over a mount or over
    raw. ``fs`` is a writable :class:`fatfs.Fat32`."""
    DB = "iPod_Control/iTunes/iTunesDB"

    def __init__(self, fs, node, desc=""):
        self.fs = fs
        self.node = node
        self.desc = desc

    # -- library -----------------------------------------------------------
    def load_library(self):
        data = self.fs.read_file(self.DB)
        return itunesdb.parse_bytes(data) if data is not None else None

    def save_library(self, lib):
        self.fs.write_file(self.DB, itunesdb.serialize(lib))
        self.fs.sync()

    # -- init --------------------------------------------------------------
    def _makedirs(self, path):
        cur = ""
        for part in path.split("/"):
            cur = cur + "/" + part if cur else part
            if not self.fs.exists(cur):
                self.fs.mkdir(cur)

    def init_structure(self, name):
        for sub in ["iTunes", "Device"] + ["Music/F%02d" % i for i in range(50)]:
            self._makedirs("iPod_Control/" + sub)
        self.save_library(itunesdb.Library(name))
        self.fs.sync()

    # -- add ---------------------------------------------------------------
    def copy(self, src, progress=None, ext=None):
        """Mirror itunesdb.copy_to_ipod over the raw FAT: spread across the
        Music/F## dirs, collision-proof name, return the ':'-style location."""
        import random
        music = "iPod_Control/Music"
        entries = self.fs.listdir(music) or []
        fdirs = sorted(e.name for e in entries if e.name.upper().startswith("F"))
        if not fdirs:
            raise OSError("no Music/F## directories (run init first?)")
        fdir = random.choice(fdirs)
        ext = (ext or os.path.splitext(src)[1] or ".mp3").lower()
        while True:
            name = "fp%06d%s" % (random.randrange(10 ** 6), ext)
            dst = "%s/%s/%s" % (music, fdir, name)
            if not self.fs.exists(dst):
                break
        with open(src, "rb") as f:
            data = f.read()
        self.fs.write_file(dst, data, progress=progress)
        return ":".join(["", "iPod_Control", "Music", fdir, name])

    # -- rm ----------------------------------------------------------------
    def remove_location(self, location):
        if not location:
            return
        path = "/".join(p for p in location.split(":") if p)
        if self.fs.exists(path):
            self.fs.remove(path)


def open_raw_target(device, writable=True):
    """Open `device` as a writable RawTarget, or print why and return None."""
    try:
        fs = open_raw_fat(device, writable=writable)
    except PermissionError:
        print(f"flashpod: need root to write {device}.\n"
              f"  sudo {' '.join(_self_cmd())} ... --raw {device}",
              file=sys.stderr)
        return None
    except (OSError, ValueError) as exc:
        print(f"flashpod: couldn't open a FAT filesystem on {device}: {exc}",
              file=sys.stderr)
        return None
    return RawTarget(fs, device)


def cmd_init_raw(target, name):
    target.init_structure(name)
    print(f"Initialized iPod directory structure on {target.node}")
    return 0


def cmd_add_raw(target, paths):
    return _cmd_add_core(
        paths,
        load=target.load_library,
        copy=target.copy,
        save=target.save_library)


def cmd_rm_raw(target, what):
    lib = target.load_library()
    if lib is None:
        print(f"flashpod: no iTunesDB on {target.node} (run `flashpod init` "
              "first?)", file=sys.stderr)
        return 1
    victims, rc = _rm_victims(lib, what)
    if victims is None:
        return rc
    for t in victims:
        try:
            target.remove_location(t.location)
        except OSError as exc:
            print(f"flashpod: couldn't remove {t.location}: {exc}",
                  file=sys.stderr)
        lib.tracks.remove(t)
        print(f"Removed: {orunknown(t.artist)} - {orunknown(t.title)}")
    if len(victims) > 1:
        print(f"Removed {len(victims)} tracks")
    target.save_library(lib)
    return 0


def first(tags, key):
    val = tags.get(key)
    if isinstance(val, list):
        val = val[0] if val else None
    return str(val) if val else None


AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff"}

# Test hook: point at a fake mounts table.
MOUNTS_FILE = os.environ.get("FLASHPOD_MOUNTS_FILE", "/proc/mounts")


def candidate_mounts():
    """Plausible iPod mountpoints from the OS mount table, best first.
    Scoring: contains iPod_Control +10, 'ipod' in the name +5, under a
    removable-media root (/media, /run/media, /Volumes) +1; score 0 entries
    are dropped. The mount table comes from the platform backend, so this is
    the same on Linux, macOS, and Windows."""
    cands = []
    for dev, mnt, fstype in platform.current().mounted_filesystems():
        ft = fstype.lower()
        if not ("fat" in ft or "msdos" in ft or "hfs" in ft):
            continue
        score = 0
        if os.path.isdir(os.path.join(mnt, "iPod_Control")):
            score += 10
        if "ipod" in os.path.basename(mnt.rstrip("/\\")).lower():
            score += 5
        if mnt.startswith(("/media/", "/run/media/", "/Volumes/")):
            score += 1
        if score:
            cands.append((score, mnt))
    cands.sort(key=lambda c: -c[0])
    return cands


def unmounted_candidates():
    """iPod-looking FAT partitions that are attached but not mounted.
    Scoring: 'ipod' in label +5, FireWire transport +5, removable/USB +1;
    score 0 (e.g. an EFI partition on an internal disk) is dropped."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,FSTYPE,LABEL,TRAN,RM,HOTPLUG,MOUNTPOINT"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    cands = []

    def walk(node, tran):
        tran = node.get("tran") or tran or ""
        if (node.get("type") == "part" and not node.get("mountpoint")
                and (node.get("fstype") or "") in ("vfat", "exfat", "hfsplus")):
            label = node.get("label") or ""
            score = 0
            if "ipod" in label.lower():
                score += 5
            if tran in ("sbp", "ieee1394"):  # FireWire: almost surely the iPod
                score += 5
            if node.get("rm") or node.get("hotplug") or tran == "usb":
                score += 1
            if score:
                cands.append((score, "/dev/" + node["name"], label, tran))
        for child in node.get("children") or []:
            walk(child, tran)

    for dev in json.loads(out)["blockdevices"]:
        walk(dev, None)
    cands.sort(key=lambda c: -c[0])
    return cands


def _sudo_mount(dev, label):
    """Mount `dev` with a privileged `mount` when udisks is unavailable. On a
    terminal, sudo prompts for the password. The FAT volume is mounted with
    the invoking user's uid/gid so they can read and write. Returns the
    mountpoint or None."""
    import pwd
    import shlex
    user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    name = (label or "").strip() or "IPOD"
    mountpoint = "/media/%s/%s" % (user, name)
    uid, gid = os.getuid(), os.getgid()
    print("flashpod: udisks unavailable; mounting %s via sudo "
          "(you may be prompted for your password)..." % dev, file=sys.stderr)
    # one sudo invocation -> a single password prompt; mkdir is idempotent
    script = "mkdir -p %s && mount -o uid=%d,gid=%d %s %s" % (
        shlex.quote(mountpoint), uid, gid, shlex.quote(dev), shlex.quote(mountpoint))
    if subprocess.run(["sudo", "sh", "-c", script]).returncode != 0:
        print("flashpod: sudo mount of %s failed" % dev, file=sys.stderr)
        return None
    print("Mounted %s at %s" % (dev, mountpoint), file=sys.stderr)
    return mountpoint


def mount_device(dev, label=None):
    """Mount a partition and return its mountpoint, or None.

    Tries udisks first (no root needed); if the udisks daemon is missing or
    unresponsive (a real failure mode on this machine — the FireWire iPod can
    leave it timing out), falls back to `sudo mount`, which prompts for the
    password on a terminal."""
    res = None
    try:
        res = subprocess.run(["udisksctl", "mount", "-b", dev],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    except FileNotFoundError:
        pass                              # no udisksctl -> go straight to sudo
    except subprocess.TimeoutExpired:
        print("flashpod: udisks timed out.", file=sys.stderr)
    if res is not None and res.returncode == 0:
        # "Mounted /dev/sdb2 at /media/david/IPOD" (older udisks: trailing ".")
        m = re.search(r" at (.+?)\.?\s*$", res.stdout)
        if m:
            print(res.stdout.strip(), file=sys.stderr)
            return m.group(1)
        print(f"flashpod: mounted {dev} but couldn't parse the mountpoint; "
              f"pass --mount", file=sys.stderr)
        return None
    if res is not None:
        print(f"flashpod: udisks mount failed: {(res.stderr or res.stdout).strip()}",
              file=sys.stderr)
    # Fall back to a privileged mount, but only where we can prompt.
    if not sys.stdin.isatty():
        print("flashpod: cannot prompt for a sudo password here; mount the "
              "partition manually and pass --mount.", file=sys.stderr)
        return None
    return _sudo_mount(dev, label)


def _device_for_mount(mount):
    """Block device currently mounted at `mount`, or None."""
    for dev, mp, _fs in platform.current().mounted_filesystems():
        if mp == mount:
            return dev
    return None


def remount_as_user(mount):
    """A detected iPod mount we can't read — typically left mounted by root
    from an earlier `sudo` run (e.g. /media/root/IPOD). Offer to unmount it
    and remount the device as the current user via the sudo fallback, so the
    iTunesDB and music files are readable/writable. Returns the new
    mountpoint or None."""
    dev = _device_for_mount(mount)
    if not sys.stdin.isatty():
        hint = f"sudo umount {mount}" + ("" if dev else "")
        print(f"flashpod: {mount} is mounted by another user and not readable "
              f"here; unmount it ({hint}) and re-run, or pass --mount.",
              file=sys.stderr)
        return None
    if not dev:
        print(f"flashpod: {mount} isn't readable and its device couldn't be "
              f"determined; unmount it and re-run.", file=sys.stderr)
        return None
    try:
        ans = input(f"{mount} is mounted by another user and not readable. "
                    f"Remount {dev} as you? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if ans.strip().lower() not in ("", "y", "yes"):
        return None
    label = os.path.basename(mount.rstrip("/")) or "IPOD"
    print(f"flashpod: unmounting {mount} (sudo)...", file=sys.stderr)
    if subprocess.run(["sudo", "umount", mount]).returncode != 0:
        print(f"flashpod: could not unmount {mount}.", file=sys.stderr)
        return None
    return _sudo_mount(dev, label)


def _report_io_error(mount, exc):
    """Print a clean, actionable message for an OSError hitting the iPod,
    instead of letting a traceback escape."""
    if getattr(exc, "errno", None) == errno.EIO:
        print(f"flashpod: I/O error talking to the iPod at {mount} — it likely "
              f"disconnected, or the cable/connector is flaky. Reconnect it, "
              f"remount, and retry. If it persists the filesystem may be "
              f"damaged (reformat with `flashpod flash`).", file=sys.stderr)
    else:
        print(f"flashpod: cannot access {mount}: {exc}", file=sys.stderr)


def _attached_ipod_count():
    """Best-effort count of attached Apple iPod disks (Linux/lsblk). Returns
    None when it can't tell."""
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TYPE,VENDOR,MODEL,LABEL"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    n = 0
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        ident = (" ".join(str(d.get(k) or "") for k in ("vendor", "model"))).lower()
        labels = " ".join((c.get("label") or "") for c in (d.get("children") or [])).upper()
        if "ipod" in ident or "apple" in ident or "IPOD" in labels:
            n += 1
    return n


def offer_mount(announce_empty=True):
    """No mounted iPod found: look for an attached, unmounted one and
    offer to mount it. Returns the mountpoint or None. With
    ``announce_empty=False`` the "nothing found" message is suppressed (the
    caller has a further fallback to try, e.g. a direct raw read)."""
    cands = unmounted_candidates()
    if not cands:
        if announce_empty:
            print("flashpod: no iPod-like mounts found (is it plugged in?), "
                  "or pass --mount", file=sys.stderr)
        return None
    if not sys.stdin.isatty():
        print("flashpod: found unmounted iPod-like partitions but can't ask "
              "to mount them here; mount one (udisksctl mount -b <dev>) "
              "and pass --mount:", file=sys.stderr)
        for _, dev, label, tran in cands:
            print(f"  {dev}  label={label or '-'} tran={tran or '-'}",
                  file=sys.stderr)
        return None

    def describe(c):
        _, dev, label, tran = c
        bits = [b for b in (label, tran) if b]
        return f"{dev}" + (f" ({', '.join(bits)})" if bits else "")

    if len(cands) == 1:
        try:
            ans = input(f"Found unmounted iPod partition {describe(cands[0])}"
                        f" — mount it? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans.strip().lower() not in ("", "y", "yes"):
            return None
        return mount_device(cands[0][1], cands[0][2])
    print("Unmounted iPod-like partitions:")
    for i, c in enumerate(cands):
        print(f"  [{i}] {describe(c)}")
    try:
        choice = input("Mount which? [0] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        return mount_device(cands[0][1], cands[0][2])
    if choice.isdigit() and int(choice) < len(cands):
        return mount_device(cands[int(choice)][1], cands[int(choice)][2])
    print("flashpod: invalid selection", file=sys.stderr)
    return None


def firewire_queue_problem(mount):
    """Early iPod FireWire bridges crash on large or queued reads; the
    kernel's default block-queue settings are therefore data-eating for
    them, and they reset on every re-attach.
    If `mount` is backed by a FireWire disk with unsafe settings, return
    (disk, [problems]); otherwise None."""
    dev = None
    try:
        f = open(MOUNTS_FILE)
    except OSError:  # no /proc/mounts (macOS) -> no Linux queue to pin
        return None
    with f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mnt = re.sub(r"\\([0-7]{3})",
                             lambda m: chr(int(m.group(1), 8)), parts[1])
                if mnt == mount:
                    dev = parts[0]
    if not dev or not dev.startswith("/dev/"):
        return None
    disk = re.sub(r"p?\d+$", "", os.path.basename(dev))  # sdb2 -> sdb
    res = subprocess.run(["lsblk", "-dno", "TRAN", "/dev/" + disk],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if res.returncode != 0 or res.stdout.strip() not in ("sbp", "ieee1394"):
        return None
    bad = []
    try:
        msk = int(open(f"/sys/block/{disk}/queue/max_sectors_kb").read())
        rak = int(open(f"/sys/block/{disk}/queue/read_ahead_kb").read())
    except (OSError, ValueError):
        return None
    if msk > 4:
        bad.append(f"max_sectors_kb={msk} (need 4)")
    if rak != 0:
        bad.append(f"read_ahead_kb={rak} (need 0)")
    try:
        qd = int(open(f"/sys/block/{disk}/device/queue_depth").read())
        if qd != 1:
            bad.append(f"queue_depth={qd} (need 1)")
    except (OSError, ValueError):
        pass
    return (disk, bad) if bad else None


def pin_firewire_queue(disk):
    """Write the safe queue settings (root or sudo). queue_depth is
    best-effort (not writable on every device)."""
    script = (f"echo 4 >/sys/block/{disk}/queue/max_sectors_kb && "
              f"echo 0 >/sys/block/{disk}/queue/read_ahead_kb && "
              f"{{ [ ! -w /sys/block/{disk}/device/queue_depth ] || "
              f"echo 1 >/sys/block/{disk}/device/queue_depth; }}")
    cmd = ["sh", "-c", script]
    if os.geteuid() != 0:
        print(f"flashpod: pinning safe FireWire I/O settings on {disk} "
              "(needs sudo)...", file=sys.stderr)
        # -n in scripts: succeed only with NOPASSWD, never hang on a prompt
        cmd = (["sudo"] if sys.stdin.isatty() else ["sudo", "-n"]) + cmd
    try:
        return subprocess.run(cmd).returncode == 0
    except (OSError, KeyboardInterrupt):
        return False


def scan_for_ipod(cands):
    """Probe each (node, desc) candidate with our FAT driver and keep the ones
    that actually hold an iPod database (iPod_Control/iTunes/iTunesDB) — the
    only reliable iPod fingerprint. Returns [(node, desc, library), ...]. Needs
    root (it reads raw devices); a candidate that isn't FAT, can't be read, or
    has no DB is silently skipped."""
    found = []
    for node, desc in cands:
        try:
            fs = open_raw_fat(node)
            data = fs.read_file("iPod_Control/iTunes/iTunesDB")
        except (OSError, ValueError):
            continue
        if data is None:
            continue
        try:
            lib = itunesdb.parse_bytes(data)
        except (ValueError, IndexError, struct.error):
            continue
        found.append((node, desc, lib))
    return found


def detect_ls_source(opts):
    """Resolve where `flashpod ls` reads from when neither --mount nor --raw
    was given. Returns ('mount', path), ('lib', Library), or None.

    Strategy: use an already-mounted iPod if there is one; otherwise SCAN —
    enumerate external disks with a FAT slice and read each to find the one
    whose FAT holds an iPod database (no labels, no bus guessing). Reading raw
    devices needs root, so if we're not root we re-exec under sudo first."""
    cands = candidate_mounts()
    if cands:
        mnt = _choose_mounted(cands)
        return ("mount", mnt) if mnt else None

    disks = platform.current().fat_disk_candidates()
    if not disks:
        # Nothing to scan. On Linux, fall back to offering to mount one.
        mnt = offer_mount(announce_empty=False)
        if mnt:
            return ("mount", mnt)
        print("flashpod: no iPod found — nothing mounted and no attached disk "
              "to scan. Plug it in, or pass --mount/--raw.", file=sys.stderr)
        return None

    # Reading the candidate FATs needs root — elevate once, then the root run
    # re-enters here and does the scan.
    if not platform.current().is_admin():
        print("flashpod: looking for an iPod means reading attached disks, "
              "which needs root — elevating via sudo...", file=sys.stderr)
        _sudo_reexec(_cmd_args(opts))              # replaces process if it can
        print("flashpod: couldn't get root to scan. Re-run with sudo, or pass "
              "--raw <device>. Candidate disk(s):", file=sys.stderr)
        for node, desc in disks:
            print(f"  {node}" + (f"  ({desc})" if desc else ""), file=sys.stderr)
        return None

    found = scan_for_ipod(disks)
    if not found:
        print(f"flashpod: scanned {len(disks)} disk(s); none held an iPod "
              "database (iPod_Control/iTunes/iTunesDB).", file=sys.stderr)
        for node, desc in disks:
            print(f"  checked {node}" + (f"  ({desc})" if desc else ""),
                  file=sys.stderr)
        return None
    if len(found) == 1:
        node, desc, lib = found[0]
        print(f"Found iPod on {node}" + (f" ({desc})" if desc else "") +
              f" — {len(lib.tracks)} tracks.", file=sys.stderr)
        return ("lib", lib)
    print("Multiple iPods found:")
    for i, (node, desc, lib) in enumerate(found):
        print(f"  [{i}] {node}" + (f"  ({desc})" if desc else "") +
              f"  — {len(lib.tracks)} tracks")
    try:
        choice = input("Read which? [0] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    idx = int(choice) if choice.isdigit() and int(choice) < len(found) else 0
    return ("lib", found[idx][2])


def _cmd_args(opts):
    """Reconstruct the subcommand + its positional args, to re-exec the same
    command under sudo."""
    c = opts.command
    if c in ("ls", "list"):
        return [c] + ([opts.field] if getattr(opts, "field", None) else [])
    if c == "add":
        return [c] + list(getattr(opts, "files", None) or [])
    if c in ("rm", "remove", "delete", "erase"):
        return [c] + list(getattr(opts, "what", None) or [])
    if c == "init":
        return [c] + ([opts.name] if getattr(opts, "name", None) else [])
    return [c]


def run_raw(opts, node):
    """Dispatch a data command (ls/add/rm/init) over the raw device ``node``,
    elevating with sudo first if we lack root."""
    cmd = opts.command
    if not platform.current().is_admin():
        print(f"flashpod: {cmd} over the raw device {node} needs root — "
              "elevating via sudo...", file=sys.stderr)
        _sudo_reexec(["--raw", node] + _cmd_args(opts))
        print(f"flashpod: couldn't get root for raw access to {node}.",
              file=sys.stderr)
        return 1
    if cmd in ("ls", "list"):
        lib = load_library_raw(node)
        return cmd_ls(lib, opts.field) if lib else 1
    target = open_raw_target(node, writable=True)
    if not target:
        return 1
    if cmd == "init":
        return cmd_init_raw(target, getattr(opts, "name", None) or "iPod")
    if cmd in ("rm", "remove", "delete", "erase"):
        return cmd_rm_raw(target, opts.what)
    if cmd == "add":
        return cmd_add_raw(target, opts.files or [prompt_for_path()])
    print(f"flashpod: --raw doesn't support `{cmd}`.", file=sys.stderr)
    return 1


def _choose_init_disk(disks):
    """For `init` without --mount/--raw: pick a FAT disk to initialize. Unlike
    add/rm there's no database to scan for, so we probe each candidate to label
    it (empty / already an iPod) and confirm before writing."""
    labelled = []
    for node, desc in disks:
        status = ""
        try:
            fs = open_raw_fat(node)
            if fs.read_file("iPod_Control/iTunes/iTunesDB") is not None:
                status = "ALREADY an iPod — init resets its database"
            elif fs.exists("iPod_Control"):
                status = "has iPod_Control but no database"
            else:
                status = "empty/freshly-flashed"
        except (OSError, ValueError):
            status = "unreadable FAT"
        labelled.append((node, desc, status))
    if len(labelled) == 1:
        node, desc, status = labelled[0]
        if not ask_yes(f"Initialize {node}" + (f" ({desc})" if desc else "") +
                       f" — {status}? [y/N] ", default_yes=False):
            return None
        return node
    print("Disks that could be initialized:")
    for i, (node, desc, status) in enumerate(labelled):
        print(f"  [{i}] {node}" + (f"  ({desc})" if desc else "") +
              f"  — {status}")
    try:
        choice = input("Initialize which? [none] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return labelled[int(choice)][0] if choice.isdigit() and \
        int(choice) < len(labelled) else None


def resolve_raw_target(opts):
    """Resolve a target for a WRITE command (add/rm/init) with no --mount/--raw.
    Returns ('mount', path), ('raw', node), or None. Mirrors detect_ls_source
    but selects a device to write to (and, for init, one without a database)."""
    cands = candidate_mounts()
    if cands:
        mnt = _choose_mounted(cands)
        return ("mount", mnt) if mnt else None
    disks = platform.current().fat_disk_candidates()
    if not disks:
        mnt = offer_mount(announce_empty=False)
        if mnt:
            return ("mount", mnt)
        print("flashpod: no iPod found — nothing mounted and no attached disk. "
              "Plug it in, or pass --mount/--raw.", file=sys.stderr)
        return None
    if not platform.current().is_admin():
        print(f"flashpod: {opts.command} over the iPod's raw device needs root "
              "— elevating via sudo...", file=sys.stderr)
        _sudo_reexec(_cmd_args(opts))
        print("flashpod: couldn't get root. Re-run with sudo, or pass "
              "--raw <device>. Candidate disk(s):", file=sys.stderr)
        for node, desc in disks:
            print(f"  {node}" + (f"  ({desc})" if desc else ""), file=sys.stderr)
        return None
    if opts.command == "init":
        node = _choose_init_disk(disks)
        return ("raw", node) if node else None
    found = scan_for_ipod(disks)
    if not found:
        print(f"flashpod: scanned {len(disks)} disk(s); none held an iPod "
              "database. For a fresh card, run `flashpod init` first.",
              file=sys.stderr)
        return None
    if len(found) == 1:
        node, desc, lib = found[0]
        print(f"Found iPod on {node}" + (f" ({desc})" if desc else "") +
              f" — {len(lib.tracks)} tracks.", file=sys.stderr)
        return ("raw", node)
    print("Multiple iPods found:")
    for i, (node, desc, lib) in enumerate(found):
        print(f"  [{i}] {node}  — {len(lib.tracks)} tracks")
    try:
        choice = input("Use which? [0] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    idx = int(choice) if choice.isdigit() and int(choice) < len(found) else 0
    return ("raw", found[idx][0])


def detect_mount():
    """Pick the iPod mountpoint when --mount wasn't given.
    Returns None (caller exits nonzero) if it can't."""
    cands = candidate_mounts()
    if not cands:
        return offer_mount()
    return _choose_mounted(cands)


def _choose_mounted(cands):
    """Interactive confirm/choose over already-mounted iPod candidates from
    :func:`candidate_mounts`. Returns the chosen mountpoint or None."""
    if not sys.stdin.isatty():
        # A guessed mount is never used unconfirmed, and we can't ask here.
        print("flashpod: no --mount given and not a terminal, so the detected "
              "mount can't be confirmed; pass --mount. Candidates:",
              file=sys.stderr)
        for _, mnt in cands:
            print(f"  {mnt}", file=sys.stderr)
        return None
    if len(cands) == 1:
        mnt = cands[0][1]
        try:
            ans = input(f"Using iPod mounted at {mnt} — continue? [Y/n] ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if ans.strip().lower() in ("", "y", "yes"):
            return mnt
        print("flashpod: aborted; pass --mount to pick a different one",
              file=sys.stderr)
        return None
    print("Possible iPod mountpoints:")
    for i, (score, mnt) in enumerate(cands):
        tag = "  (has iPod_Control)" if score >= 10 else ""
        print(f"  [{i}] {mnt}{tag}")
    try:
        choice = input("Select [0]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        return cands[0][1]
    if choice.isdigit() and int(choice) < len(cands):
        return cands[int(choice)][1]
    print("flashpod: invalid selection", file=sys.stderr)
    return None


def expand(paths):
    """Expand directories into sorted recursive lists of audio files."""
    out = []
    for p in paths:
        if not os.path.isdir(p):
            out.append(p)
            continue
        found = []
        for root, dirs, files in os.walk(p):
            dirs.sort()
            for f in sorted(files):
                if f.startswith("._"):  # macOS AppleDouble junk
                    continue
                if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                    found.append(os.path.join(root, f))
        if not found:
            print(f"warning: no audio files found under {p}", file=sys.stderr)
        out.extend(found)
    return out


def prompt_for_path():
    """Ask for a file/directory when `flashpod add` is run with no paths.
    Returns None (caller exits nonzero) if we can't get a usable one."""
    if not sys.stdin.isatty():
        print("flashpod add: no paths given and stdin is not a terminal",
              file=sys.stderr)
        return None
    try:
        import glob
        import readline
        readline.set_completer_delims("")
        def complete(text, state):
            matches = glob.glob(os.path.expanduser(text) + "*")
            matches = [m + os.sep if os.path.isdir(m) else m for m in matches]
            return matches[state] if state < len(matches) else None
        readline.set_completer(complete)
        if "libedit" in (readline.__doc__ or ""):  # macOS system readline
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    except ImportError:
        pass
    try:
        path = input("File or directory to add (TAB to complete): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    path = os.path.expanduser(path)
    if not path:
        print("flashpod add: nothing entered", file=sys.stderr)
        return None
    if not os.path.exists(path):
        print(f"flashpod add: no such file or directory: {path}", file=sys.stderr)
        return None
    return path


def fmt_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def make_track(lib, path, nr, total, report=None):
    """Read tags from `path` and build an itunesdb.Track (location unset).
    Reports the skip reason (default: stderr) and returns None on unusable
    files."""
    report = report or (lambda msg: print(msg, file=sys.stderr))
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        report(f"[{nr}/{total}] skipping {path}: unreadable ({exc})")
        return None
    if audio is None:
        report(f"[{nr}/{total}] skipping {path}: not a recognized audio file")
        return None

    tags = audio.tags or {}
    t = itunesdb.Track()
    t.id = lib.next_track_id()
    t.title = (first(tags, "title")
               or os.path.splitext(os.path.basename(path))[0])
    t.artist = first(tags, "artist")
    t.album = first(tags, "album")
    t.genre = first(tags, "genre")
    t.composer = first(tags, "composer")
    t.filetype = "MPEG audio file"

    tracknr = first(tags, "tracknumber")
    if tracknr and tracknr.split("/")[0].isdigit():
        t.track_nr = int(tracknr.split("/")[0])
    date = first(tags, "date")
    if date and date[:4].isdigit():
        t.year = int(date[:4])

    info = audio.info
    t.tracklen = int(info.length * 1000)
    t.bitrate = getattr(info, "bitrate", 0) // 1000
    t.samplerate = getattr(info, "sample_rate", 0)
    t.size = os.path.getsize(path)
    return t


def orunknown(s):
    return s if s else "(unknown)"


def cmd_ls(lib, field):
    if field in ("artists", "albums"):
        field = field[:-1]            # 'artists'/'albums' -> 'artist'/'album'
    if field in ("artist", "album"):
        counts = {}
        for t in lib.tracks:
            name = orunknown(getattr(t, field))
            counts[name.casefold()] = (name,
                                       counts.get(name.casefold(), ("", 0))[1] + 1)
        print(f'iPod "{lib.name}": {len(counts)} {field}s '
              f'({len(lib.tracks)} tracks)')
        for _, (name, n) in sorted(counts.items()):
            print(f"{n:5d}  {name}")
        return 0

    show_tracks = field == "all"
    key = lambda t: (orunknown(t.artist).casefold(),
                     orunknown(t.album).casefold(),
                     t.track_nr, orunknown(t.title).casefold())
    tracks = sorted(lib.tracks, key=key)
    albums = {(k[0], k[1]) for k in map(key, tracks)}
    artists = {k[0] for k in map(key, tracks)}
    print(f'iPod "{lib.name}": {len(tracks)} tracks, '
          f'{len(artists)} artists, {len(albums)} albums')
    prev_artist = prev_album = None
    for t in tracks:
        artist, album = orunknown(t.artist), orunknown(t.album)
        if prev_artist is None or artist.casefold() != prev_artist:
            print(artist)
            prev_artist, prev_album = artist.casefold(), None
        if prev_album is None or album.casefold() != prev_album:
            if show_tracks:
                print(f"  {album}")
            else:
                n = sum(1 for u in tracks
                        if orunknown(u.artist).casefold() == prev_artist
                        and orunknown(u.album).casefold() == album.casefold())
                print(f"  {album} ({n} track{'s' if n != 1 else ''})")
            prev_album = album.casefold()
        if show_tracks:
            nr = f"{t.track_nr:2d}." if t.track_nr else "   "
            print(f"    {t.id:6d}  {nr} {orunknown(t.title):<36.36s} "
                  f"{t.tracklen // 60000:2d}:{t.tracklen // 1000 % 60:02d}")
    return 0


def _rm_victims(lib, what):
    """Select the tracks `rm` should delete from `what` (ids, or
    `artist|album <name>`). Returns (victims, None) or (None, exit_code) with
    the error already printed."""
    if what[0] in ("artist", "album"):
        if len(what) < 2:
            print(f"flashpod rm {what[0]}: name required", file=sys.stderr)
            return None, 2
        name = " ".join(what[1:]).casefold()
        victims = [t for t in lib.tracks
                   if (getattr(t, what[0]) or "").casefold() == name]
        if not victims:
            print("flashpod rm: no tracks match that name "
                  "(see `flashpod ls`)", file=sys.stderr)
            return None, 1
        return victims, None
    try:
        ids = [int(i) for i in what]
    except ValueError:
        print("flashpod rm: expected track ids, or `artist <name>` / "
              "`album <name>`", file=sys.stderr)
        return None, 2
    by_id = {t.id: t for t in lib.tracks}
    missing = [i for i in ids if i not in by_id]
    if missing:
        print(f"flashpod rm: no track with id "
              f"{', '.join(map(str, missing))} (see `flashpod ls all`)",
              file=sys.stderr)
        return None, 1
    return [by_id[i] for i in ids], None


def cmd_rm(lib, mount, what):
    victims, rc = _rm_victims(lib, what)
    if victims is None:
        return rc

    for t in victims:
        path = t.filename_on_ipod(mount)
        if path and os.path.exists(path):
            os.unlink(path)
        lib.tracks.remove(t)
        print(f"Removed: {orunknown(t.artist)} - {orunknown(t.title)}")
    if len(victims) > 1:
        print(f"Removed {len(victims)} tracks")
    itunesdb.save(lib, mount)
    return 0


class LineWindow:
    """Scrolling n-line status window so long batches don't flood the
    scrollback: add() lines roll through the window (oldest pushed out),
    note() lines persist above it (skips/failures must survive). On a
    non-tty both print normally — add() to stdout, note() to stderr —
    matching the old per-line behavior for logs and pipes."""
    def __init__(self, size=4):
        self.lines = collections.deque(maxlen=size)
        self.drawn = 0
        self.tty = sys.stdout.isatty()

    def _erase(self):
        if self.drawn:
            # to column 1, `drawn` lines up, clear from there to screen end
            sys.stdout.write("\x1b[%dF\x1b[J" % self.drawn)
            self.drawn = 0

    def _draw(self):
        width = shutil.get_terminal_size().columns
        for line in self.lines:
            sys.stdout.write(line[:max(1, width - 1)] + "\n")
        self.drawn = len(self.lines)
        sys.stdout.flush()

    def add(self, line):
        if not self.tty:
            print(line, flush=True)
            return
        self._erase()
        self.lines.append(line)
        self._draw()

    def update(self, line):
        """Replace the most recent add()ed line in place (for progress)."""
        if not self.tty:
            return                       # don't flood logs with progress ticks
        if not self.lines:
            self.add(line)
            return
        self._erase()
        self.lines[-1] = line
        self._draw()

    def note(self, line):
        if not self.tty:
            print(line, file=sys.stderr)
            return
        self._erase()
        sys.stdout.write(line + "\n")
        self._draw()


def track_key(t):
    """Dedup identity: same byte length + duration + title. Catches the same
    file added twice (e.g. a single that is also present in its album folder)
    using only metadata already in the DB and the incoming file's tags — never
    reads the iPod's stored copies back (slow always, FireWire-fatal)."""
    return (t.size, t.tracklen, (t.title or "").strip().casefold())


def cmd_add(mount, paths):
    return _cmd_add_core(
        paths,
        load=lambda: load_library(mount),
        copy=lambda path, progress: itunesdb.copy_to_ipod(mount, path,
                                                           progress=progress),
        save=lambda lib: (itunesdb.save(lib, mount), os.sync()))


def _cmd_add_core(paths, load, copy, save):
    """Batch-add files to the iPod. The backend is three callables so this
    works the same over an OS mount (cmd_add) and over the raw device
    (cmd_add_raw): load() -> Library|None, copy(path, progress) -> location,
    save(Library) -> None."""
    if not paths or not paths[0]:
        return 1
    files = expand(paths)
    if not files:
        print("nothing to add", file=sys.stderr)
        return 1
    lib = load()
    if not lib:
        return 1
    seen = {track_key(t) for t in lib.tracks}
    start = time.monotonic()
    total = len(files)
    failures = 0
    added = 0
    skipped = 0
    added_bytes = 0
    win = LineWindow()
    for nr, path in enumerate(files, 1):
        track = make_track(lib, path, nr, total, report=win.note)
        if not track:
            failures += 1
            continue
        key = track_key(track)
        if key in seen:
            win.note(f"[{nr}/{total}] skipping {os.path.basename(path)}: "
                     f"already on iPod")
            skipped += 1
            continue
        label = track.title + (f" — {track.artist}" if track.artist else "")
        win.add(f"[{nr}/{total}] Adding: {label}...")

        last = [0.0]

        def _progress(done, total_bytes, _nr=nr, _label=label, _last=last):
            now = time.monotonic()
            # throttle to ~8/sec so the raw path's per-cluster callbacks don't
            # flood the terminal; always let 100% through
            if done < total_bytes and now - _last[0] < 0.125:
                return
            _last[0] = now
            mib = 1 << 20
            pct = (done * 100 // total_bytes) if total_bytes else 100
            win.update(f"[{_nr}/{total}] Adding: {_label}... {pct}% "
                       f"({done / mib:.1f}/{total_bytes / mib:.1f} MiB)")

        try:
            track.location = copy(path, _progress)
        except OSError as exc:
            win.note(f"[{nr}/{total}] FAILED {path}: {exc}")
            failures += 1
            continue
        lib.tracks.append(track)
        seen.add(key)
        added += 1
        added_bytes += track.size or 0
    if added:
        save(lib)
    secs = time.monotonic() - start
    elapsed = fmt_duration(secs)
    parts = [f"{added} track{'s' if added != 1 else ''} added"]
    if skipped:
        parts.append(f"{skipped} skipped (already on iPod)")
    if failures:
        parts.append(f"{failures} failed")
    summary = ", ".join(parts) + f" in {elapsed}"
    if added_bytes and secs > 0:               # throughput tells transaction- vs
        rate = added_bytes / secs              # bandwidth-limited apart
        summary += " (%.1f MiB at %s/s)" % (
            added_bytes / (1 << 20),
            ("%.0f KiB" % (rate / 1024)) if rate < (1 << 20)
            else ("%.1f MiB" % (rate / (1 << 20))))
    if failures:
        print(summary, file=sys.stderr)
        return 1
    print(summary)
    return 0


def ask_yes(prompt, default_yes=True):
    """Y/n prompt. With ``default_yes`` an empty answer means yes (the default);
    otherwise only an explicit yes counts. EOF/^C is always no."""
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans == "" and default_yes:
        return True
    return ans in ("y", "yes")


def offer_init_after_flash(dev):
    """Post-flash hook (ipod_flash.flash before_eject): offer to run init on
    the freshly flashed card right away, so it leaves the flash step fully
    usable. Must run before eject — eject powers the reader off and the
    /dev node disappears until replug. Mounts the data partition at a temp
    dir (we are root here), inits, unmounts; the normal eject follows."""
    part = dev + ("p2" if dev[-1].isdigit() else "2")
    if not os.path.exists(part):
        return
    if not ask_yes("\nThe card still needs the iPod database before it can "
                   "take music (\"flashpod init\").\n"
                   f"Run init on {part} now? [Y/n] "):
        print("Skipped. Later: mount the card and run `flashpod init`.",
              file=sys.stderr)
        return
    import tempfile
    mnt = tempfile.mkdtemp(prefix="flashpod-init-")
    try:
        subprocess.run(["mount", part, mnt], check=True)
        try:
            itunesdb.init_ipod(mnt, "iPod")
            print(f"Initialized iPod directory structure on {part}")
            if ask_yes("\nMusic can be loaded onto the card now, or later "
                       "when it is in the iPod.\n"
                       "Load music onto the card now? [Y/n] "):
                cmd_add(mnt, [prompt_for_path()])
            subprocess.run(["sync"], check=False)
        finally:
            subprocess.run(["umount", mnt], check=False)
    except subprocess.CalledProcessError as exc:
        print(f"init skipped: mounting {part} failed ({exc}); mount the card "
              "and run `flashpod init` instead.", file=sys.stderr)
    finally:
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        prog="flashpod",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mount", default=None,
                        help="iPod mountpoint (default: auto-detect from "
                             "mounted filesystems)")
    parser.add_argument("--raw", default=None, metavar="DEVICE",
                        help="read the iPod directly from a raw device (e.g. "
                             "/dev/rdisk1s2), no OS mount — for iPods the OS "
                             "can't mount, like a flaky FireWire bridge on "
                             "macOS. Needs root. Currently `ls` only.")
    # Accept --mount/--raw after the subcommand too; SUPPRESS keeps the
    # subparser from clobbering a value parsed before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mount", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--raw", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--unsafe-queue", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--unsafe-queue", action="store_true", default=False,
                        help="proceed even if the FireWire host queue "
                             "settings are known-broken for the iPod")
    sub = parser.add_subparsers(dest="command")
    sub.required = True                       # 3.6: not a kwarg until 3.7

    p_ls = sub.add_parser("ls", aliases=["list"], help="list library",
                          parents=[common])
    p_ls.add_argument("field", nargs="?",
                      choices=["all", "artist", "artists", "album", "albums"],
                      help="'all' adds tracks to the tree; "
                           "'artist'/'album' print flat counts")

    p_add = sub.add_parser("add", help="add audio files", parents=[common])
    p_add.add_argument("files", nargs="*", metavar="path",
                       help="audio files or directories (scanned recursively); "
                            "prompts if omitted")

    p_rm = sub.add_parser("rm", aliases=["remove", "delete", "erase"],
                          help="remove tracks", parents=[common])
    p_rm.add_argument("what", nargs="+", metavar="id|artist|album",
                      help="track ids, or `artist <name>` / `album <name>` "
                           "to remove every matching track")

    p_init = sub.add_parser("init", help="create directory structure + empty DB",
                            parents=[common])
    p_init.add_argument("name", nargs="?", help="iPod name (default: iPod)")

    p_fl = sub.add_parser("flash",
                          help="write iPod firmware to a CF/SD card (erases it)")
    p_fl.add_argument("device", nargs="?",
                      help="target disk, e.g. /dev/sdb (else interactive chooser)")
    p_fl.add_argument("--firmware", default=None,
                      help="firmware .ipsw (default: choose from "
                           "firmware/firmware.json)")
    p_fl.add_argument("--yes", action="store_true",
                      help="skip the typed ERASE confirmation")
    p_fl.add_argument("--dry-run", action="store_true",
                      help="show the plan, write nothing")
    p_fl.add_argument("--no-format", action="store_true",
                      help="don't mkfs the data partition")
    p_fl.add_argument("--self-test", action="store_true",
                      help="validate layout logic and exit (no hardware)")

    opts = parser.parse_args()

    if opts.command == "flash":
        if opts.self_test:
            ipod_flash.self_test()
            return 0
        plat = platform.current()
        if not opts.dry_run and not plat.is_admin():
            msg = "flashpod flash: " + plat.privilege_hint()
            if os.name != "nt":           # offer the exact sudo rerun on POSIX
                msg += "\n  sudo " + " ".join(sys.argv)
            print(msg, file=sys.stderr)
            return 1
        if opts.firmware:
            firmware = opts.firmware          # bring-your-own; no network
        else:
            manifest = _load_manifest()
            if not manifest:
                return 1
            entry = choose_firmware(manifest)
            if not entry:
                return 1
            firmware = ensure_firmware(entry, manifest.get("base_url", ""))
            if not firmware:
                return 1
        # Offer init on the fresh card only when it will work: interactive,
        # a real write, and a FAT32 data partition to mount.
        offer = offer_init_after_flash if (
            sys.stdin.isatty() and not opts.dry_run
            and not opts.no_format) else None
        return ipod_flash.flash(device=opts.device, firmware=firmware,
                                assume_yes=opts.yes,
                                dry_run=opts.dry_run,
                                do_format=not opts.no_format,
                                before_eject=offer)

    # Explicit raw-device path: operate on the FAT ourselves, bypassing the OS
    # mount and all the mount-detection / FireWire-queue machinery (the whole
    # point is that the OS can't mount this iPod). Works for ls/add/rm/init.
    raw = getattr(opts, "raw", None)
    if raw:
        return run_raw(opts, raw)

    # `ls` with no --mount/--raw: a mounted iPod, else scan attached disks for
    # one to read directly (the macOS/FireWire case). The scan reads the DB.
    mount = opts.mount
    if opts.command in ("ls", "list") and not mount:
        src = detect_ls_source(opts)
        if not src:
            return 1
        if src[0] == "lib":
            return cmd_ls(src[1], opts.field)
        mount = src[1]

    # Write commands with no --mount: same scan-or-mount resolution, then run
    # over the raw device (the only way to manage an iPod the OS can't mount).
    if opts.command in ("add", "rm", "remove", "delete", "erase", "init") \
            and not mount:
        res = resolve_raw_target(opts)
        if res is None:
            return 1
        if res[0] == "raw":
            return run_raw(opts, res[1])
        mount = res[1]

    # With more than one iPod attached, never let a destructive command guess
    # which one — require an explicit --mount.
    if opts.command in ("init", "rm", "remove", "delete", "erase") and not mount:
        n = _attached_ipod_count()
        if n and n > 1:
            print(f"flashpod: {n} iPods are attached and `{opts.command}` is "
                  f"destructive — pass --mount <path> to choose one explicitly.",
                  file=sys.stderr)
            return 1

    if mount is None:
        mount = detect_mount()
    if not mount:
        return 1

    # The backing device vanished (e.g. the iPod disconnected) — the mount is a
    # stale handle and touching it raises EIO. Detect it and bail cleanly.
    dev = _device_for_mount(mount)
    if dev and not os.path.exists(dev):
        print(f"flashpod: {mount} is a stale mount — its device ({dev}) is gone "
              f"(did the iPod disconnect?). Unmount it (sudo umount -l {mount}), "
              f"reconnect the iPod, and retry.", file=sys.stderr)
        return 1

    # A mounted iPod we can't read (e.g. left mounted by root from an earlier
    # sudo run, so /media/root is 0700 and we can't even traverse it) — offer
    # to remount it as the current user. Gate on "in the mount table but not
    # accessible" rather than os.path.isdir, which is itself False when the
    # parent dir isn't traversable.
    if not os.access(mount, os.R_OK | os.X_OK) and _device_for_mount(mount):
        mount = remount_as_user(mount)
        if not mount:
            return 1

    problem = firewire_queue_problem(mount)
    if problem and not opts.unsafe_queue:
        pin_firewire_queue(problem[0])
        problem = firewire_queue_problem(mount)  # verify, don't trust
    if problem and not opts.unsafe_queue:
        disk, bad = problem
        rule = resources.udev_rule()
        print(f"flashpod: {mount} is a FireWire iPod and the host I/O settings "
              f"are UNSAFE for it:\n  " + ", ".join(bad) + "\n"
              "Large/queued reads can crash early iPod FireWire bridges and "
              "corrupt the filesystem.\nFix for this attach:\n"
              f"  sudo sh -c 'echo 4 >/sys/block/{disk}/queue/max_sectors_kb; "
              f"echo 0 >/sys/block/{disk}/queue/read_ahead_kb; "
              f"echo 1 >/sys/block/{disk}/device/queue_depth'\n"
              "Fix permanently (settings reset on every re-attach):\n"
              f"  sudo cp {rule} /etc/udev/rules.d/ && "
              "sudo udevadm control --reload\n"
              "(--unsafe-queue overrides this check)", file=sys.stderr)
        return 1

    try:
        if opts.command in ("ls", "list"):
            lib = load_library(mount)
            return cmd_ls(lib, opts.field) if lib else 1

        if opts.command in ("rm", "remove", "delete", "erase"):
            lib = load_library(mount)
            return cmd_rm(lib, mount, opts.what) if lib else 1

        if opts.command == "init":
            itunesdb.init_ipod(mount, opts.name or "iPod")
            print(f"Initialized iPod directory structure at {mount}")
            return 0

        return cmd_add(mount, opts.files or [prompt_for_path()])
    except OSError as exc:
        _report_io_error(mount, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
