#!/usr/bin/env python3
"""The flashpod 1.0 commands that have no 0.x counterpart, on synthetic
cards only (#97): what test_cli_unchanged cannot check, because the
Python 0.x tree has nothing to compare them with.

    python3 scripts/test_cli_1_0.py --rust rust/target/release/flashpod

Each check states its expected result independently of the binary (the
synthetic firmware's own bytes, Python's hashlib), so a wrong binary
fails. Ends with one PASS/FAIL line; exit 0 only on PASS.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402

FAILS = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def run(binary, *args):
    r = subprocess.run([binary] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr


def extract_checks(binary, work):
    print("firmware extract")
    fw = synth.write_firmware(os.path.join(work, "fw.bin"))
    fw_bytes = open(fw, "rb").read()   # as the file holds it, before the install fix-ups
    osos = synth.osos_payload()
    img = os.path.join(work, "card.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    rc, out, err = run(binary, "firmware", "write", img, "--firmware", fw, "--yes", "--no-arm")
    check(rc == 0, "firmware write on a blank image (rc %d)" % rc)

    o = os.path.join(work, "osos.bin")
    rc, out, err = run(binary, "firmware", "extract", img, "-o", o, "--image", "osos")
    got = open(o, "rb").read() if os.path.exists(o) else b""
    check(rc == 0 and got == osos, "--image osos is the synthetic osos payload, byte for byte")
    note = json.load(open(o + ".json")) if os.path.exists(o + ".json") else {}
    check(note.get("sha256") == hashlib.sha256(osos).hexdigest(), "the note's sha256 is the payload's")
    check(note.get("what") == "osos" and note.get("partition_table") == "mbr" and note.get("source") == img,
          "the note says what, which table and where from")

    p = os.path.join(work, "part.bin")
    rc, out, err = run(binary, "firmware", "extract", img, "-o", p)
    part = open(p, "rb").read() if os.path.exists(p) else b""
    check(rc == 0 and len(part) == 65536 * 512, "the whole partition is 32 MiB")
    # what write puts on the card: the install fix-ups applied (Python's
    # ipod_flash.load_firmware, the reference), then zeros
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import ipod_flash
    ready = ipod_flash.load_firmware(fw)
    check(ready != fw_bytes and part[:len(ready)] == ready and not part[len(ready):].strip(b"\0"),
          "the partition is the install-ready firmware (Python's load_firmware), then zeros")

    before = hashlib.sha256(open(o, "rb").read()).hexdigest()
    rc, out, err = run(binary, "firmware", "extract", img, "-o", o, "--image", "osos")
    check(rc == 1 and "already exists" in err and hashlib.sha256(open(o, "rb").read()).hexdigest() == before,
          "an existing output is refused and left as it was")

    rc, out, err = run(binary, "firmware", "extract", img, "-o", os.path.join(work, "a.bin"), "--image", "aupd")
    check(rc == 1 and "no aupd image" in err, "a missing image is refused: %r" % err.strip())

    blank = os.path.join(work, "blank.img")
    with open(blank, "wb") as f:
        f.truncate(1 << 20)
    rc, out, err = run(binary, "firmware", "extract", blank, "-o", os.path.join(work, "b.bin"))
    check(rc == 1 and "no partition table" in err, "a blank image is refused: %r" % err.strip())

    sha_img = hashlib.sha256(open(img, "rb").read()).hexdigest()
    run(binary, "firmware", "extract", img, "-o", os.path.join(work, "c.bin"))
    check(hashlib.sha256(open(img, "rb").read()).hexdigest() == sha_img, "extract leaves the card unchanged")


def export_checks(binary, work):
    print("music export")
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import itunesdb, target
    import mutagen
    fw = synth.write_firmware(os.path.join(work, "fw2.bin"))
    music = synth.make_music(os.path.join(work, "music"))
    img = os.path.join(work, "lib.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    run(binary, "firmware", "write", img, "--firmware", fw, "--yes", "--no-arm")
    run(binary, "music", "init", "--raw", img, "Test Pod")
    rc, out, err = run(binary, "music", "add", "--raw", img, music)
    said = [l for l in (out + err).splitlines() if "added" in l]
    check(rc in (0, 1) and said, "add synthetic music to the card: %s" % said[-1:])
    # the card read by the Python reference: its tracks and their bytes, plus
    # one file the database does not know (the "other files" case)
    with open(img, "r+b") as f:
        fs = target.open_image_fat(f)
        lib = itunesdb.parse_bytes(fs.read_file("/iPod_Control/iTunes/iTunesDB"))
        fs.mkdir("/Notes")
        fs.write_file("/Notes/old thesis.txt", b"twenty years ago\n")
        tracks = {t.id: (t, fs.read_file("/" + t.location.strip(":").replace(":", "/"))) for t in lib.tracks}
        f.flush()
    check(len(tracks) >= 4, "the card holds %d tracks" % len(tracks))

    dest = os.path.join(work, "out")
    os.makedirs(os.path.join(dest, "Clash"), exist_ok=True)
    rc, out, err = run(binary, "music", "export", "--raw", img, "--to", dest, "--other-files")
    check(rc == 0, "export rc 0 (%s)" % err.strip()[:120])
    got = {}
    for root, _, names in os.walk(dest):
        for n in names:
            got[os.path.relpath(os.path.join(root, n), dest)] = open(os.path.join(root, n), "rb").read()
    music_files = {p: b for p, b in got.items() if not p.startswith("Other Files")}
    check(len(music_files) == len(tracks), "one file per track (%d)" % len(music_files))
    by_bytes = {b: p for p, b in music_files.items()}
    check(all(data in by_bytes for _, data in tracks.values()), "every track's bytes, byte for byte")
    ok = True
    for t, data in tracks.values():
        p = by_bytes.get(data, "")
        parts = p.split(os.sep)
        want_artist = (t.artist or "").strip() or "Unknown Artist"
        want_album = (t.album or "").strip() or "Unknown Album"
        if len(parts) != 3 or parts[0] != want_artist or parts[1] != want_album:
            ok = False
            print("     %r for artist %r album %r" % (p, t.artist, t.album))
        if t.title and t.title.strip() not in parts[-1]:
            ok = False
            print("     %r lacks title %r" % (p, t.title))
        if t.track_nr and not parts[-1].startswith("%02d " % t.track_nr):
            ok = False
            print("     %r lacks track number %d" % (p, t.track_nr))
    check(ok, "every path is Artist/Album/NN Title.ext from the database's tags")
    tagged = [p for p in music_files if p.endswith(".mp3")]
    if tagged:
        tags = mutagen.File(os.path.join(dest, tagged[0]), easy=True)
        check(tags is not None and (tags.get("title") or [""])[0] in tagged[0],
              "an exported mp3's own title tag is in its name (mutagen)")
    check(got.get(os.path.join("Other Files", "Notes", "old thesis.txt")) == b"twenty years ago\n",
          "--other-files exports the file the database doesn't list, at its card path")
    check(not any(p.endswith(".part") for p in got), "no .part file left")

    before = sorted(got)
    rc, out, err = run(binary, "music", "export", "--raw", img, "--to", dest, "--other-files")
    after = sorted(os.path.relpath(os.path.join(r, n), dest) for r, _, ns in os.walk(dest) for n in ns)
    check(rc == 0 and after == before and ("%d already there" % len(tracks)) in out,
          "a second run exports nothing new (resume): %r" % [l for l in out.splitlines() if "already" in l][:1])

    victim = sorted(music_files)[0]
    with open(os.path.join(dest, victim), "wb") as f:
        f.write(b"a different file")
    rc, out, err = run(binary, "music", "export", "--raw", img, "--to", dest, "--no-other-files")
    stem, ext = os.path.splitext(victim)
    second = os.path.join(dest, stem + " (2)" + ext)
    check(rc == 0 and os.path.exists(second) and open(second, "rb").read() == music_files[victim]
          and open(os.path.join(dest, victim), "rb").read() == b"a different file",
          "a different file at a track's name is kept; the track goes to ' (2)'")


def export_mount_checks(binary, work):
    print("music export over a mount (a plain directory, as test_cli_unchanged's mount scenario)")
    music = synth.make_music(os.path.join(work, "music-m"))
    mnt = os.path.join(work, "mnt")
    os.makedirs(mnt)
    run(binary, "music", "init", "--mount", mnt, "MountPod")
    run(binary, "music", "add", "--mount", mnt, music)
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import itunesdb
    lib = itunesdb.parse(os.path.join(mnt, "iPod_Control", "iTunes", "iTunesDB"))
    want = sorted(open(os.path.join(mnt, *t.location.strip(":").split(":")), "rb").read() for t in lib.tracks)
    cwd = os.path.join(work, "cwd")
    os.makedirs(cwd)
    r = subprocess.run([binary, "music", "export", "--mount", mnt, "--no-other-files"], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       stdin=subprocess.DEVNULL)
    out_dir = os.path.join(cwd, "MountPod")
    got = sorted(open(os.path.join(r_, n), "rb").read() for r_, _, ns in os.walk(out_dir) for n in ns)
    check(r.returncode == 0 and len(lib.tracks) >= 4 and got == want,
          "the default destination ./<iPod name> gets every track's bytes (%d)" % len(got))


def read_playlists(buf):
    """[(name, master, [track ids])] straight from the iTunesDB bytes: the
    reader scripts/add_playlists.py (claude/playlist-tool) uses, checking
    each playlist's items end where its record ends."""
    import struct
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import itunesdb
    u32 = lambda o: struct.unpack_from("<I", buf, o)[0]
    out, off = [], u32(4)
    while off < len(buf):
        hlen, tlen, typ = u32(off + 4), u32(off + 8), u32(off + 12)
        if typ == 2:
            p = off + hlen
            n = u32(p + 8)
            p += u32(p + 4)
            for _ in range(n):
                phl, ptl, nmhod, nitems = u32(p + 4), u32(p + 8), u32(p + 12), u32(p + 16)
                mhods, q = itunesdb._parse_mhods(buf, p + phl, nmhod)
                ids = []
                for _ in range(nitems):
                    ids.append(u32(q + 24))
                    q += u32(q + 8)
                assert q == p + ptl, "playlist items end at %d, record at %d" % (q, p + ptl)
                out.append((mhods.get(itunesdb.MHOD_TITLE), u32(p + 20) != 0, ids))
                p += ptl
        off += tlen
    return out


