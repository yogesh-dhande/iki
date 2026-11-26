"""Exports: the files Iki writes of a type's records, configured by naming its fields."""

import json
import shutil
import textwrap
from pathlib import Path

from iki_spec.model import (
    CompoundExport,
    Export,
    ProteinExport,
    ReportExport,
    plugin_from_dict,
    plugin_to_dict,
)
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""

SEQUENCE = """\
apiVersion: iki/v1
kind: EntityType
name: Construct
schema_version: 1
hrid: {{ prefix: CON }}
fields:
  bases: {{ type: string, format: dna_sequence, topology: topology, strands: strandedness }}
  topology: {{ type: enum, values: [linear, circular], default: linear }}
  strandedness: {{ type: enum, values: [double, single], default: double }}
  residues: {{ type: string, format: protein_sequence }}
  name: {{ type: string }}
  notes: {{ type: text }}
  accession: {{ type: string }}
  features: {{ type: json }}
  organism: {{ type: ref, to: iki/core.Organism }}
  other: {{ type: ref, to: iki/core.Sequence }}
  count: {{ type: int }}
exports: {exports}
"""

RECORD = """\
apiVersion: iki/v1
kind: EntityType
name: Vector
schema_version: 1
hrid: {{ prefix: VEC }}
fields:
  construct: {{ type: ref, to: Construct }}
  name: {{ type: string }}
  annotation: {{ type: json }}
exports: {exports}
"""


def made(tmp_path: Path, exports: str, vector: str | None = None) -> Result:
    root = tmp_path / "acme"
    (root / "types").mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(PLUGIN)
    (root / "types" / "construct.yaml").write_text(
        textwrap.dedent(SEQUENCE.format(exports=exports))
    )
    if vector is not None:
        (root / "types" / "vector.yaml").write_text(textwrap.dedent(RECORD.format(exports=vector)))
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def test_a_sequence_export_names_its_sequence_and_what_goes_with_it(tmp_path: Path) -> None:
    result = made(
        tmp_path,
        "[{ export: sequence, sequence: bases, features: features, description: notes, "
        "accession: accession, organism: organism }]",
    )
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    assert plugin.types["acme/lab.Construct"].exports == (
        Export(
            export="sequence",
            sequence="bases",
            features="features",
            description="notes",
            accession="accession",
            organism="organism",
        ),
    )
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_its_fields_must_exist_and_be_what_it_writes(tmp_path: Path) -> None:
    missing = made(tmp_path, "[{ export: sequence, sequence: basez }]")
    assert codes(missing) == ["unknown-field"]
    assert missing.problems[0].message == "This type has no field `basez`."
    protein = made(tmp_path, "[{ export: sequence, sequence: residues }]")
    assert codes(protein) == ["export-field"]
    assert protein.problems[0].message == (
        "`residues` isn't a DNA or RNA sequence, or a reference to a type that exports one, "
        "which a sequence export's `sequence` writes."
    )
    for role, wrong in (
        ("features", "name"),
        ("description", "count"),
        ("accession", "features"),
        ("organism", "other"),
    ):
        result = made(tmp_path, f"[{{ export: sequence, sequence: bases, {role}: {wrong} }}]")
        assert codes(result) == ["export-field"], role


def test_a_protein_export_names_its_residues_and_what_goes_with_them(tmp_path: Path) -> None:
    result = made(
        tmp_path,
        "[{ export: protein, sequence: residues, description: notes, organism: organism }]",
    )
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    assert plugin.types["acme/lab.Construct"].exports == (
        ProteinExport("protein", "residues", "notes", "organism"),
    )
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_a_protein_export_writes_a_protein_field(tmp_path: Path) -> None:
    dna = made(tmp_path, "[{ export: protein, sequence: bases }]")
    assert codes(dna) == ["export-field"]
    assert dna.problems[0].message == (
        "`bases` isn't a protein sequence (format protein_sequence), which a protein "
        "export's `sequence` writes."
    )
    for role, wrong in (("description", "count"), ("organism", "other")):
        result = made(tmp_path, f"[{{ export: protein, sequence: residues, {role}: {wrong} }}]")
        assert codes(result) == ["export-field"], role
    # A protein export names no features: FASTA holds none, and GenPept isn't written.
    assert codes(made(tmp_path, "[{ export: protein, sequence: residues, features: x }]")) == [
        "schema"
    ]


