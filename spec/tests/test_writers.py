"""Plugins' writers in their manifests (ADR 0230): what a writer declares, what a type that
exports through one maps, and what `iki plugin validate` refuses of either."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from iki_spec.model import PluginExport, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin
from iki_spec.writers import build as writer_build

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "plugins" / "liquid_handling"


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """The liquid_handling example, without its build, to change."""
    target = tmp_path / "liquid_handling"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns("target", "*.wasm"))
    return target


def codes(result: Result) -> list[str]:
    return sorted(p.code for p in result.problems if p.is_error)


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def test_the_example_declares_its_writer_and_its_transfers_export_through_it(
    plugin: Path,
) -> None:
    result = load_plugin(plugin)
    assert codes(result) == []
    assert result.plugin is not None
    writer = result.plugin.writers["acme/liquid_handling.tecan-worklist"]
    assert (writer.format, writer.many, writer.memory) == (
        "acme/liquid_handling.tecan-gwl",
        "file",
        16 * 1024 * 1024,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", writer.sha256)
    assert writer.roles["volume"].unit == "uL"
    assert writer.roles["source_plate"].to == ("acme/liquid_handling.Plate",)
    transfer = result.plugin.types["acme/liquid_handling.Transfer"]
    assert transfer.exports == (
        PluginExport(
            "acme/liquid_handling.tecan-worklist",
            {
                "source_plate": "source",
                "source_well": "source_well",
                "destination_plate": "destination",
                "destination_well": "destination_well",
                "volume": "volume",
                "liquid_class": "liquid_class",
            },
        ),
    )
    # Compiled and read back, as the server reads it.
    again = plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin))))
    assert again.writers == result.plugin.writers
    assert again.types[transfer.qualified_name].exports == transfer.exports


def test_a_writers_digest_is_required(plugin: Path) -> None:
    (plugin / "writers" / "tecan-worklist.wasm.sha256").unlink()
    assert codes(load_plugin(plugin)) == ["writer-digest"]


def test_a_writer_is_a_rust_crate_with_its_lock(plugin: Path) -> None:
    (plugin / "writers" / "tecan-worklist" / "Cargo.lock").unlink()
    assert codes(load_plugin(plugin)) == ["writer-module"]


def test_a_writer_holds_no_symbolic_links(plugin: Path) -> None:
    (plugin / "writers" / "tecan-worklist" / "src" / "escape.rs").symlink_to("/etc/passwd")
    assert codes(load_plugin(plugin)) == ["writer-module"]


def test_a_writer_cant_write_what_a_browser_would_run(plugin: Path) -> None:
    edit(plugin / "plugin.yaml", "media_types: [text/x-tecan-gwl]", "media_types: [image/svg+xml]")
    assert "writer-active-format" in codes(load_plugin(plugin))


def test_a_writers_format_is_one_the_plugin_can_name(plugin: Path) -> None:
    edit(plugin / "plugin.yaml", "format: tecan-gwl", "format: acme/other.gwl")
    assert "writer-format-dependency" in codes(load_plugin(plugin))
    edit(plugin / "plugin.yaml", "format: acme/other.gwl", "format: tecan-csv")
    assert "unknown-format" in codes(load_plugin(plugin))


def test_a_writers_memory_is_within_the_sandboxs(plugin: Path) -> None:
    edit(plugin / "plugin.yaml", "memory: 16MiB", "memory: 512MiB")
    assert codes(load_plugin(plugin)) == ["writer-memory"]


def test_the_schema_refuses_a_module_outside_writers(plugin: Path) -> None:
    edit(plugin / "plugin.yaml", "module: writers/tecan-worklist", "module: ../escape")
    assert "schema" in codes(load_plugin(plugin))


def test_a_role_reads_a_field_of_its_kind(plugin: Path) -> None:
    transfer = plugin / "types" / "transfer.yaml"
    edit(transfer, "      volume: volume\n", "      volume: source_well\n")
    assert codes(load_plugin(plugin)) == ["export-field"]


def test_a_quantity_role_reads_a_field_that_measures_the_same(plugin: Path) -> None:
    edit(plugin / "types" / "transfer.yaml", "unit: uL, min: 0", "unit: mg, min: 0")
    assert codes(load_plugin(plugin)) == ["export-field"]
    # A volume in another unit is converted, so it's fine.
    edit(plugin / "types" / "transfer.yaml", "unit: mg, min: 0", "unit: mL, min: 0")
    assert codes(load_plugin(plugin)) == []


def test_a_reference_role_reads_a_reference_to_its_targets(plugin: Path) -> None:
    edit(
        plugin / "types" / "transfer.yaml",
        "source: { type: ref, to: Plate",
        "source: { type: ref, to: Transfer",
    )
    assert codes(load_plugin(plugin)) == ["export-field"]


def test_every_required_role_is_mapped_and_no_other(plugin: Path) -> None:
    transfer = plugin / "types" / "transfer.yaml"
    edit(transfer, "      volume: volume\n", "")
    assert codes(load_plugin(plugin)) == ["export-role-missing"]
    edit(
        transfer,
        "      liquid_class: liquid_class\n",
        "      liquid_class: liquid_class\n      tip: volume\n",
    )
    assert "unknown-role" in codes(load_plugin(plugin))


def test_a_type_exports_only_through_an_installed_writer(plugin: Path) -> None:
    edit(plugin / "types" / "transfer.yaml", "export: tecan-worklist", "export: hamilton-worklist")
    assert codes(load_plugin(plugin)) == ["unknown-writer"]


def test_the_plugin_images_dockerfile_runs_the_builder_iki_plugin_writers_runs() -> None:
    for path in (
        ROOT / "deploy" / "plugins.Dockerfile",
        ROOT / "deploy" / "config-template" / "plugins.Dockerfile",
    ):
        text = path.read_text()
        assert writer_build.BUILDER_ARG in text, path
        assert writer_build.BUILDER_STAGES in text, path
        assert text.startswith(writer_build.SYNTAX), path
    # The build stage runs with no network, and says which builder it ran in.
    assert "RUN --network=none" in writer_build.BUILDER_STAGES
    assert f"> /out/{writer_build.BUILT_BY};" in writer_build.BUILDER_STAGES


def test_the_writer_builder_is_pinned_by_digest_on_its_own() -> None:
    """Moving Iki's own toolchain doesn't move the writer builder, which every writer's
    digest depends on: it's pinned in one place, by digest, and the local builds and
    iki-writer's packaging read its Rust version from there."""
    image, _, digest = writer_build.BUILDER.partition("@sha256:")
    assert image.startswith("rust:") and re.fullmatch(r"[0-9a-f]{64}", digest)
    version = image.removeprefix("rust:").split("-")[0]
    toolchain = [str(ROOT / "scripts" / "writer-toolchain.sh")]
    printed = subprocess.run(toolchain, capture_output=True, text=True, check=True)  # noqa: S603
    assert printed.stdout.strip() == version
    full = subprocess.run([*toolchain, "--image"], capture_output=True, text=True, check=True)  # noqa: S603
    assert full.stdout.strip() == writer_build.BUILDER
    for script in ("build-writer-wasm.sh", "pin-iki-writer.sh"):
        text = (ROOT / "scripts" / script).read_text()
        assert "writer-toolchain.sh" in text and "/worker/rust-toolchain.toml" not in text


