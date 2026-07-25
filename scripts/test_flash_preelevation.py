#!/usr/bin/env python3
"""Logic test: `flash` resolves its firmware BEFORE elevating via sudo.

Mocks the platform, the pickers, the download and sudo, so no device is
touched and no network is used.

    PYTHONPATH=. python3 scripts/test_flash_preelevation.py

Why this ordering is load-bearing: the firmware download writes into
~/.cache/flashpod. Done after elevation it runs as root, and sudo's handling
of HOME differs by platform -- macOS keeps the user's HOME (leaving root-owned
files that break every later unprivileged run with EACCES), Linux resets it to
/root (so the cache is silently never reused). Fetching first, as the invoking
user, avoids both; the elevated child is then handed the finished image with
--firmware and re-runs neither the picker nor the download.
"""
import sys
import types

sys.argv = ["flashpod"]
from flashpod import cli


class FakePlat:
    def __init__(self, admin):
        self._admin = admin

    def is_admin(self):
        return self._admin

    def privilege_hint(self):
        return "need root"


def run(name, argv, admin=False, tty=True, entry=None):
    """Drive cli.main() over `argv`, returning the recorded event log."""
    events = []
    entry = entry or {"file": "fw.bin.gz", "version": "1.5"}

    cli.platform.current = lambda: FakePlat(admin)
    cli.sys.stdin = types.SimpleNamespace(isatty=lambda: tty)
    cli.choose_model = lambda man, want=None: {"id": "1g", "name": "1st generation"}
    cli.choose_firmware = lambda man, model: entry

    def fake_ensure(e, base_url):
        events.append(("download", e.get("file")))
        return "/cache/" + e.get("file")
    cli.ensure_firmware = fake_ensure

    def fake_reexec(extra):
        events.append(("reexec", list(extra)))
        return                                  # pretend sudo is unavailable
    cli._sudo_reexec = fake_reexec

    def fake_flash(**kw):
        events.append(("flash", kw.get("firmware"), kw.get("max_data_gb")))
        return 0
    cli.ipod_flash.flash = fake_flash

    sys.argv = ["flashpod"] + argv
    rc = cli.main()
    print("\n[%s] rc=%s" % (name, rc))
    for e in events:
        print("    %r" % (e,))
    return events


def kinds(events):
    return [e[0] for e in events]


def reexec_args(events):
    for e in events:
        if e[0] == "reexec":
            return e[1]
    raise AssertionError("sudo re-exec never happened: %r" % (events,))


# -- THE ORDERING PROPERTY ----------------------------------------------------
ev = run("not root: download happens before the sudo re-exec", ["flash", "/dev/fake"])
assert kinds(ev) == ["download", "reexec"], \
    "expected download then reexec, got %r" % (kinds(ev),)

# and the resolved image is handed to the elevated child
args = reexec_args(ev)
assert "--firmware" in args, "--firmware not forwarded across sudo: %r" % (args,)
assert args[args.index("--firmware") + 1] == "/cache/fw.bin.gz", args
assert "flash" in args and "/dev/fake" in args, args

# -- the manifest's data cap must survive the boundary ------------------------
# The child runs the --firmware path, where the manifest entry is never
# consulted, so a cap that is not forwarded is silently lost.
ev = run("manifest max_data_gb is forwarded to the child",
         ["flash", "/dev/fake"],
         entry={"file": "fw.bin.gz", "max_data_gb": 120.0})
args = reexec_args(ev)
assert "--max-data-gb" in args, "manifest cap dropped across sudo: %r" % (args,)
assert args[args.index("--max-data-gb") + 1] == "120.0", args

# an explicit --max-data-gb still wins over the manifest's
ev = run("explicit --max-data-gb overrides the manifest cap",
         ["flash", "/dev/fake", "--max-data-gb", "64"],
         entry={"file": "fw.bin.gz", "max_data_gb": 120.0})
args = reexec_args(ev)
assert args[args.index("--max-data-gb") + 1] == "64.0", args

# -- bring-your-own firmware never downloads ----------------------------------
ev = run("--firmware skips the picker and the download",
         ["flash", "/dev/fake", "--firmware", "/tmp/mine.bin"])
assert "download" not in kinds(ev), "BYO firmware still downloaded: %r" % (kinds(ev),)
args = reexec_args(ev)
assert args[args.index("--firmware") + 1] == "/tmp/mine.bin", args

# -- already root: no re-exec, flash proceeds with the resolved image ---------
ev = run("already root: resolves and flashes, no re-exec", ["flash", "/dev/fake"],
         admin=True)
assert "reexec" not in kinds(ev), "re-exec attempted while already root: %r" % (kinds(ev),)
assert kinds(ev) == ["download", "flash"], kinds(ev)
assert ev[-1][1] == "/cache/fw.bin.gz", ev

# -- dry-run never elevates ---------------------------------------------------
ev = run("dry-run: no elevation", ["flash", "/dev/fake", "--dry-run", "--yes"])
assert "reexec" not in kinds(ev), "dry-run tried to elevate: %r" % (kinds(ev),)
assert kinds(ev) == ["download", "flash"], kinds(ev)

print("\nALL ASSERTIONS PASSED")