def test_a_compound_export_names_the_text_field_its_molfile_is_in(tmp_path: Path) -> None:
    result = made(tmp_path, "[{ export: compound, structure: notes }]")
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    assert plugin.types["acme/lab.Construct"].exports == (CompoundExport("compound", "notes"),)
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin
    wrong = made(tmp_path, "[{ export: compound, structure: name }]")
    assert codes(wrong) == ["export-field"]
    assert wrong.problems[0].message == (
        "`name` isn't a text field holding a Molfile, which a compound export's `structure` writes."
    )
    assert codes(made(tmp_path, "[{ export: compound }]")) == ["schema"]


def test_a_type_exports_a_sequence_and_its_protein_side_by_side(tmp_path: Path) -> None:
    """Their formats have names of their own (`fasta`, `protein-fasta`), so a construct with
    bases and residues offers both."""
    result = made(
        tmp_path,
        "[{ export: sequence, sequence: bases }, { export: protein, sequence: residues }]",
    )
    assert result.problems == []


def test_a_reference_exports_the_sequence_it_names(tmp_path: Path) -> None:
    """A plasmid's file is its sequence's, with the plasmid's own name and annotation."""
    result = made(
        tmp_path,
        "[{ export: sequence, sequence: bases, features: features }]",
        "[{ export: sequence, sequence: construct, features: annotation }]",
    )
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    (export,) = plugin.types["acme/lab.Vector"].exports
    assert isinstance(export, Export)
    assert export.sequence == "construct"


def test_a_reference_to_a_type_that_exports_nothing_isnt_a_sequence(tmp_path: Path) -> None:
    result = made(
        tmp_path, "[{ export: sequence, sequence: other }]", "[{ export: sequence, sequence: x }]"
    )
    assert codes(result) == ["export-field", "unknown-field"]


def test_each_export_is_given_once(tmp_path: Path) -> None:
    twice = "[{ export: sequence, sequence: bases }, { export: sequence, sequence: bases }]"
    assert codes(made(tmp_path, twice)) == ["export-twice"]


def test_the_schema_knows_each_exports_fields(tmp_path: Path) -> None:
    assert codes(made(tmp_path, "[{ export: sequence }]")) == ["schema"]
    assert codes(made(tmp_path, "[{ export: sequence, sequence: bases, file: x }]")) == ["schema"]
    # Not one of Iki's, so a plugin's writer (ADR 0230): which maps roles to `fields`.
    assert set(codes(made(tmp_path, "[{ export: chart, sequence: bases }]"))) == {"schema"}
    assert set(codes(made(tmp_path, "[{ export: Chart!, fields: {} }]"))) == {"schema"}


def test_a_signed_report_names_a_json_field_and_a_locking_signed_state(tmp_path: Path) -> None:
    root = tmp_path / "signed_report"
    shutil.copytree(Path(__file__).parent / "data" / "signed_report", root)
    report = root / "types" / "signed_report.yaml"
    manifest = report.read_text(encoding="utf-8")
    loaded = load_plugin(root)
    assert loaded.problems == []
    plugin = loaded.plugin
    assert plugin is not None
    assert plugin.types["acme/signed_reports.SignedReport"].exports == (
        ReportExport("signed_report", "report", "review", ("issued",)),
    )
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(plugin)))) == plugin
    for old, bad, code in (
        ("type: json", "type: int", "export-field"),
        ("review: review", "review: report", "export-field"),
        (
            "locks: true\n        signatures: [{ meaning: approval }]",
            "locks: false\n        signatures: [{ meaning: approval }]",
            "export-state",
        ),
        ("issued: [issued]", "issued: [recorded]", "export-state"),
    ):
        report.write_text(manifest.replace(old, bad), encoding="utf-8")
        assert codes(load_plugin(root)) == [code]
