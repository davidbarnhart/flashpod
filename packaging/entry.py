"""Frozen-binary entry point for PyInstaller.

PyInstaller analyses a real script, not a package, and a script can't use
the package-relative imports in ``flashpod/__main__.py``. This module does
the absolute import instead. For normal use run ``flashpod`` (the installed
console script) or ``python -m flashpod``.
"""

import sys

from flashpod.cli import main

if __name__ == "__main__":
    sys.exit(main())
