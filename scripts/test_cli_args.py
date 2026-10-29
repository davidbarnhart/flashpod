#!/usr/bin/env python3
"""The Rust flashpod parses its command line exactly as the Python one:
help, --version and every usage error, byte for byte (rc, stdout, stderr).

    python3 scripts/test_cli_args.py --rust rust/target/release/flashpod

Each argument line runs in both builds through a pipe at several terminal
widths (COLUMNS), then once more under a pseudo-terminal, where Python
3.14's argparse colours its help and usage. Only lines that end inside
argparse are listed (help, version, errors), so nothing touches a disk.
Run it with the Python that runs flashpod (3.14): argparse's wording
differs between versions. --mutants proves the comparison can fail.
"""
import argparse
import fcntl
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout1  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASES = [
    "", "-h", "--help", "--version", "--vers", "--ver", "-hx", "-x",
    "bogus", "--raw", "--raw=", "--mount", "--m", "--un", "--unsafe-queue",
    "--", "ls --version", "ls -h", "list -h", "ls bogus",
    "ls all extra", "ls --fir x", "ls --raw", "ls -- bogus", "ls --raw=x bogus",
    "add -h", "add --foo", "add a --raw x b", "rm", "rm -h", "remove -h",
    "rm --raw x", "erase", "init -h", "init a b", "rebuild -h", "rebuild a b",
    "burn", "burn -h", "burn --yes", "burn a b c", "burn --dry-run=1 x",
    "flash -h", "flash --arm --no-arm x", "flash --no-arm --arm",
    "flash --max-data-gb abc x", "flash --max-data-gb", "flash --firmware",
    "flash --firmware= --yes=y x", "flash --yes --dry-run --raw x",
    "flash --fi", "flash --no", "flash a b", "flash -yes",
    "--raw x --mount y bogus", "-5", "ls -5",
    "ls 'with space'", "--mount=x --raw=y",
    # a positional matched empty before an option waits for later strings
    "burn img --yes dev extra", "burn img --yes dev extra more",
]

WIDTHS = [None, "16", "18", "22", "30", "50", "62", "80", "101", "200"]


def split(line):
    import shlex
    return shlex.split(line)


def env_for(cols):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8", "PYTHONPATH": HERE, "HOME": "/nonexistent",
           "TERM": "xterm"}
    if cols:
        env["COLUMNS"] = cols
    return env


def run_pipe(cmd, env):
    p = subprocess.run(cmd, env=env, cwd="/", stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=60)
    return p.returncode, p.stdout, p.stderr


def run_pty(cmd, env, cols=80):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
    p = subprocess.Popen(cmd, env=env, cwd="/", stdin=subprocess.DEVNULL,
                         stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    out = b""
    while True:
        r, _, _ = select.select([master], [], [], 5)
        if not r:
            break
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        out += data
    os.close(master)
    return p.wait(), out, b""


# 1.0 layout (#97): the new groups, their help and their usage errors
CASES_1_0 = [
    "music", "music -h", "music bogus", "music ls bogus", "music ls -h", "music list -h",
    "music rm", "music add --foo", "music export -h", "music playlist -h",
    "firmware", "firmware -h", "firmware bogus", "firmware write -h", "firmware write a b",
    "firmware write --arm --no-arm x", "firmware extract -h", "firmware list -h", "firmware list x",
    "firmware patch -h", "card", "card -h", "card burn", "card burn -h", "card new -h",
    "inspect -h", "emulate -h", "flash -h", "ls", "--mount x rm 5", "burn a",
    "firmware extract", "firmware extract x", "firmware extract x -o", "firmware extract x -o f --image bogus",
    "firmware extract -o f", "firmware extract a b -o f",
    "music export --to", "music export --other-files --no-other-files", "music export x",
    "music playlist", "music playlist -h", "music playlist new", "music playlist add x",
    "music playlist rm -h", "music playlist delete", "music playlist ls a b", "music playlist bogus",
    "inspect", "inspect a b", "inspect --json", "card new", "card new x", "card new x 1G y",
]


def compare(rust, layout_1=False):
    """Every case at every width in both builds. With layout_1 the Rust
    binary runs the 1.0 spelling of each case (and CASES_1_0 besides), and
    the reference is scripts/layout1_parser.py: the same 1.0 tree declared
    with Python's own argparse."""
    if layout_1:
        py = [sys.executable, os.path.join(HERE, "scripts", "layout1_parser.py")]
        cases = [shlex.join(layout1.argv_1_0(split(c))) for c in CASES] + CASES_1_0
    else:
        py = [sys.executable, "-m", "flashpod"]
        cases = CASES
    bad = []
    n = 0
    for line in cases:
        argv = split(line)
        for cols in WIDTHS:
            env = env_for(cols)
            a = run_pipe(py + argv, env)
            b = run_pipe([rust] + argv, env)
            n += 1
            if a != b:
                bad.append((line, "COLUMNS=%s" % cols, a, b))
        for cols in (80, 44):
            env = env_for(None)
            a = run_pty(py + argv, env, cols)
            b = run_pty([rust] + argv, env, cols)
            n += 1
            if a != b:
                bad.append((line, "pty %d cols" % cols, a, b))
    return n, bad, len(cases)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--layout-1", action="store_true",
                    help="the Rust side runs the 1.0 command names; report which lines differ")
    ap.add_argument("--show", action="store_true",
                    help="print the first difference in full")
    opts = ap.parse_args()
    n, bad, ncases = compare(os.path.abspath(opts.rust), opts.layout_1)
    if not bad:
        print("IDENTICAL: %d runs (%d argument lines x %d pipe widths + 2 "
              "pty widths)%s" % (n, ncases, len(WIDTHS),
                                 ", 1.0 layout against scripts/layout1_parser.py" if opts.layout_1 else ""))
        return 0
    print("DIFFERENT: %d of %d runs" % (len(bad), n))
    for line, how, a, b in bad[:20]:
        print("  %r (%s): rc %s vs %s" % (line, how, a[0], b[0]))
    if opts.show:
        line, how, a, b = bad[0]
        for name, x, y in (("stdout", a[1], b[1]), ("stderr", a[2], b[2])):
            if x != y:
                print("--- python %s\n%r\n--- rust %s\n%r" % (name, x.decode(errors="replace"),
                                                             name, y.decode(errors="replace")))
    return 1


if __name__ == "__main__":
    sys.exit(main())
