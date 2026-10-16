#!/usr/bin/env python3
"""Record each catalog image's osos sha256 in firmware/firmware.json.

    PYTHONPATH=. python3 scripts/manifest_osos_hashes.py [--check]

`flashpod.card.inspect` names the firmware on a card by the sha256 of its osos
payload as installed (the install fixups touch only the directory, never the
payload). This computes that hash for every manifest entry from a VERIFIED
local copy (shipped or cached, sha256-checked against the manifest; nothing is
downloaded) and writes it into the entry as "osos_sha256", editing the JSON
text in place (one line after each entry's "sha256") so the diff is exactly
the added lines. Entries with no local copy are listed and left alone.

--check: change nothing; exit 1 if any recorded hash is missing or differs.
"""
import hashlib
import json
import re
import struct
import sys

from flashpod import catalog, resources
from flashpod.firmware import load_firmware, walk_directory


def osos_sha(path):
    fw = load_firmware(path)
    ptr, = struct.unpack_from("<I", fw, 0x104)
    fmtver, = struct.unpack_from("<H", fw, 0x10a)
    shift = 0x200 if fmtver == 3 else 0
    for _off, typ, f in walk_directory(fw, ptr + 0x200):
        if typ == b"osos":
            dev, length = f[1], f[2]
            return hashlib.sha256(fw[dev + shift:dev + shift + length]).hexdigest()
    raise ValueError("no osos in %s" % path)


def main():
    check = "--check" in sys.argv
    path = resources.firmware_manifest()
    text = open(path).read()
    manifest = json.loads(text)
    missing, bad = [], []
    for e in manifest["firmwares"]:
        local = catalog.local_path(e)
        if not local:
            missing.append(e["file"])
            continue
        h = osos_sha(local)
        if check:
            if e.get("osos_sha256") != h:
                bad.append(e["file"])
            continue
        if e.get("osos_sha256") == h:
            continue
        if "osos_sha256" in e:
            text, n = re.subn(r'("osos_sha256":\s*)"%s"' % e["osos_sha256"],
                              r'\g<1>"%s"' % h, text)
        else:
            pat = r'(\n(\s*)"sha256":\s*"%s",)' % e["sha256"]
            text, n = re.subn(pat, r'\1\n\2"osos_sha256": "%s",' % h, text)
        if n != 1:
            sys.exit("couldn't place the hash for %s (%d matches)" % (e["file"], n))
        print("%-36s osos %s" % (e["file"], h))
    if missing:
        print("no verified local copy (left alone): " + ", ".join(missing))
    if check:
        if bad or missing:
            print("osos_sha256 missing or wrong: " + ", ".join(bad + missing))
            return 1
        print("all %d osos_sha256 entries match" % len(manifest["firmwares"]))
        return 0
    json.loads(text)                    # still valid JSON
    with open(path, "w") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
