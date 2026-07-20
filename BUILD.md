# Building flashpod release binaries

flashpod ships as a single self-contained executable per OS, built with
[PyInstaller](https://pyinstaller.org/) from `flashpod.spec`. The firmware
images and the udev rule are bundled inside the binary, so the download is the
only file a user needs.

## Linux & Windows — automated

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds the
Linux and Windows binaries (attached to a GitHub Release) **and publishes the
pure-Python package to PyPI** (see [Releasing to PyPI](#releasing-to-pypi)):

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

## Releasing to PyPI

The same `v*` tag also publishes the pure-Python package to
[PyPI](https://pypi.org/project/flashpod/) via **Trusted Publishing** (OIDC — no
API tokens, no stored secrets). The `pypi` job in `release.yml` builds the
sdist + wheel and uploads them.

**One-time setup** on PyPI (Account settings → *Publishing* → add a *pending*
publisher, since the project doesn't exist on PyPI until the first release):

| Field | Value |
|-------|-------|
| PyPI Project Name | `flashpod` |
| Owner | `davidbarnhart` |
| Repository name | `flashpod` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The **Workflow name** and **Environment name** must match `release.yml` exactly
— the `pypi` job declares `environment: pypi`. The first tagged build creates
the project on PyPI automatically.

**Cutting a release** (PyPI versions are immutable and can't be re-uploaded, so
bump the version first):

```sh
# 1. bump `version` in pyproject.toml (e.g. 0.1.5 -> 0.1.6) and commit
# 2. optionally verify the package builds:
pip install build twine && python -m build && twine check dist/*
# 3. tag — this triggers the binaries + the PyPI publish together:
git tag v0.1.6 && git push origin v0.1.6
```

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

(The `.ipsw` files are gitignored, so this doesn't dirty the repo.)

### Lite builds (sync only, no card imaging)

`FLASHPOD_FLAVOR=lite` strips the card-imaging half: `flash` is hidden from
`--help` and refuses to run, pointing the user at a modern machine instead. The
firmware catalog and the Linux udev rule are left out of the bundle too, since
nothing left in the binary consults them.

```sh
FLASHPOD_FLAVOR=lite pyinstaller --clean --noconfirm flashpod.spec
```

This is the form shipped for **macOS 10.8** (see below): that machine exists to
sync music over FireWire, and imaging a card is a one-time job better done on a
modern computer with a USB card reader.

The flavor is a marker file (`packaging/flavor/build_flavor.txt`) bundled as
`flashpod/build_flavor.txt`; when it's absent the build is **full**. So a plain
`pyinstaller` run, a source checkout, and the pip package are all full builds —
only an explicitly-lite artifact is ever degraded. Verify either flavor with:

```sh
PYTHONPATH=. python3 scripts/test_lite_flavor.py
```

## macOS — manual (target: OS X 10.8)

The macOS target is **OS X 10.8 Mountain Lion** — a FireWire-equipped Mac is
the native environment for these iPods. No GitHub runner or modern Python can
produce a 10.8-compatible binary, so this one is built **by hand on 10.8
hardware** and uploaded to the release afterwards. The recipe below is
battle-tested (it's how `flashpod-macos-10.8` is built).

**Build it lite** (`FLASHPOD_FLAVOR=lite`) — see "Lite builds" above. This
artifact syncs music over FireWire and nothing else; `flash` refuses to run.

That sidesteps what used to force a *heavy* build here. A frozen binary's
`urllib` has no CA bundle on 10.8, so the runtime firmware download can't work
there, and the images had to be baked in to make `flash` usable at all. Once
imaging moved to a modern machine with a USB card reader, none of that applies:
no firmware ships, no network is needed, and the binary is smaller.

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

4. **Get the source onto the Mac.** Either fetch with `python3.6` (TLS now
   works):
   ```sh
   python3.6 - <<'EOF'
   import urllib.request as u
   u.urlretrieve("https://github.com/davidbarnhart/flashpod/archive/refs/tags/v0.1.4.tar.gz","src.tgz")
   EOF
   tar xf src.tgz
   ```
   …or copy a prepared tree over a network share. A lite build bundles no
   firmware, so there is nothing else to fetch.

5. **Build and smoke-test.** Note `FLASHPOD_FLAVOR=lite` — without it you get a
   full build that offers a `flash` command this machine shouldn't be used for:
   ```sh
   cd flashpod-*            # the unpacked source tree
   FLASHPOD_FLAVOR=lite python3.6 -m PyInstaller --clean --noconfirm flashpod.spec
   ./dist/flashpod --help          # `flash` must NOT appear in the command list
   ./dist/flashpod flash ; echo $? # must refuse with the "modern machine" note, exit 1
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