def playlist_checks(binary, work):
    print("music playlist")
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import itunesdb, target
    fw = synth.write_firmware(os.path.join(work, "fw3.bin"))
    music = synth.make_music(os.path.join(work, "music-p"))
    img = os.path.join(work, "pl.img")
    with open(img, "wb") as f:
        f.truncate(200 << 20)
    run(binary, "firmware", "write", img, "--firmware", fw, "--yes", "--no-arm")
    run(binary, "music", "init", "--raw", img)
    run(binary, "music", "add", "--raw", img, music)

    def db():
        with open(img, "rb") as f:
            return target.open_image_fat(f).read_file("/iPod_Control/iTunes/iTunesDB")
    ids = [t.id for t in itunesdb.parse_bytes(db()).tracks]
    tracks_before = [(t.id, t.title, t.location) for t in itunesdb.parse_bytes(db()).tracks]
    a, b, c = (str(i) for i in ids[:3])
    rc, out, err = run(binary, "music", "playlist", "new", "--raw", img, "Road Trip", a, b)
    check(rc == 0 and read_playlists(db())[1:] == [("Road Trip", False, ids[:2])],
          "new: Road Trip holds the two tracks, in order (raw reader)")
    rc, out, err = run(binary, "music", "playlist", "add", "--raw", img, "road trip", c, a)
    check(rc == 0 and read_playlists(db())[1][2] == [ids[0], ids[1], ids[2]],
          "add (name matched case-insensitively) appends the new one, skips the one already there")
    rc, out, err = run(binary, "music", "playlist", "rm", "--raw", img, "Road Trip", a)
    check(rc == 0 and read_playlists(db())[1][2] == [ids[1], ids[2]], "rm takes the track out of the playlist")
    check([(t.id, t.title, t.location) for t in itunesdb.parse_bytes(db()).tracks] == tracks_before,
          "the tracks themselves are untouched (Python's parser)")
    rc, out, err = run(binary, "music", "playlist", "ls", "--raw", img)
    check(rc == 0 and "Road Trip  (2 tracks)" in out, "ls lists it: %r" % out.strip())
    rc, out, err = run(binary, "music", "playlist", "new", "--raw", img, "Road Trip")
    check(rc == 1 and "already a playlist" in err, "a second playlist of that name is refused")
    rc, out, err = run(binary, "music", "playlist", "new", "--raw", img, "Bad", "99999")
    check(rc == 1 and "no track with id" in err and len(read_playlists(db())) == 2,
          "an unknown track id is refused and nothing is written")
    rc, out, err = run(binary, "music", "rm", "--raw", img, b)
    check(rc == 0 and read_playlists(db())[1][2] == [ids[2]], "music rm of a track takes it out of the playlist too")
    rc, out, err = run(binary, "music", "add", "--raw", img, os.path.join(music, "Alpha Band"))
    check(read_playlists(db())[1] == ("Road Trip", False, [ids[2]]), "music add keeps the playlist")
    rc, out, err = run(binary, "music", "playlist", "delete", "--raw", img, "Road Trip")
    check(rc == 0 and len(read_playlists(db())) == 1, "delete leaves only the master playlist")


