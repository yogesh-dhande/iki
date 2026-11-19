"""iki-spec's build backend: a wheel or source distribution is built only with iki-wasm in it,
built afresh by scripts/build-wasm.sh from a checkout."""

import importlib.util
import stat
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

SPEC = Path(__file__).parents[1]


def backend() -> ModuleType:
    found = importlib.util.spec_from_file_location(
        "iki_spec_build", SPEC / "backend" / "iki_spec_build.py"
    )
    assert found is not None and found.loader is not None
    module = importlib.util.module_from_spec(found)
    found.loader.exec_module(module)
    return module


def checkout(root: Path, script: str) -> Path:
    """A checkout's layout: the spec project, the worker's crates and the script."""
    project = root / "spec"
    (project / "src" / "iki_spec").mkdir(parents=True)
    (root / "worker").mkdir()
    (root / "scripts").mkdir()
    build = root / "scripts" / "build-wasm.sh"
    build.write_text(f"#!/bin/sh\nset -eu\n{script}\n")
    build.chmod(build.stat().st_mode | stat.S_IXUSR)
    return project


def test_iki_specs_package_is_built_by_its_own_backend() -> None:
    config = tomllib.loads((SPEC / "pyproject.toml").read_text())
    assert config["build-system"]["build-backend"] == "iki_spec_build"
    assert config["build-system"]["backend-path"] == ["backend"]
    # Carried by the source distribution, so a wheel builds from it too.
    assert "backend/iki_spec_build.py" in config["tool"]["uv"]["build-backend"]["source-include"]


def test_from_a_checkout_it_builds_iki_wasm_first(tmp_path: Path) -> None:
    wasm = "spec/src/iki_spec/iki_wasm.wasm"
    project = checkout(tmp_path, f'cd "$(dirname "$0")/.." && printf wasm > {wasm}')
    (project / "src" / "iki_spec" / "iki_wasm.wasm").write_text("stale")

    built = backend().ensure_wasm(project)

    assert built.read_text() == "wasm"


def test_a_failed_wasm_build_fails_the_package_build(tmp_path: Path) -> None:
    project = checkout(tmp_path, "exit 3")
    (project / "src" / "iki_spec" / "iki_wasm.wasm").write_text("stale")
    with pytest.raises(subprocess.CalledProcessError):
        backend().ensure_wasm(project)


def test_without_a_checkout_iki_wasm_must_be_there(tmp_path: Path) -> None:
    """A source distribution's: it carries the module, or no wheel is built from it."""
    project = tmp_path / "iki_spec-0.1.0"
    (project / "src" / "iki_spec").mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r"iki-spec needs iki-wasm .* isn't there"):
        backend().ensure_wasm(project)
    (project / "src" / "iki_spec" / "iki_wasm.wasm").write_bytes(b"\0asm")
    assert backend().ensure_wasm(project).is_file()
