# Building

flashpod ships as a single self-contained executable per OS, built with
[PyInstaller](https://pyinstaller.org/) from `flashpod.spec`. The authoritative,
step-by-step recipe lives in [`BUILD.md`](https://github.com/davidbarnhart/flashpod/blob/main/BUILD.md);
this page is the orientation + the traps worth knowing.

## Linux & Windows (automated)

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds and
attaches `flashpod-linux-x86_64` and `flashpod-windows-x86_64.exe` to a
release. You can also trigger it manually from the Actions tab.

## Any platform (local)

```sh
pip install pyinstaller mutagen
pyinstaller --clean --noconfirm flashpod.spec
./dist/flashpod flash --self-test       # smoke test
```

The result is `dist/flashpod`. By default it's a **light** build (firmware
images are *not* bundled; `flashpod flash` downloads the chosen one). To bake
the firmware in, drop the `.ipsw` files into `flashpod/firmware/` before
building — recommended for the macOS release (a frozen binary's `urllib` has no
CA bundle on 10.8, so it can't download at runtime).

## macOS — manual, target OS X 10.8 (the traps)

A FireWire-equipped Mac running 10.8 is the native environment for these iPods,
and no CI runner or modern Python can produce a 10.8-compatible binary — so it's
built **by hand on 10.8 hardware**. Two non-obvious traps:

1. **Use PyInstaller `4.2`, not newer.** 4.3+ added Apple-Silicon machinery that
   breaks on 10.8 (it ad-hoc-`codesign`s bundled Mach-Os, and reads load
   commands 10.6-built objects lack). 4.2 predates all of it and still supports
   Python 3.6.

   ```sh
   python3.6 -m pip install "pyinstaller==4.2" mutagen
   python3.6 -m PyInstaller --clean --noconfirm flashpod.spec
   ```

2. **CA certificates.** A fresh python.org 3.6.8 has TLS but no CA bundle, so
   `urllib` downloads fail with `CERTIFICATE_VERIFY_FAILED`. Run
   `/Applications/Python\ 3.6/Install\ Certificates.command` (pip works anyway —
   it ships its own certs).

Use the **same `python3.6`** you installed PyInstaller into; a mismatch shows up
as "No module named PyInstaller".

## Rebuild reminder

`dist/flashpod` is a built artifact — it does **not** update when you edit the
source. After any code change, rebuild (or run from source with
`python3 -m flashpod …`) or you'll be testing a stale binary. (This bit us more
than once during development: an error that looked like a code bug was just an
old binary.)

## Running from source (no build)

For development or a one-off, skip the binary entirely:

```sh
python3 -m pip install mutagen          # cli imports it at startup
python3 -m flashpod ls                  # same CLI as the binary
```
