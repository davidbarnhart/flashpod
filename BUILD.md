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

The result is `dist/flashpod` (`dist/flashpod.exe` on Windows).

## macOS — manual (target: OS X 10.8)

The macOS target is **OS X 10.8 Mountain Lion** — a FireWire-equipped Mac is
the native environment for these iPods. No GitHub runner or modern Python can
produce a 10.8-compatible binary, so this one is built **by hand on 10.8
hardware** and uploaded to the release afterwards.

On the 10.8 Mac:

1. Install **Python 3.6** — the last CPython with an installer that runs on
   10.6–10.8 (from python.org's archive). 3.7+ requires 10.9+.
2. Install the matching toolchain (3.6-compatible):

   ```sh
   python3.6 -m pip install "pyinstaller==4.10" mutagen
   ```

   PyInstaller 4.10 is the last release that supports Python 3.6.
3. Build and smoke-test:

   ```sh
   python3.6 -m PyInstaller --clean --noconfirm flashpod.spec
   ./dist/flashpod flash --self-test
   mv dist/flashpod dist/flashpod-macos-10.8
   ```

4. Attach it to the release (from any machine with `gh` authenticated):

   ```sh
   gh release upload v0.1.0 dist/flashpod-macos-10.8
   ```

   …or drag it onto the release in the GitHub web UI.

The single `flashpod.spec` is written to the kwargs common to PyInstaller 4.x
and 6.x, so the same spec builds with the legacy 10.8 toolchain and on CI.

> A binary built on a newer macOS via CI would only run on that macOS and
> newer — it would **not** reach 10.8 — which is why the supported macOS build
> is the manual 10.8 one.
