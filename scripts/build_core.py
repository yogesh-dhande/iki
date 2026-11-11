"""Writes the core plugin's Django app into the server: ``server/src/iki_plugin_core``.

Every deployment has the core plugin (ADR 0038), so the server ships its app,
generated from ``spec/src/iki_spec/core`` like any plugin's. Its migrations
are the server's own, made by ``make core`` with Iki's rules for history and
kept in the app's ``migrations/``. ``server/tests/test_core_plugin.py`` fails
when the app is behind its manifests.

    python scripts/build_core.py [--check]
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

from iki_codegen.build import build
from iki_spec import naming
from iki_spec.builtin import core_plugin

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "server" / "src" / naming.package("core")
#: The generated files; ``migrations/`` is kept as it is.
FILES = ("__init__.py", "apps.py", "manifest.json", "models.py")


def generate(directory: Path) -> Path:
    """Builds the app into ``directory``; returns its package."""
    (package,) = build([core_plugin()], directory)
    return package


def stale(target: Path = TARGET) -> list[str]:
    """The generated files that differ from what the manifests give."""
    with tempfile.TemporaryDirectory() as tmp:
        package = generate(Path(tmp))
        return [
            name
            for name in FILES
            if not (target / name).is_file()
            or not filecmp.cmp(package / name, target / name, shallow=False)
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only say whether it's current")
    args = parser.parse_args(argv)
    if args.check:
        behind = stale()
        if behind:
            sys.stderr.write(
                f"{TARGET} is behind the core manifests ({', '.join(behind)}): run `make core`.\n"
            )
            return 1
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        package = generate(Path(tmp))
        (TARGET / "migrations").mkdir(parents=True, exist_ok=True)
        (TARGET / "migrations" / "__init__.py").touch()
        for name in FILES:
            shutil.copyfile(package / name, TARGET / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
