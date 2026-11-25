"""Run each author example with its declared dependencies and the tests a lab copies."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from iki_spec import action_tests, naming
from iki_spec.first_party_dependencies import dependency_closures
from iki_spec.yamlsource import read_documents

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "plugins"


def check(directory: Path, dependencies: tuple[Path, ...]) -> None:
    manifest = directory / "plugin.yaml"
    docs, problems = read_documents(manifest, str(manifest))
    data = docs[0].data if len(docs) == 1 else None
    name = data.get("name") if isinstance(data, dict) else None
    if problems or not isinstance(name, str) or naming.is_reserved(naming.split(name)[0]):
        raise ValueError(f"{manifest} must declare a plugin in a lab namespace.")
    if not (directory / "README.md").is_file():
        raise ValueError(f"{directory} needs a README explaining how to use and adapt the example.")

    withs = [arg for path in dependencies for arg in ("--with", str(path))]
    for command, flags in (("validate", []), ("makemigrations", ["--check", "--no-input"])):
        subprocess.run(  # noqa: S603 - this interpreter and the workspace CLI
            [sys.executable, "-m", "iki_cli", "plugin", command, directory, *withs, *flags],
            check=True,
        )

    tests = directory / action_tests.DIRECTORY
    python_tests = sorted({*tests.rglob("test_*.py"), *tests.rglob("*_test.py")})
    action_fixtures = sorted({*tests.rglob("*.yaml"), *tests.rglob("*.yml")})
    if action_fixtures:
        subprocess.run(  # noqa: S603 - this interpreter and the workspace CLI
            [sys.executable, "-m", "iki_cli", "plugin", "test", directory, *withs], check=True
        )
    if python_tests:
        subprocess.run(  # noqa: S603 - pytest collects the example's tests
            [sys.executable, "-m", "pytest", "-c", ROOT / "pyproject.toml", *python_tests],
            env={**os.environ, "IKI_GATE": "1"},
            check=True,
        )
    elif not any(
        isinstance(doc.data, dict) and doc.data.get("kind") == action_tests.KIND
        for fixture in action_fixtures
        for doc in read_documents(fixture, str(fixture))[0]
    ):
        raise ValueError(f"{directory} needs an ActionTest or pytest test under tests/.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="*", help="default: every plugin example")
    args = parser.parse_args()
    examples = sorted(
        path
        for path in EXAMPLES.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name != "__pycache__"
    )
    selected = [path.resolve() for path in args.directories] or examples
    try:
        closures = dependency_closures(ROOT / "plugins", *dict.fromkeys([*examples, *selected]))
        for directory in selected:
            check(directory, closures[directory])
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"Checked {len(selected)} plugin example(s).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
