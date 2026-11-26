"""Plugin-declared formats survive compilation and drive every installed lookup."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from iki_spec.builtin import CORE_DIRECTORY, core_plugin, using_core
from iki_spec.formats import AmbiguousSignatureError, FormatRegistry
from iki_spec.model import (
    Action,
    Field,
    FileFormat,
    FileSignature,
    Input,
    Plugin,
    plugin_from_dict,
    plugin_to_dict,
)
from iki_spec.validate import load_plugin

MANIFEST = """apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
formats:
  spectra:
    label: Spectra
    media_types: [application/vnd.acme.spectra, application/x-spectra]
    extensions: [.spectra.gz]
    signatures: [{offset: 3, hex: "AABB"}]
    reader: jcamp-dx
    viewer: alignment
"""

TYPE = """apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: {prefix: ASY}
fields:
  data: {type: file, media_types: [acme/lab.spectra]}
display:
  viewers: [{viewer: alignment, alignment: data}]
"""


def write_plugin(root: Path, manifest: str = MANIFEST, entity: str = TYPE) -> None:
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(manifest)
    (root / "types/assay.yaml").write_text(entity)


def test_declaration_survives_build_and_drives_lookups(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    loaded = load_plugin(tmp_path)
    assert loaded.errors == []
    assert loaded.plugin is not None
    plugin = plugin_from_dict(plugin_to_dict(loaded.plugin))
    assert plugin == loaded.plugin
    registry = FormatRegistry([core_plugin(), plugin])
    fmt = registry.formats["acme/lab.spectra"]
    assert fmt.canonical == "application/vnd.acme.spectra"
    assert (fmt.reader, fmt.viewer) == ("jcamp-dx", "alignment")
    assert registry.by_signature(b"abc\xaa\xbb") == fmt
    assert registry.by_signature(b"abc\xaa") is None
    assert registry.by_extension("run.SPECTRA.GZ") == (fmt,)
    assert registry.by_media_type("Application/X-Spectra; version=1") == fmt
    assert registry.expand([fmt.id, fmt.canonical, "example/*"]) == (*fmt.media_types, "example/*")


def test_compiled_format_preference_survives_json_key_sorting(tmp_path: Path) -> None:
    extra = """
  alpha:
    label: Second variant
    media_types: [application/x-second-variant]
    extensions: [.spectra.gz]
