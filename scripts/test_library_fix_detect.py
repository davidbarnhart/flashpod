#!/usr/bin/env python3
"""`add` lifts the iTunesDB byte ceiling on firmware with the library-leak fix.

    PYTHONPATH=. python3 scripts/test_library_fix_detect.py [--asset flashpod_1.1.6_....bin.gz]

Stock 1G/2G firmware shows zero songs once the iTunesDB passes ~3.78 MB, so
`add` refuses (or trims) past IPOD_DB_SIZE_LIMIT. openpod's library-leak fix,
which flashpod 1.6 carries, removes that ceiling; what's left is the
firmware's 32,000-track playlist cap. Past the byte limit -- and only then --
`add` now reads the card's firmware for the fix (firmware.fix_on_card, the
fixes_present test reading only the fix's bytes) and applies the track cap.

On synthetic cards: a synthetic osos long enough to hold the fix's site, with
the fix's patched bytes there ("fixed") or not ("stock"). The byte and track
limits are shrunk in-process so a few synthetic songs cross them (setting
FLASHPOD_DB_LIMIT instead would switch the firmware check off, by design).
  * fix_on_card: True on the fixed card, False on stock, None on a card
    without firmware; it reads only a few sectors;
  * card.add past the byte limit: added on the fixed card (plan: kind
    "tracks", library_fix True); DbCeiling (kind "bytes") on stock;
  * the track cap: refused past it; policy "fit" adds up to it;
  * FLASHPOD_DB_LIMIT set: taken at its word, the firmware isn't read;
  * the firmware is read only past the byte limit;
  * card.inspect: ceiling kind "tracks" on the fixed card, "bytes" on stock;
  * the CLI, `add --raw` on an image: adds on the fixed card, saying why;
    refuses on stock; over a mount (MountTarget) it reads the firmware
    through the reader the CLI supplies, and says so when it can't.
With --asset also: a card flashed with the real flashpod 1.6 has the fix,
and Apple's 1.5 (from the cache, if there) doesn't.
"""
import argparse
import builtins
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, cli, firmware, layout, library, platform  # noqa: E402
from flashpod.target import MountTarget, firmware_reader  # noqa: E402

fails = []
SITE = 0x28034dd4 - synth.OSOS_ADDR


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def fix_bytes():
    table = firmware._fixes_table()
    fix = next(f for f in table["fixes"] if f["name"] == library.LIBRARY_FIX)
    return bytes.fromhex(fix["edits"][0]["patched"])


def osos(fixed):
    p = bytearray(synth.osos_payload(length=0x36000))
    if fixed:
        new = fix_bytes()
        p[SITE:SITE + len(new)] = new
    return bytes(p)


def fw_reader(img):
    """read(offset, n) over an image's firmware partition, counting bytes."""
    f = open(img, "rb")
    count = [0]

    def sectors(lba, n):
        count[0] += n * 512
        f.seek(lba * 512)
        return f.read(n * 512)
    return firmware_reader(sectors), count