def inspect_checks(binary, work):
    print("inspect (--json against Python's flashpod.card.inspect, the reference library)")
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import card
    fw = synth.write_firmware(os.path.join(work, "fw4.bin"))
    music = synth.make_music(os.path.join(work, "music-i"))
    blank = os.path.join(work, "i-blank.img")
    fresh = os.path.join(work, "i-fresh.img")
    full = os.path.join(work, "i-full.img")
    for img in (blank, fresh, full):
        with open(img, "wb") as f:
            f.truncate(150 << 20)
    for img in (fresh, full):
        run(binary, "firmware", "write", img, "--firmware", fw, "--yes", "--no-arm")
        run(binary, "music", "init", "--raw", img)
    run(binary, "music", "add", "--raw", full, music)
    for name, img in (("blank", blank), ("flashed and initialized", fresh), ("with music", full)):
        rc, out, err = run(binary, "inspect", img, "--json")
        try:
            got = json.loads(out)
        except ValueError:
            got = None
        want = json.loads(json.dumps(card.inspect(img).to_dict()))
        check(rc == 0 and got == want, "%s: inspect --json == card.inspect().to_dict()" % name)
        if got != want and got is not None:
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    print("     %s: rust %r / python %r" % (k, got.get(k), want.get(k)))
        rc, out, err = run(binary, "inspect", img)
        check(rc == 0 and ("ready for an iPod" in out) and (("not ready" in out) == (not want["ready"])),
              "%s: the text report says %sready" % (name, "" if want["ready"] else "not "))