"""
    write_plugin(tmp_path, MANIFEST + extra)
    original = load_plugin(tmp_path).plugin
    assert original is not None
    restored = plugin_from_dict(json.loads(json.dumps(plugin_to_dict(original), sort_keys=True)))
    matches = FormatRegistry([restored]).by_extension("sample.spectra.gz")
    assert [fmt.id for fmt in matches] == ["acme/lab.spectra", "acme/lab.alpha"]


@pytest.mark.parametrize(
    ("before", "after", "claim"),
    [
        ("[.spectra.gz]", "[.csv]", "extension .csv"),
        ("application/x-spectra", "text/csv", "media type text/csv"),
    ],
)
def test_deploy_refuses_conflicts_even_in_an_alias(
    tmp_path: Path, before: str, after: str, claim: str
) -> None:
    write_plugin(tmp_path, MANIFEST.replace(before, after))
    result = load_plugin(tmp_path)
    assert result.plugin is None
    assert [p.code for p in result.errors] == ["format-conflict"]
    message = result.errors[0].message
    assert "acme/lab.spectra" in message and "iki/core.csv" in message and claim in message


@pytest.mark.parametrize("media_type", ["text/" + "x" * 128, "application/octet-stream"])
def test_a_format_claims_a_specific_storable_media_type(tmp_path: Path, media_type: str) -> None:
    write_plugin(tmp_path, MANIFEST.replace("application/x-spectra", media_type))
    assert load_plugin(tmp_path).errors


def test_references_must_name_an_installed_format_and_its_dependency(tmp_path: Path) -> None:
    write_plugin(tmp_path / "lab")
    other = tmp_path / "test"
    consuming = TYPE.replace("name: Assay", "name: Test").replace("prefix: ASY", "prefix: TST")
    wrong = consuming.partition("display:")[0].replace("acme/lab.spectra", "acme/lab.mistake")
    manifest = MANIFEST.partition("formats:")[0].replace("acme/lab", "acme/test")
    write_plugin(other, manifest, wrong)
    error = load_plugin(other, dependencies=[tmp_path / "lab"]).errors[0]
    assert error.code == "unknown-format" and error.path == ("fields", "data", "media_types", 0)
    (other / "types/assay.yaml").write_text(consuming)
    errors = load_plugin(other, dependencies=[tmp_path / "lab"]).errors
    assert any(p.code == "undeclared-dependency" and "acme/lab" in p.message for p in errors)
    (other / "plugin.yaml").write_text(manifest + "dependencies: {acme/lab: {}}\n")
    result = load_plugin(other, dependencies=[tmp_path / "lab"])
    assert result.errors == []


@pytest.mark.parametrize(
    "declaration",
    [
        "    viewer: launch-a-command",
        "    reader: some.module:method",
        '    signatures: [{hex: "whitespace 01"}]',
        '    signatures: [{hex: "abcd", offset: 4095}]',
    ],
)
def test_unsupported_or_unbounded_recognizers_are_rejected(
    tmp_path: Path, declaration: str
) -> None:
    write_plugin(tmp_path, MANIFEST.partition("    signatures:")[0] + declaration + "\n")
    assert not load_plugin(tmp_path).ok


def test_signatures_are_specific_and_ambiguous_bytes_have_no_winner() -> None:
    def declared(name: str, hex: str, offset: int = 0) -> FileFormat:
        return FileFormat(
            f"acme/lab.{name}", name, (f"application/x-{name}",), (), (FileSignature(hex, offset),)
        )

    short = declared("short", "4141")
    long = declared("long", "414142")
    tail = declared("tail", "4243", 2)
    plugin = Plugin("acme/lab", "1.0.0", formats={f.id: f for f in (short, long, tail)})
    registry = FormatRegistry([plugin])
    assert registry.signature_bytes == 4
    assert registry.by_signature(b"AABC") == long
    assert registry.by_signature(b"AA") == short
    assert registry.by_signature(b"") is None
    conflicted = FormatRegistry([replace(plugin, formats={f.id: f for f in (short, tail)})])
    with pytest.raises(AmbiguousSignatureError, match=r"acme/lab\.short.*acme/lab\.tail"):
        conflicted.by_signature(b"AABC")
    duplicate = declared("duplicate", "4141")
    with pytest.raises(ValueError, match="both claim signature"):
        FormatRegistry([replace(plugin, formats={short.id: short, duplicate.id: duplicate})])


def test_parsers_are_derived_from_file_params_and_input_records(tmp_path: Path) -> None:
    write_plugin(tmp_path)
    plugin = load_plugin(tmp_path).plugin
    assert plugin is not None
    record = Action(
        plugin.name,
        "read_assay",
        "backend",
        "tools",
        ("read",),
        inputs={"assay": Input("assay", "acme/lab.Assay")},
    )
    many = Field(
        "files", "list", items=Field("item", "file", media_types=("application/x-spectra",))
    )
    param = replace(record, name="import_many", inputs={}, params={"files": many})
    unrelated = replace(param, name="import_any", params={"file": Field("file", "file")})
    with_actions = replace(
        plugin, actions={a.qualified_name: a for a in (record, param, unrelated)}
    )
    registry = FormatRegistry([with_actions])
    assert registry.parsers("acme/lab.spectra") == ("acme/lab.import_many", "acme/lab.read_assay")


def test_upgrade_probe_can_check_original_core_viewers_without_format_declarations(
    tmp_path: Path,
) -> None:
    (tmp_path / "plugin.yaml").write_text(
        (CORE_DIRECTORY / "plugin.yaml").read_text().partition("formats:")[0]
    )
    (tmp_path / "types").mkdir()
    for manifest in (CORE_DIRECTORY / "types").glob("*.yaml"):
        (tmp_path / "types" / manifest.name).write_bytes(manifest.read_bytes())
    with using_core(tmp_path):
        original = core_plugin()
        assert original.formats == {}
        assert plugin_from_dict(plugin_to_dict(original)) == original
        assert original.types["iki/core.Alignment"].viewers[0].viewer == "alignment"
