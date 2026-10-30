#!/usr/bin/env python3
"""`firmware list` and firmware by name (#101), against the real binary.

Every run has its own cache, data folder and home. A local server stands in
for GitHub and the download hosts, logs each request, and serves a catalog
whose entries are synthetic firmware. Each check would fail if flashpod
trusted a bad download, used the network when it had no need to, or guessed
between a file and a name. Ends with one PASS/FAIL line; exit 0 only on PASS.
"""
import argparse
import gzip
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_fixtures as synth  # noqa: E402

SNAPSHOT = os.path.join(HERE, "..", "flashpod", "firmware", "patches")
MANIFEST = os.path.join(HERE, "..", "flashpod", "firmware", "firmware.json")
FAILS = []


def check(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Server:
    """Path -> bytes, with a log of every request."""

    def __init__(self):
        self.files, self.log = {}, []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.log.append(self.path)
                data = owner.files.get(self.path)
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                self.wfile.write(data or b"")

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def downloads(self):
        return [p for p in self.log if p.startswith("/dl/")]


def url_of(srv, name, container, body, lie_sha=None, lie_bytes=None):
    srv.files["/dl/" + name] = body
    return {"url": srv.url + "/dl/" + name, "role": "fallback", "container": container,
            "bytes": lie_bytes if lie_bytes is not None else len(body), "sha256": lie_sha or sha(body)}


def entry(fw_id, fw, urls, models=("1G", "2G"), **kw):
    e = {"id": fw_id, "title": fw_id, "maker": "apple", "version": "1", "models": list(models),
         "bytes": len(fw), "sha256": sha(fw), "urls": urls}
    e.update(kw)
    return e


def publish(srv, entries):
    """The real snapshot's index, with the catalog replaced."""
    with open(os.path.join(SNAPSHOT, "index.json")) as f:
        index = json.load(f)
    catalog = json.dumps({"format": 3, "firmwares": entries}).encode()
    for f in index["files"]:
        if f["path"] == "catalog/firmware.json":
            f["bytes"], f["sha256"] = len(catalog), sha(catalog)
    srv.files["/catalog/firmware.json"] = catalog
    srv.files["/index.json"] = json.dumps(index).encode()


def ipsw(members):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return b.getvalue()


class Home:
    """An isolated user: cache, data folder, home, a working folder, a card."""
    count = 0

    def __init__(self, binary, work, srv_url):
        Home.count += 1
        self.root = os.path.join(work, "home%d" % Home.count)
        self.binary, self.cwd = binary, os.path.join(self.root, "cwd")
        os.makedirs(self.cwd)
        self.env = dict(os.environ, HOME=self.root, XDG_CACHE_HOME=os.path.join(self.root, "cache"),
                        XDG_DATA_HOME=os.path.join(self.root, "data"), FLASHPOD_PATCHES_URL=srv_url)
        self.env.pop("FP_MANIFEST", None)
        self.cache = os.path.join(self.root, "cache", "flashpod", "firmware")
        self.builds = os.path.join(self.root, "data", "flashpod", "builds")
        self.img = os.path.join(self.root, "card.img")
        rc, out, err = self.run("card", "new", self.img, "80M")
        assert rc == 0, out + err

    def run(self, *args):
        r = subprocess.run([self.binary] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, stdin=subprocess.DEVNULL, env=self.env, cwd=self.cwd,
                           errors="replace", timeout=300)
        return r.returncode, r.stdout, r.stderr

    def write(self, name, *extra):
        return self.run("firmware", "write", self.img, name, "--yes", "--no-arm", *extra)

    def image_sha(self):
        with open(self.img, "rb") as f:
            return sha(f.read())

    def extract(self):
        out = os.path.join(self.root, "extract.bin")
        rc, o, e = self.run("firmware", "extract", self.img, "-o", out)
        assert rc == 0, o + e
        with open(out, "rb") as f:
            data = f.read()
        os.unlink(out)
        os.unlink(out + ".json")
        return data

    def cached(self):
        return sorted(n for n in os.listdir(self.cache)) if os.path.isdir(self.cache) else []


def card_after_file(binary, work, srv_url, fw, name):
    """What the same firmware looks like on a card when written from a file."""
    h = Home(binary, work, srv_url)
    path = os.path.join(h.root, name)
    with open(path, "wb") as f:
        f.write(fw)
    rc, o, e = h.write(path)
    assert rc == 0, o + e
    return h.extract()


def downloads(binary, work):
    print("downloads")
    srv = Server()
    fws = {n: synth.firmware_bytes(seed=s) for n, s in
           [("order", 11), ("plain", 12), ("gz", 13), ("ipsw", 14), ("inner", 15), ("fail", 16), ("long", 17), ("dlsha", 18)]}
    other = synth.firmware_bytes(seed=99)
    fw = fws["order"]
    entries = [
        entry("order", fw, [
            {"url": srv.url + "/dl/gone", "role": "primary", "container": "bin", "bytes": len(fw), "sha256": sha(fw)},
            url_of(srv, "lying", "bin", fw[:-1] + b"\xff", lie_sha=sha(fw)),
            url_of(srv, "good.gz", "bin.gz", gzip.compress(fw)),
            url_of(srv, "never", "bin", fw)]),
        entry("plain", fws["plain"], [url_of(srv, "plain.bin", "bin", fws["plain"])]),
        entry("gz", fws["gz"], [url_of(srv, "gz.gz", "bin.gz", gzip.compress(fws["gz"]))]),
        entry("ipsw", fws["ipsw"], [url_of(srv, "two.ipsw", "ipsw", ipsw(
            [("Firmware-other", other), ("Firmware-this", fws["ipsw"]), ("Manifest.plist", b"x")]))]),
        entry("inner", fws["inner"], [url_of(srv, "inner.gz", "bin.gz", gzip.compress(other))]),
        entry("fail", fws["fail"], [{"url": srv.url + "/dl/gone1", "role": "primary", "container": "bin",
                                     "bytes": 10, "sha256": "0" * 64},
                                    url_of(srv, "bad2", "bin", other, lie_sha=sha(fws["fail"]))]),
        entry("long", fws["long"], [url_of(srv, "long.bin", "bin", fws["long"] + b"more", lie_bytes=len(fws["long"]))]),
        # the firmware inside is right, the download isn't what was listed
        entry("dlsha", fws["dlsha"], [url_of(srv, "dlsha.gz", "bin.gz", gzip.compress(fws["dlsha"]), lie_sha="0" * 64)]),
        entry("nourl", fws["plain"], []),
        entry("readonly", fws["plain"], [url_of(srv, "ro.bin", "bin", fws["plain"])], models=("4G", "photo")),
    ]
    publish(srv, entries)

    h = Home(binary, work, srv.url)
    rc, out, err = h.write("order")
    check(rc == 0, "a firmware by name is written (%d): %s" % (rc, err[-200:] if rc else ""))
    check(srv.downloads() == ["/dl/gone", "/dl/lying", "/dl/good.gz"],
          "URLs are tried in order and stop at the first good one: %s" % srv.downloads())
    check("/dl/lying: the download doesn't match its sha256" in err, "the bad download is named, for its own sha256")
    check(h.extract() == card_after_file(binary, work, srv.url, fw, "order.bin"),
          "the card holds the same bytes as writing that firmware from a file")

    for name in ("plain", "gz", "ipsw"):
        h = Home(binary, work, srv.url)
        rc, out, err = h.write(name)
        check(rc == 0, "container %s works (%d) %s" % (name, rc, err[-200:] if rc else ""))
        if rc == 0:
            check(h.extract() == card_after_file(binary, work, srv.url, fws[name], name + ".bin"),
                  "container %s: the right firmware reached the card" % name)
            check(h.cached() == ["%s-%s.bin" % (name, sha(fws[name])[:12])], "container %s: one file cached" % name)

    for name, says in (("inner", "firmware inside"), ("fail", "/dl/gone1"), ("long", "longer than"),
                       ("dlsha", "the download doesn't match")):
        h = Home(binary, work, srv.url)
        before = h.image_sha()
        rc, out, err = h.write(name)
        check(rc == 1 and says in err, "%s: refused, saying %r (rc %d)" % (name, says, rc))
        check(h.cached() == [], "%s: nothing cached, no leftovers: %s" % (name, h.cached()))
        check(h.image_sha() == before, "%s: the card is unchanged" % name)

    h = Home(binary, work, srv.url)
    rc, out, err = h.write("nourl")
    check(rc == 1 and "no download listed" in err, "no URLs: says so (rc %d)" % rc)

    srv.log.clear()
    h = Home(binary, work, srv.url)
    before = h.image_sha()
    rc, out, err = h.write("readonly")
    check(rc == 1 and "isn't supported yet" in err and not srv.downloads() and h.image_sha() == before,
          "4G firmware refused before any download (rc %d, %s)" % (rc, srv.downloads()))
    srv.log.clear()
    rc, out, err = h.write("plain", "--model", "3G")
    check(rc == 1 and "not 3G" in err and not srv.downloads(), "--model mismatch refused before any download")
    rc, out, err = h.write("plain", "--model", "2g")
    check(rc == 0, "--model that matches is accepted, whatever its case")

    # the cache
    h = Home(binary, work, srv.url)
    rc, out, err = h.write("plain")
    check(rc == 0, "cached by a first write")
    srv.log.clear()
    first = h.extract()
    rc, out, err = h.write("plain")
    check(rc == 0 and srv.log == [], "a cached firmware makes no request at all: %s" % srv.log)
    check(h.extract() == first, "the cached copy writes the same firmware")
    path = os.path.join(h.cache, h.cached()[0])
    with open(path, "r+b") as f:
        f.seek(0x5000)
        f.write(b"\xaa\xbb\xcc")
    srv.log.clear()
    rc, out, err = h.write("plain")
    check(rc == 0 and "/dl/plain.bin" in srv.downloads() and "downloading it again" in err,
          "a damaged cache copy is downloaded again (%s)" % srv.downloads())
    check(h.extract() == first, "and the damaged bytes didn't reach the card")

    rc, out, err = h.run("firmware", "list", "--json")
    listed = json.loads(out)
    check(listed["source"] == "network", "list reads the served catalog")
    by = {e["id"]: e for e in listed["firmwares"]}
    check(by["plain"]["cached"] and not by["gz"]["cached"], "--json says what's cached")
    check(not by["readonly"]["writable"] and by["plain"]["writable"], "--json says what's writable")
    check(by["plain"]["sha256"] == sha(fws["plain"]) and by["plain"]["urls"][0]["container"] == "bin",
          "--json has the full sha256 and the URLs")

    h = Home(binary, work, srv.url)
    srv.log.clear()
    rc, out, err = h.write("no-such-firmware")
    check(rc == 1 and "not in the catalog or your builds" in err, "an unknown name is refused (rc %d)" % rc)

    # the server gone, the cache still works
    h = Home(binary, work, srv.url)
    check(h.write("plain")[0] == 0, "cached before the server stops")
    h.write("gz")
    srv.httpd.shutdown()
    srv.httpd.server_close()
    rc, out, err = h.write("plain")
    check(rc == 0, "server down, cached firmware still writes (rc %d %s)" % (rc, err[-200:] if rc else ""))
    rc, out, err = h.run("firmware", "list")
    check(rc == 0 and "couldn't refresh" in out and "the copy flashpod last downloaded" in out,
          "server down, list says it uses the old copy")
    rc, out, err = h.write("order")
    check(rc == 1 and "no download of order worked" in err, "server down, an uncached firmware fails and says why")


def offline_list(binary, work):
    print("the list, from the real snapshot")
    h = Home(binary, work, "http://127.0.0.1:9/")
    rc, out, err = h.run("firmware", "list", "--offline")
    with open(os.path.join(SNAPSHOT, "catalog", "firmware.json")) as f:
        catalog = json.load(f)["firmwares"]
    check(rc == 0, "runs with no network (rc %d)" % rc)
    check(all(e["id"] in out for e in catalog) and len(catalog) >= 26, "every catalog entry is listed (%d)" % len(catalog))
    for title in ("Preserved Apple releases:", "flashpod releases:"):
        check(title in out, "section: " + title)
    check("Your builds" not in out, "no builds, no such section")
    check("the copy built into flashpod" in out, "says where the catalog came from")
    line = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
    check("4G*" in line["apple-4g-3.0"] and "photo*" in line["apple-photo-1.0"], "4G and photo are marked")
    check(" yes " in line["apple-1g2g-1.5"] and " ? " in line["apple-1g2g-1.4"], "tested: yes, and ? where nothing's recorded")
    check("apple-1g2g-1.5" in line["flashpod-1.6"] and " 22 " in line["flashpod-1.6"], "a build's base and patch count come from its build file")
    check("No download listed: flashpod-1.6" in out, "entries with no URL are named")
    rc, out, err = h.run("firmware", "list", "--offline", "--json")
    by = {e["id"]: e for e in json.loads(out)["firmwares"]}
    check(by["apple-1g2g-1.4"]["tested_on_hardware"] is None, "--json: no test recorded is null, not false")
    check(len(by["flashpod-1.6"]["patches"]) == 22, "--json: patches listed")


def cross_check():
    print("the built-in manifest and the catalog")
    with open(MANIFEST) as f:
        manifest = json.load(f)
    with open(os.path.join(SNAPSHOT, "catalog", "firmware.json")) as f:
        catalog = json.load(f)["firmwares"]
    urls = {u["sha256"]: e for e in catalog for u in e.get("urls", [])}
    images = [m for m in manifest["firmwares"] if m.get("maker") != "flashpod"]
    check(len(images) >= 25, "%d stock images in the manifest" % len(images))
    for m in images:
        e = urls.get(m["sha256"])
        check(e is not None and e["version"] == m["version"] and sorted(e["models"]) == sorted(m["models"]),
              "%s is in the catalog, same sha256, version and models" % m["file"])


def builds(binary, work):
    print("your builds")
    srv = Server()
    publish(srv, [entry("in-catalog", synth.firmware_bytes(seed=30),
                        [url_of(srv, "c.bin", "bin", synth.firmware_bytes(seed=30))])])
    fw = synth.firmware_bytes(seed=21)
    h = Home(binary, work, srv.url)
    os.makedirs(h.builds)

    def add(name, e, bin_data=fw):
        with open(os.path.join(h.builds, name + ".json"), "w") as f:
            f.write(e if isinstance(e, str) else json.dumps(e))
        if bin_data is not None:
            with open(os.path.join(h.builds, name + ".bin"), "wb") as f:
                f.write(bin_data)

    add("mine", entry("mine", fw, [], maker="me", base="apple-1g2g-1.5", patches=["flac", "library-leak"]))
    # a valid firmware of the right size, but not the one its entry names
    add("damaged", entry("damaged", fw, [], maker="me"), bin_data=synth.firmware_bytes(seed=22))
    add("badjson", "{not json")
    add("wrongname", entry("other-id", fw, [], maker="me"))
    add("in-catalog", entry("in-catalog", fw, [], maker="me"))
    add("nobin", entry("nobin", fw, [], maker="me"), bin_data=None)
    rc, out, err = h.run("firmware", "list")
    check(rc == 0 and "Your builds:" in out, "builds are listed in their own section (rc %d)" % rc)
    line = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
    check(" no " in line.get("mine", "") and " 2 " in line.get("mine", ""), "a build is Tested no unless it says so, with its patch count")
    for name, says in (("badjson", "skipping your build"), ("wrongname", "named after it"),
                       ("in-catalog", "already has a firmware"), ("nobin", "is missing")):
        check(name + ".json" in err and says in err, "%s is warned about" % name)
    check("badjson" not in out and "wrongname" not in out and "nobin" not in out, "and left out of the list")

    srv.log.clear()
    rc, out, err = h.write("mine")
    check(rc == 0 and srv.log == [], "a build is written by name with no request (%d, %s)" % (rc, srv.log))
    check(h.extract() == card_after_file(binary, work, srv.url, fw, "mine.bin"), "and its bytes reach the card")
    before = h.image_sha()
    rc, out, err = h.write("damaged")
    check(rc == 1 and "doesn't match its sha256" in err and h.image_sha() == before,
          "a damaged build is refused for its sha256 (rc %d)" % rc)


def names_and_files(binary, work):
    print("a name, a file, or both")
    srv = Server()
    fw = synth.firmware_bytes(seed=41)
    publish(srv, [entry("both", fw, [url_of(srv, "both.bin", "bin", fw)])])
    h = Home(binary, work, srv.url)
    file_fw = synth.firmware_bytes(seed=42)
    for name in ("both", "plainfile", "fw.bin"):
        with open(os.path.join(h.cwd, name), "wb") as f:
            f.write(file_fw)
    wanted = card_after_file(binary, work, srv.url, file_fw, "x.bin")
    rc, out, err = h.run("firmware", "list")      # the catalog is now in this home's cache
    check(rc == 0, "the served catalog is in the cache")
    srv.log.clear()

    before = h.image_sha()
    rc, out, err = h.write("both")
    check(rc == 1 and "both a file here" in err and "./both" in err and h.image_sha() == before,
          "a name that's a file and a firmware is refused (rc %d)" % rc)
    rc, out, err = h.run("firmware", "write", h.img, "--firmware", "both", "--yes", "--no-arm")
    check(rc == 1 and "both a file here" in err, "the same with --firmware")
    rc, out, err = h.write("./both")
    check(rc == 0 and h.extract() == wanted, "./name is the file")
    rc, out, err = h.write("plainfile")
    check(rc == 0 and h.extract() == wanted, "a bare name that's only a file is the file")
    rc, out, err = h.run("firmware", "write", h.img, "--firmware", "fw.bin", "--yes", "--no-arm")
    check(rc == 0 and h.extract() == wanted, "--firmware FILE still works")
    rc, out, err = h.run("flash", h.img, "plainfile", "--yes", "--no-arm")
    check(rc == 0, "`flash` takes FIRMWARE too")
    rc, out, err = h.write("missing.bin")
    check(rc != 0 and "not in the catalog" not in err, "a missing path gets the file's own error, not the catalog's")
    check(srv.log == [], "files never touch the network: %s" % srv.log)

    rc, out, err = h.run("firmware", "write", h.img, "plainfile", "--firmware", "fw.bin", "--yes", "--no-arm")
    check(rc == 2 and "not both" in err, "FIRMWARE and --firmware together are refused (rc %d)" % rc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", required=True)
    opts = ap.parse_args()
    binary = os.path.abspath(opts.rust)
    work = synth.scratch_dir("fp-fwlist-")
    cross_check()
    offline_list(binary, work)
    downloads(binary, work)
    builds(binary, work)
    names_and_files(binary, work)
    print("%s: %d failed" % ("FAIL" if FAILS else "PASS", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
