#!/usr/bin/env python3
"""End-to-end flash test against a file-backed block device -- no card, no reader.

    sudo PYTHONPATH=. python3 scripts/simulate_card.py --music ~/some/mp3s

Creates a disk image, attaches it as a REAL block device (hdiutil on macOS,
losetup on Linux, a diskpart VHD on Windows), runs `flashpod flash` against
it, detaches, and verifies the backing file with scripts/verify_ipod_image.py.
A run that verifies clean then corrupts the image's MBR signature and demands
that verification FAIL -- proving the checker discriminates rather than
always passing.

Elevation is required: flashpod refuses any non-dry-run write without root,
and on Windows both `diskpart attach` and raw PhysicalDrive writes need an
Administrator prompt.

flashpod is driven through a pty on purpose (ConPTY via pywinpty on Windows).
The post-flash init offer is gated on sys.stdin.isatty(), so a plain pipe
would silently skip the very prompts this exists to exercise -- the offer,
and loading music.

Windows-specific limits:
  * the image must be a *fixed* VHD (raw data + a 512-byte footer) so the
    backing file stays flat enough to verify; a dynamic VHD has a block
    allocation table and is useless here. Fixed also means NO SPARSENESS:
    a --size-gb 128 run really writes 128 GB to disk. CI uses 2.
  * pip install pywinpty (the ConPTY driver).

Fidelity limits, stated up front:
  * the device is virtual, so `diskutil list physical` does not list it and it
    will NOT appear in flashpod's interactive picker; the device is passed
    explicitly here. Picker behaviour still needs a real reader.
  * no FireWire bridge, so the single-sector transfer cap that real gen-1
    hardware needs is not exercised.
  * the Windows arm exercises a different init path by design: Windows never
    mounts the partition -- init writes through the still-open raw handle via
    fatfs before the MBR exists (Platform.init_before_mbr).
"""
import argparse
import ctypes
import math
import os
import re
import subprocess
import sys
import tempfile
import time

IS_WIN = sys.platform == "win32"
if not IS_WIN:
    import pty
    import select

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
def _powershell(script):
    """Run a PowerShell snippet, return stdout text ('' on failure)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        if out.returncode == 0:
            return out.stdout
    except OSError:
        pass
    return ""


def _diskpart(commands):
    """Run a diskpart script; returns (returncode, combined output)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="flashpod-diskpart-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(commands) + "\n")
        out = subprocess.run(["diskpart", "/s", path],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True)
        return out.returncode, out.stdout
    finally:
        os.unlink(path)


def _attach_windows(img, size):
    """Create `img` as a fixed VHD of `size` bytes and attach it.

    Fixed, not dynamic ("expandable"): a fixed VHD is the raw image plus a
    512-byte footer, so verify_ipod_image.py can read the backing file
    directly. A dynamic VHD interleaves a block-allocation table and is not
    flat at all. The cost is that creation really writes every byte.

    diskpart rather than New-VHD: the Hyper-V PowerShell module that
    provides New-VHD is not installed on GitHub's windows runners.
    """
    if os.path.exists(img):
        os.remove(img)
    mib = max(3, int(math.ceil(size / (1 << 20))))
    say("creating %s (%d MiB fixed VHD -- fully allocated, not sparse)"
        % (img, mib))
    rc, out = _diskpart([
        'create vdisk file="%s" maximum=%d type=fixed' % (img, mib),
        "attach vdisk",           # create leaves the new vdisk selected
    ])
    if rc != 0 or not os.path.exists(img):
        sys.exit("diskpart create/attach failed:\n%s" % out)
    # Map the attached VHD to its PhysicalDrive number. Attach is
    # asynchronous enough that the disk object can lag; poll briefly.
    num = None
    for _ in range(50):
        txt = _powershell(
            "(Get-DiskImage -ImagePath '%s' | Get-Disk).Number" % img).strip()
        if txt.isdigit():
            num = int(txt)
            break
        time.sleep(0.2)
    if num is None:
        _diskpart(['select vdisk file="%s"' % img, "detach vdisk"])
        sys.exit("could not map %s to a PhysicalDrive number" % img)
    # A freshly attached disk can come up offline or read-only depending on
    # the SAN policy; clear both so the raw write isn't refused.
    _powershell("Set-Disk -Number %d -IsOffline $false "
                "-ErrorAction SilentlyContinue; "
                "Set-Disk -Number %d -IsReadOnly $false "
                "-ErrorAction SilentlyContinue" % (num, num))
    return "\\\\.\\PhysicalDrive%d" % num


