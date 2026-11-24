"""Where things are: the repository, and the ikisci.org site's sources and build."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: The ikisci.org site (Astro).
SITE = ROOT / "docs" / "site"
#: What ``python -m sitegen generate`` writes for the site's build (never committed).
GENERATED = SITE / "src" / "generated"
DIST = SITE / "dist"

#: Where the site is served.
ORIGIN = "https://ikisci.org"
#: The GitHub account that hosts the repository (docs/tests/test_release_owner.py).
REPOSITORY = "https://github.com/yogesh-dhande/iki"
