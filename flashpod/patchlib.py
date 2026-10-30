"""The ipod-patches library: patches, bases, builds and the firmware catalog,
kept current from github.com/davidbarnhart/ipod-patches.

Where it comes from, in order (SPEC.md "Index"):

1. **network** -- fetch ``index.json``, then only the files whose sha256 the
   cache doesn't already hold, each checked against the index before it is
   kept. The index is written last, so an interrupted refresh leaves the old
   cache whole.
2. **cache** -- ~/.cache/flashpod/patches, as the last refresh left it, every
   file re-checked against its index.
3. **bundled** -- the snapshot shipped inside flashpod
   (flashpod/firmware/patches), checked the same way.

``load`` says which one it used (``Library.source``) and, when it fell back,
why (``Library.fallback_reason``). Nothing here prints.

The index is untrusted input from the network: every path must be relative,
plain and under one of the spec's top-level folders, or the whole index is
refused (``reason="index"``) and the next source is used.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

from . import resources
from .catalog import _ssl_context
from .errors import FlashpodError

INDEX_URL = ("https://raw.githubusercontent.com/davidbarnhart/ipod-patches/"
             "main/")
FORMAT = 3
TOP_DIRS = ("bases", "builds", "catalog", "patches", "schema")
SOURCES = ("network", "cache", "bundled")


class PatchLibraryUnavailable(FlashpodError):
    code = "patch_library_unavailable"


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or \
        os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "flashpod", "patches")


def bundled_dir():
    return resources.resource_path("firmware", "patches")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def parse_index(data):
    """The index's file list, checked: [{path, bytes, sha256}].
    Raises PatchLibraryUnavailable(reason="index")."""
    def bad(why):
        return PatchLibraryUnavailable("patch index unusable: %s" % why,
                                       reason="index")
    try:
        index = json.loads(data.decode("utf-8"),
                           parse_constant=_no_constant)   # NaN is not JSON
    except (UnicodeDecodeError, ValueError):
        raise bad("not JSON")
    if not isinstance(index, dict):
        raise bad("not an object")
    if type(index.get("format")) is not int or index["format"] != FORMAT:
        raise bad("format is %s, flashpod reads %d"
                  % (_q(index.get("format")), FORMAT))
    files = index.get("files")
    if not isinstance(files, list):
        raise bad("no file list")
    seen = set()
    for f in files:
        if not isinstance(f, dict):
            raise bad("file entry is not an object")
        path, size, sha = f.get("path"), f.get("bytes"), f.get("sha256")
        if not isinstance(path, str) or not _plain_path(path):
            raise bad("path %s" % _q(path))
        if path in seen:
            raise bad("%s listed twice" % _q(path))
        seen.add(path)
        if type(size) is not int or not 0 <= size < 1 << 64:
            raise bad("%s: bytes is not a size" % _q(path))
        if not (isinstance(sha, str) and len(sha) == 64
                and all(c in "0123456789abcdef" for c in sha)):
            raise bad("%s: sha256 %s" % (_q(path), _q(sha)))
    return files


def _no_constant(name):
    raise ValueError(name)


def _q(value):
    """A value quoted for a message, as JSON (the Rust port's serde_json
    prints the same)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plain_path(path):
    parts = path.split("/")
    return (len(parts) >= 2 and parts[0] in TOP_DIRS
            and all(p and p not in (".", "..") and "\\" not in p
                    and "\0" not in p and ":" not in p for p in parts))


def _read_checked(root, entry):
    """The file's bytes if ``root`` holds it as the index says, else None."""
    try:
        with open(os.path.join(root, *entry["path"].split("/")), "rb") as f:
            data = f.read(entry["bytes"] + 1)
    except OSError:
        return None
    if len(data) != entry["bytes"] or _sha256(data) != entry["sha256"]:
        return None
    return data


