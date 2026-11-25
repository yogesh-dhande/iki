"""Lists the current git tree of each of Iki's plugins, in ``iki_spec/first_party_trees.json``.

``iki deploy build`` accepts one of Iki's plugins from a mirror of Iki's
repository only if its tree is listed there (ADR 0114). Run it, as
``make first-party``, whenever a plugin under ``plugins/`` changes. With
``--check``, it changes nothing and fails if a tree is missing.

A tree is what ``git rev-parse <commit>:plugins/<name>`` prints once the
plugin's files are committed. It's computed from the working tree through a
scratch index, so it's right before the commit is made, and it leaves out
what git ignores.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from iki_spec import first_party

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "spec" / "src" / "iki_spec" / first_party.FILENAME


def tree(directory: str) -> str:
    """The git tree of ``directory`` (relative to the repository) as it is on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def git(*args: str) -> str:
            return subprocess.run(  # noqa: S603 - git, with arguments we build
                ["git", "-C", str(ROOT), *args],  # noqa: S607 - git from the PATH
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("read-tree", "--empty")
        git("add", "--", directory)
        return git("write-tree", f"--prefix={directory}/")


def current() -> dict[str, str]:
    """Each of Iki's fetched plugins (all but the core plugin, which ships with the
    server), with its tree."""
    return {
        name: tree(directory.relative_to(ROOT).as_posix())
        for name, directory in first_party.catalog(ROOT / "plugins").items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail if a tree is missing.")
    args = parser.parse_args(argv)
    listed: dict[str, list[str]] = json.loads(LIST.read_text("utf-8"))["trees"]
    try:
        trees = current()
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    missing = {name: t for name, t in trees.items() if t not in listed.get(name, [])}
    if args.check:
        for name, t in missing.items():
            sys.stderr.write(f"{name}'s tree {t} isn't listed: run `make first-party`.\n")
        return 1 if missing else 0
    for name, t in missing.items():
        listed.setdefault(name, []).append(t)
    LIST.write_text(json.dumps({"trees": dict(sorted(listed.items()))}, indent=2) + "\n", "utf-8")
    for name, t in missing.items():
        sys.stdout.write(f"Listed {name}'s tree {t}.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
