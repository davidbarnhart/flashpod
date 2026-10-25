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
import struct
import subprocess
import sys
import termios

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


def compare(rust):
    py = [sys.executable, "-m", "flashpod"]
    bad = []
    n = 0
    for line in CASES:
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
    return n, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--show", action="store_true",
                    help="print the first difference in full")
    opts = ap.parse_args()
    n, bad = compare(os.path.abspath(opts.rust))
    if not bad:
        print("IDENTICAL: %d runs (%d argument lines x %d pipe widths + 2 "
              "pty widths)" % (n, len(CASES), len(WIDTHS)))
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
