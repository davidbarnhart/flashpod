"""The firmware catalog (firmware/firmware.json) and its download cache.

Library side of what ``flashpod flash`` offers: list the stock images, say
which are already available offline, and fetch one (cached under
~/.cache/flashpod/firmware, sha256-verified). Nothing here prints; the CLI's
``ensure_firmware`` keeps its own messages and shares these helpers.
"""
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.request

from . import resources
from .errors import FlashpodError

def _firmware_cache_dir():
    """Where downloaded firmware is cached.

    `flash` resolves its firmware before elevating (see main()), so the normal
    path reaches this as the invoking user and caches in their home. Running
    `sudo flashpod flash` yourself instead does the download as root, which
    caches somewhere else -- /root/.cache on Linux, and as root-owned files in
    your own ~/.cache on macOS, where sudo preserves HOME. That path is not a
    documented way to use flashpod; prefer plain `flashpod flash` and let it
    elevate itself.
    """
    base = os.environ.get("XDG_CACHE_HOME") or \
        os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "flashpod", "firmware")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Well-known CA bundles, in the order we'd rather find them.
_CA_BUNDLES = (
    "/etc/ssl/cert.pem",                    # macOS, FreeBSD
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu, Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL, Fedora, CentOS
    "/etc/ssl/ca-bundle.pem",               # openSUSE
    "/etc/ssl/certs",                       # a hashed directory, as a last resort
)


def _ca_bundle():
    """A CA bundle we can actually verify against, or None to use the defaults.

    The frozen builds are the problem this exists for. PyInstaller bundles no
    certificates, and the ssl module it packages was compiled with the CA path
    of the BUILD machine baked in -- so on someone else's Mac that path simply
    does not exist and every HTTPS fetch dies with CERTIFICATE_VERIFY_FAILED
    (unable to get local issuer certificate). Nothing is wrong with the user's
    machine or the network; Python is just looking in a directory that was
    never there.

    Order: honour whatever ssl already resolves to (that covers a normal pip
    install, and SSL_CERT_FILE, which get_default_verify_paths reflects), then
    certifi if a build bundled it, then the platform's own store.
    """
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.exists(paths.cafile)) or \
       (paths.capath and os.path.isdir(paths.capath)):
        return None                        # defaults are fine -- don't override
    try:
        import certifi                     # not a dependency; used if present
        where = certifi.where()
        if os.path.exists(where):
            return where
    except Exception:                      # noqa: BLE001 - absent or broken
        pass
    for cand in _CA_BUNDLES:
        if os.path.exists(cand):
            return cand
    return None                            # nothing found; let ssl raise


def _ssl_context():
    bundle = _ca_bundle()
    if bundle and os.path.isdir(bundle):
        ctx = ssl.create_default_context(capath=bundle)
    else:
        ctx = ssl.create_default_context(cafile=bundle)   # None = ssl defaults
    return ctx


class FirmwareUnavailable(FlashpodError):
    code = "firmware_unavailable"


def load_manifest():
    """The parsed firmware/firmware.json. Raises FirmwareUnavailable."""
    try:
        with open(resources.firmware_manifest()) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        raise FirmwareUnavailable("firmware manifest unusable: %s" % exc,
                                  reason="manifest")


def local_path(entry):
    """A verified local copy of ``entry`` (shipped in the build, or in the
    cache), or None. Never downloads."""
    want = entry.get("sha256")
    for path in (os.path.join(resources.firmware_dir(), entry["file"]),
                 os.path.join(_firmware_cache_dir(), entry["file"])):
        if os.path.exists(path) and (not want or _sha256(path) == want):
            return path
    return None


def entry_url(entry, manifest):
    base = manifest.get("base_url", "")
    return entry.get("url") or (base.rstrip("/") + "/" + entry["file"]
                                if base else None)


def fetch(entry, manifest=None, progress=None):
    """Local path of ``entry``, downloading + verifying + caching it first if
    need be. ``progress(done, total)`` during a download. Raises
    FirmwareUnavailable (``reason``: no_url, download, checksum, cache_dir)."""
    path = local_path(entry)
    if path:
        return path
    manifest = manifest if manifest is not None else load_manifest()
    url = entry_url(entry, manifest)
    if not url:
        raise FirmwareUnavailable("no download URL for %s" % entry["file"],
                                  reason="no_url", file=entry["file"])
    cache = _firmware_cache_dir()
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError as exc:
        raise FirmwareUnavailable("cannot create cache dir: %s" % exc,
                                  reason="cache_dir")
    dst = os.path.join(cache, entry["file"])
    tmp = dst + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "flashpod"})
        with urllib.request.urlopen(req, context=_ssl_context()) as resp, \
                open(tmp, "wb") as out:
            total = entry.get("size") or int(resp.headers.get("Content-Length")
                                             or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, OSError) as exc:
        _remove(tmp)
        raise FirmwareUnavailable("download failed: %s" % exc,
                                  reason="download", url=url)
    want = entry.get("sha256")
    if want and _sha256(tmp) != want:
        _remove(tmp)
        raise FirmwareUnavailable("%s failed checksum verification"
                                  % entry["file"], reason="checksum", url=url)
    os.replace(tmp, dst)
    return dst


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