def attach(img, size):
    """Create `img` (`size` bytes) and attach it as a block device; returns
    its node."""
    if IS_WIN:
        return _attach_windows(img, size)
    say("creating %s (%.1f GB, sparse)" % (img, size / 1e9))
    with open(img, "wb") as f:
        f.truncate(size)
    if sys.platform == "darwin":
        out = subprocess.run(
            ["hdiutil", "attach", "-imagekey", "diskimage-class=CRawDiskImage",
             "-nomount", img],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        if out.returncode != 0:
            sys.exit("hdiutil attach failed: %s" % out.stderr.strip())
        node = out.stdout.split()[0].strip()
        return node
    # Linux: a loop device with partition scanning, so loopNp2 appears
    out = subprocess.run(["losetup", "--find", "--show", "--partscan", img],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True)
    if out.returncode != 0:
        sys.exit("losetup failed: %s" % out.stderr.strip())
    return out.stdout.strip()


def detach(node, img):
    """Detach `node`, reporting what actually happened.

    A successful flash ends with flashpod ejecting the device, and for a disk
    image `diskutil eject` detaches it outright -- so by the time we get here
    the node is usually already gone. That is the good path, not a failure.
    (Windows differs: flashpod's eject only flushes, so the VHD is always
    still attached and the detach here always does the work.)
    """
    if not node:
        return True
    if IS_WIN:
        # Dismount-DiskImage rather than diskpart `select vdisk file=`: the
        # latter matches the attached instance by comparing path STRINGS, so
        # a short-name path (C:\Users\RUNNER~1\...) selects a phantom vdisk
        # and reports "already detached" while the real one stays attached --
        # holding an exclusive lock on the backing file, which then cannot
        # be verified.
        _powershell("Dismount-DiskImage -ImagePath '%s'" % img)
        for _ in range(25):
            txt = _powershell(
                "(Get-DiskImage -ImagePath '%s').Attached" % img).strip()
            if txt.lower() == "false":
                break
            time.sleep(0.2)
        else:
            rc, out = _diskpart(['select vdisk file="%s"' % img,
                                 "detach vdisk"])
            if rc != 0 and "already detached" not in out:
                say("WARNING: could not detach %s:\n%s"
                    % (node, out.strip()), RED)
                return False
        # The lock can outlive the dismount by a moment; verification needs
        # the file readable, so wait for that, not just for detach to return.
        for _ in range(25):
            try:
                with open(img, "rb"):
                    pass
                break
            except OSError:
                time.sleep(0.2)
        else:
            say("WARNING: %s is still locked after detach" % img, RED)
            return False
        say("detached %s" % node)
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


# ConPTY renders through a virtual terminal, so the output stream carries
# escape sequences (colors, cursor moves, window titles) interleaved with the
# text. Strip them before matching or the $-anchored prompt patterns never
# fire. Harmless on the Unix ptys too, where flashpod's own colors appear.
_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC (titles)
                      r"|\x1b\[[0-9;?]*[ -/]*[@-~]")         # CSI


class RepromptLoop(Exception):
    """The same prompt fired more than 3 times: our answer isn't taking."""


class PromptMatcher:
    """Feeds output chunks in, hands prompt replies back.

    The match window is cleared after each answer so our own echo cannot
    re-trigger the pattern just satisfied.
    """

    def __init__(self, answers):
        self.answers = answers
        self.counts = {}
        self.buf = ""

    def feed(self, text):
        """Accumulate `text`; return the reply now due, or None."""
        self.buf += text
        tail = _ANSI_RE.sub("", self.buf[-400:])
        for i, (pat, reply) in enumerate(self.answers):
            if not pat.search(tail):
                continue
            self.counts[i] = self.counts.get(i, 0) + 1
            if self.counts[i] > 3:
                raise RepromptLoop("pattern %d asked more than 3 times; "
                                   "giving up" % i)
            self.buf = ""
            return reply
        return None


