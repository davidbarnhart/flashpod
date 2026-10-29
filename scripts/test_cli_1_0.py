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
    finally:
        if not opts.keep:
            shutil.rmtree(work, ignore_errors=True)
    print("%s: %d failed" % ("FAIL" if FAILS else "PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
