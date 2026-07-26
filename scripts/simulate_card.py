#!/usr/bin/env python3
"""End-to-end flash test against a file-backed block device -- no card, no reader.

    sudo PYTHONPATH=. python3 scripts/simulate_card.py --music ~/some/mp3s

Creates a sparse image, attaches it as a REAL block device (hdiutil on macOS,
losetup on Linux), runs `flashpod flash` against it, detaches, and verifies the
backing file with scripts/verify_ipod_image.py.

Root is required: flashpod refuses any non-dry-run write without it.

flashpod is driven through a pty on purpose. The post-flash init offer is gated
on sys.stdin.isatty(), so a plain pipe would silently skip the very prompts
this exists to exercise -- the offer, and loading music.

Fidelity limits, stated up front:
  * the device is virtual, so `diskutil list physical` does not list it and it
    will NOT appear in flashpod's interactive picker; the device is passed
    explicitly here. Picker behaviour still needs a real reader.
  * no FireWire bridge, so the single-sector transfer cap that real gen-1
    hardware needs is not exercised.
"""
import argparse
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GRN, RED, YEL, RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def say(msg, colour=YEL):
    print("%s>> %s%s" % (colour, msg, RST), flush=True)


def generate_music(dirpath, count=3, seconds=2):
    """Write `count` small but genuinely valid WAV files.

    WAV is in flashpod's AUDIO_EXTS and the stdlib can author it, so the test
    needs no encoder and no music of your own. A real .mp3 exercises the tag
    reader more thoroughly -- pass --music for that.
    """
    import math
    import struct as _struct
    import wave
    os.makedirs(dirpath, exist_ok=True)
    rate = 8000
    made = []
    for i in range(count):
        path = os.path.join(dirpath, "test-track-%02d.wav" % (i + 1))
        freq = 220.0 * (i + 1)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            frames = bytearray()
            for n in range(rate * seconds):
                v = int(16000 * math.sin(2 * math.pi * freq * n / rate))
                frames += _struct.pack("<h", v)
            w.writeframes(bytes(frames))
        made.append(path)
    return made


