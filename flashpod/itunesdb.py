"""Pure-Python classic iTunesDB reader/writer — no libgpod.

Targets pre-2007 iPods only (no checksum/hash in the DB), the same scope
as the rest of flashpod. The parser accepts anything from iTunes-4-era
DBs up to modern libgpod output (it walks the record tree generically and
ignores datasets it doesn't know). The writer emits the old two-dataset
format (tracks + playlists) that gen-1/2 firmware is native to.

Format reference: ipodlinux wiki "ITunesDB" + libgpod's itdb_itunesdb.c.
All integers little-endian; strings UTF-16LE; timestamps are Mac epoch
(seconds since 1904-01-01).
"""

import os
import random
import struct
import time

MAC_EPOCH_OFFSET = 2082844800  # 1904-01-01 -> 1970-01-01

# mhod string types
MHOD_TITLE, MHOD_LOCATION, MHOD_ALBUM, MHOD_ARTIST, MHOD_GENRE, \
    MHOD_FILETYPE = 1, 2, 3, 4, 5, 6
MHOD_COMPOSER = 12
MHOD_PLAYLIST_POS = 100

STR_MHODS = {MHOD_TITLE, MHOD_LOCATION, MHOD_ALBUM, MHOD_ARTIST,
             MHOD_GENRE, MHOD_FILETYPE, 7, 8, MHOD_COMPOSER}


class Track:
    FIELDS = ("title", "location", "album", "artist", "genre", "filetype",
              "composer")

    def __init__(self):
        self.id = 0
        self.size = 0
        self.tracklen = 0      # ms
        self.track_nr = 0
        self.year = 0
        self.bitrate = 0
        self.samplerate = 0
        self.time_added = int(time.time()) + MAC_EPOCH_OFFSET
        self.dbid = random.getrandbits(64)
        for f in self.FIELDS:
            setattr(self, f, None)

    def filename_on_ipod(self, mount):
        """':iPod_Control:Music:F12:x.mp3' -> absolute host path."""
        if not self.location:
            return None
        return os.path.join(mount, *self.location.lstrip(":").split(":"))


class Library:
    def __init__(self, name="iPod"):
        self.name = name
        self.tracks = []
        self.dbid = random.getrandbits(64)

    def next_track_id(self):
        return max([t.id for t in self.tracks], default=51) + 1


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------
def _u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def _u64(b, off):
    return struct.unpack_from("<Q", b, off)[0]


def _parse_mhods(buf, off, count):
    """Parse `count` mhod records at off; return ({type: value}, new_off)."""
    out = {}
    for _ in range(count):
        if buf[off:off + 4] != b"mhod":
            break
        hlen, tlen, typ = _u32(buf, off + 4), _u32(buf, off + 8), \
            _u32(buf, off + 12)
        if typ in STR_MHODS:
            slen = _u32(buf, off + hlen + 4)
            data = buf[off + hlen + 16: off + hlen + 16 + slen]
            out[typ] = data.decode("utf-16-le", errors="replace")
        elif typ == MHOD_PLAYLIST_POS:
            out[typ] = _u32(buf, off + hlen)
        off += tlen
    return out, off


def _parse_mhit(buf, off):
    hlen, tlen, nmhod = _u32(buf, off + 4), _u32(buf, off + 8), \
        _u32(buf, off + 12)
    t = Track()
    t.id = _u32(buf, off + 16)
    t.size = _u32(buf, off + 36)
    t.tracklen = _u32(buf, off + 40)
    t.track_nr = _u32(buf, off + 44)
    t.year = _u32(buf, off + 52)
    t.bitrate = _u32(buf, off + 56)
    t.samplerate = _u32(buf, off + 60) >> 16
    if hlen >= 104:
        t.dbid = _u64(buf, off + 96)
    mhods, _ = _parse_mhods(buf, off + hlen, nmhod)
    t.title = mhods.get(MHOD_TITLE)
    t.location = mhods.get(MHOD_LOCATION)
    t.album = mhods.get(MHOD_ALBUM)
    t.artist = mhods.get(MHOD_ARTIST)
    t.genre = mhods.get(MHOD_GENRE)
    t.filetype = mhods.get(MHOD_FILETYPE)
    t.composer = mhods.get(MHOD_COMPOSER)
    return t, off + tlen


