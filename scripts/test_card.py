#!/usr/bin/env python3
"""Tests for flashpod.card, the library API, on synthetic image files.

    PYTHONPATH=. python3 scripts/test_card.py

Every operation the web app will call, on images this test makes itself, with
synthetic firmware and ffmpeg-made audio (scripts/synth_fixtures.py). Where the
CLI already does the same thing, the library's result is checked AGAINST the
CLI (same image bytes after a flash; the CLI lists what the library added),
so the two frontends can't drift apart unnoticed.

Every refusal is also checked to leave the image byte-for-byte unchanged.
"""
import fcntl
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import synth_fixtures as synth  # noqa: E402
from flashpod import card, errors, firmware, layout  # noqa: E402

fails = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)


def raises(exc_type, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc_type as exc:
        return exc
    except Exception as exc:                      # noqa: BLE001
        return AssertionError("raised %s: %s" % (type(exc).__name__, exc))
    return None


def sha(path, start=0, length=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        left = length if length is not None else os.path.getsize(path) - start
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def cli(*args, env=None):
    e = dict(os.environ, PYTHONPATH=ROOT)
    e.update(env or {})
    p = subprocess.run([sys.executable, "-m", "flashpod"] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, env=e, cwd=ROOT)
    return p.returncode, p.stdout.decode() + p.stderr.decode()


def jsonable(obj, what):
    try:
        json.dumps(obj.to_dict() if hasattr(obj, "to_dict") else obj)
        check(True, what + " is JSON-serializable")
    except (TypeError, ValueError) as exc:
        check(False, what + " is JSON-serializable (%s)" % exc)


def refused_unchanged(exc_type, img, fn, *a, **kw):
    before = sha(img)
    exc = raises(exc_type, fn, *a, **kw)
    return isinstance(exc, exc_type) and sha(img) == before, exc


def main():
    tmp = synth.scratch_dir("fp-card-")
    music = synth.make_music(os.path.join(tmp, "music"))
    fw = synth.write_firmware(os.path.join(tmp, "fw.bin"))
    fw2 = synth.write_firmware(os.path.join(tmp, "fw2.bin"), seed=7,
                               version_word=0x01608000)
    badfw = synth.write_firmware(os.path.join(tmp, "bad.bin"), bad_checksum=True)
    big = synth.big_wav(os.path.join(tmp, "big.wav"), 60)
    SIZE = 96 << 20

    print("version")
    v = card.version()
    check(v["api"] == card.API_VERSION and v["version"], "version() %r" % v)

    print("create_blank")
    img = os.path.join(tmp, "a.img")
    info = card.create_blank(img, SIZE + 100)
    check(os.path.getsize(img) == SIZE, "size rounded down to whole sectors")
    check(os.stat(img).st_blocks * 512 < 1 << 20, "the image is sparse")
    check(info.partition_table is None and not info.ipod_compatible,
          "a blank card has no partition table and isn't compatible")
    check(isinstance(raises(errors.CardExists, card.create_blank, img, SIZE),
                     errors.CardExists), "refuses an existing path")
    small = os.path.join(tmp, "small.img")
    check(isinstance(raises(errors.CardTooSmall, card.create_blank, small,
                            card.min_card_bytes() - 512), errors.CardTooSmall)
          and not os.path.exists(small), "refuses too small, creates nothing")
    jsonable(info, "CardInfo of a blank card")

    print("only image files")
    for path, why in (("/dev/null", "not_a_file"), (tmp, "not_a_file"),
                      (os.path.join(tmp, "nope.img"), "missing")):
        exc = raises(errors.NotACard, card.inspect, path)
        check(isinstance(exc, errors.NotACard) and exc.details.get("reason") == why,
              "inspect(%s) -> NotACard(%s)" % (path, why))
    link = os.path.join(tmp, "dev-link.img")
    os.symlink("/dev/null", link)
    exc = raises(errors.NotACard, card.flash, link, fw)
    check(isinstance(exc, errors.NotACard), "a symlink to a device is refused "
          "(checked on the open file)")

    print("flash = the CLI's flash, byte for byte")
    ok, _ = refused_unchanged(errors.FirmwareInvalid, img, card.flash, img, badfw)
    check(ok, "bad firmware: FirmwareInvalid, image unchanged")
    r = card.flash(img, fw)
    jsonable(r, "flash result")
    cli_img = os.path.join(tmp, "cli.img")
    with open(cli_img, "wb") as f:
        f.truncate(SIZE)
    rc, out = cli("flash", cli_img, "--firmware", fw, "--yes")
    check(rc == 0, "CLI flash of the same size ran (rc %d)" % rc)
    check(sha(img) == sha(cli_img), "library and CLI images are identical "
          "(sha256 %s)" % sha(img)[:16])
    tiny = os.path.join(tmp, "tiny.img")
    with open(tiny, "wb") as f:
        f.truncate(64 << 20)            # too few clusters for FAT32
    ok, exc = refused_unchanged(errors.CardTooSmall, tiny, card.flash, tiny, fw)
    check(ok, "64 MiB: CardTooSmall up front, image unchanged (the CLI writes "
          "it unformatted)")

    print("inspect a flashed card")
    info = card.inspect(img)
    check(info.layout_ok and info.ipod_compatible and not info.ready,
          "layout ok, compatible, not ready (no iTunesDB)")
    f = info.firmware
    check(f["present"] and f["osos"]["checksum_ok"] and f["build"] == "unrecognized",
          "synthetic firmware: present, checksum ok, unrecognized")
    check(f["version_word_means"] == "1.5", "version word reads 1.5")
    check(info.filesystem["type"] == "FAT32" and info.filesystem["label"] == "IPOD",
          "FAT32 labelled IPOD")
    check(info.itunesdb["state"] == "absent", "no iTunesDB yet")

    print("fix detection (synthetic table)")
    osos = bytearray(synth.osos_payload())
    addr = firmware.PP5002_OSOS_ADDR
    blob = b"appended code!" * 3
    osos[0x100:0x104] = b"\x01\x02\x03\x04"
    osos[0x200:0x200 + len(blob)] = blob
    table = {"fixes": [
        {"name": "edit-only", "edits": [{"image": "osos", "at": hex(addr + 0x100),
                                         "patched": "01020304"}], "append": None},
        {"name": "with-code", "edits": [], "append": {
            "at": hex(addr + 0x200), "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}},
        {"name": "absent", "edits": [{"image": "osos", "at": hex(addr + 0x100),
                                      "patched": "01020305"}], "append": None},
        {"name": "aupd-only", "edits": [{"image": "aupd", "at": hex(addr),
                                         "patched": "ff"}], "append": None},
        {"name": "code-differs", "edits": [], "append": {
            "at": hex(addr + 0x200), "bytes": len(blob),
            "sha256": hashlib.sha256(blob[:-1] + b"?").hexdigest()}},
    ]}
    found = firmware.fixes_present(bytes(osos), addr, table)
    check(found == ["edit-only", "with-code"],
          "present: edit match, code hash match; absent: an edit or the "
          "appended code one byte off, and an aupd fix with aupd unread (%r)"
          % found)
    aupd_addr = 0x28100000
    found = firmware.fixes_present(bytes(osos), addr, table,
                                   aupd=(b"\xff" + b"\0" * 15, aupd_addr))
    check("aupd-only" not in found, "an aupd fix is absent when aupd lacks its bytes at its address")
    table["fixes"][3]["edits"][0]["at"] = hex(aupd_addr)
    found = firmware.fixes_present(bytes(osos), addr, table,
                                   aupd=(b"\xff" + b"\0" * 15, aupd_addr))
    check(found == ["edit-only", "with-code", "aupd-only"],
          "an aupd fix is present when aupd holds its bytes (%r)" % found)

    print("init")
    r = card.init(img, "Synth Pod")
    info = card.inspect(img)
    check(info.itunesdb["state"] == "ok" and info.itunesdb["tracks"] == 0
          and info.itunesdb["name"] == "Synth Pod", "empty iTunesDB named")
    ok, _ = refused_unchanged(errors.AlreadyInitialized, img, card.init, img)
    check(ok, "init again: AlreadyInitialized, image unchanged")

    print("plan_add writes nothing")
    before = sha(img)
    plan = card.plan_add(img, [music])
    check(sha(img) == before, "image unchanged by plan_add")
    check(plan.files == 6 and len(plan.new) == 5 and len(plan.unreadable) == 1
          and not plan.duplicates and plan.fits_all,
          "6 files: 5 new, 1 unreadable, fits (%d/%d/%d)"
          % (plan.files, len(plan.new), len(plan.unreadable)))
    jsonable(plan, "AddPlan")

    print("add")
    rep = card.add(img, [music], plan=plan)
    jsonable(rep, "AddReport")
    check(len(rep.added) == 5 and not rep.failed and not rep.cancelled,
          "5 added")
    info = card.inspect(img)
    db = info.itunesdb
    check((db["tracks"], db["artists"], db["albums"]) == (5, 3, 4),
          "inspect: 5 tracks, 3 artists, 4 albums")
    check(info.ready and info.music_files == 5 and not info.orphans
          and not info.missing, "ready; 5 files, no orphans, none missing")
    rc, out = cli("ls", "--raw", img, "all")
    check(rc == 0 and 'iPod "Synth Pod": 5 tracks, 3 artists, 4 albums' in out
          and "Second Song" in out, "the CLI lists what the library added")
    rep = card.add(img, [music])
    check(not rep.added and len(rep.duplicates) == 5, "again: all duplicates")

    print("plan_add then a change: PlanStale")
    one = os.path.join(music, "Alpha Band", "First Album", "01 Opening.mp3")
    img2 = os.path.join(tmp, "b.img")
    card.create_blank(img2, SIZE)
    card.flash(img2, fw)
    card.init(img2)
    plan = card.plan_add(img2, [music])
    card.add(img2, [one])
    ok, _ = refused_unchanged(errors.PlanStale, img2, card.add, img2, [music],
                              plan=plan)
    check(ok, "add(plan=stale): PlanStale, image unchanged")

    print("the database ceiling")
    os.environ["FLASHPOD_DB_LIMIT"] = "3000"
    try:
        img3 = os.path.join(tmp, "c.img")
        card.create_blank(img3, SIZE)
        card.flash(img3, fw)
        card.init(img3)
        plan = card.plan_add(img3, [music])
        check(plan.db["fit"] == 4 and plan.fits == 4 and not plan.fits_all,
              "plan: 4 of 5 fit under 3000 bytes (the CLI says the same)")
        ok, exc = refused_unchanged(errors.DbCeiling, img3, card.add, img3, [music])
        check(ok and exc.details["fit"] == 4, "default policy: DbCeiling, unchanged")
        rep = card.add(img3, [music], policy={"ceiling": "fit"})
        check(len(rep.added) == 4 and rep.dropped_ceiling == 1, "fit: 4 added, 1 dropped")
        os.environ["FLASHPOD_DB_LIMIT"] = "100"
        ok, _ = refused_unchanged(errors.DbCeiling, img3, card.add, img3, [music],
                                  policy={"ceiling": "fit"})
        check(ok, "fit with nothing fitting: DbCeiling, unchanged")
        rep = card.add(img3, [music], policy={"ceiling": "force"})
        check(len(rep.added) == 1, "force: the rest added anyway")
    finally:
        del os.environ["FLASHPOD_DB_LIMIT"]

    print("free space")
    img4 = os.path.join(tmp, "d.img")
    card.create_blank(img4, 80 << 20)
    # a longer firmware than fw2, so flash_firmware must clear its tail
    fwlong = os.path.join(tmp, "fwlong.bin")
    with open(fwlong, "wb") as fh:
        fh.write(synth.firmware_bytes(payload=synth.osos_payload(
            length=synth.OSOS_LEN * 3)))
    card.flash(img4, fwlong)
    card.init(img4)
    ok, exc = refused_unchanged(errors.NoSpace, img4, card.add, img4, [big, music])
    check(ok and exc.details["fit"] == 0, "big file first: NoSpace, unchanged")
    rep = card.add(img4, [music, big], policy={"full": "fit"})
    check(len(rep.added) == 5 and rep.dropped_space == 1,
          "fit: the leading 5 added, the big one dropped")

    print("cancel keeps exactly what was copied")
    img5 = os.path.join(tmp, "e.img")
    card.create_blank(img5, SIZE)
    card.flash(img5, fw)
    card.init(img5)
    seen = []

    def progress(stage, done, total, item):
        seen.append(stage)

    copies = [0]

    def prog2(stage, done, total, item):
        progress(stage, done, total, item)
        if stage == "copy":
            copies[0] += 1

    rep = card.add(img5, [music], progress=prog2,
                   cancelled=lambda: copies[0] >= 2)
    info = card.inspect(img5)
    check(rep.cancelled and len(rep.added) == 2, "cancelled after 2 copies")
    check(info.itunesdb["tracks"] == 2 and info.music_files == 2
          and not info.orphans and not info.missing,
          "the card holds exactly those 2: no orphans, nothing missing")
    check({"tags", "copy", "write"} <= set(seen), "progress stages tags/copy/write")
    rep = card.add(img5, [music], cancelled=lambda: True)
    check(rep.cancelled and not rep.added, "cancelled during tag reading: nothing")

    print("remove")
    lib = card.tracks(img)
    ids = [t["id"] for t in lib["tracks"]]
    ok, _ = refused_unchanged(errors.NoSuchTrack, img, card.remove, img,
                              [ids[0], 99999])
    check(ok, "an unknown id: NoSuchTrack, nothing removed")
    gone = card.remove(img, [ids[0]])
    check(len(gone) == 1 and card.inspect(img).itunesdb["tracks"] == 4,
          "remove by id")
    gone = card.remove(img, artist="alpha band")
    info = card.inspect(img)
    check(len(gone) == 2 and info.itunesdb["tracks"] == 2 and not info.orphans,
          "remove by artist (case-insensitive), files gone too")

    print("rebuild")
    r = card.rebuild(img, "Rebuilt")
    info = card.inspect(img)
    check(r.tracks == 2 and info.itunesdb["name"] == "Rebuilt"
          and info.itunesdb["tracks"] == 2, "rebuilt from the files: 2 tracks")

    print("a corrupt iTunesDB")
    with open(img2, "r+b") as fh:
        data = fh.read()
        i = data.find(b"mhbd", layout.DATA_START * 512)
        while i >= 0:
            fh.seek(i)
            fh.write(b"XXXX")
            i = data.find(b"mhbd", i + 4)
    check(card.inspect(img2).itunesdb["state"] == "corrupt", "inspect: corrupt")
    ok, _ = refused_unchanged(errors.CorruptLibrary, img2, card.add, img2, [music])
    check(ok, "add: CorruptLibrary, unchanged")
    check(isinstance(raises(errors.CorruptLibrary, card.tracks, img2),
                     errors.CorruptLibrary), "tracks: CorruptLibrary")

    print("the lock")
    holder = open(img4, "rb")
    fcntl.flock(holder.fileno(), fcntl.LOCK_SH)
    check(card.inspect(img4).ipod_compatible, "shared lock held: inspect works")
    ok, exc = refused_unchanged(errors.CardBusy, img4, card.add, img4, [music])
    check(ok, "shared lock held: add -> CardBusy, unchanged")
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    check(isinstance(raises(errors.CardBusy, card.inspect, img4), errors.CardBusy),
          "exclusive lock held: inspect -> CardBusy")
    holder.close()
    check(card.inspect(img4).ipod_compatible, "released: inspect works again")

    print("flash_firmware keeps the music")
    data_start = layout.DATA_START * 512
    before_data = sha(img4, data_start)
    before_tracks = card.tracks(img4)
    r = card.flash_firmware(img4, fw2)
    fresh = os.path.join(tmp, "fresh.img")
    card.create_blank(fresh, 80 << 20)
    card.flash(fresh, fw2)
    fwlen = layout.FW_BLOCKS * 512
    check(sha(img4, layout.FW_START * 512, fwlen) == sha(fresh, layout.FW_START * 512, fwlen),
          "firmware partition identical to a fresh flash of the new firmware")
    check(sha(img4, data_start) == before_data, "data partition untouched")
    check(card.tracks(img4) == before_tracks, "tracks unchanged")
    check(card.inspect(img4).firmware["version_word_means"] == "1.6",
          "inspect sees the new firmware")
    bare = os.path.join(tmp, "bare.img")
    card.create_blank(bare, SIZE)
    ok, _ = refused_unchanged(errors.LayoutMismatch, bare, card.flash_firmware,
                              bare, fw2)
    check(ok, "no flashpod layout: LayoutMismatch, unchanged")

    print("format_data")
    before_fw = sha(img4, 0, data_start)
    r = card.format_data(img4)
    info = card.inspect(img4)
    check(sha(img4, 0, data_start) == before_fw, "MBR + firmware untouched")
    check(info.itunesdb["state"] == "absent" and info.music_files == 0,
          "music and iTunesDB gone")

    print("catalog")
    cat = card.firmware_catalog()
    check(cat and all("osos_sha256" in e and "local" in e for e in cat),
          "every catalog entry has osos_sha256 and a local field")

    print()
    if fails:
        print("%d FAILED:\n  %s" % (len(fails), "\n  ".join(fails)))
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
