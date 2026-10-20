#!/usr/bin/env python3
"""Synthetic audio files for comparing the Rust tag reader with mutagen.

    from tag_corpus import build, damaged
    files = build(directory)                 # [(label, path), ...]
    copies = damaged(files, directory, n)    # seeded random damage

Every file is made here: ffmpeg sine tones in each format flashpod takes,
tags written with mutagen's own writers in many shapes (ID3v2.2-2.4, the
four text encodings, ID3v1, APEv2, extended headers, unsynchronisation,
compressed frames, numeric genres, TYER/TDAT/TIME, Vorbis comments, MP4
atoms) and byte-level oddities mutagen has rules for (no BOM, bogus sizes,
stacked tags, junk before the first frame, missing chunks). No real music.
"""
import io
import os
import random
import struct
import subprocess
import zlib

import mutagen.id3 as id3
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.wave import WAVE


def _ff(out, *args, dur="1"):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=%s" % dur] + list(args) + [out],
                   check=True, stdin=subprocess.DEVNULL)
    return out


def _syncsafe(n):
    return bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])


def _strip_id3(data):
    """The audio of an MP3 without its leading ID3v2 tag."""
    if data[:3] == b"ID3":
        size = 10 + sum((b & 0x7f) << (7 * (3 - i)) for i, b in enumerate(data[6:10]))
        return data[size:]
    return data


def _id3v2(vmaj, frames, flags=0, ext=b"", pad=64):
    """A raw ID3v2 tag: frames are (name, flags, payload)."""
    body = b""
    for name, fflags, payload in frames:
        if vmaj == 2:
            body += name + struct.pack(">I", len(payload))[1:] + payload
        else:
            size = _syncsafe(len(payload)) if vmaj == 4 else struct.pack(">I", len(payload))
            body += name + size + struct.pack(">H", fflags) + payload
    body = ext + body + b"\x00" * pad
    return b"ID3" + bytes([vmaj, 0, flags]) + _syncsafe(len(body)) + body