class Library:
    """A checked copy of the patch library on disk. ``source`` is one of
    SOURCES; ``files`` maps every indexed path to its index entry."""

    def __init__(self, root, files, source, fallback_reason=None):
        self.root = root
        self.files = {f["path"]: f for f in files}
        self.source = source
        self.fallback_reason = fallback_reason

    def read(self, path):
        entry = self.files.get(path)
        if entry is None:
            raise PatchLibraryUnavailable("%s is not in the patch library"
                                          % path, reason="missing", path=path)
        data = _read_checked(self.root, entry)
        if data is None:
            raise PatchLibraryUnavailable("%s changed on disk since it was "
                                          "checked" % path, reason="changed",
                                          path=path)
        return data

    def json(self, path):
        return json.loads(self.read(path).decode("utf-8"))

    def paths(self, top):
        return sorted(p for p in self.files if p.startswith(top + "/"))

    def bases(self):
        return [self.json(p) for p in self.paths("bases") if p.endswith(".json")]

    def builds(self):
        return [self.json(p) for p in self.paths("builds") if p.endswith(".json")]

    def patches(self, base=None):
        return [self.json(p) for p in self.paths("patches")
                if p.endswith("/patch.json")
                and (base is None or p.split("/")[1] == base)]

    def catalog(self):
        return self.json("catalog/firmware.json")


def open_dir(root, source):
    """The library in ``root`` if its index parses and every file checks.
    Raises PatchLibraryUnavailable."""
    try:
        with open(os.path.join(root, "index.json"), "rb") as f:
            raw = f.read()
    except OSError:
        raise PatchLibraryUnavailable("no %s patch library" % source,
                                      reason="absent")
    files = parse_index(raw)
    for entry in files:
        if _read_checked(root, entry) is None:
            raise PatchLibraryUnavailable(
                "%s patch library: %s is missing or fails its sha256"
                % (source, entry["path"]), reason="checksum",
                path=entry["path"])
    return Library(root, files, source)


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "flashpod"})
    with urllib.request.urlopen(req, context=_ssl_context(),
                                timeout=30) as resp:
        return resp.read()


def refresh(base_url=INDEX_URL, get=_http_get, cache=None):
    """Bring the cache up to date from ``base_url``; the refreshed Library.
    ``get(url) -> bytes`` does the fetching (tests pass their own). Raises
    PatchLibraryUnavailable (reason: download, index, checksum, cache_dir)."""
    cache = cache or cache_dir()
    base_url = base_url.rstrip("/") + "/"
    try:
        raw_index = get(base_url + "index.json")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PatchLibraryUnavailable("cannot fetch the patch index: %s"
                                      % exc, reason="download")
    files = parse_index(raw_index)
    bundled = bundled_dir()
    for entry in files:
        if _read_checked(cache, entry) is not None:
            continue
        data = _read_checked(bundled, entry)
        if data is None:
            url = base_url + entry["path"]
            try:
                data = get(url)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise PatchLibraryUnavailable("cannot fetch %s: %s"
                                              % (entry["path"], exc),
                                              reason="download", url=url)
            if len(data) != entry["bytes"] or \
                    _sha256(data) != entry["sha256"]:
                raise PatchLibraryUnavailable(
                    "%s does not match the patch index" % entry["path"],
                    reason="checksum", url=url)
        _write(cache, entry["path"], data)
    _write(cache, "index.json", raw_index)
    return Library(cache, files, "network")


def _write(root, path, data):
    dst = os.path.join(root, *path.split("/"))
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    except OSError as exc:
        raise PatchLibraryUnavailable("cannot write the patch cache: %s"
                                      % exc, reason="cache_dir")


def load(online=True, base_url=INDEX_URL, get=_http_get, cache=None):
    """The patch library from the best source available (see the module
    docstring). With ``online=False`` the network is not tried. Raises
    PatchLibraryUnavailable only when no source is usable."""
    cache = cache or cache_dir()
    reasons = []
    if online:
        try:
            return refresh(base_url, get, cache)
        except PatchLibraryUnavailable as exc:
            reasons.append(str(exc))
    for root, source in ((cache, "cache"), (bundled_dir(), "bundled")):
        try:
            lib = open_dir(root, source)
        except PatchLibraryUnavailable as exc:
            reasons.append(str(exc))
            continue
        lib.fallback_reason = "; ".join(reasons) or None
        return lib
    raise PatchLibraryUnavailable("no usable patch library: %s"
                                  % "; ".join(reasons), reason="none")
