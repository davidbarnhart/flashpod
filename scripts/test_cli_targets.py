#!/usr/bin/env python3
"""Every flashpod 1.0 command names the target it acts on, when it succeeds
as well as when it fails (#112), on synthetic cards only.

    python3 scripts/test_cli_targets.py --rust rust/target/release/flashpod
    python3 scripts/test_cli_targets.py --rust BIN --mutants    # the check can fail
    python3 scripts/test_cli_targets.py --rust BIN --loop       # CI: also a loop
                                       # device and a real mount (needs sudo -n)

Each command runs on each kind of target it can take: an image file, a mount
(a plain directory; with --loop also a mounted image) and a card (with --loop,
a block device backed by this run's own file; nothing else is ever aimed at).
A check needs rc 0, unless the table says the target is refused, and the output
must name the target and no other. The music commands must also print exactly
`Target: PATH (KIND)` on stderr, and keep it out of stdout.

The commands come from --help: a new one fails here until it is listed.
NOT_BUILT commands must still say so, so a built one fails here until it joins
(#100-#103). --mutants strips the lines naming the target from one command at a
time and requires the run to fail on that command and no other. Ends with one
PASS/FAIL line; exit 0 only on PASS.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402

NOT_BUILT = ["firmware list", "firmware patch", "firmware arm", "firmware recover", "emulate"]
EXEMPT = {"firmware patches": "reads the patch library; it has no target"}
GROUPS = ["firmware", "card", "music"]
MUSIC = ["music ls", "music list", "music add", "music rm", "music remove", "music delete",
         "music erase", "music init", "music rebuild", "music export", "music playlist"]
NOT_APPLICABLE = {
    "mount": "inspect, firmware extract/write, flash, card new and card burn take a file or disk, not a mount",
    "card": "card new makes a file",
}

FAILS = []
COVERED = {"image file": set(), "mount": set(), "card": set()}


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def run(cmd, env=None, cwd=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       stdin=subprocess.DEVNULL, env=env, cwd=cwd, errors="replace")
    return r.returncode, r.stdout, r.stderr


def names(text, path):
    return re.search(r"(?<![\w./-])%s(?![\w./-])" % re.escape(path), text) is not None


def step(binary, key, args, target, kind, decoy, refused=False, line=False, env=None):
    """One command against one target; returns its stdout."""
    rc, out, err = run(binary + key.split() + args, env=env)
    text = out + err
    head = "%s on %s" % (key, kind)
    if refused:
        check(rc != 0 and names(text, target), "%s: refused, and the refusal names it (rc %d)" % (head, rc))
    else:
        check(rc == 0, "%s: succeeds (rc %d)%s" % (head, rc, "" if rc == 0 else ": " + err.strip()[-300:]))
        check(names(text, target), "%s: output names %s" % (head, target))
    if decoy:
        check(not names(text, decoy), "%s: output names no other target" % head)
    if line:
        want = "Target: %s (%s)" % (target, "mounted iPod" if kind.startswith("mount") else kind)
        check(want in err.split("\n"), "%s: stderr has the line %r" % (head, want))
        check("Target:" not in out, "%s: stdout stays the command's own" % head)
    COVERED["mount" if kind.startswith("mount") else kind].add(key)
    return out


def music_steps(binary, flag, target, kind, decoy, music, work):
    """The music commands in an order that gives each one something to do."""
    def go(key, *args):
        return step(binary, key, [flag, target] + list(args), target, kind, decoy, line=True)
    go("music init", "TargetPod")
    go("music add", music)
    listing = go("music ls", "all")
    go("music list")
    ids = re.findall(r"\[\s*(\d+)\]|^\s*(\d+)\s", listing, re.M)
    ids = [a or b for a, b in ids]
    check(len(ids) >= 5, "music ls all on %s lists the tracks (%d ids)" % (kind, len(ids)))
    ids += ["0"] * 5
    go("music playlist", "new", "Mix", ids[0])
    for key, i in zip(["music rm", "music remove", "music delete", "music erase"], ids):
        go(key, i)
    go("music rebuild", "TargetPod")
    dest = tempfile.mkdtemp(prefix="export-", dir=work)
    go("music export", "--to", dest, "--no-other-files")


def image_cells(binary, fw, music, work, decoy):
    print("an image file")
    img = os.path.join(work, "target.img")
    step(binary, "card new", [img, "200M"], img, "image file", decoy)
    step(binary, "inspect", [img], img, "image file", decoy)
    step(binary, "firmware write", [img, "--firmware", fw, "--yes", "--no-arm"], img, "image file", decoy)
    step(binary, "flash", [img, "--firmware", fw, "--yes", "--no-arm"], img, "image file", decoy)
    step(binary, "firmware extract", [img, "-o", os.path.join(work, "fw-out.bin")], img, "image file", decoy)
    music_steps(binary, "--raw", img, "image file", decoy, music, work)
    # burn's target is the disk it writes; a file is not one
    step(binary, "card burn", [img, decoy + ".not-a-disk", "--dry-run"], decoy + ".not-a-disk", "image file",
         None, refused=True)


def mount_cells(binary, music, work):
    print("a mount (a plain directory)")
    mnt = os.path.join(work, "mnt")
    other = os.path.join(work, "mnt-other")
    os.makedirs(mnt)
    os.makedirs(other)
    music_steps(binary, "--mount", mnt, "mount (directory)", other, music, work)


def sudo(*cmd):
    r = subprocess.run(["sudo", "-n"] + list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError("sudo %s: %s" % (" ".join(cmd), r.stderr.strip()))
    return r.stdout.strip()


def attach(img):
    return sudo("losetup", "--find", "--show", "--partscan", img)


def loop_mount_cells(binary, fw, music, work):
    print("a mount (a mounted card image)")
    img = os.path.join(work, "mounted.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    rc, _, err = run(binary + ["firmware", "write", img, "--firmware", fw, "--yes", "--no-arm"])
    check(rc == 0, "the image to mount is flashed (rc %d) %s" % (rc, err.strip()[-200:] if rc else ""))
    node = attach(img)
    mnt = os.path.join(work, "real-mnt")
    os.makedirs(mnt)
    mounted = False
    try:
        for _ in range(50):
            if os.path.exists(node + "p2"):
                break
            time.sleep(0.1)
        sudo("mount", "-o", "uid=%d,gid=%d" % (os.getuid(), os.getgid()), node + "p2", mnt)
        mounted = True
        music_steps(binary, "--mount", mnt, "mount (mounted image)", None, music, work)
    finally:
        if mounted:
            sudo("umount", mnt)
        sudo("losetup", "-d", node)


def card_cells(binary, fw, music, work, decoy):
    print("a card (a loop device on this run's own file)")
    img = os.path.join(work, "card.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    node = attach(img)
    if not stat.S_ISBLK(os.stat(node).st_mode) or not node.startswith("/dev/loop"):
        raise RuntimeError("losetup gave %r" % node)
    root = ["sudo", "-n", "env", "PATH=" + os.environ["PATH"]] + binary
    try:
        step(root, "inspect", [node], node, "card", decoy)
        step(root, "firmware write", [node, "--firmware", fw, "--yes", "--no-arm"], node, "card", decoy)
        step(root, "flash", [node, "--firmware", fw, "--yes", "--no-arm"], node, "card", decoy)
        step(root, "firmware extract", [node, "-o", os.path.join(work, "card-fw.bin")], node, "card", decoy)
        music_steps(root, "--raw", node, "card", decoy, music, work)
        # burn only takes a removable disk, which a loop device is not: the
        # stand-in lsblk reports this one as removable. A dry run writes nothing.
        fake = os.path.join(work, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "lsblk"), "w") as f:
            json_text = json.dumps({"blockdevices": [{
                "name": os.path.basename(node), "size": 200 << 20, "type": "disk", "rm": True, "hotplug": True,
                "tran": "usb", "model": "stand-in", "vendor": "test"}]})
            f.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % json_text)
        os.chmod(os.path.join(fake, "lsblk"), 0o755)
        burnable = os.path.join(work, "burn.img")
        with open(burnable, "wb") as f:
            f.truncate(100 << 20)
        run(binary + ["firmware", "write", burnable, "--firmware", fw, "--yes", "--no-arm"])
        env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"])
        step(binary, "card burn", [burnable, node, "--dry-run"], node, "card", decoy, env=env)
    finally:
        sudo("losetup", "-d", node)


def listed_commands(binary):
    """Every command --help offers, as "group sub" or "name"."""
    def choices(*args):
        _, out, _ = run(binary + list(args) + ["--help"])
        m = re.search(r"\{([^}]*)\}", out)
        return m.group(1).split(",") if m else []
    found = []
    for c in choices():
        found += ["%s %s" % (c, sub) for sub in choices(c)] if c in GROUPS else [c]
    return found


def walk_checks(binary, loop):
    print("the commands --help offers")
    offered = listed_commands(binary)
    check(len(offered) >= 20, "--help offers the commands (%d)" % len(offered))
    for key in NOT_BUILT:
        rc, _, err = run(binary + key.split())
        check(rc == 2 and "not built yet" in err,
              "%s still says not built yet: when built, move it into a cell (#100-#103)" % key)
    for key, why in EXEMPT.items():
        print("  exempt %s: %s" % (key, why))
    for key in offered:
        if key in NOT_BUILT or key in EXEMPT:
            continue
        check(key in COVERED["image file"], "%s is checked on an image file" % key)
        if key in MUSIC:
            check(key in COVERED["mount"], "%s is checked on a mount" % key)
        if loop and key != "card new":
            check(key in COVERED["card"], "%s is checked on a card" % key)
    for kind, why in NOT_APPLICABLE.items():
        print("  not applicable on a %s: %s" % (kind, why))
    if not loop:
        print("  NOT RUN here: a card and a mounted image need sudo (--loop, as CI runs it)")


WRAPPER = """#!/bin/sh
exec "%(py)s" - "$@" <<'PY'
import subprocess, sys
args = sys.argv[1:]
drop = [a for a in args if a.startswith("/")]
key = %(key)r.split()
r = subprocess.run([%(bin)r] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
def strip(b):
    if args[:len(key)] != key:
        return b
    return b"".join(l for l in b.splitlines(True) if not any(d.encode() in l for d in drop))
sys.stdout.buffer.write(strip(r.stdout))
sys.stderr.buffer.write(strip(r.stderr))
sys.exit(r.returncode)
PY
"""


def mutants(binary, work):
    """Strip the target from one command at a time: the run must fail, on that
    command and no other."""
    keys = [k for k in listed_commands([binary]) if k not in NOT_BUILT and k not in EXEMPT]
    for key in keys:
        wrap = os.path.join(work, "strip-%s" % key.replace(" ", "-"))
        with open(wrap, "w") as f:
            f.write(WRAPPER % {"py": sys.executable, "key": key, "bin": binary})
        os.chmod(wrap, 0o755)
        rc, out, _ = run([sys.executable, os.path.abspath(__file__), "--rust", wrap])
        bad = [l.split(None, 1)[1] for l in out.split("\n") if l.startswith("  FAIL")]
        others = [b for b in bad if not b.startswith(key + " ")
                  and not b.startswith(key + ":")]
        check(rc != 0 and bad and not others,
              "the target stripped from %s: %d failed, all on it%s"
              % (key, len(bad), "" if not others else "; also " + "; ".join(others[:3])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--loop", action="store_true", help="also a loop device and a real mount (sudo -n)")
    ap.add_argument("--mutants", action="store_true")
    opts = ap.parse_args()
    binary = os.path.abspath(opts.rust)
    work = synth.scratch_dir("fp-targets-")
    if opts.mutants:
        mutants(binary, work)
    else:
        fw = synth.write_firmware(os.path.join(work, "fw.bin"))
        music = synth.make_music(os.path.join(work, "music"))
        os.unlink(os.path.join(music, "Beta Group", "broken.mp3"))     # add's rc is 1 with it
        decoy = os.path.join(work, "decoy.img")
        with open(decoy, "wb") as f:
            f.truncate(100 << 20)
        before = hashlib.sha256(open(decoy, "rb").read()).hexdigest()
        image_cells([binary], fw, music, work, decoy)
        mount_cells([binary], music, work)
        if opts.loop:
            loop_mount_cells([binary], fw, music, work)
            card_cells([binary], fw, music, work, decoy)
        walk_checks([binary], opts.loop)
        check(hashlib.sha256(open(decoy, "rb").read()).hexdigest() == before, "the other image was not touched")
    print("%s: %d failed" % ("FAIL" if FAILS else "PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
