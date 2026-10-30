#!/usr/bin/env python3
"""flashpod.patchlib: the ipod-patches client (network -> cache -> bundled).

Runs against the bundled snapshot and a fake server (a dict of URL -> bytes),
never the network. Each check would fail if the client trusted what it
shouldn't, fetched what it already holds, or lost a good cache to a bad
refresh.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from flashpod import patchlib  # noqa: E402

BASE = "https://example.invalid/ipod-patches/"
FAILS = []


def check(cond, what):
    print("%s  %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def server_from(root):
    """URL -> bytes for every file a library dir holds, plus its index."""
    files = {}
    with open(os.path.join(root, "index.json"), "rb") as f:
        files[BASE + "index.json"] = f.read()
    for e in json.loads(files[BASE + "index.json"])["files"]:
        with open(os.path.join(root, e["path"]), "rb") as f:
            files[BASE + e["path"]] = f.read()
    return files


def reindex(server):
    index = json.loads(server[BASE + "index.json"])
    for e in index["files"]:
        data = server[BASE + e["path"]]
        e["bytes"], e["sha256"] = len(data), hashlib.sha256(data).hexdigest()
    server[BASE + "index.json"] = json.dumps(index, indent=1).encode()


class Fake:
    def __init__(self, files):
        self.files, self.got = files, []

    def __call__(self, url):
        self.got.append(url[len(BASE):])
        if url not in self.files:
            raise urllib.error.URLError("404 %s" % url)
        return self.files[url]


def main():
    tmp = tempfile.mkdtemp(prefix="patchlib-")
    flags = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    try:
        run(tmp)
        if "--rust" in flags:
            parity(tmp, flags["--rust"])
        if "--cli" in flags:
            cli(tmp, flags["--cli"])
    finally:
        shutil.rmtree(tmp)
    if FAILS:
        sys.exit("%d check(s) failed" % len(FAILS))
    print("all passed")


def run(tmp):
    bundled = patchlib.open_dir(patchlib.bundled_dir(), "bundled")
    check(len(bundled.patches("apple-1g2g-1.5")) == 22, "bundled: 22 patches for apple-1g2g-1.5")
    check([b["id"] for b in bundled.builds()] == ["flashpod-1.6"], "bundled: build flashpod-1.6")
    check(bundled.catalog().get("format") == 3, "bundled: catalog readable")

    readme = "patches/apple-1g2g-1.5/flac/README.md"
    server = server_from(patchlib.bundled_dir())
    server[BASE + readme] += b"\nA newer note.\n"
    reindex(server)
    cache = os.path.join(tmp, "cache")

    # 1. first refresh: only the index and the one changed file come over the wire
    fake = Fake(server)
    lib = patchlib.load(base_url=BASE, get=fake, cache=cache)
    check(lib.source == "network" and lib.fallback_reason is None, "first load: source network")
    check(fake.got == ["index.json", readme], "first load fetched only index + changed file (%s)" % fake.got)
    check(lib.read(readme).endswith(b"A newer note.\n"), "first load: served the new README")

    # 2. nothing changed: only the index is fetched
    fake = Fake(server)
    patchlib.load(base_url=BASE, get=fake, cache=cache)
    check(fake.got == ["index.json"], "second load fetched only the index (%s)" % fake.got)

    # 3. server publishes a file that doesn't match its index (same length,
    #    wrong bytes): refused, and the old cache index survives
    c3 = os.path.join(tmp, "c3cache")
    shutil.copytree(cache, c3)
    os.remove(os.path.join(c3, readme))                      # so the client must download it
    good_index = open(os.path.join(c3, "index.json"), "rb").read()
    bad = dict(server)
    bad[BASE + readme] = bad[BASE + readme][:-1] + b"?"
    fake = Fake(bad)
    lib = patchlib.load(base_url=BASE, get=fake, cache=c3)
    check(readme in fake.got and lib.source == "bundled" and "does not match" in (lib.fallback_reason or ""),
          "tampered file: refused, fell back (%s)" % lib.fallback_reason)
    check(open(os.path.join(c3, "index.json"), "rb").read() == good_index, "tampered file: cache index untouched")

    # 4. network down: cache
    lib = patchlib.load(base_url=BASE, get=Fake({}), cache=cache)
    check(lib.source == "cache" and "cannot fetch the patch index" in lib.fallback_reason, "offline: cache")

    # 5. cache corrupted, offline: bundled
    with open(os.path.join(cache, readme), "ab") as f:
        f.write(b"rot")
    lib = patchlib.load(online=False, cache=cache)
    check(lib.source == "bundled" and "fails its sha256" in lib.fallback_reason,
          "corrupt cache: bundled (%s)" % lib.fallback_reason)

    # 6. a corrupt cache file is re-fetched, not trusted, on the next refresh
    fake = Fake(server)
    lib = patchlib.load(base_url=BASE, get=fake, cache=cache)
    check(lib.source == "network" and fake.got == ["index.json", readme], "corrupt cache file re-fetched (%s)" % fake.got)

    # 7. hostile or unknown indexes are refused whole, and write nothing outside the cache
    outside = os.path.join(tmp, "escaped")
    for path in ["../escaped", "/etc/escaped", "patches/../../escaped", "elsewhere/x",
                 "patches", "patches//x", "patches/a\\..\\x", "patches/C:x"]:
        evil = copy.deepcopy(json.loads(server[BASE + "index.json"]))
        evil["files"].append({"path": path, "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()})
        files = dict(server)
        files[BASE + "index.json"] = json.dumps(evil).encode()
        files[BASE + path] = b"x"
        lib = patchlib.load(base_url=BASE, get=Fake(files), cache=os.path.join(tmp, "c2"))
        check(lib.source != "network" and "patch index unusable" in lib.fallback_reason,
              "hostile path %r refused" % path)
    check(not os.path.exists(outside) and not os.path.exists("/etc/escaped"), "nothing written outside the cache")
    for fmt in (2, 4, "3", None, 3.0, True):
        idx = json.loads(server[BASE + "index.json"])
        idx["format"] = fmt
        files = dict(server)
        files[BASE + "index.json"] = json.dumps(idx).encode()
        lib = patchlib.load(base_url=BASE, get=Fake(files), cache=os.path.join(tmp, "c3"))
        check(lib.source != "network", "index format %r refused" % (fmt,))

    # 8. read() re-checks: a file changed after load is refused, not served
    lib = patchlib.load(online=False, cache=os.path.join(tmp, "c4"))
    check(lib.source == "bundled", "empty cache, offline: bundled")
    lib2 = patchlib.open_dir(cache, "cache")
    with open(os.path.join(cache, readme), "ab") as f:
        f.write(b"!")
    try:
        lib2.read(readme)
        check(False, "read() refuses a file changed after load")
    except patchlib.PatchLibraryUnavailable as exc:
        check(exc.details.get("reason") == "changed", "read() refuses a file changed after load")


def python_side(fn, *args):
    try:
        return 0, fn(*args)
    except patchlib.PatchLibraryUnavailable as exc:
        return 1, exc.to_dict()


def rust_side(fpcore, *args):
    p = subprocess.run([fpcore] + list(args), stdout=subprocess.PIPE)
    return p.returncode, json.loads(p.stdout.decode())


def parity(tmp, fpcore):
    """Python and Rust (fpcore) agree on every index in a corpus -- the real
    one, hostile paths, wrong types -- and on the bundled library."""
    real = open(os.path.join(patchlib.bundled_dir(), "index.json"), "rb").read()
    good = json.loads(real)
    entry = good["files"][0]
    corpus = [real, b"", b"\xff\xfe", b"[]", b"{}", b'{"format": NaN}', b'{"format": 3}',
              b'{"format": 3, "files": {}}', b'{"format": 3, "files": [7]}',
              b'{"format": 3, "files": []}']
    for fmt in (2, "3", 3.0, True, None, [3]):
        corpus.append(json.dumps(dict(good, format=fmt)).encode())
    for k, v in [("path", "../x"), ("path", "patches/\u00e9/../x"), ("path", 7), ("path", "patches/a\u0000b"),
                 ("bytes", -1), ("bytes", 1.0), ("bytes", True), ("bytes", "9"), ("bytes", 2 ** 70),
                 ("sha256", "A" * 64), ("sha256", "0" * 63), ("sha256", None)]:
        corpus.append(json.dumps(dict(good, files=[dict(entry, **{k: v})]), ensure_ascii=False).encode())
    corpus.append(json.dumps(dict(good, files=[entry, entry])).encode())
    corpus.append(json.dumps(dict(good, files=[{"path": "patches/x/\u00e9.json", "bytes": 0,
                                                 "sha256": "0" * 64}]), ensure_ascii=False).encode())
    probe = os.path.join(tmp, "probe.json")
    for i, data in enumerate(corpus):
        with open(probe, "wb") as f:
            f.write(data)
        py, rs = python_side(patchlib.parse_index, data), rust_side(fpcore, "patch-index", probe)
        check(py == rs, "parity: index case %d (%s)" % (i, "error" if py[0] else "%d files" % len(py[1])))
        if py != rs:
            print("    python %r\n    rust   %r" % (py, rs))
    lib = patchlib.open_dir(patchlib.bundled_dir(), "bundled")
    py = {"files": len(lib.files), "bases": [b["id"] for b in lib.bases()],
          "builds": [b["id"] for b in lib.builds()], "patches": [p["id"] for p in lib.patches()],
          "catalog": lib.catalog()}
    check((0, py) == rust_side(fpcore, "patch-library", patchlib.bundled_dir()), "parity: bundled library")
    # a comparison that can't fail is caught: one altered field must show
    check(python_side(patchlib.parse_index, corpus[0]) != rust_side(fpcore, "patch-index", _altered(tmp, good)),
          "parity: an altered index is told apart")


def cli(tmp, flashpod):
    """The Rust CLI's `firmware patches` over a local HTTP server: downloads
    what changed, refuses a tampered file and keeps its cache."""
    import functools
    import http.server
    import threading
    served = os.path.join(tmp, "served")
    shutil.copytree(patchlib.bundled_dir(), served)
    readme = os.path.join(served, "patches", "apple-1g2g-1.5", "flac", "README.md")
    with open(readme, "ab") as f:
        f.write(b"\nA newer note.\n")
    files = server_from(served)
    reindex(files)
    with open(os.path.join(served, "index.json"), "wb") as f:
        f.write(files[BASE + "index.json"])
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = functools.partial(Quiet, directory=served)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, XDG_CACHE_HOME=os.path.join(tmp, "xdg"),
               FLASHPOD_PATCHES_URL="http://127.0.0.1:%d/" % srv.server_address[1])
    cached = os.path.join(tmp, "xdg", "flashpod", "patches", "patches", "apple-1g2g-1.5", "flac", "README.md")

    def run_cli(*extra):
        p = subprocess.run([flashpod, "firmware", "patches"] + list(extra), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode()
    try:
        rc, out = run_cli()
        check(rc == 0 and out.startswith("Patch library: downloaded") and "library-leak" in out
              and "flashpod-1.6" in out, "cli: downloaded and listed")
        check(open(cached, "rb").read().endswith(b"A newer note.\n"), "cli: cache holds the new file")
        with open(readme, "r+b") as f:                 # same length, wrong bytes
            f.seek(-2, 2)
            f.write(b"?")
        os.remove(cached)
        rc, out = run_cli()
        check(rc == 0 and out.startswith("Patch library: the copy built into flashpod")
              and "does not match the patch index" in out, "cli: tampered download refused")
        rc, out = run_cli("--offline")
        check(rc == 0 and "couldn't refresh" not in out, "cli: --offline doesn't touch the network")
    finally:
        srv.shutdown()


def _altered(tmp, good):
    path = os.path.join(tmp, "altered.json")
    files = [dict(f) for f in good["files"]]
    files[0]["bytes"] += 1
    with open(path, "w") as f:
        json.dump(dict(good, files=files), f)
    return path


if __name__ == "__main__":
    main()
