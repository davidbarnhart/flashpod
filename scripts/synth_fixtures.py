"""Synthetic test inputs: a firmware image and tagged audio, made from nothing.

Tests must never use David's music, iTunesDB or card images, nor Apple's
firmware (the shipped images are downloaded, not bundled). These builders
produce stand-ins that exercise the same code:

* :func:`firmware_bytes` -- a format-2 (1G-3G) firmware volume that passes
  ``ipod_flash.load_firmware``: STOP block, ``]ih[`` header, a staging and a
  live directory, one ``osos`` entry whose byte-sum checksum is correct and
  whose load address is still in the pre-install staging state the fixups
  rewrite. The payload carries the version word where 1.5 keeps it (osos
  offset 0x3174c), so version recognition can be tested too.
* :func:`make_music` -- small tagged MP3s (ffmpeg) plus the junk a real
  collection holds: an AppleDouble ``._`` file, a text file, an untagged WAV
  and a corrupt ``.mp3``.
"""
import math
import os
import shutil
import struct
import subprocess
import wave

OSOS_ADDR = 0x28000000
VERSION_WORD_OFF = 0x3174c          # osos offset of 1.5's 0x01508000
OSOS_DEV_OFFSET = 0x5000
OSOS_LEN = 0x32000                  # covers VERSION_WORD_OFF


def osos_payload(version_word=0x01508000, seed=1, length=OSOS_LEN):
    """Deterministic pseudo-code bytes with the version word in place."""
    out = bytearray(length)
    x = seed & 0xFFFFFFFF or 1
    for i in range(0, length, 4):
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        struct.pack_into("<I", out, i, x)
    struct.pack_into("<I", out, VERSION_WORD_OFF, version_word)
    return bytes(out)


def firmware_bytes(version_word=0x01508000, seed=1, payload=None,
                   bad_checksum=False, aupd=None):
    """A format-2 firmware volume as Apple ships it (pre-install state).
    ``aupd`` (bytes) adds a disk-mode program after osos, with an aupd
    entry in both directories in Apple's state: id 0, "update pending"."""
    payload = payload if payload is not None else osos_payload(version_word, seed)
    fw = bytearray(OSOS_DEV_OFFSET + len(payload))
    fw[0:4] = b"{{~~"
    fw[0x100:0x104] = b"]ih["
    ptr = 0x4000
    struct.pack_into("<I", fw, 0x104, ptr)
    struct.pack_into("<H", fw, 0x10a, 2)
    fw[OSOS_DEV_OFFSET:] = payload
    cksum = sum(payload) & 0xFFFFFFFF
    if bad_checksum:
        cksum ^= 1
    for base, dev_off in ((ptr, OSOS_DEV_OFFSET - 0x200),   # staging copy
                          (ptr + 0x200, OSOS_DEV_OFFSET)):   # live copy
        fw[base:base + 4] = b"ksid"
        fw[base + 4:base + 8] = b"soso"
        struct.pack_into("<8I", fw, base + 8, 0, dev_off, len(payload),
                         OSOS_ADDR, 0, cksum, 0x00010000, 0x048D0000)
    if aupd is not None:
        a_off = len(fw)
        fw += aupd
        for base, dev_off in ((ptr, a_off - 0x200), (ptr + 0x200, a_off)):
            fw[base + 40:base + 44] = b"ksid"
            fw[base + 44:base + 48] = b"dpua"
            struct.pack_into("<8I", fw, base + 48, 0, dev_off, len(aupd),
                             0x40000000, 0, sum(aupd) & 0xFFFFFFFF,
                             0x00010000, 0x048D0040)
    return bytes(fw)


def write_firmware(path, **kw):
    with open(path, "wb") as f:
        f.write(firmware_bytes(**kw))
    return path


def _wav(path, seconds, freq, rate=8000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * n / rate)))
            for n in range(int(rate * seconds))))


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def mp3(path, title, artist, album, track, seconds=1.0, freq=440):
    """A tagged MP3 via ffmpeg (bit-exact flags, so reruns match)."""
    tmp = path + ".src.wav"
    _wav(tmp, seconds, freq, rate=22050)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-bitexact", "-i", tmp,
         "-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3",
         "-metadata", "title=" + title, "-metadata", "artist=" + artist,
         "-metadata", "album=" + album, "-metadata", "track=%d" % track,
         "-fflags", "+bitexact", "-flags:a", "+bitexact", path],
        check=True, stdin=subprocess.DEVNULL)
    os.remove(tmp)
    return path


#: (relative path, title, artist, album, track, seconds)
SONGS = [
    ("Alpha Band/First Album/01 Opening.mp3", "Opening", "Alpha Band", "First Album", 1, 1.0),
    ("Alpha Band/First Album/02 Second Song.mp3", "Second Song", "Alpha Band", "First Album", 2, 1.5),
    ("Alpha Band/Later Album/01 Return.mp3", "Return", "Alpha Band", "Later Album", 1, 2.0),
    ("Beta Group/Only Album/03 Third.mp3", "Third", "Beta Group", "Only Album", 3, 1.2),
]


def make_music(root):
    """Build the synthetic collection under ``root``; returns ``root``.
    Raises RuntimeError without ffmpeg rather than quietly testing less."""
    if not have_ffmpeg():
        raise RuntimeError("ffmpeg is required to make the synthetic MP3s")
    for i, (rel, title, artist, album, track, secs) in enumerate(SONGS):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mp3(path, title, artist, album, track, secs, freq=220 * (i + 1))
    _wav(os.path.join(root, "Beta Group", "Only Album", "untagged.wav"), 0.5, 330)
    with open(os.path.join(root, "Alpha Band", "First Album",
                           "._01 Opening.mp3"), "wb") as f:
        f.write(b"\x00\x05\x16\x07AppleDouble junk")
    with open(os.path.join(root, "Alpha Band", "notes.txt"), "w") as f:
        f.write("not audio\n")
    with open(os.path.join(root, "Beta Group", "broken.mp3"), "wb") as f:
        f.write(b"this is not an mp3 at all" * 10)
    return root


def big_wav(path, megabytes):
    """A valid WAV of roughly ``megabytes`` MiB (silence), for space tests."""
    rate = 8000
    frames = (megabytes << 20) // 2
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        chunk = b"\x00\x00" * rate
        left = frames
        while left > 0:
            n = min(left, rate)
            w.writeframes(chunk[:n * 2])
            left -= n
    return path
