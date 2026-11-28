"""iki-spec's build backend: uv_build's, with the WebAssembly modules built first.

iki-spec checks plugins' rules and guards (ADR 0127) with iki-wasm, the module browsers run,
which ``scripts/build-wasm.sh`` builds from the worker's crates into ``src/iki_spec``. A
wheel built without it would install, then refuse every plugin with rules. So, from a
checkout, building a wheel or a source distribution runs that script first, every time, and
a stale module can't ship; from a source distribution, which carries the module, a wheel
is built only if it's there. An editable install, for development, needs neither: its
checks say to run the script when they find no module.

A first-party plugin may carry a module of its own too -- the plasmid plugin's molecular
module, which places primers and cuts DNA (ADR 0200). None of them is tracked in git:
git holds the Rust and each module's SHA-256 digest, and a build makes the bytes. So the
same build runs their scripts, each of which checks what it built against the tracked
digest, and a checkout that cannot build them says so rather than shipping a plugin whose
actions have no module to call.

It carries the plugin runtime's wheel too (ADR 0288), built from ``runtime/`` the same way.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: Where the module goes, in the project's source.
WASM = Path("src", "iki_spec", "iki_wasm.wasm")
#: A first-party plugin's own module, built from the same crates and checked against the
#: digest git tracks beside it: each script in ``scripts/``, and the copy of the module it
#: leaves in iki-spec (``iki_spec.modules``), which every wheel must carry.
PLUGIN_MODULES = {
    "build-plasmid-tools-wasm.sh": "plasmid-tools.wasm",
    "build-oligo-checks-wasm.sh": "oligo-checks.wasm",
}
#: Where iki-spec carries them.
MODULES = Path("src", "iki_spec", "modules")


def ensure_wasm(project: Path) -> Path:
    """Builds iki-wasm into ``project`` from the checkout it's in, or, outside one (a source
    distribution), checks it's there. Raises :class:`RuntimeError` saying what to do."""
    scripts = project.parent / "scripts"
    in_a_checkout = (project.parent / "worker").is_dir()
    script = scripts / "build-wasm.sh"
    if script.is_file() and in_a_checkout:
        subprocess.run([str(script)], check=True)  # noqa: S603 - the checkout's own script
    wasm = project / WASM
    if not wasm.is_file() or wasm.stat().st_size == 0:
        raise RuntimeError(
            f"iki-spec needs iki-wasm ({WASM}), and it isn't there: build iki-spec from Iki's "
            "repository, where scripts/build-wasm.sh builds it first, or from a source "
            "distribution that carries it."
        )
    measurement = wasm.with_name("oligo-measurement.wasm")
    if not measurement.is_file() or measurement.stat().st_size == 0:
        raise RuntimeError("iki-spec needs oligo-measurement.wasm alongside iki_wasm.wasm.")
    notice = wasm.with_name("rust-standard-library.html")
    if not notice.is_file() or notice.stat().st_size == 0:
        raise RuntimeError("iki-spec needs rust-standard-library.html alongside its Rust modules.")
    for name, carried in PLUGIN_MODULES.items():
        module = scripts / name
        if in_a_checkout and module.is_file():
            # Each checks its own bytes against the digest git tracks, so a module that no
            # longer matches its Rust fails the build rather than shipping.
            subprocess.run([str(module)], check=True)  # noqa: S603 - the checkout's own script
        # From a checkout or a source distribution alike: a wheel without it would install,
        # then fail building a deployment of its plugin.
        built = project / MODULES / carried
        if not built.is_file() or built.stat().st_size == 0:
            raise RuntimeError(
                f"iki-spec needs {carried} ({MODULES / carried}), and it isn't there: build "
                f"iki-spec from Iki's repository, where scripts/{name} builds it, or from a "
                "source distribution that carries it."
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


#: The iki-writer SDK's crate (ADR 0230), which iki-spec carries so a deployment's build has
#: the build script plugins' writers are built with, and this Iki's iki-writer, which a
#: writer pinned to it builds against before it's on crates.io, or offline. The same files
#: as iki_spec.writers.build.SDK_FILES, which this build can't import.
WRITER_SDK = Path("src", "iki_spec", "writers", "sdk", "iki-writer")
_SDK_FILES = ("Cargo.toml", "Cargo.lock", "README.md", "build-writer.sh", "iki-writer.crate.sha256")


def ensure_writer_sdk(project: Path) -> Path:
    """Copies the SDK's source into ``project`` from the checkout it's in, or, outside one,
    checks it's there."""
    source = project.parent / "worker" / "crates" / "iki-writer"
    target = project / WRITER_SDK
    if (source / "Cargo.toml").is_file():
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in _SDK_FILES:
            if (source / name).is_file():
                shutil.copy2(source / name, target / name)
        shutil.copytree(source / "src", target / "src")
    if not (target / "Cargo.toml").is_file():
        raise RuntimeError(f"iki-spec needs the iki-writer SDK ({WRITER_SDK}) to build.")
    return target


#: The plugin runtime's wheel (ADR 0288), which iki-spec carries for `iki deploy build` to
#: install into the environments that declare it.
PLUGIN_RUNTIME = Path("src", "iki_spec", "plugin_runtime")


def ensure_plugin_runtime(project: Path) -> Path:
    """Builds the plugin runtime's wheel into ``project`` from the checkout it's in, or,
    outside one, checks it's there."""
    script = project.parent / "scripts" / "build-plugin-runtime.sh"
    if script.is_file() and (project.parent / "runtime" / "pyproject.toml").is_file():
        subprocess.run([str(script)], check=True)  # noqa: S603 - the checkout's own script
    found = sorted((project / PLUGIN_RUNTIME).glob("iki_plugin_runtime-*-py3-none-any.whl"))
    if len(found) != 1 or found[0].stat().st_size == 0:
        raise RuntimeError(
            f"iki-spec needs one iki-plugin-runtime wheel in {PLUGIN_RUNTIME}, and has "
            f"{len(found)}: build iki-spec from Iki's repository, where "
            "scripts/build-plugin-runtime.sh builds it, or from a source distribution that "
            "carries it."
        )
    return found[0]


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
    ensure_writer_sdk(Path.cwd())
    ensure_plugin_runtime(Path.cwd())
    return str(_uv().build_wheel(wheel_directory, config_settings, metadata_directory))


def build_sdist(sdist_directory: str, config_settings: dict[str, Any] | None = None) -> str:
    ensure_wasm(Path.cwd())
    ensure_dna_annotation(Path.cwd())
    ensure_writer_sdk(Path.cwd())
    ensure_plugin_runtime(Path.cwd())
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
