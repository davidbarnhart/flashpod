#!/usr/bin/env python3
"""#115's own promises, on synthetic image files only:

    python3 scripts/test_firmware_write_only.py --rust rust/target/release/flashpod
    python3 scripts/test_firmware_write_only.py --rust BIN --mutants   # the checks can fail

1. `firmware write` changes no byte outside the firmware partition, on an MBR
   (FAT32) drive and an Apple-map (HFS+) drive. The partition bounds are read
   from the table here, not asked of flashpod.
2. No prompt path writes without a yes (lead #4494). Each refusal leaves the
   image unchanged: same mtime and same digest of every data extent.
   - `firmware write` without --yes: Enter, "n", and no terminal.
   - `drive partition` without --yes: an empty ERASE, a wrong ERASE, no
     terminal, and --dry-run. Its plan names what is lost (the track count).
   - the large-disk gate (a data partition past 128 GiB, firmware that can't
     be shown to read it): "n", --yes then an empty ERASE, a wrong ERASE, no
     terminal, --dry-run. Only "y" plus the right ERASE writes, and that
     gives a data partition inside 128 GiB.
3. `firmware write` with no FIRMWARE argument uses the catalog choosers (a
   local served catalog): the default writes the catalog's image; a model
   with no firmware, or an image whose checksum is wrong, writes nothing.
4. `bootrom arm` and `bootrom disarm` on Apple's 1.1.5 from the firmware
   cache (~/.cache/flashpod/firmware). There is no synthetic stand-in, so
   without it this prints NOT RUN and still passes.

--mutants changes one byte where each check says nothing changes (or the
wrong byte where it says only the firmware partition changes) and requires
that check to fail. Ends with one PASS/FAIL line; exit 0 only on PASS.
"""
import argparse
import glob
import json
import hashlib
import os
import pty
import re
import select
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402
from test_cli_unchanged import serve_catalog  # noqa: E402

SECTOR = 512
GIB = 1 << 30
LBA28 = 1 << 28                 # sectors: the 128 GiB line
fails = []
MUTANT = [None]                 # the check name being mutated, when --mutants runs one
REACHED = set()                 # mutation sites this run reached (a section that is NOT RUN reaches none)


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


# -- images -------------------------------------------------------------------
def digest(path):
    """mtime + size + sha256 over every data extent (offset and bytes), so a
    200 GiB sparse image costs only what was written to it."""
    st = os.stat(path)
    h = hashlib.sha256(struct.pack("<QQ", st.st_size, st.st_mtime_ns))
    fd = os.open(path, os.O_RDONLY)
    try:
        off = 0
        while off < st.st_size:
            try:
                off = os.lseek(fd, off, os.SEEK_DATA)
            except OSError:
                break
            end = os.lseek(fd, off, os.SEEK_HOLE)
            h.update(struct.pack("<QQ", off, end))
            os.lseek(fd, off, os.SEEK_SET)
            left = end - off
            while left:
                b = os.read(fd, min(left, 8 << 20))
                if not b:
                    break
                h.update(b)
                left -= len(b)
            off = end
    finally:
        os.close(fd)
    return h.hexdigest()


def poke(path, offset):
    """The mutant's one-byte change."""
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1) or b"\0"
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


def mutate(name, path, offset):
    REACHED.add(name)
    if MUTANT[0] == name:
        poke(path, offset)


def table(path):
    """(kind, firmware (start, count), data (start, count)) in sectors."""
    with open(path, "rb") as f:
        s0 = f.read(SECTOR)
        if s0[:2] == b"ER":
            fw = data = None
            n = None
            i = 1
            while n is None or i <= n:
                f.seek(i * SECTOR)
                e = f.read(SECTOR)
                if e[:2] != b"PM":
                    break
                n = struct.unpack(">I", e[4:8])[0]
                start, count = struct.unpack(">II", e[8:16])
                kind = e[48:80].split(b"\0")[0]
                if kind == b"Apple_MDFW":
                    fw = (start, count)
                elif kind == b"Apple_HFS":
                    data = (start, count)
                i += 1
            return "apm", fw, data
        if s0[510:512] != b"\x55\xaa":
            return None, None, None
        ents = [struct.unpack("<B3xII", s0[0x1BE + 16 * i + 4:0x1BE + 16 * i + 16]) for i in range(4)]
        fw = next(((s, c) for t, s, c in ents if t == 0 and c), None)
        data = next(((s, c) for t, s, c in ents if t in (0x0B, 0x0C) and c), None)
        return "mbr", fw, data