def parse(path):
    """Parse an iTunesDB file into a Library."""
    buf = open(path, "rb").read()
    if buf[0:4] != b"mhbd":
        raise ValueError("not an iTunesDB (no mhbd header)")
    lib = Library()
    lib.dbid = _u64(buf, 24)
    hlen = _u32(buf, 4)
    nchildren = _u32(buf, 20)
    off = hlen
    for _ in range(nchildren):
        if buf[off:off + 4] != b"mhsd":
            break
        sd_hlen, sd_tlen, sd_type = _u32(buf, off + 4), _u32(buf, off + 8), \
            _u32(buf, off + 12)
        if sd_type == 1:
            _parse_tracks(buf, off + sd_hlen, lib)
        elif sd_type == 2:
            _parse_playlists(buf, off + sd_hlen, lib)
        off += sd_tlen
    return lib


def _parse_tracks(buf, off, lib):
    if buf[off:off + 4] != b"mhlt":
        return
    hlen, ntracks = _u32(buf, off + 4), _u32(buf, off + 8)
    off += hlen
    for _ in range(ntracks):
        if buf[off:off + 4] != b"mhit":
            break
        track, off = _parse_mhit(buf, off)
        lib.tracks.append(track)


def _parse_playlists(buf, off, lib):
    """Only the master playlist's name is interesting to us."""
    if buf[off:off + 4] != b"mhlp":
        return
    hlen, nlists = _u32(buf, off + 4), _u32(buf, off + 8)
    off += hlen
    for _ in range(nlists):
        if buf[off:off + 4] != b"mhyp":
            break
        p_hlen, p_tlen, nmhod = _u32(buf, off + 4), _u32(buf, off + 8), \
            _u32(buf, off + 12)
        is_master = _u32(buf, off + 20) != 0
        mhods, _ = _parse_mhods(buf, off + p_hlen, nmhod)
        if is_master and MHOD_TITLE in mhods:
            lib.name = mhods[MHOD_TITLE]
        off += p_tlen


# ----------------------------------------------------------------------------
# Writing (old two-dataset format, native to gen-1/2 firmware)
# ----------------------------------------------------------------------------
def _mk_string_mhod(typ, text):
    data = text.encode("utf-16-le")
    body = struct.pack("<IIII", 1, len(data), 0, 0) + data
    return b"mhod" + struct.pack("<III", 24, 24 + 16 + len(data), typ) + \
        struct.pack("<II", 0, 0) + body


def _mk_pos_mhod(position):
    return b"mhod" + struct.pack("<III", 24, 24 + 20, MHOD_PLAYLIST_POS) + \
        struct.pack("<II", 0, 0) + struct.pack("<IIIII", position, 0, 0, 0, 0)


def _mk_mhit(track):
    mhods = b""
    nmhod = 0
    for typ, field in ((MHOD_TITLE, "title"), (MHOD_LOCATION, "location"),
                       (MHOD_ALBUM, "album"), (MHOD_ARTIST, "artist"),
                       (MHOD_GENRE, "genre"), (MHOD_FILETYPE, "filetype"),
                       (MHOD_COMPOSER, "composer")):
        val = getattr(track, field)
        if val:
            mhods += _mk_string_mhod(typ, val)
            nmhod += 1
    HLEN = 156  # iTunes-4.x-era mhit header
    hdr = bytearray(HLEN)
    hdr[0:4] = b"mhit"
    struct.pack_into("<III", hdr, 4, HLEN, HLEN + len(mhods), nmhod)
    struct.pack_into("<II", hdr, 16, track.id, 1)            # id, visible
    hdr[24:28] = b"\x00" + "3PM"[::-1].encode()              # 'MP3 ' marker
    struct.pack_into("<I", hdr, 32, track.time_added)        # time modified
    struct.pack_into("<III", hdr, 36, track.size, track.tracklen,
                     track.track_nr)
    struct.pack_into("<II", hdr, 52, track.year, track.bitrate)
    struct.pack_into("<I", hdr, 60, (track.samplerate & 0xFFFF) << 16)
    struct.pack_into("<I", hdr, 88, track.time_added)        # time added
    struct.pack_into("<Q", hdr, 96, track.dbid)
    hdr[104] = 1                                             # checked
    return bytes(hdr) + mhods


