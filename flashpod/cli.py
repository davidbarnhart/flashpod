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
import json
import os
import re
import shutil
import subprocess
import sys
import time

import mutagen

from . import ipod_flash
from . import itunesdb
from . import resources

FIRMWARE_DIR = resources.firmware_dir()
FIRMWARE_MANIFEST = resources.firmware_manifest()


def choose_firmware():
    """Pick a firmware image from firmware/firmware.json when `flash` got
    no --firmware. Interactive chooser on a tty (manifest default
    preselected); non-tty uses the default entry outright."""
    try:
        with open(FIRMWARE_MANIFEST) as f:
            entries = json.load(f)["firmwares"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"flashpod flash: no --firmware given and {FIRMWARE_MANIFEST} "
              f"is unusable ({exc})", file=sys.stderr)
        return None
    available = []
    for e in entries:
        if os.path.exists(os.path.join(FIRMWARE_DIR, e["file"])):
            available.append(e)
        else:
            print(f"flashpod flash: manifest entry {e['file']} not found "
                  "on disk, skipping", file=sys.stderr)
    if not available:
        print("flashpod flash: no firmware files available; pass --firmware",
              file=sys.stderr)
        return None

    default = next((i for i, e in enumerate(available) if e.get("default")), 0)
    if not sys.stdin.isatty():
        e = available[default]
        print(f"flashpod flash: using default firmware {e['file']} "
              f"({e['generation']}, {e['version']})", file=sys.stderr)
        return os.path.join(FIRMWARE_DIR, e["file"])

    print("Available firmware:")
    for i, e in enumerate(available):
        mark = "  [default]" if i == default else ""
        print(f"  [{i}] {e['generation']} — version {e['version']}{mark}\n"
              f"      {e['description']}")
    try:
        choice = input(f"Select firmware [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        choice = str(default)
    if not (choice.isdigit() and int(choice) < len(available)):
        print("flashpod flash: invalid selection", file=sys.stderr)
        return None
    return os.path.join(FIRMWARE_DIR, available[int(choice)]["file"])


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


def first(tags, key):
    val = tags.get(key)
    if isinstance(val, list):
        val = val[0] if val else None
    return str(val) if val else None


AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff"}

# Test hook: point at a fake mounts table.
MOUNTS_FILE = os.environ.get("FLASHPOD_MOUNTS_FILE", "/proc/mounts")


def candidate_mounts():
    """Plausible iPod mountpoints from the mounts table, best first.
    Scoring: contains iPod_Control +10, 'ipod' in the name +5,
    under /media or /run/media +1; score 0 entries (e.g. /boot/efi)
    are dropped."""
    cands = []
    try:
        f = open(MOUNTS_FILE)
    except OSError:  # no /proc/mounts (macOS) -> rely on --mount
        return []
    with f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mnt, fstype = parts[0], parts[1], parts[2]
            if not dev.startswith("/dev/"):
                continue
            if fstype not in ("vfat", "exfat", "hfsplus"):
                continue
            # /proc/mounts octal-escapes spaces etc. as \040
            mnt = re.sub(r"\\([0-7]{3})",
                         lambda m: chr(int(m.group(1), 8)), mnt)
            score = 0
            if os.path.isdir(os.path.join(mnt, "iPod_Control")):
                score += 10
            if "ipod" in os.path.basename(mnt).lower():
                score += 5
            if mnt.startswith(("/media/", "/run/media/")):
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
            capture_output=True, text=True, check=True).stdout
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


def mount_device(dev):
    """Mount a partition via udisks (no root needed) and return its
    mountpoint, or None."""
    res = subprocess.run(["udisksctl", "mount", "-b", dev],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"flashpod: mount failed: {(res.stderr or res.stdout).strip()}",
              file=sys.stderr)
        return None
    # "Mounted /dev/sdb2 at /media/david/IPOD" (older udisks: trailing ".")
    m = re.search(r" at (.+?)\.?\s*$", res.stdout)
    if m:
        print(res.stdout.strip(), file=sys.stderr)
        return m.group(1)
    print(f"flashpod: mounted {dev} but couldn't parse the mountpoint; "
          f"pass --mount", file=sys.stderr)
    return None


