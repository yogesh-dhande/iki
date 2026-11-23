"""Import workspace modules on Python 3.12 to catch errors static checks cannot see."""

import argparse
import os
import shlex
import sys
import tomllib
import traceback
from importlib import import_module
from pathlib import Path


def source_roots(root: Path) -> list[Path]:
    with (root / "pyproject.toml").open("rb") as config:
        members = tomllib.load(config)["tool"]["uv"]["workspace"]["members"]
    # The spec's PEP 517 build backend is also distributed, outside its runtime package.
    return [*(Path(member) / "src" for member in members), Path("spec/backend")]


def module_name(path: Path, source: Path) -> str:
    parts = path.relative_to(source).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="read sources from this tree")
    parser.add_argument("--all", action="store_true", help="import all workspace modules")
    parser.add_argument("paths", type=Path, nargs="*", help="paths relative to the source tree")
    args = parser.parse_args()
    if args.all == bool(args.paths):
        parser.error("pass --all or one or more source paths")
    if sys.version_info[:2] != (3, 12):
        parser.error("run scripts/check-imports-py312 to use Python 3.12")

    root = args.root.resolve()
    sources = source_roots(root)
    # Editable installs add the working tree's src directories through .pth files.
    # Replace them so unstaged edits and deletions cannot leak into an index snapshot.
    sys.path[:] = [
        entry
        for entry in sys.path
        if not (Path(entry).name == "src" and Path(entry).is_relative_to(Path.cwd()))
    ]
    sys.path[:0] = [str(root / source) for source in sources]
    modules: dict[Path, str] = {}
    for source in sources:
        if args.all:
            paths = sorted(path.relative_to(root) for path in (root / source).rglob("*.py"))
        else:
            paths = [p for p in args.paths if p.is_relative_to(source) and p.suffix == ".py"]
        modules.update((p, module_name(p, source)) for p in paths)

    current = "Django setup"
    try:
        if any(p.is_relative_to("server/src") for p in modules):
            import django

            # Server imports need the app registry, but never deployment credentials or a DB.
            for key in list(os.environ):
                if key.startswith(("IKI_", "OTEL_")):
                    del os.environ[key]
            os.environ["DJANGO_SETTINGS_MODULE"] = "iki_server.plugins.build_settings"
            os.environ["IKI_HARNESS_DATABASE_URL"] = "postgres://build@build.invalid/build"
            django.setup()
        for path, name in modules.items():
            current = str(path)
            import_module(name)
    except (Exception, SystemExit) as error:
        traceback.print_exc()
        if isinstance(error, TypeError) and "is not subscriptable" in str(error):
            sys.stderr.write(
                "For a type-only annotation, quote the annotation or add "
                "`from __future__ import annotations` to the module.\n"
            )
        selection = "--all" if current == "Django setup" else "-- " + shlex.quote(current)
        sys.stderr.write(
            f"Fix the Python 3.12 import error in {current} shown above, "
            f"then run scripts/check-imports-py312 {selection}.\n"
        )
        return 1
    count = len(modules)
    sys.stdout.write(f"Python 3.12 imported {count} workspace module{'s' if count != 1 else ''}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