def _run_flashpod_posix(argv, matcher, timeout, idle_warn):
    """Run flashpod on a pty, answering its prompts. Returns (status, output).

    Anything sitting quiet for `idle_warn` seconds gets its tail dumped: an
    unanswered prompt is the likely cause and guessing at it from a frozen
    terminal is miserable.
    """
    pid, fd = pty.fork()
    if pid == 0:                                   # child
        os.execvp(argv[0], argv)
        os._exit(127)

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
                full += text
                last_out = time.time()
                warned = False
                try:
                    reply = matcher.feed(text)
                except RepromptLoop as exc:
                    say(str(exc), RED)
                    os.kill(pid, 9)
                    break
                if reply is not None:
                    time.sleep(0.2)
                    os.write(fd, (reply + "\n").encode())
                    say("answered %r" % reply)
            elif not warned and time.time() - last_out > idle_warn:
                warned = True
                say("no output for %ds -- flashpod may be waiting on a prompt "
                    "we have no answer for. Tail:" % idle_warn, RED)
                print("---8<---\n%s\n---8<---" % matcher.buf[-300:], flush=True)
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


def _run_flashpod_conpty(argv, matcher, timeout, idle_warn):
    """The Windows twin of _run_flashpod_posix, on a ConPTY via pywinpty.

    ConPTY gives the child real console handles, so sys.stdin.isatty() is
    True inside flashpod and the init/music prompts actually fire -- the
    whole reason this harness avoids pipes. pywinpty's read() blocks with no
    timeout, so a pump thread feeds a queue and the loop polls that instead.
    """
    try:
        from winpty import PtyProcess
    except ImportError:
        sys.exit("pywinpty is required on Windows: pip install pywinpty")
    import queue
    import threading

    # A wide terminal so no prompt line reaches the edge: ConPTY hard-wraps
    # at the width, and a wrapped prompt would defeat the $-anchoring.
    proc = PtyProcess.spawn(argv, dimensions=(100, 250),   # (rows, cols)
                            env=dict(os.environ))
    chunks = queue.Queue()

    def pump():
        while True:
            try:
                data = proc.read(4096)
            except (EOFError, OSError):
                break
            if not data:
                break
            chunks.put(data)
        chunks.put(None)

    threading.Thread(target=pump, daemon=True).start()

    full = ""
    deadline = time.time() + timeout
    last_out = time.time()
    warned = False
    while True:
        if time.time() > deadline:
            say("TIMEOUT after %ds -- killing flashpod" % timeout, RED)
            proc.terminate(force=True)
            break
        try:
            data = chunks.get(timeout=1.0)
        except queue.Empty:
            if not warned and time.time() - last_out > idle_warn:
                warned = True
                say("no output for %ds -- flashpod may be waiting on a prompt "
                    "we have no answer for. Tail:" % idle_warn, RED)
                print("---8<---\n%s\n---8<---" % matcher.buf[-300:], flush=True)
            if not proc.isalive():
                break
            continue
        if data is None:
            break
        text = data if isinstance(data, str) else data.decode("utf-8", "replace")
        sys.stdout.write(text)
        sys.stdout.flush()
        full += text
        last_out = time.time()
        warned = False
        try:
            reply = matcher.feed(text)
        except RepromptLoop as exc:
            say(str(exc), RED)
            proc.terminate(force=True)
            break
        if reply is not None:
            time.sleep(0.2)
            # "\r" is Enter to a Windows console; a trailing "\n" would be
            # read as a SECOND Enter and pre-answer the next [Y/n] prompt.
            proc.write(reply + "\r")
            say("answered %r" % reply)

    for _ in range(100):
        if not proc.isalive():
            break
        time.sleep(0.1)
    rc = proc.exitstatus
    return (rc if rc is not None else 1), full


def run_flashpod(argv, music_dir, dev, model="0", timeout=1800, idle_warn=25):
    """Run flashpod on a pty/ConPTY, answering its prompts.
    Returns (status, output)."""
    matcher = PromptMatcher(build_answers(dev, music_dir, model))
    runner = _run_flashpod_conpty if IS_WIN else _run_flashpod_posix
    return runner(argv, matcher, timeout, idle_warn)


