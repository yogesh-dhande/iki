"""Viewers: Iki's own viewers on a type's pages, configured by naming its fields."""

import textwrap
from pathlib import Path

from iki_spec.model import Track, Viewer, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme
version: 1.0.0
"""

TYPE = """\
apiVersion: iki/v1
kind: EntityType
name: Plasmid
schema_version: 1
hrid: {{ prefix: PLS }}
fields:
  bases: {{ type: string, format: dna_sequence }}
  name: {{ type: string }}
  features: {{ type: json }}
  model: {{ type: file, media_types: [chemical/x-mmcif, chemical/x-pdb] }}
  map: {{ type: file, media_types: [chemical/x-mmcif, application/octet-stream] }}
  anything: {{ type: file }}
  hydropathy: {{ type: json, schema: {{ type: array, items: {{ type: [number, "null"] }} }} }}
  charge: {{ type: json, schema: {{ type: array, items: {{ type: integer }} }} }}
  labels: {{ type: json, schema: {{ type: array, items: {{ type: string }} }} }}
display:
  viewers: {viewers}
"""


def made(tmp_path: Path, viewers: str) -> Result:
    root = tmp_path / "acme"
    (root / "types").mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(PLUGIN)
    (root / "types" / "plasmid.yaml").write_text(textwrap.dedent(TYPE.format(viewers=viewers)))
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def test_a_sequence_viewer_names_its_sequence_and_features(tmp_path: Path) -> None:
    result = made(tmp_path, "[{ viewer: sequence, sequence: bases, features: features }]")
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    plasmid = plugin.types["acme.Plasmid"]
    assert plasmid.viewers == (
        Viewer(viewer="sequence", label="Sequence", sequence="bases", features="features"),
    )
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_a_viewer_can_name_its_tab(tmp_path: Path) -> None:
    plugin = made(tmp_path, "[{ viewer: sequence, sequence: bases, label: Map }]").plugin
    assert plugin is not None
    assert plugin.types["acme.Plasmid"].viewers[0].label == "Map"


def test_its_fields_must_exist_and_be_what_it_shows(tmp_path: Path) -> None:
    missing = made(tmp_path, "[{ viewer: sequence, sequence: basez }]")
    assert codes(missing) == ["unknown-field"]
    assert missing.problems[0].message == "This type has no field `basez`."
    plain = made(tmp_path, "[{ viewer: sequence, sequence: name }]")
    assert codes(plain) == ["viewer-field"]
    assert plain.problems[0].message == (
        "`name` isn't a DNA or protein sequence, which a sequence viewer's `sequence` shows."
    )
    features = made(tmp_path, "[{ viewer: sequence, sequence: bases, features: name }]")
    assert codes(features) == ["viewer-field"]


def test_each_viewer_is_given_once(tmp_path: Path) -> None:
    twice = "[{ viewer: sequence, sequence: bases }, { viewer: sequence, sequence: bases }]"
    assert codes(made(tmp_path, twice)) == ["viewer-twice"]


def test_the_schema_knows_each_viewers_fields(tmp_path: Path) -> None:
    assert codes(made(tmp_path, "[{ viewer: sequence }]")) == ["schema"]
    assert codes(made(tmp_path, "[{ viewer: sequence, sequence: bases, file: x }]")) == ["schema"]
    assert codes(made(tmp_path, "[{ viewer: chart }]")) == ["schema"]


def test_a_structure_viewer_shows_a_file_of_structures(tmp_path: Path) -> None:
    both = "[{ viewer: sequence, sequence: bases }, { viewer: structure, structure: model }]"
    result = made(tmp_path, both)
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    assert plugin.types["acme.Plasmid"].viewers[1] == Viewer(
        viewer="structure", label="Structure", structure="model"
    )
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_a_structure_viewers_file_holds_only_what_it_reads(tmp_path: Path) -> None:
    """Mol* reads mmCIF and PDB: a field that may hold anything else isn't its to show."""
    for field in ("map", "anything", "name"):
        result = made(tmp_path, f"[{{ viewer: structure, structure: {field} }}]")
        assert codes(result) == ["viewer-field"], field
    assert made(tmp_path, "[{ viewer: structure, structure: map }]").problems[0].message == (
        "`map` isn't a file field of mmCIF or PDB files (media types chemical/x-mmcif or "
        "chemical/x-pdb), which a structure viewer's `structure` shows."
    )
    assert codes(made(tmp_path, "[{ viewer: structure }]")) == ["schema"]
    assert codes(made(tmp_path, "[{ viewer: structure, structure: model, sequence: bases }]")) == [
        "schema"
    ]


def test_a_sequence_viewer_shows_tracks_of_values_along_its_residues(tmp_path: Path) -> None:
    result = made(
        tmp_path,
        "[{ viewer: sequence, sequence: bases,"
        " tracks: [{ field: hydropathy }, { field: charge, label: Net charge }] }]",
    )
    assert result.problems == []
    plugin = result.plugin
    assert plugin is not None
    assert plugin.types["acme.Plasmid"].viewers[0].tracks == (
        Track(field="hydropathy"),
        Track(field="charge", label="Net charge"),
    )
    assert plugin_from_dict(plugin_to_dict(plugin)) == plugin


def test_a_track_is_a_json_field_of_one_number_per_residue(tmp_path: Path) -> None:
    def track(name: str) -> Result:
        return made(
            tmp_path, f"[{{ viewer: sequence, sequence: bases, tracks: [{{ field: {name} }}] }}]"
        )

    assert codes(track("hydropathz")) == ["unknown-field"]
    for name in ("features", "labels", "name"):
        result = track(name)
        assert codes(result) == ["viewer-field"], name
        assert result.problems[0].message == (
            f"`{name}` isn't a json field of one number per residue, which a track shows."
        )
    twice = (
        "[{ viewer: sequence, sequence: bases, tracks: [{ field: charge }, { field: charge }] }]"
    )
    assert codes(made(tmp_path, twice)) == ["viewer-field"]
    structure = "[{ viewer: structure, structure: model, tracks: [{ field: charge }] }]"
    assert codes(made(tmp_path, structure)) == ["schema"]
