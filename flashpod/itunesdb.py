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


# The mhit header carries a four-byte type marker at offset 24. Everything
# flashpod has ever written says "\0MP3" regardless of the actual format, and
# that is left alone on purpose: AAC and WAV libraries in the field were built
# with it, and this is not the change to find out what depends on it. FLAC is
# new, so it gets its own -- NUL + a three-character code, the same shape, which
# is also the shape the 1G firmware's own codec tags use ('mp3', 'wav', 'aif',
# 'va', 'mp4' -- three characters and a NUL).
MARKER_MP3 = b"\x00MP3"
MARKER_FLAC = b"\x00FLA"


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
        self.marker = MARKER_MP3        # mhit+24; see the note above
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
    return parse_bytes(open(path, "rb").read())


def parse_bytes(buf):
    """Parse raw iTunesDB bytes into a Library (used by the raw-device path,
    which reads the DB through the userspace FAT driver rather than a file)."""
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
    hdr[24:28] = getattr(track, "marker", MARKER_MP3)         # type marker
    struct.pack_into("<I", hdr, 32, track.time_added)        # time modified
    struct.pack_into("<III", hdr, 36, track.size, track.tracklen,
                     track.track_nr)
    struct.pack_into("<II", hdr, 52, track.year, track.bitrate)
    struct.pack_into("<I", hdr, 60, (track.samplerate & 0xFFFF) << 16)
    struct.pack_into("<I", hdr, 88, track.time_added)        # time added
    struct.pack_into("<Q", hdr, 96, track.dbid)
    hdr[104] = 1                                             # checked
    # where this track stands in the album view and in Songs (see sort_order)
    struct.pack_into("<HH", hdr, 108, *getattr(track, "sort_pos", (0, 0)))
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


# ----------------------------------------------------------------------------
# The order the iPod will want the tracks in
# ----------------------------------------------------------------------------
# The 1st-generation firmware sorts EVERY track the first time Artists, an
# artist's albums, or Songs is opened after a boot, and with a few thousand
# tracks each of those is a pause of seconds (openpod, docs/wiki/24). The three
# orders, read out of the firmware's key tables:
#
#     Artists          artist, album, disc, track number, title
#     an artist/album  album, disc, track number, artist, title
#     Songs            title
#
# We know all of that when the database is written, so we do the work here:
#
#   * the tracks are WRITTEN in the Artists order -- every list the firmware
#     sorts starts as a copy of the order they are in the file;
#     (and their ids are dealt out again in that order: see serialize)
#   * each track carries where it stands in the other two, as two 16-bit
#     numbers (1-based; 0 = not given) at mhit+108 and mhit+110 -- four bytes
#     the stock firmware loads and never looks at.
#
# Stock firmware ignores all of it and sorts as it always did. An openpod
# firmware with the `fastsort` part places the tracks by the numbers and then
# CHECKS the order with the firmware's own comparison, repairing whatever is
# out of place -- so a mistake here costs time on the iPod, never a wrong list.
#
# The string order below is the firmware's own, read out of the 1.5 firmware
# and checked against it (openpod, docs/wiki/24): 500,000 comparisons run through
# the firmware's routine under emulation, none answered differently.
#
# It is two rules, and that is why it is not a true order:
#
#   * QUICK. Walk both names a character at a time. While both characters are
#     a letter, a digit, space, '!' or '.', compare them as capitals; the first
#     difference decides.
#   * FULL. If a character outside that set turns up before any difference,
#     start again and compare the whole names by weight: punctuation and
#     spaces weigh nothing and are skipped, an accented letter weighs the same
#     as the plain one, and a character above U+00FF weighs its own number.
#     The name with fewer weighted characters comes first on a tie.
#
# Before either: blanks at the front are dropped, and an empty name comes last.
# The two rules disagree about where a space goes, so three names can go round
# in a circle (a < b < c < a). There is then no one right list; we produce one
# the firmware accepts, and check that below.
SORT_POS_MAX = 0xFFFF