# -- elevation ----------------------------------------------------------------
def require_elevation():
    if IS_WIN:
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:                                   # noqa: BLE001
            admin = False
        if not admin:
            sys.exit("this needs an Administrator prompt (diskpart attach and "
                     "raw PhysicalDrive writes both require it)")
    elif os.geteuid() != 0:
        sys.exit("this needs root (flashpod refuses non-dry-run writes without "
                 "it):\n  sudo PYTHONPATH=. python3 %s ..." % sys.argv[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size-gb", type=float, default=128.0,
                    help="simulated card size in decimal GB (default 128; "
                         "NOT sparse on Windows -- the VHD really is this big)")
    ap.add_argument("--music", help="file or folder of audio to load onto the card")
    ap.add_argument("--firmware", help="firmware image (skips the interactive picker)")
    ap.add_argument("--image", help="where to keep the backing file "
                                    "(default: a temp path; .vhd on Windows)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the backing file after verifying")
    ap.add_argument("--model", default="0",
                    help="answer for the model picker (default 0 = 1st gen)")
    ap.add_argument("--flashpod", default=None,
                    help="flashpod command to test (default: this checkout)")
    opts = ap.parse_args()

    require_elevation()

    img = opts.image or os.path.join(
        tempfile.gettempdir(),
        "flashpod-sim-card.vhd" if IS_WIN else "flashpod-sim-card.img")
    # Resolve 8.3 short names (C:\Users\RUNNER~1\...) to the long form NOW:
    # the virtual-disk service matches vdisks by path string, and a short
    # spelling at detach time selects a phantom instead of the attached disk.
    img = os.path.join(os.path.realpath(os.path.dirname(img) or "."),
                       os.path.basename(img))
    if IS_WIN and not img.lower().endswith(".vhd"):
        # diskpart picks the VirtDisk provider from the extension; anything
        # else fails with "the specified file extension is not valid".
        sys.exit("--image on Windows must end in .vhd")
    size = int(opts.size_gb * 1000 * 1000 * 1000)

    music = opts.music
    if not music:
        music = os.path.join(os.path.dirname(img), "flashpod-sim-music")
        made = generate_music(music)
        say("generated %d test WAVs in %s (pass --music for real audio)"
            % (len(made), music))

    node = None
    try:
        node = attach(img, size)
        say("attached as %s" % node)
        if not IS_WIN:
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
        detach(node, img)

    say("verifying the backing file")
    vargv = [sys.executable, os.path.join(ROOT, "scripts", "verify_ipod_image.py"),
             img, "--min-tracks", "1"]
    if opts.firmware:
        vargv += ["--firmware", opts.firmware]
    # Capture and reprint rather than letting the child inherit the console:
    # on the Windows runner the verifier's directly-written output never
    # reached the log (post-ConPTY console handles are the suspect), which
    # turned a failing verify into a silent exit code. The parent's stdout
    # demonstrably works -- route everything through it.
    v = subprocess.run(vargv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    sys.stdout.write(v.stdout or "")
    sys.stdout.flush()
    ret = v.returncode
    say("verifier exited %d" % ret, GRN if ret == 0 else RED)

    if ret == 0:
        # The checker must discriminate, not just pass: break the MBR
        # signature, demand a failure, put the original bytes back.
        say("negative check: corrupting the MBR signature -- verification "
            "must now FAIL")
        with open(img, "r+b") as f:
            f.seek(510)
            saved = f.read(2)
            f.seek(510)
            f.write(b"\x00\x00")
        bad = subprocess.run(vargv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        with open(img, "r+b") as f:
            f.seek(510)
            f.write(saved)
        if bad.returncode == 0:
            say("verifier PASSED a corrupted image -- it does not "
                "discriminate", RED)
            ret = 1
        else:
            say("corrupted image rejected (exit %d), as it should be"
                % bad.returncode, GRN)

    if not opts.keep:
        try:
            os.remove(img)
            say("removed %s" % img)
        except OSError as exc:
            # A remove that fails means the image is still held open (an
            # undetached VHD, say) -- exactly the kind of state worth
            # hearing about rather than swallowing.
            say("WARNING: could not remove %s: %s" % (img, exc), RED)
    else:
        say("kept %s" % img)
    return ret


if __name__ == "__main__":
    sys.exit(main())
