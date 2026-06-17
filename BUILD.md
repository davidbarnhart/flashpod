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
hardware** and uploaded to the release afterwards.

**Build it heavy.** Python 3.6 on 10.8 ships an OpenSSL too old to negotiate
the TLS GitHub requires, so the on-demand firmware download won't work there.
Bundle the firmware in (see "heavy builds" above) so the binary is fully
self-contained — users won't need `--firmware` or a network.

Steps (firmware images already dropped into `flashpod/firmware/`, see below):

1. Install **Python 3.6** — the last CPython with an installer that runs on
   10.6–10.8 (from python.org's archive). 3.7+ requires 10.9+.
2. Install the matching toolchain (3.6-compatible):

   ```sh
   python3.6 -m pip install "pyinstaller==4.10" mutagen
   ```

   PyInstaller 4.10 is the last release that supports Python 3.6.
3. Put the firmware images in place (download them on a modern machine —
   `gh release download firmware --dir flashpod/firmware` — and copy them to
   the 10.8 Mac's `flashpod/firmware/`, since neither `gh` nor a TLS download
   runs on 10.8).
4. Build and smoke-test:

   ```sh
   python3.6 -m PyInstaller --clean --noconfirm flashpod.spec
   ./dist/flashpod flash --self-test
   mv dist/flashpod dist/flashpod-macos-10.8
   ```

5. Attach it to the release (from any machine with `gh` authenticated — `gh`
   itself won't run on 10.8):

   ```sh
   gh release upload v0.1.3 dist/flashpod-macos-10.8
   ```

   …or drag it onto the release in the GitHub web UI.

The single `flashpod.spec` is written to the kwargs common to PyInstaller 4.x
and 6.x, so the same spec builds with the legacy 10.8 toolchain and on CI.

> A binary built on a newer macOS via CI would only run on that macOS and
> newer — it would **not** reach 10.8 — which is why the supported macOS build
> is the manual 10.8 one.
