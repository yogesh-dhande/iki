"""The DNA annotation module and its notice travel with the iki-spec distribution."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def backend() -> ModuleType:
    source = Path(__file__).parents[1] / "backend" / "iki_spec_build.py"
    spec = importlib.util.spec_from_file_location("iki_spec_build", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "spec"
    (project / "src" / "iki_spec").mkdir(parents=True)
    return project


def test_checkout_rebuilds_annotation_and_its_notice(backend: ModuleType, project: Path) -> None:
    scripts = project.parent / "scripts"
    scripts.mkdir()
    (project.parent / "worker").mkdir()
    build = scripts / "build-dna-annotation-wasm.sh"
    build.write_text(
        "#!/bin/sh\nset -eu\n"
        "printf '\\000asm' > \"$1/dna-annotation.wasm\"\n"
        "printf 'Rust library notice' > \"$1/rust-standard-library.html\"\n"
    )
    build.chmod(0o755)
    resources = project / "src" / "iki_spec"
    (resources / "dna-annotation.wasm").write_bytes(b"stale")

    module = backend.ensure_dna_annotation(project)

    assert module == resources / "dna-annotation.wasm"
    assert module.read_bytes() == b"\0asm"
    assert (resources / "rust-standard-library.html").read_text() == "Rust library notice"


@pytest.mark.parametrize("contents", [None, b""], ids=["missing", "empty"])
def test_sdist_requires_a_nonempty_annotation(
    backend: ModuleType, project: Path, contents: bytes | None
) -> None:
    resources = project / "src" / "iki_spec"
    (resources / "rust-standard-library.html").write_text("Rust library notice")
    if contents is not None:
        (resources / "dna-annotation.wasm").write_bytes(contents)

    with pytest.raises(RuntimeError, match=r"dna-annotation\.wasm"):
        backend.ensure_dna_annotation(project)


@pytest.mark.parametrize("build", ["build_wheel", "build_sdist"])
def test_distribution_build_requires_the_notice(
    backend: ModuleType, project: Path, monkeypatch: pytest.MonkeyPatch, build: str
) -> None:
    resources = project / "src" / "iki_spec"
    (resources / "iki_wasm.wasm").write_bytes(b"\0asm")
    (resources / "dna-annotation.wasm").write_bytes(b"\0asm")
    monkeypatch.chdir(project)

    with pytest.raises(RuntimeError, match=r"rust-standard-library\.html"):
        getattr(backend, build)(str(project / "dist"))
