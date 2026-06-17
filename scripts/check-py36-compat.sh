#!/bin/sh
# Guard the Python 3.6 floor — the macOS 10.8 build target runs Python 3.6,
# the newest CPython that installs on 10.8. These stdlib APIs were added in 3.7
# and a plain syntax check won't catch them (they fail only at runtime on 3.6).
# Add patterns here as new 3.7+ traps are found.
#
#   capture_output= / text=     -> subprocess.run (3.7)  -> use stdout/stderr=PIPE, universal_newlines=
#   add_subparsers(..required=) -> argparse (3.7)        -> set sub.required = True afterwards
set -e
if grep -rnE 'capture_output=|text=True|add_subparsers\([^)]*required=' flashpod/; then
    echo "ERROR: Python 3.7+ stdlib API above — keep flashpod 3.6-compatible" \
         "(the macOS 10.8 build). See scripts/check-py36-compat.sh." >&2
    exit 1
fi
echo "py36 compatibility check: OK"
