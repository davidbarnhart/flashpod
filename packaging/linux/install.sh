#!/bin/sh
# flashpod installer — copies the bundled `flashpod` binary onto your PATH.
#
#   ./install.sh          install to ~/.local/bin   (no root needed)
#   sudo ./install.sh     install to /usr/local/bin (system-wide)
#   ./install.sh DIR      install to DIR
#
# Or skip this entirely and just run ./flashpod in place.
set -e

here=$(cd "$(dirname "$0")" && pwd)
src="$here/flashpod"
[ -f "$src" ] || { echo "error: 'flashpod' not found next to this script" >&2; exit 1; }

if [ -n "$1" ]; then
    dest="$1"
elif [ "$(id -u)" = "0" ]; then
    dest="/usr/local/bin"
else
    dest="$HOME/.local/bin"
fi

mkdir -p "$dest"
cp "$src" "$dest/flashpod"
chmod +x "$dest/flashpod"
echo "Installed flashpod -> $dest/flashpod"

case ":$PATH:" in
    *":$dest:"*) ;;
    *)
        echo
        echo "NOTE: $dest is not on your PATH. Add it with:"
        echo "  echo 'export PATH=\"$dest:\$PATH\"' >> ~/.profile && . ~/.profile"
        ;;
esac

echo
echo "Done. Try:  flashpod --help"
if [ "$dest" = "/usr/local/bin" ] || [ "$(id -u)" = "0" ]; then
    echo "Flashing a card needs root:  sudo flashpod flash"
else
    echo "Flashing a card needs root. If 'sudo flashpod' isn't found, use:"
    echo "  sudo \"$dest/flashpod\" flash"
fi