def hash_ranges(path, ranges):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for a, b in ranges:
            f.seek(a)
            left = b - a
            while left:
                chunk = f.read(min(left, 8 << 20))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
    return h.hexdigest()


# -- running flashpod -----------------------------------------------------------
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")


def run_plain(binary, args, env=None):
    """No terminal: stdin is /dev/null."""
    r = subprocess.run(binary + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, env=env, errors="replace")
    return r.returncode, r.stdout


def run_pty(binary, args, answers, env=None, timeout=300):
    """On a pty, answering each (regex, reply) in order when its prompt is
    the last thing printed. A prompt nobody answers is a hang: killed,
    reported as rc None."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(binary[0], binary + args, env or os.environ)
        finally:
            os._exit(127)
    out = ""
    todo = list(answers)
    idle_since = time.time()
    t0 = time.time()
    hung = False
    while True:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                d = os.read(fd, 65536)
            except OSError:
                break
            if not d:
                break
            out += ANSI.sub("", d.decode(errors="replace")).replace("\r", "")
            idle_since = time.time()
        if todo and re.search(todo[0][0] + r"\s*$", out):
            time.sleep(0.1)
            os.write(fd, todo.pop(0)[1].encode() + b"\r")
            idle_since = time.time()
            continue
        if time.time() - idle_since > 15 or time.time() - t0 > timeout:
            hung = True
            os.kill(pid, 9)
            break
    _, st = os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass
    return (None if hung else os.waitstatus_to_exitcode(st)), out


def unchanged(name, img, before, rc, out, wrote_hint=""):
    mutate(name, img, 4096)
    check(rc not in (0, None), "%s: refused (rc %r)" % (name, rc))
    check(digest(img) == before, "%s: the image is unchanged%s" % (name, wrote_hint))
    return out


def fresh_card(binary, path, size, fmt=None):
    with open(path, "wb") as f:
        f.truncate(size)
    args = ["drive", "partition", "--target", path, "--yes"] + (["--format", fmt] if fmt else [])
    rc, out = run_plain(binary, args)
    if rc != 0:
        raise SystemExit("setup: drive partition failed (rc %d):\n%s" % (rc, out))


# -- the checks -----------------------------------------------------------------
def outside_unchanged(binary, work, fw, fwbytes):
    print("firmware write touches only the firmware partition")
    for fmt in ("fat32", "hfs"):
        img = os.path.join(work, "only-%s.img" % fmt)
        fresh_card(binary, img, 512 << 20, "hfs" if fmt == "hfs" else None)
        kind, fwp, data = table(img)
        check(fwp is not None and data is not None, "%s: the table has a firmware and a data partition (%s)" % (fmt, kind))
        if fwp is None:
            continue
        # marks in the data partition and the unallocated tail, so 'outside'
        # holds more than zeros
        size = os.path.getsize(img)
        with open(img, "r+b") as f:
            for off in (data[0] * SECTOR + (8 << 20), data[0] * SECTOR + (300 << 20), size - 4096):
                f.seek(off)
                f.write(os.urandom(4096))
        a, b = fwp[0] * SECTOR, (fwp[0] + fwp[1]) * SECTOR
        outside = [(0, a), (b, size)]
        before_out = hash_ranges(img, outside)
        before_in = hash_ranges(img, [(a, b)])
        rc, out = run_plain(binary, ["firmware", "write", "--target", img, fw, "--yes"])
        name = "%s outside" % fmt
        mutate(name, img, b)          # the first byte past the partition
        check(rc == 0, "%s (%s): firmware write succeeds (rc %d)%s" % (fmt, kind, rc, "" if rc == 0 else ": " + out[-300:]))
        check(hash_ranges(img, outside) == before_out,
              "%s (%s): every byte outside sectors %d..%d is unchanged" % (fmt, kind, fwp[0], fwp[0] + fwp[1] - 1))
        with open(img, "rb") as f:
            f.seek(a)
            head = f.read(len(fwbytes))
        check(hash_ranges(img, [(a, b)]) != before_in and head[0x100:0x104] == b"]ih[",
              "%s (%s): the firmware partition now holds the firmware" % (fmt, kind))


def write_confirm(binary, work, fw):
    print("firmware write asks before writing")
    img = os.path.join(work, "confirm.img")
    fresh_card(binary, img, 256 << 20)
    args = ["firmware", "write", "--target", img, fw]
    q = r"Write the firmware to .*\? \[y/N\]"
    for name, answers in (("Enter", [(q, "")]), ("n", [(q, "n")])):
        before = digest(img)
        rc, out = run_pty(binary, args, answers)
        unchanged("firmware write, answer %s" % name, img, before, rc, out)
        check("Nothing written." in out, "firmware write, answer %s: says nothing was written" % name)
    before = digest(img)
    rc, out = run_plain(binary, args)
    unchanged("firmware write, no terminal", img, before, rc, out)
    check("pass --yes" in out, "firmware write, no terminal: says to pass --yes")
    rc, out = run_pty(binary, args, [(q, "y")])
    check(rc == 0, "firmware write, answer y: writes (rc %r)" % rc)


def partition_confirm(binary, work, fw, music):
    print("drive partition asks before erasing, and says what is lost")
    img = os.path.join(work, "erase.img")
    fresh_card(binary, img, 256 << 20)
    run_plain(binary, ["firmware", "write", "--target", img, fw, "--yes"])
    run_plain(binary, ["music", "init", "--raw", img, "Pod"])
    rc, out = run_plain(binary, ["music", "add", "--raw", img, music])
    n = len(re.findall(r"^\s*\[?\s*\d+\]?\s", run_plain(binary, ["music", "ls", "all", "--raw", img])[1], re.M))
    m = re.search(r"(\d+) tracks? added", out)
    tracks = int(m.group(1)) if m else n
    check(tracks > 0, "setup: the card holds %d tracks" % tracks)
    args = ["drive", "partition", "--target", img]
    q = r'Type "ERASE [^"]+" to proceed:'
    for name, answers in (("an empty ERASE", [(q, "")]), ("a wrong ERASE", [(q, "ERASE sdz")])):
        before = digest(img)
        rc, out = run_pty(binary, args, answers)
        unchanged("drive partition, %s" % name, img, before, rc, out)
        if name == "an empty ERASE":
            want = "%d track%s of music" % (tracks, "" if tracks == 1 else "s")
            check(want in out and "will be lost" in out, "drive partition: the plan says %r will be lost" % want)
    before = digest(img)
    rc, out = run_plain(binary, args)
    unchanged("drive partition, no terminal", img, before, rc, out)
    before = digest(img)
    rc, out = run_pty(binary, args + ["--dry-run"], [])
    mutate("drive partition, --dry-run", img, 4096)
    check(rc == 0 and digest(img) == before, "drive partition, --dry-run: rc 0, the image is unchanged")


def large_disk_gate(binary, work, fw, label_fw):
    print("the large-disk gate (%s)" % label_fw)
    img = os.path.join(work, "big.img")
    fresh_card(binary, img, 200 * GIB)
    _, _, data = table(img)
    check(data and data[0] + data[1] > LBA28, "setup: the data partition ends past 128 GiB")
    args = ["firmware", "write", "--target", img, fw]
    q = r"Re-partition(, then write the firmware| anyway)\? \[y/N\]"
    e = r'Type "ERASE [^"]+" to proceed:'
    want = 'ERASE %s' % os.path.basename(img)
    for name, extra, answers in (
            ("n", [], [(q, "n")]),
            ("--yes, then an empty ERASE", ["--yes"], [(q, "y"), (e, "")]),
            ("y, then a wrong ERASE", [], [(q, "y"), (e, "ERASE other.img")])):
        before = digest(img)
        rc, out = run_pty(binary, args + extra, answers)
        unchanged("large-disk gate, %s" % name, img, before, rc, out)
    for name, extra in (("no terminal, --yes", ["--yes"]), ("--dry-run", ["--dry-run", "--yes"])):
        before = digest(img)
        rc, out = (run_plain if "terminal" in name else
                   lambda b, a: run_pty(b, a, []))(binary, args + extra)
        unchanged("large-disk gate, %s" % name, img, before, rc, out)
        check("drive partition --target %s --128g" % img in out,
              "large-disk gate, %s: names the commands that fix it" % name)
    rc, out = run_pty(binary, args, [(q, "y"), (e, want)])
    _, fwp, data = table(img)
    check(rc == 0, "large-disk gate, y + the right ERASE: succeeds (rc %r)" % rc)
    check(data and data[0] + data[1] <= LBA28, "large-disk gate, y + the right ERASE: the data partition now ends inside 128 GiB")
    with open(img, "rb") as f:
        f.seek(fwp[0] * SECTOR + 0x100)
        check(f.read(4) == b"]ih[", "large-disk gate, y + the right ERASE: the firmware is written")


def catalog_choosers(binary, work, fw):
    print("firmware write with no FIRMWARE: the catalog choosers")
    manifest = serve_catalog(os.path.join(work, "cat"), fw)
    env = dict(os.environ, FP_MANIFEST=manifest, XDG_CACHE_HOME=os.path.join(work, "cache"))
    img = os.path.join(work, "catalog.img")
    fresh_card(binary, img, 256 << 20)
    args = ["firmware", "write", "--target", img, "--yes"]
    with open(fw, "rb") as f:
        want = f.read()
    rc, out = run_pty(binary, args, [(r"Select model:", "1"), (r"Select firmware \[\d+\]:", "")], env=env)
    _, fwp, _ = table(img)
    with open(img, "rb") as f:
        f.seek(fwp[0] * SECTOR)
        got = f.read(len(want))
    check(rc == 0, "catalog: model 1, the default firmware: succeeds (rc %r)" % rc)
    # the write applies the install fixups, so compare past the directories
    check(got[0x5000:] == want[0x5000:], "catalog: the default firmware's payload is on the card")
    lines = [l for l in out.split("\n") if re.match(r"\s*\d+[.)]?\s", l)]
    pick = next((re.match(r"\s*(\d+)", l).group(1) for l in lines if "checksum will not match" in l), None)
    check(pick is not None, "catalog: the chooser lists the tampered image")
    before = digest(img)
    rc, out = run_pty(binary, args + ["--model", "2G"], [(r"Select firmware \[\d+\]:", pick or "1")], env=env)
    unchanged("catalog: an image whose checksum is wrong", img, before, rc, out)
    check("checksum" in out.lower() or "sha256" in out.lower(),
          "catalog: an image whose checksum is wrong: the refusal says so")
    before = digest(img)
    rc, out = run_plain(binary, args + ["--model", "9G"], env=env)
    unchanged("catalog: an unknown model", img, before, rc, out)


def bootrom_real(binary, work):
    print("bootrom arm/disarm on Apple's 1.1.5")
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "flashpod", "firmware")
    found = sorted(glob.glob(os.path.join(cache, "iPod_1.1.5_*.bin*")))
    if not found:
        print("  NOT RUN: no iPod_1.1.5_*.bin in %s (`flashpod firmware write` with the catalog fetches it)" % cache)
        return
    img = os.path.join(work, "arm.img")
    fresh_card(binary, img, 256 << 20)
    rc, out = run_plain(binary, ["firmware", "write", "--target", img, found[0], "--yes"])
    check(rc == 0, "1.1.5 written (rc %d)" % rc)
    _, fwp, _ = table(img)
    region = [(fwp[0] * SECTOR, (fwp[0] + fwp[1]) * SECTOR)]
    size = os.path.getsize(img)
    outside = [(0, region[0][0]), (region[0][1], size)]
    written = hash_ranges(img, region)
    before_out = hash_ranges(img, outside)
    before = digest(img)
    rc, out = run_pty(binary, ["bootrom", "arm", "--target", img], [(r"\[y/N\]", "n")])
    unchanged("bootrom arm, answer n", img, before, rc, out)
    rc, out = run_plain(binary, ["bootrom", "arm", "--target", img])
    unchanged("bootrom arm, no terminal", img, before, rc, out)
    rc, out = run_plain(binary, ["bootrom", "arm", "--target", img, "--yes"])
    check(rc == 0, "bootrom arm --yes: succeeds (rc %d)" % rc)
    check(hash_ranges(img, region) != written, "bootrom arm: the firmware partition changed")
    mutate("bootrom outside", img, region[0][1])
    check(hash_ranges(img, outside) == before_out, "bootrom arm: nothing outside the firmware partition changed")
    rc, out = run_plain(binary, ["bootrom", "disarm", "--target", img])
    check(rc == 0, "bootrom disarm: succeeds (rc %d)" % rc)
    check(hash_ranges(img, region) == written, "bootrom disarm: the firmware partition is back as written")



def splice_stock(card, donor):
    """The oracle for `bootrom arm stock`, written from the directory format,
    not from the Rust: donor's aupd bytes at the card's aupd offset, both
    directory entries take the donor's fields but devOffset, then id 0 (armed)."""
    def entries(fw, base):
        out = []
        for i in range(10):
            o = base + 40 * i
            if fw[o:o + 4] == b"\0\0\0\0":
                break
            out.append((o, fw[o + 4:o + 8][::-1], list(struct.unpack_from("<8I", fw, o + 8))))
        return out
    ptr = struct.unpack_from("<I", card, 0x104)[0]
    live = lambda fw: [e for e in entries(fw, struct.unpack_from("<I", fw, 0x104)[0] + 0x200) if e[1] == b"aupd"][0][2]
    cf, df = live(card), live(donor)
    blob = donor[df[1]:df[1] + df[2]]
    out = bytearray(card)
    at = cf[1]
    if len(out) < at + len(blob):
        out.extend(b"\0" * (at + len(blob) - len(out)))
    out[at:at + len(blob)] = blob
    for i in range(at + len(blob), at + cf[2]):
        out[i] = 0
    for base in (ptr, ptr + 0x200):
        for o, typ, f in entries(card, base):
            if typ == b"aupd":
                w = list(df)
                w[1] = f[1]
                w[0] = 0
                struct.pack_into("<8I", out, o + 8, *w)
    return bytes(out)