def card_new_checks(binary, work):
    print("card new (sizes and refusals against card.create_blank's rules)")
    sys.path.insert(0, os.path.dirname(HERE))
    from flashpod import card
    for text, want in (("150M", 150 * 10**6 // 512 * 512), ("1GiB", 1 << 30), ("200000001", 200000001 // 512 * 512)):
        img = os.path.join(work, "new-%s.img" % text)
        rc, out, err = run(binary, "card", "new", img, text)
        check(rc == 0 and os.path.getsize(img) == want, "%s makes %d bytes" % (text, want))
    img = os.path.join(work, "new-150M.img")
    rc, out, err = run(binary, "card", "new", img, "300M")
    check(rc == 1 and "already exists" in err and os.path.getsize(img) == 150 * 10**6 // 512 * 512,
          "an existing image is refused and left as it was")
    tiny = card.min_card_bytes() - 512
    rc, out, err = run(binary, "card", "new", os.path.join(work, "tiny.img"), str(tiny))
    check(rc == 1 and "smaller than" in err and not os.path.exists(os.path.join(work, "tiny.img")),
          "one sector under min_card_bytes() (%d) is refused, nothing made" % card.min_card_bytes())
    rc, out, err = run(binary, "card", "new", os.path.join(work, "bad.img"), "lots")
    check(rc == 2 and "not a size" in err, "a non-size is a usage error")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    ap.add_argument("--keep", action="store_true")
    opts = ap.parse_args()
    work = tempfile.mkdtemp(prefix="fp-1-0-")
    try:
        extract_checks(os.path.abspath(opts.rust), work)
        export_checks(os.path.abspath(opts.rust), work)
        export_mount_checks(os.path.abspath(opts.rust), work)
        playlist_checks(os.path.abspath(opts.rust), work)
        inspect_checks(os.path.abspath(opts.rust), work)
        card_new_checks(os.path.abspath(opts.rust), work)
    finally:
        if not opts.keep:
            shutil.rmtree(work, ignore_errors=True)
    print("%s: %d failed" % ("FAIL" if FAILS else "PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