SDK = ROOT / "worker" / "crates" / "iki-writer"


def _lock(path: Path) -> dict[str, tuple[str, str]]:
    packages = tomllib.loads(path.read_text())["package"]
    return {p["name"]: (p["version"], p.get("checksum", "")) for p in packages}


def test_iki_writer_is_versioned_with_iki_and_locks_what_the_workspace_does() -> None:
    sdk = tomllib.loads((SDK / "Cargo.toml").read_text())["package"]
    workspace = tomllib.loads((ROOT / "worker" / "Cargo.toml").read_text())
    assert sdk["version"] == workspace["workspace"]["package"]["version"]
    assert "build-writer.sh" not in sdk["include"]
    # The lock its published crate carries pins the crates the workspace's lock does.
    ours, theirs = _lock(SDK / "Cargo.lock"), _lock(ROOT / "worker" / "Cargo.lock")
    assert ours.pop("iki-writer") == (sdk["version"], "")
    assert ours and all(theirs.get(name) == pinned for name, pinned in ours.items()), (
        "Run scripts/pin-iki-writer.sh."
    )


def test_the_build_stages_what_iki_writer_is_packaged_from() -> None:
    backend = ast.parse((ROOT / "spec" / "backend" / "iki_spec_build.py").read_text())
    (carried,) = [
        ast.literal_eval(node.value)
        for node in backend.body
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "_SDK_FILES"
    ]
    assert carried == writer_build.SDK_FILES
    dockerfile = (ROOT / "server" / "Dockerfile").read_text()
    for name in writer_build.SDK_FILES:
        assert (SDK / name).is_file(), name
        assert f"worker/crates/iki-writer/{name}" in dockerfile, name


def test_the_example_pins_this_ikis_iki_writer_from_crates_io_with_its_checksum() -> None:
    sum_, name = (SDK / "iki-writer.crate.sha256").read_text().split()
    version = tomllib.loads((SDK / "Cargo.toml").read_text())["package"]["version"]
    assert name == f"iki-writer-{version}.crate" and re.fullmatch(r"[0-9a-f]{64}", sum_)
    crate = EXAMPLE / "writers" / "tecan-worklist"
    locked = tomllib.loads((crate / "Cargo.lock").read_text())["package"]
    (pin,) = [p for p in locked if p["name"] == "iki-writer"]
    assert pin == {
        "name": "iki-writer",
        "version": version,
        "source": "registry+https://github.com/rust-lang/crates.io-index",
        "checksum": sum_,
        "dependencies": ["serde", "serde_json"],
    }, "Run scripts/pin-iki-writer.sh."
    assert f'iki-writer = "={version}"' in (crate / "Cargo.toml").read_text()


def test_the_examples_digest_says_what_it_was_built_with() -> None:
    crate = EXAMPLE / "writers" / "tecan-worklist"
    text = (EXAMPLE / "writers" / "tecan-worklist.wasm.sha256").read_text()
    assert re.fullmatch(r"[0-9a-f]{64}  tecan-worklist\.wasm", text.splitlines()[0])
    found = writer_build.read_built_with(text)
    now = writer_build.built_with(crate)
    assert found is not None and re.fullmatch(r"[0-9a-f]{64}", found.script)
    # A build script that changes without changing modules doesn't make digests stale.
    assert (found.source, found.sdk, found.builder) == (now.source, now.sdk, now.builder), (
        "The example's source or the writer builder changed since its digest was written: "
        "run uv run iki plugin writers examples/plugins/liquid_handling --update."
    )


def test_a_writer_pinning_iki_writer_by_a_path_patch_is_refused(plugin: Path) -> None:
    lock = plugin / "writers" / "tecan-worklist" / "Cargo.lock"
    text = lock.read_text()
    pinned = re.sub(
        r'(name = "iki-writer"\nversion = "[^"]+"\n)source = "[^"]+"\nchecksum = "[^"]+"\n',
        r"\1",
        text,
    )
    assert pinned != text
    lock.write_text(pinned)
    result = load_plugin(plugin)
    assert codes(result) == ["writer-module"]
    assert "with no checksum, as a path patch does" in result.problems[0].message