def bootrom_stock(binary, work):
    print("bootrom arm stock: Apple 1.5's disk-mode program onto flashpod 1.6")
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "flashpod", "firmware")
    apple = sorted(glob.glob(os.path.join(cache, "iPod_1.1.5_*.bin*")))
    ours = sorted(glob.glob(os.path.join(cache, "flashpod_1.1.6*.bin*")))
    if not apple or not ours:
        print("  NOT RUN: needs iPod_1.1.5_* and flashpod_1.1.6* in %s" % cache)
        return
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import firmware as pyfw    # the Python 0.x reader: unwrap + fix up, independent of the Rust
    a15, f16 = pyfw.load_firmware(apple[0]), pyfw.load_firmware(ours[0])
    want = splice_stock(f16, a15)
    img = os.path.join(work, "stock.img")
    fresh_card(binary, img, 256 << 20)
    rc, out = run_plain(binary, ["firmware", "write", "--target", img, ours[0], "--yes"])
    check(rc == 0, "flashpod 1.6 written (rc %d)" % rc)
    _, fwp, _ = table(img)
    region = (fwp[0] * SECTOR, (fwp[0] + fwp[1]) * SECTOR)
    outside = [(0, region[0]), (region[1], os.path.getsize(img))]
    before_out = hash_ranges(img, outside)
    before = digest(img)
    rc, out = run_plain(binary, ["bootrom", "arm", "stock", "--target", img])
    unchanged("bootrom arm stock, no terminal", img, before, rc, out)
    rc, out = run_pty(binary, ["bootrom", "arm", "stock", "--target", img], [(r"\[y/N\]", "n")])
    unchanged("bootrom arm stock, answer n", img, before, rc, out)
    rc, out = run_plain(binary, ["bootrom", "arm", "stock", "--target", img, "--dry-run"])
    check(rc == 0 and digest(img) == before, "bootrom arm stock --dry-run: rc 0, the image is unchanged")
    rc, out = run_plain(binary, ["bootrom", "arm", "stock", "--target", img, "--yes"])
    check(rc == 0 and "Apple 1.5" in out, "bootrom arm stock --yes: armed Apple 1.5 (rc %d)" % rc)
    mutate("stock outside", img, region[1])
    check(hash_ranges(img, outside) == before_out, "bootrom arm stock: nothing outside the firmware partition changed")
    with open(img, "rb") as f:
        f.seek(region[0])
        got = f.read(region[1] - region[0])
    REACHED.add("stock splice")
    if MUTANT[0] == "stock splice":
        got = got[:0x4400] + bytes([got[0x4400] ^ 1]) + got[0x4401:]
    check(got[:len(want)] == want, "bootrom arm stock: the firmware partition is 1.6 with Apple 1.5's disk-mode program, armed")
    check(not any(got[len(want):]), "bootrom arm stock: the rest of the firmware partition is still zero")
    rc, out = run_plain(binary, ["inspect", img, "--json"])
    try:
        rep = json.loads(out[out.index("{"):])
    except ValueError:
        rep = {}
    flat = json.dumps(rep)
    check("Apple 1.5" in flat and "flashpod 1.6" in flat, "inspect: osos is still flashpod 1.6, the disk-mode program is Apple 1.5")

