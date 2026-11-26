"""`iki plugin writers` (ADR 0230): a plugin's writers, the crates they pin, and their
digests; and the worker bundle, which leaves writers to the plugin image."""

from __future__ import annotations

import argparse
import gzip
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from iki_cli import workers, writer_commands
from iki_spec.writers import build as writer_build

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "liquid_handling"


def run(directory: Path, *, update: bool = False) -> tuple[int, str]:
    out = io.StringIO()
    args = argparse.Namespace(directory=directory, update=update, mirror="")
    return writer_commands.run_writers(args, out), out.getvalue()


def test_it_lists_each_writer_with_its_digest_and_crates() -> None:
    code, shown = run(EXAMPLE)
    assert code == 0
    tracked = (EXAMPLE / "writers" / "tecan-worklist.wasm.sha256").read_text().split()[0]
    assert "acme/liquid_handling.tecan-worklist: Tecan worklist" in shown
    assert f"sha256 {tracked}" in shown
    assert "iki-writer 0.1.0-dev.0" in shown and "serde_json " in shown


def test_updating_needs_docker_to_build_as_the_image_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "liquid_handling"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("target", "*.wasm"))
    monkeypatch.setattr("iki_spec.writers.build.shutil.which", lambda _: None)
    code, _ = run(copy, update=True)
    assert code == 1
    assert "needs Docker" in capsys.readouterr().err


def test_the_builder_stages_a_context_as_a_deployments_build_does(tmp_path: Path) -> None:
    from iki_spec.validate import load_plugin

    loaded = load_plugin(EXAMPLE)
    assert loaded.plugin is not None
    writers = list(loaded.plugin.writers.values())
    crates = writer_build.stage(loaded.plugin.name, EXAMPLE, writers, tmp_path)
    assert crates == {"acme/liquid_handling.tecan-worklist": "acme__liquid_handling/tecan-worklist"}
    staged = tmp_path / "plugins" / "iki_installed_plugins" / "writers"
    assert (staged / "writers.txt").read_text() == "acme__liquid_handling/tecan-worklist\n"
    assert (
        not list(staged.rglob("*.wasm"))
        and not (staged / "acme__liquid_handling" / "tecan-worklist" / "target").exists()
    )
    assert (tmp_path / "Dockerfile").read_text() == writer_build.dockerfile()


def test_a_workers_bundle_leaves_out_the_writers(tmp_path: Path) -> None:
    copy = tmp_path / "liquid_handling"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("target", "*.wasm"))
    workers.bundle(copy, tmp_path / "bundle.tar.gz")
    with gzip.open(tmp_path / "bundle.tar.gz") as raw, tarfile.open(fileobj=raw) as tar:
        names = tar.getnames()
    assert "plugin.yaml" in names
    assert not [n for n in names if n.startswith("writers/")]