def offer_mount():
    """No mounted iPod found: look for an attached, unmounted one and
    offer to mount it. Returns the mountpoint or None."""
    cands = unmounted_candidates()
    if not cands:
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
        return mount_device(cands[0][1])
    print("Unmounted iPod-like partitions:")
    for i, c in enumerate(cands):
        print(f"  [{i}] {describe(c)}")
    try:
        choice = input("Mount which? [0] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not choice:
        return mount_device(cands[0][1])
    if choice.isdigit() and int(choice) < len(cands):
        return mount_device(cands[int(choice)][1])
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
                         capture_output=True, text=True)
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


def detect_mount():
    """Pick the iPod mountpoint when --mount wasn't given.
    Returns None (caller exits nonzero) if it can't."""
    cands = candidate_mounts()
    if not cands:
        return offer_mount()
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
        path = input("File or directory to add: ").strip()
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


def cmd_rm(lib, mount, what):
    if what[0] in ("artist", "album"):
        if len(what) < 2:
            print(f"flashpod rm {what[0]}: name required", file=sys.stderr)
            return 2
        name = " ".join(what[1:]).casefold()
        victims = [t for t in lib.tracks
                   if (getattr(t, what[0]) or "").casefold() == name]
        if not victims:
            print("flashpod rm: no tracks match that name "
                  "(see `flashpod ls`)", file=sys.stderr)
            return 1
    else:
        try:
            ids = [int(i) for i in what]
        except ValueError:
            print("flashpod rm: expected track ids, or `artist <name>` / "
                  "`album <name>`", file=sys.stderr)
            return 2
        by_id = {t.id: t for t in lib.tracks}
        missing = [i for i in ids if i not in by_id]
        if missing:
            print(f"flashpod rm: no track with id "
                  f"{', '.join(map(str, missing))} (see `flashpod ls all`)",
                  file=sys.stderr)
            return 1
        victims = [by_id[i] for i in ids]

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
    if not paths or not paths[0]:
        return 1
    files = expand(paths)
    if not files:
        print("nothing to add", file=sys.stderr)
        return 1
    lib = load_library(mount)
    if not lib:
        return 1
    seen = {track_key(t) for t in lib.tracks}
    start = time.monotonic()
    total = len(files)
    failures = 0
    added = 0
    skipped = 0
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
        try:
            track.location = itunesdb.copy_to_ipod(mount, path)
        except OSError as exc:
            win.note(f"[{nr}/{total}] FAILED {path}: {exc}")
            failures += 1
            continue
        lib.tracks.append(track)
        seen.add(key)
        added += 1
    if added:
        itunesdb.save(lib, mount)
        os.sync()
    elapsed = fmt_duration(time.monotonic() - start)
    parts = [f"{added} track{'s' if added != 1 else ''} added"]
    if skipped:
        parts.append(f"{skipped} skipped (already on iPod)")
    if failures:
        parts.append(f"{failures} failed")
    summary = ", ".join(parts) + f" in {elapsed}"
    if failures:
        print(summary, file=sys.stderr)
        return 1
    print(summary)
    return 0


def ask_yes(prompt):
    """[Y/n] prompt: empty answer means yes; EOF/^C means no."""
    try:
        return input(prompt).strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


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
    # Accept --mount after the subcommand too; SUPPRESS keeps the subparser
    # from clobbering a value parsed before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mount", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--unsafe-queue", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--unsafe-queue", action="store_true", default=False,
                        help="proceed even if the FireWire host queue "
                             "settings are known-broken for the iPod")
    sub = parser.add_subparsers(dest="command", required=True)

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
    p_fl.add_argument("--flavor", choices=("mac", "windows"), default="windows",
                      help="windows = MBR + FAT32 (default); mac = APM + HFS+")
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
        if os.geteuid() != 0 and not opts.dry_run:
            print("flashpod flash: writing a card needs root — rerun as:\n"
                  f"  sudo {' '.join(sys.argv)}", file=sys.stderr)
            return 1
        firmware = opts.firmware or choose_firmware()
        if not firmware:
            return 1
        # Offer init on the fresh card only when it will work: interactive,
        # a real write, and a FAT32 data partition to mount (Linux can't
        # reliably write the mac flavor's HFS+).
        offer = offer_init_after_flash if (
            sys.stdin.isatty() and not opts.dry_run
            and opts.flavor == "windows" and not opts.no_format) else None
        return ipod_flash.flash(device=opts.device, firmware=firmware,
                                flavor=opts.flavor, assume_yes=opts.yes,
                                dry_run=opts.dry_run,
                                do_format=not opts.no_format,
                                before_eject=offer)

    mount = opts.mount or detect_mount()
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


if __name__ == "__main__":
    sys.exit(main())