MUTANTS = ["fat32 outside", "hfs outside", "firmware write, answer Enter", "firmware write, no terminal",
           "drive partition, a wrong ERASE", "drive partition, --dry-run",
           "large-disk gate, --yes, then an empty ERASE", "large-disk gate, no terminal, --yes",
           "catalog: an image whose checksum is wrong", "bootrom outside", "stock outside", "stock splice"]


def run_all(binary):
    work = synth.scratch_dir("fp-fwonly-")
    fw = synth.write_firmware(os.path.join(work, "fw.bin"))
    with open(fw, "rb") as f:
        fwbytes = f.read()
    music = synth.make_music(os.path.join(work, "music"))
    outside_unchanged(binary, work, fw, fwbytes)
    write_confirm(binary, work, fw)
    partition_confirm(binary, work, fw, music)
    large_disk_gate(binary, work, fw, "synthetic: flashpod can't tell")
    cache = os.path.join(os.path.expanduser("~/.cache"), "flashpod", "firmware")
    stock = sorted(glob.glob(os.path.join(cache, "iPod_1.1.2.6_*.bin*")))
    if stock:
        large_disk_gate(binary, work, stock[0], "Apple 1.1.2.6: can't read past 128 GiB")
    else:
        print("  NOT RUN: the gate with stock firmware (no iPod_1.1.2.6_*.bin in %s)" % cache)
    catalog_choosers(binary, work, fw)
    bootrom_real(binary, work)
    bootrom_stock(binary, work)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rust", required=True, help="the flashpod binary")
    ap.add_argument("--mutants", action="store_true")
    o = ap.parse_args()
    binary = [os.path.abspath(o.rust)]
    if not o.mutants:
        run_all(binary)
        print("PASS" if not fails else "FAIL: %d check(s)" % len(fails))
        sys.exit(0 if not fails else 1)
    missed, not_run = [], []
    for m in MUTANTS:
        MUTANT[0] = m
        del fails[:]
        REACHED.clear()
        print("== mutant: %s" % m)
        run_all(binary)
        if m not in REACHED:
            # its section printed NOT RUN (e.g. no Apple 1.1.5 in the cache): nothing to catch here
            not_run.append(m)
            continue
        # the unmutated run passes, so any failure here is the mutant's
        if not fails:
            missed.append(m)
    if len(not_run) == len(MUTANTS):
        print("FAIL: no mutant's site was reached")
        sys.exit(1)
    for m in not_run:
        print("  NOT RUN mutant: %s (its section did not run here)" % m)
    del fails[:]
    for m in missed:
        fails.append("mutant not caught: %s" % m)
        print("  FAIL mutant not caught: %s" % m)
    print("PASS (%d mutants caught)" % (len(MUTANTS) - len(not_run)) if not missed else "FAIL: %d mutant(s) not caught" % len(missed))
    sys.exit(0 if not missed else 1)


if __name__ == "__main__":
    main()