def _text(enc, *values, term=True):
    codec = {0: "latin1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}[enc]
    nul = b"\x00\x00" if enc in (1, 2) else b"\x00"
    parts = [v.encode(codec) for v in values]
    return bytes([enc]) + nul.join(parts) + (nul if term else b"")


def _id3v1(title=b"", artist=b"", album=b"", year=b"", comment=b"", track=0, genre=255):
    c = comment.ljust(30, b"\x00")[:30]
    if track:
        c = c[:28] + b"\x00" + bytes([track])
    return (b"TAG" + title.ljust(30, b"\x00")[:30] + artist.ljust(30, b"\x00")[:30]
            + album.ljust(30, b"\x00")[:30] + year.ljust(4, b"\x00")[:4] + c + bytes([genre]))


def _rename_atom(path, old, new):
    data = open(path, "rb").read()
    with open(path, "wb") as f:
        f.write(data.replace(old, new, 1))


def build(d):
    os.makedirs(d, exist_ok=True)
    out = []

    def add(label, path):
        out.append((label, path))

    def raw(label, name, data):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        add(label, p)
        return p

    # ---- MPEG audio ------------------------------------------------------
    cbr = _ff(os.path.join(d, "base-cbr.mp3"), "-c:a", "libmp3lame", "-b:a", "128k")
    vbr = _ff(os.path.join(d, "base-vbr.mp3"), "-c:a", "libmp3lame", "-q:a", "4", dur="3")
    mono = _ff(os.path.join(d, "base-mono22.mp3"), "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "32k")
    m25 = _ff(os.path.join(d, "base-8k.mp3"), "-ac", "1", "-ar", "8000", "-c:a", "libmp3lame", "-b:a", "8k")
    noxing = _ff(os.path.join(d, "base-noxing.mp3"), "-c:a", "libmp3lame", "-b:a", "96k", "-write_xing", "0")
    mp2 = _ff(os.path.join(d, "base-layer2.mp2"), "-c:a", "mp2", "-b:a", "192k")
    for p in (cbr, vbr, mono, m25, noxing):
        add("plain " + os.path.basename(p), p)
    add("MPEG layer 2", mp2)
    audio = _strip_id3(open(cbr, "rb").read())
    vaudio = _strip_id3(open(vbr, "rb").read())
    raw("layer 2 named .mp3", "layer2.mp3", open(mp2, "rb").read())

    def mutagen_tag(label, name, frames, v2=4, src=audio, v1=False):
        p = raw(label, name, src)
        t = id3.ID3()
        for f in frames:
            t.add(f)
        t.save(p, v2_version=v2, v1=2 if v1 else 0)
        return p

    full = [id3.TIT2(encoding=3, text="Title ünï"), id3.TPE1(encoding=1, text="Artist"),
            id3.TALB(encoding=0, text="Album"), id3.TCON(encoding=3, text="Rock"),
            id3.TCOM(encoding=2, text="Composer"), id3.TRCK(encoding=0, text="3/12"),
            id3.TDRC(encoding=0, text="2004-05-06")]
    mutagen_tag("ID3v2.4, all keys", "v24.mp3", full)
    mutagen_tag("ID3v2.3, all keys", "v23.mp3", full, v2=3)
    mutagen_tag("ID3v2.4 + ID3v1", "v24v1.mp3", full, v1=True)
    mutagen_tag("VBR + ID3v2.4", "vbr-tags.mp3", full, src=vaudio)
    mutagen_tag("multi values", "multi.mp3",
                [id3.TIT2(encoding=3, text=["One", "Two"]), id3.TPE1(encoding=1, text=["", "Second"])])
    mutagen_tag("big picture", "apic.mp3",
                [id3.TIT2(encoding=3, text="Pic"),
                 id3.APIC(encoding=3, mime="image/jpeg", type=3, desc="c", data=os.urandom(120000))])
    for i, g in enumerate(["(17)", "(17)Rock", "17", "(255)", "((foo)", "(RX)(CR)", "Rock\nPop",
                           "(17)(18)Rock", "300", "(9999)", "CR", "٢", "(17)42", "(1)(", "", "((42)",
                           "4" * 5000]):
        mutagen_tag("genre %r" % g, "genre%02d.mp3" % i, [id3.TCON(encoding=3, text=g)])
    for i, ts in enumerate(["2004", "2004-05-06T07:08:09", "abcd", "٢٠٠٤", "+5", "2004 05", "04",
                            "2004/05/06", "5_0", "2004-13-45"]):
        mutagen_tag("date %r" % ts, "date%02d.mp3" % i, [id3.TDRC(encoding=3, text=ts)])

    # raw ID3 tags: versions, encodings, the date frames mutagen merges
    t = lambda *v, **k: _text(*v, **k)
    cases = [
        ("v2.3 TYER/TDAT/TIME", 3, [(b"TYER", 0, t(0, "1999")), (b"TDAT", 0, t(0, "0512")),
                                     (b"TIME", 0, t(0, "1345"))]),
        ("v2.3 two TYERs", 3, [(b"TYER", 0, t(0, "1999", "2001"))]),
        ("v2.3 TYER bad", 3, [(b"TYER", 0, t(0, "99")), (b"TDAT", 0, t(0, "0512"))]),
        ("v2.3 TYER + TDRC", 3, [(b"TYER", 0, t(0, "1999")), (b"TDRC", 0, t(0, "2010"))]),
        ("v2.3 TYER full date", 3, [(b"TYER", 0, t(0, "1999-01-02")), (b"TIME", 0, t(0, "1200"))]),
        ("v2.2 frames", 2, [(b"TT2", 0, t(1, "Two Two")), (b"TP1", 0, t(0, "Art")), (b"TAL", 0, t(3, "Alb")),
                            (b"TCO", 0, t(0, "(8)")), (b"TRK", 0, t(0, "7")), (b"TYE", 0, t(0, "1988")),
                            (b"TCM", 0, t(0, "Comp")), (b"TDA", 0, t(0, "3112"))]),
        ("v2.3 2.2 names", 3, [(b"TT2\x00", 0, t(0, "Old name"))]),
        ("v2.4 utf16 no BOM", 4, [(b"TIT2", 0, b"\x01" + "No BOM".encode("utf-16-le") + b"\x00\x00")]),
        ("v2.4 utf16be no term", 4, [(b"TIT2", 0, t(2, "BE", term=False))]),
        ("v2.4 utf16 odd byte", 4, [(b"TIT2", 0, b"\x01\xff\xfeA\x00B")]),
        ("v2.4 bad surrogate", 4, [(b"TIT2", 0, b"\x01\xff\xfe\x00\xd8A\x00\x00\x00"),
                                   (b"TPE1", 0, t(0, "kept"))]),
        ("v2.4 bad utf8", 4, [(b"TIT2", 0, b"\x03\xc3\x28\x00"), (b"TALB", 0, t(3, "ok"))]),
        ("v2.4 bad encoding", 4, [(b"TIT2", 0, b"\x07abc")]),
        ("v2.4 encoding only", 4, [(b"TIT2", 0, b"\x03")]),
        ("v2.3 zero padded", 3, [(b"TIT2", 0, b"\x00Pad\x00\x00\x00\x00")]),
        ("v2.4 empty first", 4, [(b"TIT2", 0, b"\x00\x00Second\x00")]),
        ("v2.4 duplicate frames", 4, [(b"TIT2", 0, t(0, "A")), (b"TIT2", 0, t(0, "B", "A"))]),
        ("v2.3 compressed", 3, [(b"TIT2", 0x0080, struct.pack(">I", 9) + zlib.compress(t(0, "Squeezed")))]),
        ("v2.3 compressed bad", 3, [(b"TIT2", 0x0080, b"\x00\x00\x00\x09junk"), (b"TALB", 0, t(0, "x"))]),
        ("v2.4 compressed", 4, [(b"TIT2", 0x0009, _syncsafe(9) + zlib.compress(t(0, "Squeezed")))]),
        ("v2.4 compressed no len", 4, [(b"TIT2", 0x0008, zlib.compress(t(0, "Old QL")))]),
        ("v2.4 encrypted", 4, [(b"TIT2", 0x0004, b"\x01" + t(0, "secret")), (b"TALB", 0, t(0, "vis"))]),
        ("v2.4 frame unsync", 4, [(b"TIT2", 0x0002, b"\x00A\xff\x00B\x00")]),
        ("v2.3 empty frame", 3, [(b"TIT2", 0, b""), (b"TALB", 0, t(0, "after"))]),
        ("v2.4 unknown frames", 4, [(b"XXXX", 0, b"junk"), (b"TIT2", 0, t(3, "Known"))]),
        ("v2.4 non-ascii name", 4, [(b"T\xffT2", 0, t(0, "x")), (b"TIT2", 0, t(0, "y"))]),
    ]
    for i, (label, vmaj, frames) in enumerate(cases):
        raw(label, "raw%02d.mp3" % i, _id3v2(vmaj, frames) + audio)
    # tag-level flags and sizes
    body = [(b"TIT2", 0, t(0, "ExtHdr"))]
    raw("v2.3 extended header", "ext23.mp3",
        _id3v2(3, body, flags=0x40, ext=struct.pack(">I", 6) + b"\x00" * 6) + audio)
    raw("v2.4 extended header", "ext24.mp3",
        _id3v2(4, body, flags=0x40, ext=_syncsafe(6) + b"\x01\x00") + audio)
    raw("bogus extended flag", "extbogus.mp3", _id3v2(4, body, flags=0x40) + audio)
    raw("v2.4 ext not synchsafe", "extbad.mp3",
        _id3v2(4, body, flags=0x40, ext=b"\x00\x00\x00\x86\x01\x00") + audio)
    raw("v2.3 bad flags", "flags23.mp3", _id3v2(3, body, flags=0x01) + audio)
    raw("v2.4 bad flags", "flags24.mp3", _id3v2(4, body, flags=0x02) + audio)
    tag = bytearray(_id3v2(3, [(b"TIT2", 0, t(0, "Un\xffsync"))]))
    tag[5] = 0x80
    raw("v2.3 tag unsync", "unsync23.mp3", bytes(tag).replace(b"Un\xffs", b"Un\xff\x00s") + audio)
    tag = bytearray(_id3v2(4, body))
    tag[6:10] = b"\x00\x00\x02\x80"
    raw("size not synchsafe", "notsafe.mp3", bytes(tag) + audio)
    raw("ID3v2.5", "v25.mp3", b"ID3\x05\x00\x00" + _syncsafe(20) + b"\x00" * 20 + audio)
    raw("ID3v2.5 + v1", "v25v1.mp3",
        b"ID3\x05\x00\x00" + _syncsafe(20) + b"\x00" * 20 + audio + _id3v1(title=b"From v1"))
    raw("truncated tag", "trunc.mp3", _id3v2(4, body, pad=100000)[:500])
    raw("size-0 tag", "size0.mp3", b"ID3\x03\x00\x00\x00\x00\x00\x00" + audio)
    raw("stacked tags", "stacked.mp3", _id3v2(3, [(b"TIT2", 0, t(0, "First"))])
        + _id3v2(4, [(b"TALB", 0, t(0, "Second"))]) + audio)
    raw("junk before audio", "junk.mp3", _id3v2(4, body) + os.urandom(3000) + audio)
    raw("iTunes plain sizes", "itunes.mp3",
        b"ID3\x04\x00\x00" + _syncsafe(200) + b"TIT2" + struct.pack(">I", 130) + b"\x00\x00"
        + t(0, "X" * 128) + b"TALB" + struct.pack(">I", 5) + b"\x00\x00" + t(0, "alb") + b"\x00" * 50 + audio)
    # ID3v1
    raw("v1 only", "v1.mp3", audio + _id3v1(b"V1 Title", b"V1 Artist", b"V1 Album", b"1997",
                                             b"comment", track=4, genre=17))
    raw("v1 year with comma", "v1comma.mp3", audio + _id3v1(b"T", year=b"19,9"))
    raw("v1 genre past list", "v1genre.mp3", audio + _id3v1(b"T", genre=200))
    raw("v1 blank", "v1blank.mp3", audio + _id3v1())
    raw("v1 spaces", "v1spaces.mp3", audio + _id3v1(b"  padded  ", b"\tart\x00junk"))
    raw("v1 short year", "v1short.mp3", audio + _id3v1(b"Short")[:-2])
    raw("v2.3 + v1 merge", "merge23.mp3",
        _id3v2(3, [(b"TIT2", 0, t(0, "V2 title"))]) + audio + _id3v1(b"V1", b"V1 artist", year=b"1990"))
    raw("v2.4 + v1 merge", "merge24.mp3",
        _id3v2(4, [(b"TIT2", 0, t(0, "V2 title"))]) + audio + _id3v1(b"V1", album=b"V1 album", year=b"1990"))
    raw("APE then v1", "ape.mp3", audio + b"APETAGEX" + b"\x00" * 24 + _id3v1(b"after ape"))
    raw("APETAGEX at the TAG", "ape2.mp3", audio[:-200] + b"xxAPETAGEX" + b"\x00" * 121)
    # broken MPEG
    raw("only a tag", "onlytag.mp3", _id3v2(4, body))
    raw("random named mp3", "random.mp3", random.Random(1).randbytes(5000))
    raw("empty mp3", "empty.mp3", b"")
    raw("two bytes", "two.mp3", b"\xff\xfb")
    raw("one frame", "oneframe.mp3", audio[:418])
    raw("cut mid-stream", "cut.mp3", audio[:3000])
    vb = bytearray(vaudio)
    i = vb.find(b"Xing")
    if i > 0:
        raw("Xing without frames", "xing-noframes.mp3", bytes(vb[:i + 7]) + b"\x00" + bytes(vb[i + 8:]))
        vb2 = bytearray(vb)
        vb2[i:i + 4] = b"Info"
        raw("Info header", "info.mp3", bytes(vb2))
        j = vb.find(b"LAME")
        if j > 0:
            vb3 = bytearray(vb)
            vb3[j + 9] = 0x10
            raw("LAME revision 1", "lamerev.mp3", bytes(vb3))
            vb4 = bytearray(vb)
            vb4[j:j + 9] = b"LAME3.89 "
            raw("old LAME", "lameold.mp3", bytes(vb4))
    # a VBRI header, hand-made after a silent MPEG-1 frame
    hdr = b"\xff\xfb\x90\x64" + b"\x00" * 32
    vbri = b"VBRI" + struct.pack(">HHHIIHHHH", 1, 0, 75, 400000, 300, 2, 1, 2, 150) + b"\x00\x05\x00\x05"
    frame = (hdr + vbri).ljust(417, b"\x00")
    raw("VBRI header", "vbri.mp3", frame + audio)
    raw("VBRI bad toc size", "vbri3.mp3",
        (hdr + vbri[:22] + b"\x00\x03" + vbri[24:]).ljust(417, b"\x00") + audio)

    # a real LAME tag (ffmpeg writes "Lavc" there): delay 576, padding 1000
    i = vaudio.find(b"Xing")
    if i > 0:
        flags = struct.unpack(">I", vaudio[i + 4:i + 8])[0]
        at = i + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
        lame = bytearray(vaudio)
        lame[at:at + 9] = b"LAME3.100"
        lame[at + 9] = 0x04                       # revision 0, VBR method 4
        lame[at + 21:at + 24] = bytes([576 >> 4, ((576 & 0xf) << 4) | (1000 >> 8), 1000 & 0xff])
        raw("LAME tag", "lame.mp3", bytes(lame))
        old = bytearray(lame)
        old[at:at + 9] = b"LAME3.89 "
        raw("old LAME", "lameold2.mp3", bytes(old))
        rev = bytearray(lame)
        rev[at + 9] = 0x14
        raw("LAME revision 1", "lamerev2.mp3", bytes(rev))
        big = bytearray(lame)
        big[at + 21:at + 24] = b"\xff\xff\xff"
        raw("LAME delay past the samples", "lamebig.mp3", bytes(big))
    # Xing with 1 frame and 2 audio bytes: 612.5 kbps, rounded half to even
    xh = b"\xff\xfb\x90\x64" + b"\x00" * 32 + b"Xing" + struct.pack(">III", 3, 1, 417 + 2)
    raw("Xing half-way bitrate", "xingtie.mp3", xh.ljust(417, b"\x00") + audio)

    # ---- FLAC -----------------------------------------------------------
    fl = _ff(os.path.join(d, "base.flac"), "-c:a", "flac", "-metadata", "title=Flac title",
             "-metadata", "artist=Flac artist", "-metadata", "TRACKNUMBER=5", "-metadata", "date=2001",
             "-metadata", "genre=Jazz", "-metadata", "composer=Who")
    add("FLAC tags", fl)
    flac_audio = open(fl, "rb").read()
    p = raw("FLAC multi + picture", "multi.flac", flac_audio)
    f = FLAC(p)
    f["title"] = ["First", "Second"]
    f["ALBUM"] = ["Älbum"]
    pic = Picture()
    pic.data = os.urandom(50000)
    pic.mime = "image/png"
    f.add_picture(pic)
    f.save()
    p = raw("FLAC no tags", "notags.flac", flac_audio)
    f = FLAC(p)
    f.delete()
    f.save()
    p = raw("FLAC with ID3 in front", "id3.flac", _id3v2(3, [(b"TIT2", 0, t(0, "ignored"))]) + flac_audio)
    # hand-made FLACs from the parts
    si = flac_audio[8:42]
    frames_at = 4
    blocks = []
    while True:
        h = flac_audio[frames_at]
        size = int.from_bytes(flac_audio[frames_at + 1:frames_at + 4], "big")
        blocks.append((h & 0x7f, flac_audio[frames_at + 4:frames_at + 4 + size]))
        frames_at += 4 + size
        if h & 0x80:
            break
    frames = flac_audio[frames_at:]

    def flac(*blks):
        b = b"fLaC"
        for i, (code, data) in enumerate(blks):
            last = 0x80 if i == len(blks) - 1 else 0
            b += bytes([code | last]) + len(data).to_bytes(3, "big") + data
        return b + frames

    def vc(*comments, vendor=b"x"):
        b = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
        for c in comments:
            b += struct.pack("<I", len(c)) + c
        return b
    raw("FLAC odd comments", "oddvc.flac",
        flac((0, si), (4, vc(b"TITLE=ok", b"no equals", b"t\xc3\xadtle=accent key", b"ARTIST=bad \xff utf8",
                             b"=empty key", b"ALBUM=", b"genre=G"))))
    raw("FLAC two comment blocks", "twovc.flac", flac((0, si), (4, vc(b"TITLE=one")), (4, vc(b"TITLE=two"))))
    raw("FLAC zero samples", "zero.flac", flac((0, si[:13] + b"\x00" * 5 + si[18:]), (4, vc(b"TITLE=z"))))
    raw("FLAC sample rate 0", "sr0.flac", flac((0, b"\x00" * 10 + b"\x00\x00\x0f" + si[13:])))
    raw("FLAC no streaminfo", "nosi.flac", flac((4, vc(b"TITLE=x"))))
    raw("FLAC short streaminfo", "shortsi.flac", flac((0, si[:20])))
    raw("FLAC two streaminfos", "twosi.flac", flac((0, si), (0, si[:13] + b"\x00" * 5 + si[18:])))
    raw("FLAC bad picture", "badpic.flac", flac((0, si), (6, b"\x00\x00\x00\x03\x00\x00\xff\xff")))
    raw("FLAC two seektables", "twoseek.flac", flac((0, si), (3, b"\x00" * 18), (3, b"\x00" * 18)))
    raw("FLAC cuesheet", "cue.flac", flac((0, si), (5, b"\x00" * 395 + b"\x01" + b"\x00" * 35 + b"\x01"
                                               + b"\x00" * 12)))
    raw("FLAC short cuesheet", "badcue.flac", flac((0, si), (5, b"\x00" * 100)))
    raw("FLAC unknown block", "unk.flac", flac((0, si), (9, b"abc"), (127, b"")))
    raw("FLAC truncated header", "cutflac.flac", flac_audio[:30])
    raw("not FLAC named .flac", "fake.flac", audio)
    raw("FLAC named .mp3", "flac.mp3", flac_audio)

    # ---- MP4 -------------------------------------------------------------
    m4a = _ff(os.path.join(d, "base.m4a"), "-c:a", "aac", "-b:a", "96k", "-metadata", "title=M4A title",
              "-metadata", "artist=M4A artist", "-metadata", "album=M4A album", "-metadata", "genre=Pop",
              "-metadata", "date=2012", "-metadata", "track=2/9", "-metadata", "composer=Nobody")
    add("M4A tags", m4a)
    add("M4A he-aac", _ff(os.path.join(d, "mono.m4a"), "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "24k"))
    add("ALAC", _ff(os.path.join(d, "alac.m4a"), "-c:a", "alac"))
    add("AC-3 in MP4", _ff(os.path.join(d, "ac3.mp4"), "-c:a", "ac3"))
    add("M4B", _ff(os.path.join(d, "book.m4b"), "-c:a", "aac", "-f", "mp4", "-metadata", "title=Book"))
    m4a_bytes = open(m4a, "rb").read()
    p = raw("M4A mutagen tags", "mut.m4a", m4a_bytes)
    m = MP4(p)
    m["\xa9nam"] = ["One", "Two"]
    m["\xa9gen"] = ["Custom"]
    m["tmpo"] = [17]                  # renamed 'gnre' below: mutagen can't write one
    m["trkn"] = [(4, 0)]
    m["covr"] = [MP4Cover(os.urandom(2000), MP4Cover.FORMAT_PNG)]
    m["----:com.apple.iTunes:foo"] = [MP4FreeForm(b"bar")]
    m["rtng"] = [1]
    m["cpil"] = True
    m.save()
    _rename_atom(p, b"tmpo", b"gnre")
    p = raw("M4A gnre only", "gnre.m4a", m4a_bytes)
    m = MP4(p)
    m.delete()
    m["tmpo"] = [1]
    m.save()
    _rename_atom(p, b"tmpo", b"gnre")
    p = raw("M4A no tags", "none.m4a", m4a_bytes)
    m = MP4(p)
    m.delete()
    m.save()

    def patch_atom(data, name, new_payload):
        """Replace the first atom called `name` (inside ilst) with new payload."""
        i = data.find(name)
        if i < 4:
            return None
        size = struct.unpack(">I", data[i - 4:i])[0]
        new = struct.pack(">I", 8 + len(new_payload)) + name + new_payload
        out = data[:i - 4] + new + data[i - 4 + size:]
        # fix the sizes of the enclosing atoms (moov, udta, meta, ilst)
        diff = len(new) - size
        for parent in (b"ilst", b"meta", b"udta", b"moov"):
            j = out.find(parent)
            if 4 <= j < i:
                ps = struct.unpack(">I", out[j - 4:j])[0]
                out = out[:j - 4] + struct.pack(">I", ps + diff) + out[j:]
        return out
    tagged = open(os.path.join(d, "mut.m4a"), "rb").read()
    gnre_only = open(os.path.join(d, "gnre.m4a"), "rb").read()

    def data_atom(flags, payload):
        return struct.pack(">I", 16 + len(payload)) + b"data" + struct.pack(">I", flags) + b"\x00" * 4 + payload
    for label, name, payload in [
            ("M4A trkn short (mutagen raises)", b"trkn", data_atom(0, b"\x00\x00\x00")),
            ("M4A title not utf8", b"\xa9nam", data_atom(1, b"\xff\xfe")),
            ("M4A title wrong type", b"\xa9nam", data_atom(13, b"jpeg")),
            ("M4A title bad data atom", b"\xa9nam", b"\x00\x00\x00\x05xxxx"),
            ("M4A gnre 0", b"gnre", data_atom(0, b"\x00\x00")),
            ("M4A gnre 255", b"gnre", data_atom(0, b"\x00\xff")),
            ("M4A gnre negative", b"gnre", data_atom(0, b"\xff\xf0")),
            ("M4A gnre -192", b"gnre", data_atom(0, b"\xff\x41")),
            ("M4A covr short (mutagen raises)", b"covr", b"\x00\x00\x00\x10data\x00"),
            ("M4A freeform short", b"----", b"\x00\x00"),
            ("M4A trkn two", b"trkn", data_atom(0, b"\x00\x00\x00\x01\x00\x02\x00\x00")
             + data_atom(0, b"\x00\x00\x00\x03\x00\x00\x00\x00"))]:
        new = patch_atom(gnre_only if name == b"gnre" else tagged, name, payload)
        if new:
            raw(label, "patch-%s.m4a" % label.split(" ")[1], new)
    raw("M4A truncated", "cut.m4a", m4a_bytes[:len(m4a_bytes) // 2])
    raw("M4A tail cut", "cut2.m4a", m4a_bytes[:-20])
    raw("M4A named .mp3", "m4a.mp3", m4a_bytes)
    raw("MP3 named .m4a", "mp3.m4a", open(os.path.join(d, "v24.mp3"), "rb").read())
    raw("bad atom size", "badatom.m4a", b"\x00\x00\x00\x04ftyp" + m4a_bytes[8:])
    raw("ftyp only", "ftyp.m4a", b"\x00\x00\x00\x10ftypM4A \x00\x00\x00\x00")

    # ---- AAC ------------------------------------------------------------
    aac = _ff(os.path.join(d, "base.aac"), "-c:a", "aac", "-b:a", "128k")
    add("ADTS", aac)
    raw("ADTS with ID3", "id3.aac", _id3v2(4, body) + open(aac, "rb").read())
    raw("ADTS cut short", "cut.aac", open(aac, "rb").read()[:700])
    raw("ADIF header", "adif.aac", b"ADIF\x00" + b"\x10\x00\x00\x00" + b"\x00" * 64)

    # ---- WAV / AIFF -----------------------------------------------------
    wav = _ff(os.path.join(d, "base.wav"), "-c:a", "pcm_s16le", "-metadata", "title=Wav title")
    add("WAV with LIST", wav)
    p = raw("WAV with ID3", "id3.wav", open(wav, "rb").read())
    w = WAVE(p)
    w.add_tags()
    w.tags.add(id3.TIT2(encoding=3, text="Wave ID3"))
    w.save()
    wb = open(wav, "rb").read()
    raw("WAV no fmt", "nofmt.wav", wb[:12] + b"junk\x04\x00\x00\x00abcd" + wb[wb.find(b"data"):])
    raw("WAV not WAVE", "notwave.wav", wb[:8] + b"AVI " + wb[12:])
    raw("WAV data past end", "longdata.wav", wb[:-1000])
    raw("WAV bad ID3 chunk", "badid3.wav", wb + b"ID3 \x14\x00\x00\x00ID3\x04\x00\x00\x00\x00\x02\x80" + b"\x00" * 10)
    aif = _ff(os.path.join(d, "base.aiff"), "-c:a", "pcm_s16be")
    add("AIFF", aif)
    p = raw("AIFF with ID3", "id3.aiff", open(aif, "rb").read())
    a = AIFF(p)
    a.add_tags()
    a.tags.add(id3.TIT2(encoding=3, text="Aiff ID3"))
    a.save()
    ab = open(aif, "rb").read()
    i = ab.find(b"COMM")
    raw("AIFF rate inf", "inf.aiff", ab[:i + 16] + b"\x7f\xff" + ab[i + 18:])
    raw("AIFF rate huge", "huge.aiff", ab[:i + 16] + b"\x43\xff" + ab[i + 18:])
    raw("AIFF rate tiny", "tiny.aiff", ab[:i + 16] + b"\x00\x01" + ab[i + 18:])
    raw("AIFF rate negative", "neg.aiff", ab[:i + 16] + b"\xc0\x0e" + ab[i + 18:])
    raw("AIFF no COMM", "nocomm.aiff", ab[:i] + b"XXXX" + ab[i + 4:])

    # ---- other formats and sniffing ------------------------------------
    add("Ogg Vorbis (not read by Rust)", _ff(os.path.join(d, "x.ogg"), "-c:a", "libvorbis"))
    raw("text file named .wav", "text.wav", b"hello world\n" * 20)
    raw("text named .txt", "text.txt", b"hello world\n" * 20)
    raw("RIFF named .flac", "riff.flac", wb)
    raw("FORM named .mp3", "form.mp3", ab)
    return out


def damaged(files, d, n, seed=20261020):
    """n copies of the corpus files, each damaged a seeded random way."""
    os.makedirs(d, exist_ok=True)
    rng = random.Random(seed)
    pool = [(lab, p) for lab, p in files if os.path.getsize(p) > 0 and "Ogg" not in lab]
    out = []
    for k in range(n):
        label, src = pool[k % len(pool)]
        data = bytearray(open(src, "rb").read())
        kind = rng.randrange(6)
        if kind == 0:                                  # flip bytes near the start
            for _ in range(rng.randint(1, 8)):
                i = rng.randrange(min(len(data), 4096))
                data[i] = rng.randrange(256)
        elif kind == 1:                                # flip bytes anywhere
            for _ in range(rng.randint(1, 16)):
                i = rng.randrange(len(data))
                data[i] ^= 1 << rng.randrange(8)
        elif kind == 2:                                # cut short
            data = data[:rng.randrange(len(data))]
        elif kind == 3:                                # insert junk
            i = rng.randrange(len(data))
            data[i:i] = rng.randbytes(rng.randint(1, 64))
        elif kind == 4:                                # zero a run
            i = rng.randrange(len(data))
            j = min(len(data), i + rng.randint(1, 256))
            data[i:j] = b"\x00" * (j - i)
        else:                                          # damage the tail
            for _ in range(rng.randint(1, 8)):
                i = len(data) - 1 - rng.randrange(min(len(data), 256))
                data[i] = rng.randrange(256)
        ext = os.path.splitext(src)[1]
        p = os.path.join(d, "dmg%05d%s" % (k, ext))
        with open(p, "wb") as f:
            f.write(bytes(data))
        out.append(("damaged %s (%d)" % (label, kind), p))
    return out