def _mk_mhyp(name, track_ids, master):
    mhods = _mk_string_mhod(MHOD_TITLE, name)
    items = b""
    now = int(time.time()) + MAC_EPOCH_OFFSET
    for pos, tid in enumerate(track_ids, 1):
        pos_mhod = _mk_pos_mhod(pos)
        item = b"mhip" + struct.pack("<IIIIIII", 76, 76 + len(pos_mhod),
                                     1, 0, pos, tid, now)
        item += bytes(76 - len(item)) + pos_mhod
        items += item
    HLEN = 108
    hdr = bytearray(HLEN)
    hdr[0:4] = b"mhyp"
    struct.pack_into("<IIII", hdr, 4, HLEN,
                     HLEN + len(mhods) + len(items), 1, len(track_ids))
    struct.pack_into("<I", hdr, 20, 1 if master else 0)
    struct.pack_into("<I", hdr, 24, now)
    struct.pack_into("<Q", hdr, 28, random.getrandbits(64))
    return bytes(hdr) + mhods + items


def _mk_mhsd(sd_type, payload):
    return b"mhsd" + struct.pack("<III", 96, 96 + len(payload), sd_type) + \
        bytes(96 - 16) + payload


def serialize(lib):
    tracks = b"".join(_mk_mhit(t) for t in lib.tracks)
    mhlt = b"mhlt" + struct.pack("<II", 92, len(lib.tracks)) + bytes(92 - 12)
    sd1 = _mk_mhsd(1, mhlt + tracks)

    mpl = _mk_mhyp(lib.name, [t.id for t in lib.tracks], master=True)
    mhlp = b"mhlp" + struct.pack("<II", 92, 1) + bytes(92 - 12)
    sd2 = _mk_mhsd(2, mhlp + mpl)

    HLEN = 104
    hdr = bytearray(HLEN)
    hdr[0:4] = b"mhbd"
    struct.pack_into("<IIIII", hdr, 4, HLEN, HLEN + len(sd1) + len(sd2),
                     1, 0x0b, 2)                  # unk=1, version(4.7), 2 sds
    struct.pack_into("<Q", hdr, 24, lib.dbid)
    return bytes(hdr) + sd1 + sd2


# ----------------------------------------------------------------------------
# Mountpoint-level operations
# ----------------------------------------------------------------------------
def db_path(mount):
    return os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")


def load(mount):
    return parse(db_path(mount))


def save(lib, mount):
    tmp = db_path(mount) + ".new"
    with open(tmp, "wb") as f:
        f.write(serialize(lib))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, db_path(mount))


def init_ipod(mount, name="iPod"):
    """Create the iPod_Control structure + an empty DB (like itdb_init_ipod)."""
    for sub in ["iTunes", "Device"] + ["Music/F%02d" % i for i in range(50)]:
        os.makedirs(os.path.join(mount, "iPod_Control", sub), exist_ok=True)
    save(Library(name), mount)


def copy_to_ipod(mount, src, ext=None):
    """Copy a music file into a Music/F## dir; return its ':'-style
    location. Spreads across F dirs; name collision-proofed."""
    music = os.path.join(mount, "iPod_Control", "Music")
    fdirs = sorted(d for d in os.listdir(music) if d.startswith("F"))
    if not fdirs:
        raise OSError("no Music/F## directories (run init?)")
    fdir = random.choice(fdirs)
    ext = ext or os.path.splitext(src)[1] or ".mp3"
    while True:
        name = "fp%06d%s" % (random.randrange(10 ** 6), ext.lower())
        dst = os.path.join(music, fdir, name)
        if not os.path.exists(dst):
            break
    # copy with modest buffers; shutil.copyfile is fine on a sane host but
    # the FireWire iPod path likes small sequential writes
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(1 << 16)
            if not chunk:
                break
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return ":".join(["", "iPod_Control", "Music", fdir, name])
