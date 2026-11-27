"""Plugins' writers in their manifests (ADR 0230): what a writer declares, what a type that
exports through one maps, and what `iki plugin validate` refuses of either."""

from __future__ import annotations

import json
import re
import shutil
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
    writer = result.plugin.writers["acme/liquid_handling.tecan_worklist"]
    assert (writer.format, writer.many, writer.memory) == (
        "acme/liquid_handling.tecan_gwl",
        "file",
        16 * 1024 * 1024,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", writer.sha256)
    assert writer.roles["volume"].unit == "uL"
    assert writer.roles["source_plate"].to == ("acme/liquid_handling.Plate",)
    transfer = result.plugin.types["acme/liquid_handling.Transfer"]
    assert transfer.exports == (
        PluginExport(
            "acme/liquid_handling.tecan_worklist",
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
    edit(plugin / "plugin.yaml", "format: tecan_gwl", "format: acme/other.gwl")
    assert "writer-format-dependency" in codes(load_plugin(plugin))
    edit(plugin / "plugin.yaml", "format: acme/other.gwl", "format: tecan_csv")
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
    edit(plugin / "types" / "transfer.yaml", "export: tecan_worklist", "export: hamilton_worklist")
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
    # The build stage runs with no network.
    assert "RUN --network=none" in writer_build.BUILDER_STAGES
    assert writer_build.BUILDER.split("@")[0] in (ROOT / "server" / "Dockerfile").read_text()
