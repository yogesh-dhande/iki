"""iki-spec's build backend: uv_build's, with iki-wasm built first.

iki-spec checks plugins' rules and guards (ADR 0127) with iki-wasm, the module browsers run,
which ``scripts/build-wasm.sh`` builds from the worker's crates into ``src/iki_spec``. A
wheel built without it would install, then refuse every plugin with rules. So, from a
checkout, building a wheel or a source distribution runs that script first, every time, and
a stale module can't ship; from a source distribution, which carries the module, a wheel
is built only if it's there. An editable install, for development, needs neither: its
checks say to run the script when they find no module.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

#: Where the module goes, in the project's source.
WASM = Path("src", "iki_spec", "iki_wasm.wasm")


def ensure_wasm(project: Path) -> Path:
    """Builds iki-wasm into ``project`` from the checkout it's in, or, outside one (a source
    distribution), checks it's there. Raises :class:`RuntimeError` saying what to do."""
    script = project.parent / "scripts" / "build-wasm.sh"
    if script.is_file() and (project.parent / "worker").is_dir():
        subprocess.run([str(script)], check=True)  # noqa: S603 - the checkout's own script
    wasm = project / WASM
    if not wasm.is_file() or wasm.stat().st_size == 0:
        raise RuntimeError(
            f"iki-spec needs iki-wasm ({WASM}), and it isn't there: build iki-spec from Iki's "
            "repository, where scripts/build-wasm.sh builds it first, or from a source "
            "distribution that carries it."
        )
    return wasm


def ensure_dna_annotation(project: Path) -> Path:
    """Build the matcher in a checkout, or require the module and notice in an sdist."""
    package = project / WASM.parent
    script = project.parent / "scripts" / "build-dna-annotation-wasm.sh"
    if script.is_file() and (project.parent / "worker").is_dir():
        subprocess.run([str(script), str(package)], check=True)  # noqa: S603 - checked-in build
    module = package / "dna-annotation.wasm"
    for artifact in (module, package / "rust-standard-library.html"):
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError(f"iki-spec needs {artifact.name} alongside iki_wasm.wasm.")
    return module


def _uv() -> Any:  # noqa: ANN401 - uv_build's module, which has no types
    """uv_build, which only the build's environment has."""
    return importlib.import_module("uv_build")


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    ensure_wasm(Path.cwd())
    ensure_dna_annotation(Path.cwd())
    return str(_uv().build_wheel(wheel_directory, config_settings, metadata_directory))


def build_sdist(sdist_directory: str, config_settings: dict[str, Any] | None = None) -> str:
    ensure_wasm(Path.cwd())
    ensure_dna_annotation(Path.cwd())
    return str(_uv().build_sdist(sdist_directory, config_settings))


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return str(_uv().build_editable(wheel_directory, config_settings, metadata_directory))


def get_requires_for_build_wheel(config_settings: dict[str, Any] | None = None) -> list[str]:
    return list(_uv().get_requires_for_build_wheel(config_settings))


def get_requires_for_build_sdist(config_settings: dict[str, Any] | None = None) -> list[str]:
    return list(_uv().get_requires_for_build_sdist(config_settings))


def get_requires_for_build_editable(config_settings: dict[str, Any] | None = None) -> list[str]:
    return list(_uv().get_requires_for_build_editable(config_settings))


def prepare_metadata_for_build_wheel(
    metadata_directory: str, config_settings: dict[str, Any] | None = None
) -> str:
    return str(_uv().prepare_metadata_for_build_wheel(metadata_directory, config_settings))


def prepare_metadata_for_build_editable(
    metadata_directory: str, config_settings: dict[str, Any] | None = None
) -> str:
    return str(_uv().prepare_metadata_for_build_editable(metadata_directory, config_settings))