# -- attaching a file as a block device ---------------------------------------
def attach(path):
    """Attach `path` as a block device; returns its node."""
    if sys.platform == "darwin":
        out = subprocess.run(
            ["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage",
             "-nomount", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        if out.returncode != 0:
            sys.exit("hdiutil attach failed: %s" % out.stderr.strip())
        node = out.stdout.split()[0].strip()
        return node
    # Linux: a loop device with partition scanning, so loopNp2 appears
    out = subprocess.run(["losetup", "--find", "--show", "--partscan", path],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    if out.returncode != 0:
        sys.exit("losetup failed: %s" % out.stderr.strip())
    return out.stdout.strip()


def detach(node):
    """Detach `node`, reporting what actually happened.

    A successful flash ends with flashpod ejecting the device, and for a disk
    image `diskutil eject` detaches it outright -- so by the time we get here
    the node is usually already gone. That is the good path, not a failure.
    """
    if not node:
        return True
    if not os.path.exists(node):
        say("%s was already detached (flashpod ejected it)" % node, GRN)
        return True
    if sys.platform == "darwin":
        for attempt in range(5):
            r = subprocess.run(["hdiutil", "detach", node] +
                               (["-force"] if attempt else []),
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               universal_newlines=True)
            if r.returncode == 0 or not os.path.exists(node):
                say("detached %s" % node)
                return True
            time.sleep(1)
        say("WARNING: %s is still attached — detach it by hand with "
            "`hdiutil detach %s -force`" % (node, node), RED)
        return False
    r = subprocess.run(["losetup", "-d", node], stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode == 0:
        say("detached %s" % node)
        return True
    say("WARNING: could not detach %s: %s" % (node, r.stderr.strip()), RED)
    return False


# -- driving flashpod through a pty -------------------------------------------
def build_answers(dev, music_dir, model="0"):
    """(regex, reply) for every prompt flashpod can raise in this flow.

    Matched against the tail of the output, so each pattern is anchored at
    end-of-buffer -- a prompt is the thing sitting there with no newline after
    it. Miss one and the run just hangs, so keep this in step with the
    input() calls in cli.py.
    """
    return [
        # firmware selection (skipped entirely when --firmware is passed)
        (re.compile(r"Select model:\s*$"),                        model),
        (re.compile(r"Select firmware \[\d+\]:\s*$"),             ""),   # default
        # the destructive confirmation (skipped by --yes, here for completeness)
        (re.compile(r'Type "ERASE [^"]+" to proceed:\s*$'),
         "ERASE " + os.path.basename(dev)),
        # the post-flash init offer -- the whole point of the exercise
        (re.compile(r"Run init on .* now\? \[Y/n\]\s*$"),         "y"),
        (re.compile(r"Load music onto the card now\? \[Y/n\]\s*$"), "y"),
        (re.compile(r"File or directory to add \(TAB to complete\):\s*$"),
         music_dir),
    ]


def run_flashpod(argv, music_dir, dev, model="0", timeout=1800, idle_warn=25):
    """Run flashpod on a pty, answering its prompts. Returns (status, output).

    Anything sitting quiet for `idle_warn` seconds gets its tail dumped: an
    unanswered prompt is the likely cause and guessing at it from a frozen
    terminal is miserable.
    """
    answers = build_answers(dev, music_dir, model)
    counts = {}
    pid, fd = pty.fork()
    if pid == 0:                                   # child
        os.execvp(argv[0], argv)
        os._exit(127)

    buf = ""          # match window: cleared after each answer so our own echo
                      # cannot re-trigger the pattern we just satisfied
    full = ""         # the whole transcript, for the caller to inspect
    deadline = time.time() + timeout
    last_out = time.time()
    warned = False
    try:
        while True:
            if time.time() > deadline:
                say("TIMEOUT after %ds -- killing flashpod" % timeout, RED)
                os.kill(pid, 9)
                break
            r, _, _ = select.select([fd], [], [], 1.0)
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                buf += text
                full += text
                last_out = time.time()
                warned = False
                tail = buf[-400:]
                for i, (pat, reply) in enumerate(answers):
                    if not pat.search(tail):
                        continue
                    if counts.get(i, 0) >= 3:       # stop a reprompt loop
                        say("pattern %d asked more than 3 times; giving up" % i, RED)
                        os.kill(pid, 9)
                        break
                    counts[i] = counts.get(i, 0) + 1
                    time.sleep(0.2)
                    os.write(fd, (reply + "\n").encode())
                    say("answered %r" % reply)
                    buf = ""                        # don't re-match our own echo
                    break
            elif not warned and time.time() - last_out > idle_warn:
                warned = True
                say("no output for %ds -- flashpod may be waiting on a prompt "
                    "we have no answer for. Tail:" % idle_warn, RED)
                print("---8<---\n%s\n---8<---" % buf[-300:], flush=True)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return (status >> 8), full
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        _, status = os.waitpid(pid, 0)
        return (status >> 8), full
    except ChildProcessError:
        return 0, full


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size-gb", type=float, default=128.0,
                    help="simulated card size in decimal GB (default 128)")
    ap.add_argument("--music", help="file or folder of audio to load onto the card")
    ap.add_argument("--firmware", help="firmware image (skips the interactive picker)")
    ap.add_argument("--image", help="where to keep the backing file "
                                    "(default: a temp path)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the backing file after verifying")
    ap.add_argument("--model", default="0",
                    help="answer for the model picker (default 0 = 1st gen)")
    ap.add_argument("--flashpod", default=None,
                    help="flashpod command to test (default: this checkout)")
    opts = ap.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        sys.exit("this needs root (flashpod refuses non-dry-run writes without "
                 "it):\n  sudo PYTHONPATH=. python3 %s ..." % sys.argv[0])

    img = opts.image or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "flashpod-sim-card.img")
    size = int(opts.size_gb * 1000 * 1000 * 1000)

    music = opts.music
    if not music:
        music = os.path.join(os.path.dirname(img), "flashpod-sim-music")
        made = generate_music(music)
        say("generated %d test WAVs in %s (pass --music for real audio)"
            % (len(made), music))

    say("creating %s (%.0f GB, sparse)" % (img, opts.size_gb))
    with open(img, "wb") as f:
        f.truncate(size)

    node = None
    try:
        node = attach(img)
        say("attached as %s" % node)
        subprocess.run(["ls", "-la", node])

        cmd = opts.flashpod.split() if opts.flashpod else \
            [sys.executable, "-m", "flashpod"]
        argv = cmd + ["flash", node, "--yes"]
        if opts.firmware:
            argv += ["--firmware", opts.firmware]
        say("running: %s" % " ".join(argv))

        env_root = os.environ.copy()
        env_root.setdefault("PYTHONPATH", ROOT)
        os.environ.update(env_root)

        rc, _ = run_flashpod(argv, music, node, model=opts.model)
        say("flashpod exited %d" % rc, GRN if rc == 0 else RED)
    finally:
        detach(node)

    say("verifying the backing file")
    vargv = [sys.executable, os.path.join(ROOT, "scripts", "verify_ipod_image.py"),
             img, "--min-tracks", "1"]
    if opts.firmware:
        vargv += ["--firmware", opts.firmware]
    v = subprocess.run(vargv)

    if not opts.keep:
        try:
            os.remove(img)
            say("removed %s" % img)
        except OSError:
            pass
    else:
        say("kept %s" % img)
    return v.returncode


if __name__ == "__main__":
    sys.exit(main())
