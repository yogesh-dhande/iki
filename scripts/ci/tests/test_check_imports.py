"""The oldest supported interpreter must evaluate annotations from the selected sources."""

import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).parents[1] / "check_imports.py"


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="checks Python 3.12 import semantics")
@pytest.mark.parametrize("selection", [["--", "cli/src/iki_cli/preflight.py"], ["--all"]])
def test_evaluates_annotations_in_the_selected_tree(tmp_path: Path, selection: list[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.uv.workspace]\nmembers = ["cli"]\n')
    package = tmp_path / "cli/src/iki_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    preflight = package / "preflight.py"
    annotation = (
        "import argparse\n"
        "def command(p: argparse._SubParsersAction[argparse.ArgumentParser]): pass\n"
    )
    preflight.write_text(annotation)
    command = [sys.executable, "-I", str(CHECK), "--root", str(tmp_path), *selection]

    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0
    assert "type '_SubParsersAction' is not subscriptable" in result.stderr
    assert "from __future__ import annotations" in result.stderr

    preflight.write_text("from __future__ import annotations\n" + annotation)
    subprocess.run(command, check=True)  # noqa: S603