def run_cli(argv):
    out = io.StringIO()
    platform._cached = None
    saved = sys.argv, sys.stdout, sys.stderr, sys.stdin.isatty
    sys.argv = ["flashpod"] + argv
    sys.stdout = sys.stderr = out
    sys.stdin.isatty = lambda: False
    try:
        try:
            rc = cli.main()
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv, sys.stdout, sys.stderr, sys.stdin.isatty = saved
    return rc, out.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default=None)
    opts = ap.parse_args()
    os.environ.pop("FLASHPOD_DB_LIMIT", None)
    tmp = tempfile.mkdtemp(prefix="fp-libfix-")
    music = synth.make_music(os.path.join(tmp, "music"))
    alpha = os.path.join(music, "Alpha Band")
    beta = os.path.join(music, "Beta Group", "Only Album", "03 Third.mp3")
    fw = {}
    for kind in ("fixed", "stock"):
        fw[kind] = os.path.join(tmp, kind + ".bin")
        open(fw[kind], "wb").write(
            synth.firmware_bytes(payload=osos(kind == "fixed")))

    def fresh(name, kind):
        p = os.path.join(tmp, name)
        card.create_blank(p, 96 << 20)
        card.flash(p, fw[kind])
        card.init(p)
        return p

    print("fix_on_card")
    fixed_img, stock_img = fresh("f.img", "fixed"), fresh("s.img", "stock")
    read, count = fw_reader(fixed_img)
    check(firmware.fix_on_card(read, library.LIBRARY_FIX) is True,
          "the fixed card has it")
    check(count[0] <= 8 * 512, "read only %d bytes" % count[0])
    read, _ = fw_reader(stock_img)
    check(firmware.fix_on_card(read, library.LIBRARY_FIX) is False,
          "the stock card doesn't")
    blank = os.path.join(tmp, "blank.img")
    card.create_blank(blank, 96 << 20)
    check(fw_reader(blank)[0] is None, "a blank card has no firmware partition")

    # shrink the limits so a few synthetic songs cross them
    one = len(library.itunesdb.serialize(card.library(fixed_img)))
    library.IPOD_DB_SIZE_LIMIT = one + 600       # about one track fits
    library.IPOD_PATCHED_TRACK_LIMIT = 4

    print("card.add past the byte limit")
    plan = card.plan_add(fixed_img, [alpha])
    check(plan.db["kind"] == "tracks" and plan.db["library_fix"] is True
          and plan.db["fit"] == len(plan.new),
          "fixed: plan says track cap, all %d fit (%r)"
          % (len(plan.new), plan.db))
    try:
        added = len(card.add(fixed_img, [alpha]).added)
    except card.DbCeiling as e:
        added = "DbCeiling: %s" % e
    check(added == 3, "fixed: all 3 added (%r)" % (added,))
    try:
        card.add(stock_img, [alpha])
        got = None
    except card.DbCeiling as e:
        got = e.details.get("kind")
    check(got == "bytes", "stock: DbCeiling, kind bytes (%r)" % got)
    plan = card.plan_add(stock_img, [alpha])
    check(plan.db["kind"] == "bytes" and plan.db["library_fix"] is False,
          "stock: plan says byte limit, firmware read: no fix")

    print("the track cap")
    try:
        card.add(fixed_img, [beta, os.path.join(music, "Beta Group")])
        got = None
    except card.DbCeiling as e:
        got = (e.details.get("kind"), e.details.get("fit"))
    check(got == ("tracks", 1), "past 4 tracks: DbCeiling kind tracks, fit 1 "
          "(%r)" % (got,))
    rep = card.add(fixed_img, [beta], policy={"ceiling": "fit"})
    check(len(rep.added) == 1 and len(card.tracks(fixed_img)["tracks"]) == 4,
          "policy fit: up to the cap")

    print("FLASHPOD_DB_LIMIT is taken at its word")
    f2 = fresh("f2.img", "fixed")
    calls = []
    real = firmware.fix_on_card
    firmware.fix_on_card = lambda *a, **k: calls.append(1) or real(*a, **k)
    os.environ["FLASHPOD_DB_LIMIT"] = str(library.IPOD_DB_SIZE_LIMIT)
    try:
        card.add(f2, [alpha])
        got = "added"
    except card.DbCeiling as e:
        got = e.details.get("kind")
    del os.environ["FLASHPOD_DB_LIMIT"]
    check(got == "bytes" and not calls,
          "refused by bytes, firmware not read (%r, %d reads)"
          % (got, len(calls)))
    del calls[:]
    f3 = fresh("f3.img", "fixed")
    card.add(f3, [beta])                       # one track: under the limit
    check(not calls, "under the byte limit the firmware isn't read")
    card.add(f3, [alpha])                      # past it: read, once
    check(len(calls) == 1, "control: past it the firmware is read once "
          "(%d reads)" % len(calls))
    firmware.fix_on_card = real

    print("card.inspect")
    c = card.inspect(fixed_img).itunesdb["ceiling"]
    check(c["kind"] == "tracks" and c["limit"] == 4 and c["headroom"] == 0,
          "fixed: %r" % c)
    c = card.inspect(stock_img).itunesdb["ceiling"]
    check(c["kind"] == "bytes", "stock: %r" % c)

    print("the CLI")
    f4, s4 = fresh("f4.img", "fixed"), fresh("s4.img", "stock")
    rc, out = run_cli(["add", "--raw", f4, alpha])
    check(rc == 0 and "large-library fix" in out
          and len(card.tracks(f4)["tracks"]) == 3,
          "add --raw on the fixed card: added, says why (rc %r)" % rc)
    rc, out = run_cli(["add", "--raw", s4, alpha])
    check(rc == 1 and "ZERO songs" in out
          and not card.tracks(s4)["tracks"],
          "add --raw on stock: refused (rc %r)" % rc)

    print("over a mount")
    lib = card.library(f4)
    db = library.ceiling(lib, lib.tracks[:2], library.library_fix_probe(
        MountTarget(tmp, lambda: fw_reader(f4)[0])))
    check(db.kind == "tracks" and db.firmware is True,
          "MountTarget with a reader: the fix is seen")
    db = library.ceiling(lib, lib.tracks[:2],
                         library.library_fix_probe(MountTarget(tmp)))
    check(db.kind == "bytes" and db.firmware is None,
          "MountTarget without one: byte limit, firmware unknown")

    if opts.asset:
        print("real firmware")
        img = os.path.join(tmp, "real16.img")
        card.create_blank(img, 96 << 20)
        card.flash(img, opts.asset)
        check(firmware.fix_on_card(fw_reader(img)[0], library.LIBRARY_FIX)
              is True, "flashpod 1.6 has the fix")
        stock = os.path.join(os.path.expanduser("~"), ".cache", "flashpod",
                             "firmware", "iPod_1.1.5_2005_02_18.bin.gz")
        if os.path.exists(stock):
            img = os.path.join(tmp, "real15.img")
            card.create_blank(img, 96 << 20)
            card.flash(img, stock)
            check(firmware.fix_on_card(fw_reader(img)[0], library.LIBRARY_FIX)
                  is False, "Apple 1.5 doesn't")

    print()
    if fails:
        print("%d FAILED" % len(fails))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
