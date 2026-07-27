#!/usr/bin/env python3
"""Logic test: HTTPS still verifies when ssl's baked-in CA path is missing.

    PYTHONPATH=. python3 scripts/test_ca_bundle_fallback.py

No network: ssl.get_default_verify_paths is faked to describe machines whose
certificate store is somewhere else (or nowhere).

Reported against the macOS binary: every firmware download died with

    CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate

PyInstaller ships no certificates, and the ssl module it packages was compiled
with the BUILD machine's CA path baked in -- /opt/local/... on the machine that
produced the binary. On any other Mac that path does not exist, so ssl has
nothing to verify against, and it fails identically for every user of that
build. `SSL_CERT_FILE=/etc/ssl/cert.pem` fixed it by hand, which is what
_ca_bundle now does automatically.
"""
import os
import ssl
import sys

sys.argv = ["flashpod"]
from flashpod import cli

Paths = type(ssl.get_default_verify_paths())
fails = []


def check(name, fn):
    try:
        fn()
    except Exception as exc:                       # noqa: BLE001
        fails.append("%s: %s: %s" % (name, type(exc).__name__, exc))
        print("  FAIL  %s: %s" % (name, exc))
    else:
        print("  ok    %s" % name)


def fake_paths(cafile=None, capath=None):
    """Pretend ssl resolves to `cafile`/`capath`."""
    return lambda: Paths(cafile=cafile, capath=capath,
                         openssl_cafile_env="SSL_CERT_FILE",
                         openssl_cafile=cafile or "",
                         openssl_capath_env="SSL_CERT_DIR",
                         openssl_capath=capath or "")


_real = ssl.get_default_verify_paths


def with_paths(cafile, capath, fn):
    ssl.get_default_verify_paths = fake_paths(cafile, capath)
    try:
        return fn()
    finally:
        ssl.get_default_verify_paths = _real


# -- when the defaults work, leave them alone ---------------------------------
def t_existing_cafile_is_left_alone():
    """A normal pip install already verifies; don't second-guess it."""
    got = with_paths(__file__, None, cli._ca_bundle)
    assert got is None, "overrode a working default with %r" % got


def t_existing_capath_is_left_alone():
    here = os.path.dirname(os.path.abspath(__file__))
    got = with_paths(None, here, cli._ca_bundle)
    assert got is None, "overrode a working capath with %r" % got


# -- the frozen-binary case ----------------------------------------------------
# What has to hold on EVERY platform is that we end up able to verify, not that
# we found a file. Windows has no CA file: ssl loads the system certificate
# store, so _ca_bundle returning None is the correct answer there and
# create_default_context still comes back with a few hundred CAs. Asserting a
# path would be asserting a POSIX implementation detail.
def _assert_usable_bundle(got):
    if os.name == "nt":
        assert got is None or os.path.exists(got), \
            "pointed Windows at a bundle that isn't there: %r" % got
        return
    assert got is not None, \
        "no fallback found -- every HTTPS fetch would fail on this machine"
    assert os.path.exists(got), "fell back to something that isn't there: %r" % got
    assert got in cli._CA_BUNDLES or got.endswith("cacert.pem"), \
        "unexpected bundle %r" % got


def t_missing_cafile_falls_back():
    """The actual bug: the baked-in path does not exist on this machine."""
    _assert_usable_bundle(with_paths("/nope/does/not/exist/cert.pem", None,
                                     cli._ca_bundle))


def t_no_paths_at_all_falls_back():
    _assert_usable_bundle(with_paths(None, None, cli._ca_bundle))


def t_context_is_verifying():
    """Whatever bundle we pick, the context must still actually verify.

    A fallback that quietly disabled verification would 'fix' the download by
    making it insecure, which is worse than the bug.
    """
    for cafile, capath in ((__file__, None),
                           ("/nope/cert.pem", None),
                           (None, None)):
        ctx = with_paths(cafile, capath, cli._ssl_context)
        assert ctx.verify_mode == ssl.CERT_REQUIRED, \
            "verification disabled for %r/%r" % (cafile, capath)
        assert ctx.check_hostname is True, \
            "hostname checking disabled for %r/%r" % (cafile, capath)


def t_fallback_loaded_real_certs():
    """The real invariant: whatever we resolve to, we can actually verify.

    Covers both broken shapes -- a cafile pointing nowhere, and nothing
    configured at all -- because on Windows this is the ONLY assertion with
    teeth: _ca_bundle correctly returns None there and the certificates come
    from the system store rather than any file.
    """
    for cafile, capath, label in (("/nope/cert.pem", None, "missing cafile"),
                                  (None, None, "nothing configured")):
        ctx = with_paths(cafile, capath, cli._ssl_context)
        n = len(ctx.get_ca_certs())
        assert n > 0, \
            "%s: context loaded 0 CA certificates; every HTTPS fetch would " \
            "fail" % label
        print("         (%s -> %d CA certificates loaded)" % (label, n))


print("CA bundle fallback")
for name, fn in [
    ("working cafile is left alone", t_existing_cafile_is_left_alone),
    ("working capath is left alone", t_existing_capath_is_left_alone),
    ("missing cafile falls back to a real bundle", t_missing_cafile_falls_back),
    ("no configured paths falls back", t_no_paths_at_all_falls_back),
    ("the context still verifies + checks hostnames", t_context_is_verifying),
    ("the fallback bundle holds real CAs", t_fallback_loaded_real_certs),
]:
    check(name, fn)

if fails:
    print("\nCA bundle fallback: %d FAILED" % len(fails), file=sys.stderr)
    for f in fails:
        print("  - " + f, file=sys.stderr)
    sys.exit(1)
print("\nALL ASSERTIONS PASSED")
