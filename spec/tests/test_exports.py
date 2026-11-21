"""Exports: the files Iki writes of a type's records, configured by naming its fields."""

import textwrap
from pathlib import Path

from iki_spec.model import Export, plugin_from_dict, plugin_to_dict
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
    assert plugin.types["acme/lab.Vector"].exports[0].sequence == "construct"


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
    assert codes(made(tmp_path, "[{ export: chart, sequence: bases }]")) == ["schema"]
