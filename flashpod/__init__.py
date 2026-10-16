"""flashpod — command-line iPod sync + card-flashing tooling for early
(1st/2nd/3rd-generation, FireWire-era) iPods.

Pure Python, no libgpod. See the README for usage.
"""

# The ONE place the version lives. pyproject.toml reads it (setuptools
# dynamic attr), release.yml refuses to build a tag that doesn't match it.
# v0.3.1 shipped self-reporting 0.3.0 because pyproject and this line were
# bumped independently -- never again.
__version__ = "0.4.0"
