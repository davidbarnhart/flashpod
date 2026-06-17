# Building flashpod release binaries

flashpod ships as a single self-contained executable per OS, built with
[PyInstaller](https://pyinstaller.org/) from `flashpod.spec`. The firmware
images and the udev rule are bundled inside the binary, so the download is the
only file a user needs.

## Linux & Windows — automated

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds the
Linux and Windows binaries and attaches them to a GitHub Release:

```sh
git tag v0.1.0
git push origin v0.1.0
```

You can also trigger the workflow manually from the **Actions** tab
(*workflow_dispatch*) to get downloadable artifacts without publishing a
release.

Artifacts:

| File | Platform |
|------|----------|
| `flashpod-linux-x86_64` | Linux (built on glibc 2.35 / Ubuntu 22.04, runs on that and newer) |
| `flashpod-windows-x86_64.exe` | Windows 10/11 x86-64 |

## Building locally (any platform)

```sh
pip install pyinstaller mutagen
pyinstaller --clean --noconfirm flashpod.spec
./dist/flashpod flash --self-test       # smoke test
```

The result is `dist/flashpod` (`dist/flashpod.exe` on Windows). By default this
is a **light** build: the firmware `.ipsw` images are *not* inside it; at flash
time flashpod downloads the chosen one from the firmware release (or you pass
`--firmware`).

### Self-contained ("heavy") builds

To bake the firmware into the binary so it never needs the network, just place
the `.ipsw` files next to the manifest before building — the spec bundles the
whole `flashpod/firmware/` directory, and at runtime flashpod prefers a bundled
copy (verified against its SHA-256) over downloading:

```sh
gh release download firmware --dir flashpod/firmware    # the six .ipsw images
pyinstaller --clean --noconfirm flashpod.spec           # now a heavy build
```

(The `.ipsw` files are gitignored, so this doesn't dirty the repo.) This is the
recommended form for the **macOS 10.8** release — see below.

## macOS — manual (target: OS X 10.8)

The macOS target is **OS X 10.8 Mountain Lion** — a FireWire-equipped Mac is
the native environment for these iPods. No GitHub runner or modern Python can
produce a 10.8-compatible binary, so this one is built **by hand on 10.8
hardware** and uploaded to the release afterwards. The recipe below is
battle-tested (it's how `flashpod-macos-10.8` is built).

**Build it heavy** (firmware baked in). An end user's 10.8 machine generally
can't do the runtime firmware download (a frozen binary's `urllib` has no CA
bundle there), so bundle the images so the shipped binary needs no network and
no `--firmware`. See "Self-contained builds" above.

### The two non-obvious traps

- **Use PyInstaller `4.2`, not a newer one.** PyInstaller 4.3+ added
  Apple-Silicon macOS machinery that breaks on 10.8: it ad-hoc-`codesign`s every
  bundled Mach-O (10.8's `codesign` is too old → *"object file format
  unrecognized"*), and it reads each binary's `LC_VERSION_MIN_MACOSX`/
  `LC_BUILD_VERSION` load command (10.6-built objects have none →
  *"Expected exactly one … command"*). 4.2 predates all of it and still supports
  Python 3.6.
- **CA certificates.** A fresh python.org 3.6.8 has working TLS but no CA
  bundle, so `urllib` downloads fail with `CERTIFICATE_VERIFY_FAILED`. `pip`
  works anyway (it ships its own certs); fix `urllib` so you can fetch the
  source/firmware with `python3.6`.

### Steps (on the 10.8 Mac)

1. **Install Python 3.6.8** — python.org's *"macOS 64-bit/32-bit installer"*
   (runs on 10.6+; the 64-bit-only one needs 10.9+). 3.6.8 is the last 3.6 with
   a macOS installer. If the Mac's browser can't reach python.org, download the
   `.pkg` on a modern machine and copy it over.

2. **Fix CA certs** so `python3.6` can download over HTTPS:
   ```sh
   /Applications/Python\ 3.6/Install\ Certificates.command
   # …or, equivalently:
   python3.6 -m pip install --upgrade certifi
   export SSL_CERT_FILE="$(python3.6 -m certifi)"
   ```

3. **Install the build toolchain** (always use `python3.6 -m pip`, not a bare
   `pip`):
   ```sh
   python3.6 -m pip install "pyinstaller==4.2" mutagen
   ```

4. **Get the source + firmware onto the Mac.** Either fetch with `python3.6`
   (TLS now works):
   ```sh
   python3.6 - <<'EOF'
   import urllib.request as u, os
   u.urlretrieve("https://github.com/davidbarnhart/flashpod/archive/refs/tags/v0.1.4.tar.gz","src.tgz")
   base="https://github.com/davidbarnhart/flashpod/releases/download/firmware/"
   os.makedirs("fw",exist_ok=True)
   for f in ["iPod_1.1.5.ipsw","iPod_2.2.3.ipsw","iPod_4.3.1.1.ipsw","iPod_10.3.1.1.ipsw","iPod_5.1.2.1.ipsw","iPod_11.1.2.1.ipsw"]:
       u.urlretrieve(base+f,"fw/"+f)
   EOF
   tar xf src.tgz && cp fw/*.ipsw flashpod-*/flashpod/firmware/
   ```
   …or copy a prepared tree over a network share. Either way, the six `.ipsw`
   must be in `flashpod/firmware/` for a heavy build.

5. **Build and smoke-test:**
   ```sh
   cd flashpod-*            # the unpacked source tree
   python3.6 -m PyInstaller --clean --noconfirm flashpod.spec
   ./dist/flashpod flash --self-test                       # expect "self-test OK"
   python3.6 -c "open('dummy.img','wb').truncate(64*1024*1024)"
   ./dist/flashpod flash dummy.img --dry-run --yes         # must NOT say "downloading"
   ```

6. **Package it like the CI builds** — a tarball with the binary, README, and
   license (so it isn't a lone mystery executable on the releases page):
   ```sh
   stage=flashpod-macos-10.8
   mkdir -p "$stage"
   cp dist/flashpod "$stage/flashpod" && chmod +x "$stage/flashpod"
   cp packaging/macos/README.txt LICENSE "$stage/"
   tar czf flashpod-macos-10.8.tar.gz "$stage"
   ```

7. **Attach it to the release** from any machine with `gh` (gh won't run on
   10.8 — copy the tarball off via a share/USB):
   ```sh
   gh release upload v0.1.4 flashpod-macos-10.8.tar.gz
   ```

> If you're forced onto a newer PyInstaller, you'd need a `codesign` no-op
> stub on `PATH` (`printf '#!/bin/sh\nexit 0\n' > ~/bin/codesign; chmod +x
> ~/bin/codesign`) — but that only dodges the signing trap, not the
> `LC_VERSION_MIN` one. Sticking to 4.2 is the supported path.

> A binary built on a newer macOS via CI would only run on that macOS and
> newer — it would **not** reach 10.8 — which is why the supported macOS build
> is the manual 10.8 one.