# The weight of each character below U+0100, from the firmware's table
# (0 = weighs nothing). Digits, then the Latin letters with their accents.
_PRIMARY = (
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0x6c5, 0x6c6, 0x6c7, 0x6c8, 0x6c9, 0x6ca, 0x6cb, 0x6cc, 0x6cd, 0x6ce, 0, 0,
    0, 0, 0, 0, 0, 0x6cf, 0x6e3, 0x6f7, 0x703, 0x71f, 0x74b, 0x753,
    0x773, 0x78d, 0x7a1, 0x7b1, 0x7bd, 0x7e5, 0x7ed, 0x805, 0x81d, 0x829, 0x835, 0x85d,
    0x875, 0x88d, 0x8a5, 0x8b1, 0x8b9, 0x8bd, 0x8c9, 0, 0, 0, 0, 0,
    0, 0x6cf, 0x6e3, 0x6f7, 0x703, 0x71f, 0x74b, 0x753, 0x773, 0x78d, 0x7a1, 0x7b1,
    0x7bd, 0x7e5, 0x7ed, 0x805, 0x81d, 0x829, 0x835, 0x85d, 0x875, 0x88d, 0x8a5, 0x8b1,
    0x8b9, 0x8bd, 0x8c9, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0x6cf, 0, 0, 0, 0, 0, 0, 0, 0x6c7, 0x6c8,
    0, 0x944, 0, 0, 0, 0x6c6, 0x805, 0, 0, 0, 0, 0,
    0x6cf, 0x6cf, 0x6cf, 0x6cf, 0x6cf, 0x6cf, 0x6d3, 0x6f7, 0x71f, 0x71f, 0x71f, 0x71f,
    0x78d, 0x78d, 0x78d, 0x78d, 0x717, 0x7ed, 0x805, 0x805, 0x805, 0x805, 0x805, 0,
    0x80d, 0x88d, 0x88d, 0x88d, 0x88d, 0x8bd, 0x8e9, 0, 0x6cf, 0x6cf, 0x6cf, 0x6cf,
    0x6cf, 0x6cf, 0x6d3, 0x6f7, 0x71f, 0x71f, 0x71f, 0x71f, 0x78d, 0x78d, 0x78d, 0x78d,
    0x717, 0x7ed, 0x805, 0x805, 0x805, 0x805, 0x805, 0, 0x80d, 0x88d, 0x88d, 0x88d,
    0x88d, 0x8bd, 0x8e9, 0x8bd,
)
_QUICK = frozenset(b" !.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
# The leading words the firmware moves to the back of an ARTIST's name, in the
# order it tries them; the first that fits wins and capitals must match as
# written. This is the list an iPod set to English uses -- which is every
# language's articles, not only "The": De La Soul is filed as "La Soul, De".
# (German, Finnish, French, Italian and Dutch iPods use a slightly longer
# list; a name that differs there is put right on the iPod.)
_ARTICLES = tuple(w + " " for w in """
    The the THE De de DE Den den DEN Det det DET En en EN Et et ET Die die
    DIE Der der DER Das das DAS El el EL La la LA Los los LOS Las las LAS Le
    le LE Les les LES Il il IL Lo lo LO I i Gli gli GLI Het het HET Een een
    EEN O o A a Os os OS As as AS
""".split())


def _units(s):
    """A name as the firmware holds it: UTF-16 code units, front blanks off."""
    b = (s or "").encode("utf-16-le", "surrogatepass")
    u = struct.unpack("<%dH" % (len(b) // 2), b)
    i = 0
    while i < len(u) and u[i] <= 0x20:
        i += 1
    return u[i:]


# openpod firmware 1.6 (its `sortunk` part) files a character the iPod cannot
# draw after every letter: from U+0100 to U+0944 the font has only the fifteen
# below, and every other character in that range weighs its number + 0xF000.
# Stock firmware weighs them by their own number, but stock ignores the order
# written here anyway; a 1.6 without that part finds the few affected names
# out of place and sorts them itself.
_DRAWN = ((0x131, 0x131), (0x152, 0x153), (0x178, 0x178), (0x192, 0x192),
          (0x2c6, 0x2c7), (0x2d8, 0x2dd), (0x3a9, 0x3a9), (0x3c0, 0x3c0))


def _weight(c):
    if c < 256:
        return _PRIMARY[c]
    if c <= 0x944 and not any(a <= c <= b for a, b in _DRAWN):
        return c + 0xF000
    return c


def _full(a, b):
    wa = [_weight(c) for c in a]
    wb = [_weight(c) for c in b]
    wa = [w for w in wa if w]
    wb = [w for w in wb if w]
    for x, y in zip(wa, wb):
        if x != y:
            return -1 if x < y else 1
    return (len(wa) > len(wb)) - (len(wa) < len(wb))


def _name_cmp(a, b):
    """The firmware's comparison of two names given as _units(): -1, 0 or 1."""
    if not a or not b:
        return (not a) - (not b)
    for x, y in zip(a, b):
        if x not in _QUICK or y not in _QUICK:
            return _full(a, b)
        if 0x61 <= x <= 0x7A:
            x -= 0x20
        if 0x61 <= y <= 0x7A:
            y -= 0x20
        if x != y:
            return -1 if x < y else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def _sort_artist(s):
    """The firmware files "The Sines" as "Sines, The" (artists only)."""
    s = s or ""
    for art in _ARTICLES:
        if s.startswith(art):
            return s[len(art):] + ", " + art.rstrip()
    return s


def _settle(order, cmp, passes=50):
    """A comparison that goes in circles can leave neighbours the wrong way
    round after a sort. Carry each such item back until it sits after its new
    neighbour, and repeat until no pair is left (or give up: the iPod repairs
    whatever remains). Swapping neighbours once a pass was not enough: a title
    60 places late needed 60 passes, and one pair survived in a 27,488-track
    library, which cost the iPod a full sort of Songs."""
    for _ in range(passes):
        moved = False
        for i in range(len(order) - 1):
            j = i + 1
            while j > 0 and cmp(order[j - 1], order[j]) > 0:
                order[j - 1], order[j] = order[j], order[j - 1]
                j -= 1
                moved = True
        if not moved:
            break
    return order


def sort_order(tracks):
    """`tracks` in the order to write them (the Artists order), with each
    track's `sort_pos` set to its 1-based place in (the album view, Songs)."""
    import functools
    key = {id(t): (_units(_sort_artist(t.artist)), _units(t.album),
                   getattr(t, "disc_nr", 0), t.track_nr, _units(t.title))
           for t in tracks}

    def chain(*parts):
        # parts: indexes into the key; names by the firmware's rule, numbers plainly
        def cmp(s, t):
            ks, kt = key[id(s)], key[id(t)]
            for p in parts:
                x, y = ks[p], kt[p]
                r = (x > y) - (x < y) if p in (2, 3) else _name_cmp(x, y)
                if r:
                    return r
            return 0
        return cmp

    def ordered_by(seq, cmp):
        return _settle(sorted(seq, key=functools.cmp_to_key(cmp)), cmp)

    ordered = ordered_by(tracks, chain(0, 1, 2, 3, 4))
    for t in ordered:
        t.sort_pos = (0, 0)
    if len(ordered) <= SORT_POS_MAX:
        # Ties are broken by the order just chosen, not by the order we were
        # handed, so saving a library twice gives every track the same place
        # and the same numbers both times (the timestamps and the playlist's
        # random ids, stamped on every save as before, still differ).
        by_album = ordered_by(ordered, chain(1, 2, 3, 0, 4))
        by_title = ordered_by(ordered, chain(4))
        place = {id(t): i for i, t in enumerate(by_album, 1)}
        for i, t in enumerate(by_title, 1):
            t.sort_pos = (place[id(t)], i)
    return ordered


def serialize(lib):
    ordered = sort_order(lib.tracks)
    # THE FIRMWARE FINDS A TRACK BY ITS ID WITH A BINARY SEARCH over the tracks
    # in file order: written out of id order, a 300-track library loads as one
    # song (measured in openpod's emulator). So the ids follow the order. The
    # same ids are handed round among the same tracks -- none is new, none is
    # lost -- but a track's id is no longer the same from one save to the next.
    for t, i in zip(ordered, sorted(t.id for t in ordered)):
        t.id = i
    tracks = b"".join(_mk_mhit(t) for t in ordered)
    mhlt = b"mhlt" + struct.pack("<II", 92, len(lib.tracks)) + bytes(92 - 12)
    sd1 = _mk_mhsd(1, mhlt + tracks)

    mpl = _mk_mhyp(lib.name, [t.id for t in ordered], master=True)
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


def copy_to_ipod(mount, src, ext=None, progress=None):
    """Copy a music file into a Music/F## dir; return its ':'-style
    location. Spreads across F dirs; name collision-proofed.

    `progress(done_bytes, total_bytes)` is called periodically during the
    copy (throttled to ~4x/sec) so callers can show movement — important on
    FireWire iPods, where the safe block-queue settings make writes slow and
    a silent copy looks like a hang."""
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
    total = os.path.getsize(src)
    done = 0
    last = 0.0
    timing = os.environ.get("FLASHPOD_TIMING")
    t0 = time.monotonic()
    # copy with modest buffers; shutil.copyfile is fine on a sane host but
    # the FireWire iPod path likes small sequential writes
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        t_open = time.monotonic()
        chunk = fin.read(1 << 16)
        t_first = time.monotonic()
        while chunk:
            fout.write(chunk)
            done += len(chunk)
            if progress:
                now = time.monotonic()
                if now - last >= 0.25:
                    progress(done, total)
                    last = now
            chunk = fin.read(1 << 16)
        if progress:
            progress(done, total)        # final 100% before the (slow) flush
        t_stream = time.monotonic()
        fout.flush()
        os.fsync(fout.fileno())
        t_sync = time.monotonic()
    if timing:
        import sys
        print("[timing] %s: open %.2fs  first-read %.2fs  stream %.2fs  "
              "fsync %.2fs"
              % (os.path.basename(src), t_open - t0, t_first - t_open,
                 t_stream - t_first, t_sync - t_stream), file=sys.stderr)
    return ":".join(["", "iPod_Control", "Music", fdir, name])
