import json
import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec import naming, schemas
from iki_spec.model import (
    FORMATS,
    SIGNATURE_MEANINGS,
    Field,
    matches_format,
    parse_size,
    plugin_from_dict,
    plugin_to_dict,
)
from iki_spec.problems import Location, Severity
from iki_spec.validate import (
    Result,
    check_settings_text,
    check_settings_values,
    load_plugin,
    looks_secret,
    pattern_problems,
    satisfies,
)
from iki_spec.yamlsource import read_documents

PLUGINS = Path(__file__).parent / "plugins"
DNA, PROTEIN, PREDICTION = PLUGINS / "dna", PLUGINS / "protein", PLUGINS / "structure_prediction"

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""

TYPE = """\
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: { prefix: ASY }
"""


def make(
    tmp_path: Path,
    types: str | dict[str, str] = "",
    plugin: str = PLUGIN,
    *,
    with_: tuple[Path, ...] = (),
) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root / "types", ignore_errors=True)
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(textwrap.dedent(plugin))
    files = {"types/assay.yaml": types} if isinstance(types, str) else types
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(textwrap.dedent(text))
    return load_plugin(root, dependencies=with_)


def fields(text: str) -> str:
    return TYPE + "fields:\n" + textwrap.indent(textwrap.dedent(text), "  ")


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


# --- The first-party style examples ------------------------------------------------------------


def test_example_plugins_are_valid() -> None:
    dna = load_plugin(DNA)
    assert dna.ok
    assert dna.problems == []
    assert dna.plugin is not None
    assert set(dna.plugin.types) == {
        "acme/dna.Sequence",
        "acme/dna.DNASequence",
        "acme/dna.Plasmid",
        "acme/dna.Vector",
    }

    protein = load_plugin(PROTEIN, dependencies=[DNA])
    assert protein.problems == []
    prediction = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA])
    assert prediction.problems == []


def test_builds_the_model_codegen_reads() -> None:
    result = load_plugin(PROTEIN, dependencies=[DNA])
    assert result.plugin is not None
    protein = result.plugin.types["acme/protein.Protein"]
    assert protein.qualified_name == "acme/protein.Protein"
    assert protein.table == "acme__protein_protein"
    assert protein.extends == "acme/dna.Sequence"
    assert protein.hrid_prefix == "PRT"
    assert protein.identity == ("sequence",)
    assert protein.label == "Protein"
    assert protein.label_plural == "Proteins"
    # Without a plural of its own, a type's is its label's, not its name's.
    dna = load_plugin(DNA).plugin
    assert dna is not None
    assert dna.types["acme/dna.DNASequence"].label_plural == "DNA sequences"
    weight = protein.fields["molecular_weight"]
    assert weight.type == "quantity"
    assert weight.unit == "u"
    assert weight.label == "Molecular weight"
    assert protein.fields["encoded_by"].to == ("acme/dna.DNASequence",)
    assert result.plugin.dependencies == {
        "acme/dna": {"Sequence": ">=1", "DNASequence": ">=2,<4", "Plasmid": ">=1"}
    }
    structure = result.plugin.types["acme/protein.ProteinStructure"]
    assert structure.fields["structure"].max_size == 2 * 1024**3
    chains = structure.fields["chains"]
    assert chains.items is not None
    assert chains.items.to == ("acme/protein.Protein",)
    assert result.plugin.types["acme/protein.ProteinStructure"].label == "Protein Structure"

    prediction = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA]).plugin
    assert prediction is not None
    extension, of_protein = sorted(prediction.extensions, key=lambda e: e.table, reverse=True)
    assert extension.extends_type == "acme/protein.ProteinStructure"
    assert of_protein.fields["disorder"].registry == "derived"
    assert extension.table == "acme__prediction__acme__protein_proteinstructure"
    assert prediction.settings["allowed_presets"].default == ["monomer"]


# --- Files and headers ---------------------------------------------------------------------------


def test_needs_a_plugin_manifest(tmp_path: Path) -> None:
    result = load_plugin(tmp_path)
    assert only(result, "missing-plugin-manifest") == (
        "There's no plugin.yaml here, so this isn't a plugin."
    )
    assert result.plugin is None


def test_a_directory_that_isnt_there(tmp_path: Path) -> None:
    assert codes(load_plugin(tmp_path / "nope")) == ["not-a-directory"]


def test_plugin_yaml_holds_only_the_plugin(tmp_path: Path) -> None:
    result = make(tmp_path, plugin=PLUGIN + "---\n" + TYPE)
    assert only(result, "wrong-file").startswith("plugin.yaml holds only the Plugin manifest")
    result = make(tmp_path, plugin=PLUGIN + "---\n" + PLUGIN)
    assert codes(result) == ["duplicate-plugin-manifest"]
    result = make(tmp_path, plugin=TYPE)
    assert codes(result) == ["missing-plugin-manifest", "wrong-file"]


def test_types_hold_only_types(tmp_path: Path) -> None:
    result = make(tmp_path, PLUGIN)
    assert only(result, "wrong-file").startswith("Files under types/ hold entity types")


@pytest.mark.parametrize(
    ("text", "code", "message"),
    [
        ("kind: EntityType\nname: A\n", "missing-api-version", "`apiVersion` is missing."),
        ("apiVersion: iki/v2\nkind: EntityType\n", "unsupported-api-version", "not `iki/v2`"),
        ("apiVersion: iki/v1\nname: A\n", "missing-kind", "`kind` is missing."),
        ("apiVersion: iki/v1\nkind: EntityTyp\n", "unknown-kind", "`EntityTyp` isn't a kind"),
        ("- a\n- b\n", "not-a-mapping", "must be a mapping"),
        ("a: [\n", "yaml-syntax", "isn't valid YAML"),
    ],
)
def test_explains_bad_headers(tmp_path: Path, text: str, code: str, message: str) -> None:
    assert message in only(make(tmp_path, text), code)


def test_unknown_kind_suggests_the_closest(tmp_path: Path) -> None:
    result = make(tmp_path, "apiVersion: iki/v1\nkind: EntityTyp\n")
    assert result.problems[0].hint == "Did you mean `EntityType`?"


def test_reads_every_yaml_file_and_document_under_types(tmp_path: Path) -> None:
    other = TYPE.replace("Assay", "Sample").replace("ASY", "SMP")
    third = TYPE.replace("Assay", "Plate").replace("ASY", "PLT")
    result = make(
        tmp_path,
        {
            "types/a.yaml": TYPE + "---\n" + other + "---\n",
            "types/nested/b.yml": third,
            "types/.hidden/c.yaml": "not: [valid",
            "types/notes.txt": "ignored",
        },
    )
    assert result.problems == []
    assert result.plugin is not None
    assert sorted(result.plugin.types) == ["acme/lab.Assay", "acme/lab.Plate", "acme/lab.Sample"]


def test_refuses_symlinked_manifests(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(TYPE)
    root = tmp_path / "acme"
    make(tmp_path, {})
    (root / "types" / "linked.yaml").symlink_to(outside)
    assert codes(load_plugin(root)) == ["symlink"]


def test_points_at_the_line_and_column(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("""\
            weight: { type: quantity, unit: mgg }
        """),
    )
    (problem,) = result.problems
    assert problem.code == "unknown-unit"
    assert problem.file == "types/assay.yaml"
    assert problem.location == Location(7, 35)
    assert problem.path == ("fields", "weight", "unit")
    assert problem.hint == "Did you mean `mg`?"


# --- Schema problems are reported plainly --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message", "hint"),
    [
        ("x: { type: strng }", "`fields.x.type` can't be `strng`.", "Did you mean `string`?"),
        (
            "x: { type: string, requried: true }",
            "`requried` isn't an option for string fields.",
            "Did you mean `required`?",
        ),
        ("x: { type: string, unit: mg }", "`unit` isn't an option for string fields.", None),
        (
            "x: { type: string, searchable: false }",
            "`searchable` isn't an option for string fields.",
            None,
        ),
        (
            "x: { type: list, items: { type: text, searchable: false } }",
            "`searchable` isn't allowed here in `fields.x.items`.",
            None,
        ),
        ("x: { type: quantity }", "`unit` is missing in `fields.x`.", "such as `unit: mg`"),
        ("x: { type: enum }", "`values` is missing in `fields.x`.", None),
        ("x: { type: ref }", "`to` is missing in `fields.x`.", None),
        (
            "x: { type: string, required: yes }",
            "`fields.x.required` must be `true` or `false`, not `yes`.",
            "YAML 1.2 reads `yes`",
        ),
        ("Weight: { type: int }", "`Weight` isn't a valid name.", None),
        ("x: { type: int, default: 1.5 }", "`fields.x.default` must be a whole number", None),
        (
            "x: { type: list, items: { type: list, items: { type: int } } }",
            "List items can't be `list`.",
            "json field",
        ),
        (
            "x: { type: list, items: { type: int, required: true } }",
            "`required` isn't allowed here in `fields.x.items`.",
            None,
        ),
        ("x: { type: file, max_size: 2 GB }", "`fields.x.max_size` can't be `2 GB`.", None),
        ("x: { type: enum, values: [a, a] }", "lists something twice", None),
        (
            "x: { type: enum, values: low }",
            "`fields.x.values` must be a list, not `low`.",
            "Write a list, such as `[low]`.",
        ),
        ("x: { type: string, label: 5 }", "`fields.x.label` must be text, not `5`.", "Quote it"),
        ("x: { type: file, media_types: [json] }", "Media types look like", None),
        ("x: { type: string, max_length: 0 }", "`fields.x.max_length`: must be at least 1.", None),
        ("x: { type: string, description: " + "a" * 2001 + " }", "at most 2000 characters", None),
    ],
)
def test_schema_problems_read_plainly(
    tmp_path: Path, text: str, message: str, hint: str | None
) -> None:
    result = make(tmp_path, fields(text + "\n"))
    assert all(p.code == "schema" for p in result.problems), result.problems
    assert any(message in p.message for p in result.problems), [p.message for p in result.problems]
    if hint:
        assert any(p.hint and hint in p.hint for p in result.problems)


def test_unknown_top_level_keys_suggest_the_right_one(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + "hird: { prefix: X }\n")
    (problem,) = result.problems
    assert problem.message == "`hird` isn't allowed here."
    assert problem.hint == "Did you mean `hrid`?"
    assert problem.location == Location(6, 1)


def test_bad_plugin_manifests(tmp_path: Path) -> None:
    result = make(tmp_path, plugin="apiVersion: iki/v1\nkind: Plugin\nname: Acme\nversion: '1.0'\n")
    messages = [p.message for p in result.problems]
    assert any(m.startswith("`name` can't be `Acme`.") for m in messages)
    assert any("Versions are semantic versions" in m for m in messages)
    result = make(
        tmp_path,
        plugin=PLUGIN + "dependencies:\n  acme/dna:\n    types:\n      DNASequence: '2'\n",
    )
    assert any("Constraints compare whole schema versions" in p.message for p in result.problems)


def test_project_starters_pin_a_recipe_version_in_the_built_manifest(tmp_path: Path) -> None:
    # An Iki plugin's: a lab plugin's starters are warned of (below).
    iki = PLUGIN.replace("acme/lab", "iki/oligo")
    result = make(tmp_path, TYPE, plugin=iki + 'starters: {dose-response: "1.0.0"}\n')
    assert result.plugin is not None and not result.problems
    assert plugin_from_dict(plugin_to_dict(result.plugin)).starters == {"dose-response": "1.0.0"}
    bad = make(tmp_path, TYPE, plugin=iki + 'starters: {dose-response: "latest"}\n')
    assert codes(bad) == ["schema"]


# --- Rules across the plugin ---------------------------------------------------------------------


def test_plugins_are_named_in_a_namespace(tmp_path: Path) -> None:
    result = make(tmp_path, plugin=PLUGIN.replace("acme/lab", "lab"))
    assert only(result, "unqualified-name") == (
        "`lab` needs a namespace: plugins are named `namespace/name`."
    )
    assert result.problems[0].hint == (
        "Name it under your lab's or company's namespace, such as `acme/lab`. Iki's own "
        "plugins are in `iki`."
    )
    result = make(tmp_path, plugin=PLUGIN.replace("acme/lab", "acme/lab/x"))
    assert codes(result) == ["schema"]
    result = make(tmp_path, plugin=PLUGIN.replace("acme/lab", "Acme/lab"))
    assert codes(result) == ["schema"]


def test_the_iki_namespace_is_reserved(tmp_path: Path) -> None:
    result = make(tmp_path, plugin=PLUGIN.replace("acme/lab", "iki/lab"))
    assert "`iki` is Iki's own" in only(result, "reserved-namespace")
    assert result.problems[0].hint == (
        "Name it under your lab's or company's namespace, such as `acme/lab`."
    )
    # Not even with a first-party plugin's name, outside Iki's repository's build.
    assert codes(make(tmp_path, plugin=PLUGIN.replace("acme/lab", "iki/core"))) == [
        "reserved-namespace"
    ]
    # Nor a namespace that would pass for Iki's.
    assert codes(make(tmp_path, plugin=PLUGIN.replace("acme/lab", "iki_labs/lab"))) == [
        "reserved-namespace"
    ]
    # Iki's own plugins use it.
    assert codes(make(tmp_path, plugin=PLUGIN.replace("acme/lab", "iki/dna"))) == []


def test_any_name_is_free_in_a_namespace(tmp_path: Path) -> None:
    # A plugin's app is `namespace__name`, which no app of Iki's or Django's is.
    for name in ("acme/core", "acme/auth", "acme/iki_tools", "acme/class", "django/admin"):
        assert codes(make(tmp_path, plugin=PLUGIN.replace("acme/lab", name))) == [], name


def test_duplicate_types(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + "---\n" + TYPE.replace("ASY", "ASZ"))
    assert (
        only(result, "duplicate-type")
        == "`Assay` is defined twice (first in types/assay.yaml, line 1)."
    )


def test_types_whose_tables_would_collide(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        TYPE.replace("Assay", "AssayRun")
        + "---\n"
        + TYPE.replace("Assay", "Assayrun").replace("ASY", "ASR"),
    )
    assert "share the table `acme__lab_assayrun`" in only(result, "duplicate-table")


def test_names_that_are_too_long_for_postgres(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        TYPE.replace("Assay", "A" + "b" * 49),
        plugin=PLUGIN.replace("acme", "a" * 30),
    )
    assert "PostgreSQL allows 63" in only(result, "name-too-long")


def test_concrete_types_need_hrids_and_abstract_ones_cant_have_them(tmp_path: Path) -> None:
    result = make(
        tmp_path, TYPE.replace("hrid: { prefix: ASY }\n", "").replace("Assay", "CellLine")
    )
    assert (
        only(result, "missing-hrid")
        == "`CellLine` needs an HRID prefix for its human-readable IDs."
    )
    assert result.problems[0].hint is not None
    assert "prefix: CL" in result.problems[0].hint
    result = make(tmp_path, TYPE + "abstract: true\n")
    assert codes(result) == ["hrid-on-abstract"]


def test_hrid_prefixes_are_unique(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + "---\n" + TYPE.replace("Assay", "Other"))
    assert only(result, "duplicate-hrid-prefix") == (
        "`acme/lab.Assay` and `acme/lab.Other` both use the HRID prefix `ASY`."
    )
    dna_clash = TYPE.replace("ASY", "DNA")
    result = make(
        tmp_path,
        dna_clash,
        plugin=PLUGIN + "dependencies:\n  acme/dna: {}\n",
        with_=(DNA,),
    )
    assert only(result, "duplicate-hrid-prefix") == (
        "`acme/dna.DNASequence` and `acme/lab.Assay` both use the HRID prefix `DNA`."
    )


def test_reserved_field_names(tmp_path: Path) -> None:
    result = make(tmp_path, fields("type: { type: string }\nclass: { type: string }\n"))
    assert codes(result) == ["reserved-name", "reserved-name"]
    assert "`sample_type` instead of `type`" in (result.problems[0].hint or "")
    # The names a generated models.py uses in its classes' bodies.
    result = make(tmp_path, fields("models: { type: string }\ndatetime: { type: string }\n"))
    assert codes(result) == ["reserved-name", "reserved-name"]
    # What a search names on every entity, such as what a rule holds (ADR 0135).
    result = make(tmp_path, fields("held: { type: bool }\nassigned: { type: string }\n"))
    assert codes(result) == ["reserved-name", "reserved-name"]


def test_ref_columns_dont_collide(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("parent: { type: ref, to: Assay }\nparent_id: { type: string }\n"),
    )
    assert "column named `parent_id`" in only(result, "column-conflict")


def test_a_reference_can_name_several_types(tmp_path: Path) -> None:
    """ADR 0136: one of several types, each with its subtypes."""
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    result = make(tmp_path, fields("part: { type: ref, to: [Assay, Batch] }\n") + "---\n" + batch)
    assert codes(result) == []
    assert result.plugin is not None
    part = result.plugin.types["acme/lab.Assay"].fields["part"]
    assert part.to == ("acme/lab.Assay", "acme/lab.Batch")
    one = make(tmp_path, fields("part: { type: ref, to: Assay }\n"))
    assert one.plugin is not None
    assert one.plugin.types["acme/lab.Assay"].fields["part"].to == ("acme/lab.Assay",)


ROWS = """\
parts:
  type: list
  items:
    type: row
    fields:
      part: { type: ref, to: [Assay, Batch], required: true }
      copies: { type: int, min: 1, default: 1 }
      role: { type: enum, values: [heavy, light], required: true }
    by: role
    roles:
      heavy: { min: 2, max: 2, to: { part: Assay } }
      light: { label: Light chain, max: 2 }
"""


def test_a_list_can_have_rows_of_typed_fields(tmp_path: Path) -> None:
    """ADR 0137: each row's fields, and per role its count and what it refers to."""
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    result = make(tmp_path, fields(ROWS) + "---\n" + batch)
    assert codes(result) == []
    assert result.plugin is not None
    parts = result.plugin.types["acme/lab.Assay"].fields["parts"]
    assert parts.is_rows
    assert parts.items is not None
    row = parts.items
    assert list(row.fields) == ["part", "copies", "role"]
    assert row.fields["part"].to == ("acme/lab.Assay", "acme/lab.Batch")
    assert row.by == "role"
    assert row.roles["heavy"].min == 2
    assert row.roles["heavy"].to == {"part": ("acme/lab.Assay",)}
    assert row.roles["light"].label == "Light chain"
    # It survives the manifest.
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin


def test_rows_roles_name_the_rows_fields(tmp_path: Path) -> None:
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")

    def problems(change: tuple[str, str]) -> list[str]:
        return codes(make(tmp_path, fields(ROWS.replace(*change)) + "---\n" + batch))

    assert problems(("by: role", "by: copies")) == ["row-role-not-enum"]
    assert problems(("by: role", "by: colour")) == ["row-role-not-enum"]
    assert problems(("values: [heavy, light], required: true", "values: [heavy, light]")) == [
        "row-role-optional"
    ]
    assert problems(("light: {", "medium: {")) == ["unknown-row-role"]
    assert problems(("min: 2, max: 2", "min: 3, max: 2")) == ["min-above-max"]
    assert problems(("to: { part: Assay }", "to: { copies: Assay }")) == ["row-role-not-reference"]
    assert problems(("to: { part: Assay }", "to: { part: Thing }")) == ["unknown-type"]
    assert problems(("part: { type: ref, to: [Assay, Batch]", "part: { type: ref, to: Batch")) == [
        "row-role-type-not-allowed"
    ]
    assert problems(("copies:", "position:")) == ["reserved-name"]
    assert problems(("copies: { type: int, min: 1, default: 1 }", "copies: { type: list }")) == [
        "schema"
    ]


READINGS = """\
readings:
  type: list
  items:
    type: row
    row_id: reading_id
    fields:
      read_at: { type: datetime, required: true }
      value: { type: float, required: true }
      reading_id: { type: string, max_length: 36 }
"""


def test_a_rows_id_is_an_optional_string_iki_fills_in(tmp_path: Path) -> None:
    """A list's rows can each have a stable ID, as an experiment's observations do: the
    server gives a new row one and keeps it through edits."""
    result = make(tmp_path, fields(READINGS))
    assert codes(result) == []
    assert result.plugin is not None
    row = result.plugin.types["acme/lab.Assay"].fields["readings"].items
    assert row is not None
    assert row.row_id == "reading_id"
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin

    def problem(change: tuple[str, str]) -> str:
        return only(make(tmp_path, fields(READINGS.replace(*change))), "row-id-field")

    assert "isn't one of its fields" in problem(("row_id: reading_id", "row_id: reading"))
    assert "is a float field" in problem(("row_id: reading_id", "row_id: value"))
    assert "is required" in problem(("max_length: 36 }", "max_length: 36, required: true }"))
    assert "has a default" in problem(("max_length: 36 }", "max_length: 36, default: r1 }"))
    assert "at most 12 characters" in problem(("max_length: 36", "max_length: 12"))


STRING_ROWS = """\
parts:
  type: list
  items:
    type: row
    fields:
      part: { type: ref, to: [Assay, Batch], required: true }
      role: { type: string, pattern: "^[a-z]+$", max_length: 20, default: part, required: true }
    by: role
    roles:
      heavy: { min: 1, max: 2, to: { part: Assay } }
"""


def test_a_rows_role_can_be_a_string_and_then_its_roles_are_all_it_takes(tmp_path: Path) -> None:
    """ADR 0164: a role held by a string, whose roles, if it has any, are every role a row
    may have; without any, a row may have any role its pattern takes."""
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    result = make(tmp_path, fields(STRING_ROWS) + "---\n" + batch)
    assert codes(result) == []
    assert result.plugin is not None
    row = result.plugin.types["acme/lab.Assay"].fields["parts"].items
    assert row is not None
    assert (row.by, row.closed) == ("role", True)
    # A role its pattern refuses can't be one.
    bad = make(tmp_path, fields(STRING_ROWS.replace("heavy: {", "Heavy: {")) + "---\n" + batch)
    assert codes(bad) == ["unknown-row-role"]
    # Without roles, it's open.
    open_ = make(tmp_path, fields(STRING_ROWS.split("    roles:")[0]) + "---\n" + batch)
    assert codes(open_) == []
    assert open_.plugin is not None
    items = open_.plugin.types["acme/lab.Assay"].fields["parts"].items
    assert items is not None and items.closed is False


SUBTYPE = """\
apiVersion: iki/v1
kind: EntityType
name: Pair
extends: Assay
schema_version: 1
hrid: { prefix: PAI }
refines:
  parts:
    roles:
      heavy: { min: 1, max: 1, to: { part: Assay } }
      light: { max: 1, to: { part: Batch } }
"""


def test_a_subtype_refines_its_parents_rows_roles(tmp_path: Path) -> None:
    """ADR 0164: a subtype says which roles its inherited rows take, and they're all its
    rows may have, each no wider than its parent's."""
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    base = fields(STRING_ROWS.split("    roles:")[0]).replace(
        "hrid: { prefix: ASY }\n", "abstract: true\n"
    )

    def made(subtype: str) -> Result:
        return make(
            tmp_path,
            {"types/assay.yaml": base, "types/batch.yaml": batch, "types/pair.yaml": subtype},
        )

    result = made(SUBTYPE)
    assert codes(result) == []
    assert result.plugin is not None
    pair = result.plugin.types["acme/lab.Pair"]
    assert list(pair.refines["parts"]) == ["heavy", "light"]
    assert pair.refines["parts"]["light"].to == {"part": ("acme/lab.Batch",)}
    # It survives the manifest.
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin
    # A role the parent's role field can't hold.
    assert codes(made(SUBTYPE.replace("light: {", "Light: {"))) == ["unknown-row-role"]
    # Its own field, a field that isn't rows, or one it hasn't.
    own = (
        SUBTYPE
        + "fields:\n  extra: { type: list, items: { type: row, fields: { x: { type: int } } } }\n"
    )
    assert codes(made(own.replace("  parts:\n    roles:", "  extra:\n    roles:"))) == [
        "refines-own-field"
    ]
    assert codes(made(SUBTYPE.replace("  parts:\n", "  nothing:\n"))) == ["refines-not-rows"]
    # Wider than its parent's: a type the parent's cell doesn't refer to.
    thing = TYPE.replace("Assay", "Thing").replace("ASY", "THG")
    wider = make(
        tmp_path,
        {
            "types/assay.yaml": base,
            "types/batch.yaml": batch,
            "types/thing.yaml": thing,
            "types/pair.yaml": SUBTYPE.replace("to: { part: Batch }", "to: { part: Thing }"),
        },
    )
    assert codes(wider) == ["row-role-type-not-allowed"]
    # And a subtype of the subtype is no wider than it.
    deeper = SUBTYPE.replace("name: Pair", "name: Single").replace(
        "extends: Assay", "extends: Pair"
    )
    deeper = deeper.replace("PAI", "SNG")

    def nested(single: str) -> Result:
        return make(
            tmp_path,
            {
                "types/assay.yaml": base,
                "types/batch.yaml": batch,
                "types/pair.yaml": SUBTYPE,
                "types/single.yaml": single,
            },
        )

    assert codes(nested(deeper)) == []
    light = "light: { max: 1, to: { part: Batch } }"
    assert codes(nested(deeper.replace(light, "light: { max: 2, to: { part: Batch } }"))) == [
        "refines-wider"
    ]
    # Nor narrower by leaving out what its parent requires: a role its parent's rows must
    # have, or the types its parent's role refers to, which would leave it any.
    dropped = nested(deeper.replace("      heavy: { min: 1, max: 1, to: { part: Assay } }\n", ""))
    assert only(dropped, "refines-drops-role") == (
        "Its parent's rows have at least 1 heavy row, and a subtype narrows what its parent's "
        "rows take, so it can't leave out the heavy role."
    )
    unbounded = nested(deeper.replace(light, "light: { max: 1 }"))
    assert only(unbounded, "refines-wider") == (
        "A light row's `part` refers to acme/lab.Batch in this type's parent, and a subtype "
        "narrows what its parent's rows take, so it can't leave that out."
    )
    # A role its parent's rows may have, but needn't, it may leave out: it narrows them.
    assert codes(nested(deeper.replace(f"      {light}\n", ""))) == []


def test_a_subtype_refines_what_an_inherited_reference_refers_to(tmp_path: Path) -> None:
    """ADR 0235: a subtype narrows an inherited reference to types among its parent's, and
    its rules read what the narrower type has."""
    request = TYPE.replace("Assay", "Request").replace(
        "hrid: { prefix: ASY }\n", "abstract: true\n"
    )
    special = (
        TYPE.replace("name: Assay", "name: SpecialRequest\nextends: Request").replace("ASY", "SPR")
        + "fields:\n  panel: { type: enum, values: [purity, mass] }\n"
    )
    other = TYPE.replace("Assay", "Thing").replace("ASY", "THG")
    result_base = (
        fields(
            """\
        request: { type: ref, to: Request, required: true }
        tubes: { type: list, items: { type: ref, to: iki/core.Entity } }
        title: { type: string }
        """
        )
        .replace("name: Assay", "name: Answer")
        .replace("ASY", "ANS")
    )

    def made(subtype: str) -> Result:
        return make(
            tmp_path,
            {
                "types/request.yaml": request,
                "types/special.yaml": special,
                "types/thing.yaml": other,
                "types/answer.yaml": result_base,
                "types/special_answer.yaml": subtype,
            },
        )

    subtype = """\
apiVersion: iki/v1
kind: EntityType
name: SpecialAnswer
extends: Answer
schema_version: 1
hrid: { prefix: SPA }
refines:
  request: { to: SpecialRequest }
  tubes: { to: [Thing] }
rules:
  measured:
    check: record.request.panel != null
    message: The request names its panel.
"""
    result = made(subtype)
    assert codes(result) == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    answer = result.plugin.types["acme/lab.SpecialAnswer"]
    assert answer.refines_to == {
        "request": ("acme/lab.SpecialRequest",),
        "tubes": ("acme/lab.Thing",),
    }
    assert answer.refines == {}
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin
    # Without the refinement, the parent's type has no `panel` to read.
    assert "invalid-expression" in codes(
        made(subtype.replace("  request: { to: SpecialRequest }\n", ""))
    )
    # Wider than its parent's: a type the parent's reference doesn't refer to.
    assert codes(made(subtype.replace("to: SpecialRequest", "to: Thing"))) == [
        "refines-wider",
        "invalid-expression",
    ]
    # Anything the parent's refers to (`iki/core.Entity`) can be narrowed to any type.
    assert codes(made(subtype.replace("to: [Thing]", "to: [Request]"))) == []
    # Its own field, or one that isn't a reference.
    own = subtype + "fields:\n  mine: { type: ref, to: Thing }\n"
    assert codes(made(own.replace("tubes: { to: [Thing] }", "mine: { to: Thing }"))) == [
        "refines-own-field"
    ]
    assert codes(made(subtype.replace("tubes: { to:", "nothing: { to:"))) == [
        "refines-not-a-reference"
    ]
    assert codes(made(subtype.replace("tubes: { to: [Thing] }", "title: { to: Thing }"))) == [
        "refines-not-a-reference"
    ]


def test_an_input_includes_a_rows_references(tmp_path: Path) -> None:
    """ADR 0161: `parts.part` includes each row's reference; a cell that isn't one, or a
    field that isn't rows, can't be."""
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")

    def action(include: str) -> list[str]:
        text = f"""\
apiVersion: iki/v1
kind: Action
name: weigh
inputs:
  assay: {{ type: Assay, include: [{include}] }}
effects:
  creates: [{{ type: Batch, cardinality: one }}]
runs_on: {{ tier: backend, environment: tools }}
command: [python, ./weigh.py]
"""
        env = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
resources: { cpu: 0.25, memory: 128Mi }
python: { version: "3.12", requirements: requirements.txt }
"""
        result = make(
            tmp_path,
            {
                "types/assay.yaml": fields(STRING_ROWS),
                "types/batch.yaml": batch,
                "actions/weigh.yaml": text,
                "environments/tools.yaml": env,
                "weigh.py": "",
                "requirements.txt": "",
            },
        )
        return codes(result)

    assert action("parts.part") == []
    assert action("parts.role") == ["include-not-reference"]
    assert action("parts.nothing") == ["include-unknown-field"]


def test_settings_cant_be_rows(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        plugin=PLUGIN
        + "settings:\n"
        + "  hosts: { type: list, items: { type: row, fields: { a: { type: int } } } }\n",
    )
    # Only entity types' fields have rows.
    assert codes(result) == ["schema"]


def test_a_reference_to_several_types_names_each_once(tmp_path: Path) -> None:
    base = TYPE.replace("Assay", "Thing").replace("hrid: { prefix: ASY }\n", "abstract: true\n")
    sub = TYPE.replace("Assay", "Batch").replace("ASY", "BTC") + "extends: Thing\n"
    result = make(
        tmp_path,
        fields("part: { type: ref, to: [Thing, Batch] }\n") + "---\n" + base + "---\n" + sub,
    )
    message = only(result, "reference-type-twice")
    assert message.startswith("`acme/lab.Batch` extends `acme/lab.Thing`")
    # At the type named again, the list's second.
    assert result.problems[0].location is not None
    assert result.problems[0].location.column == len("  part: { type: ref, to: [Thing, ") + 1
    result = make(tmp_path, fields("part: { type: ref, to: [Assay, iki/core.Entity] }\n"))
    assert codes(result) == ["reference-to-any-and-more"]
    result = make(tmp_path, fields("part: { type: ref, to: [Assay, Asay] }\n"))
    assert codes(result) == ["unknown-type"]
    result = make(tmp_path, fields("part: { type: ref, to: [Assay, Assay] }\n"))
    assert set(codes(result)) == {"schema"}


def test_a_registry_field_refers_to_entries_whichever_type(tmp_path: Path) -> None:
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    result = make(
        tmp_path,
        IDENTIFIED + "  made_of: { type: ref, to: [Assay, Batch], registry: fact }\n---\n" + batch,
    )
    assert codes(result) == ["registry-refers-to-record"]


def test_flags_that_contradict(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields(
            "a: { type: int, computed: true, default: 1 }\nb: { type: int, computed: true, "
            "required: true }\n"
        ),
    )
    assert codes(result) == ["computed-with-default", "computed-required"]


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ("{ type: text, indexed: true }", "unsupported-index"),
        ("{ type: json, indexed: true }", "unsupported-index"),
        ("{ type: file, indexed: true }", "unsupported-index"),
        ("{ type: list, items: { type: int }, indexed: true }", "unsupported-index"),
        ("{ type: string, indexed: true }", "index-needs-max-length"),
        ("{ type: string, indexed: true, max_length: 601 }", "index-needs-max-length"),
    ],
)
def test_indexes(tmp_path: Path, spec: str, code: str) -> None:
    only(make(tmp_path, fields(f"x: {spec}\n")), code)


def test_machine_text_can_go_without_word_search(tmp_path: Path) -> None:
    """A Molfile's "words" mean nothing to a reader, so it has no full-text index (ADR 0233):
    text is searchable unless it says otherwise."""
    result = make(
        tmp_path,
        fields("""\
            molfile: { type: text, searchable: false }
            notes: { type: text }
            remarks: { type: text, searchable: true }
        """),
    )
    assert result.ok, result.problems
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"].fields
    assert (assay["molfile"].searchable, assay["notes"].searchable) == (False, True)
    assert assay["remarks"].searchable


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ("{ type: string, max_length: 64, unique: true }", "unique-needs-index"),
        # A list's items, and other types, can't be: the schema says so.
        ("{ type: list, items: { type: string, unique: true } }", "schema"),
        ("{ type: int, indexed: true, unique: true }", "schema"),
    ],
)
def test_unique_fields(tmp_path: Path, spec: str, code: str) -> None:
    only(make(tmp_path, fields(f"x: {spec}\n")), code)


def test_a_unique_external_id(tmp_path: Path) -> None:
    result = make(
        tmp_path, fields("lims_id: { type: string, max_length: 64, indexed: true, unique: true }\n")
    )
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.types["acme/lab.Assay"].fields["lims_id"].unique


@pytest.mark.parametrize(
    ("link", "code"),
    [
        ("http://lims.example.org/{value}", "link-not-https"),
        ("javascript:alert({value})", "link-not-https"),
        ("https://lims.example.org/samples", "link-without-value"),
        ("https://lims.example.org/{id}", "link-unknown-placeholder"),
        ("{settings.nope}/{value}", "link-unknown-placeholder"),
    ],
)
def test_links(tmp_path: Path, link: str, code: str) -> None:
    result = make(tmp_path, fields(f'x: {{ type: string, link: "{link}" }}\n'))
    assert code in codes(result), result.problems


def test_a_link_can_start_with_a_setting(tmp_path: Path) -> None:
    plugin = PLUGIN + "settings:\n  lims_url: { type: string, default: https://lims.example.org }\n"
    result = make(
        tmp_path, fields('x: { type: string, link: "{settings.lims_url}/s/{value}" }\n'), plugin
    )
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.types["acme/lab.Assay"].fields["x"].link == "{settings.lims_url}/s/{value}"


def test_indexable_fields(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields(
            "a: { type: string, indexed: true, max_length: 600 }\n"
            "b: { type: int, indexed: true }\nc: { type: date, indexed: true }\n"
            "d: { type: ref, to: Assay, indexed: true }\n"
            "e: { type: quantity, unit: mg, indexed: true }\n"
        ),
    )
    assert result.problems == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("{ type: int, min: 5, max: 1 }", "`min` (5) is more than `max` (1)."),
        ("{ type: string, min_length: 5, max_length: 1 }", "`min_length` (5) is more than"),
        ("{ type: list, items: { type: int }, min_items: 3, max_items: 2 }", "`min_items` (3)"),
    ],
)
def test_ranges(tmp_path: Path, spec: str, message: str) -> None:
    assert message in only(make(tmp_path, fields(f"x: {spec}\n")), "min-above-max")


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("'[a-'", "isn't a valid regular expression"),
        ("'^(?=a)'", "lookahead"),
        ("'(?<=a)b'", "lookbehind"),
        ("'(?P<x>a)'", "named groups"),
        ("'(a)\\1'", "backreferences"),
        ("'(?i)abc'", "inline flags"),
    ],
)
def test_patterns_must_work_in_python_and_postgres(
    tmp_path: Path, pattern: str, message: str
) -> None:
    result = make(tmp_path, fields(f"x: {{ type: string, pattern: {pattern} }}\n"))
    assert message in only(result, "invalid-pattern")


def test_a_patterns_help_needs_its_pattern(tmp_path: Path) -> None:
    result = make(tmp_path, fields("x: { type: string, pattern_help: lower-case letters }\n"))
    assert not result.ok
    assert any("pattern" in p.message for p in result.problems), result.problems


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("{ type: enum, values: [a, b], default: c }", "must be one of: `a`, `b`."),
        ("{ type: int, min: 0, default: -1 }", "must be at least 0."),
        ("{ type: float, max: 1, default: 2 }", "must be at most 1."),
        ("{ type: string, max_length: 2, default: abc }", "at most 2 characters"),
        ("{ type: string, format: dna_sequence, default: ACGZ }", "isn't a valid dna sequence."),
        ("{ type: string, pattern: '^[a-z]+$', default: A1 }", "doesn't match the pattern"),
        (
            "{ type: string, pattern: '^[a-z]+$', pattern_help: lower-case letters, default: A1 }",
            "must use lower-case letters.",
        ),
        ("{ type: date, default: '2024-02-30' }", "must be a date"),
        (
            "{ type: list, items: { type: int, min: 0 }, default: [1, -1] }",
            "item 2 must be at least",
        ),
    ],
)
def test_defaults_must_be_valid(tmp_path: Path, spec: str, message: str) -> None:
    assert message in only(make(tmp_path, fields(f"x: {spec}\n")), "invalid-default")


def test_quantities_need_real_units(tmp_path: Path) -> None:
    result = make(tmp_path, fields("weight: { type: quantity, unit: Da }\n"))
    assert only(result, "unknown-unit") == "`Da` isn't a UCUM unit."
    assert "`u` is the unified atomic mass unit" in (result.problems[0].hint or "")
    result = make(
        tmp_path,
        fields("weight: { type: quantity, unit: g/mol, display_unit: mL }\n"),
    )
    assert "doesn't measure the same thing as `g/mol`" in only(result, "display-unit-mismatch")
    result = make(tmp_path, fields("weight: { type: quantity, unit: mg, display_unit: Mgg }\n"))
    assert codes(result) == ["unknown-unit"]


def test_files_and_json(tmp_path: Path) -> None:
    result = make(tmp_path, fields("f: { type: file, max_size: 6TiB }\n"))
    assert "at most 5 TiB" in only(result, "file-too-large")
    result = make(tmp_path, fields("j: { type: json, schema: { type: nope } }\n"))
    assert "isn't a valid JSON Schema" in only(result, "invalid-json-schema")
    # A form starts from its schema's default, so the default must fit it.
    result = make(
        tmp_path,
        fields(
            "j: { type: json, schema: { type: object, required: [panels], "
            "default: { version: 1 } } }\n"
        ),
    )
    assert (
        only(result, "invalid-default")
        == "The default doesn't fit `schema`: 'panels' is a required property."
    )
    result = make(
        tmp_path,
        fields("j: { type: json, schema: { type: object, default: { panels: [] } } }\n"),
    )
    assert codes(result) == []


def test_identity(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        TYPE
        + "identity: [code, weight, name, missing]\n"
        + "fields:\n  code: { type: string, required: true, immutable: true }\n"
        + "  weight: { type: quantity, unit: mg, required: true, immutable: true }\n"
        + "  name: { type: string }\n",
    )
    assert codes(result) == ["identity-type", "identity-not-fixed", "unknown-field"]
    assert result.problems[1].message == "`name` is part of the identity, so it must be immutable."


def test_an_identity_can_have_optional_fields_beside_a_required_one(tmp_path: Path) -> None:
    pair = (
        "fields:\n  heavy: { type: string, required: true, immutable: true }\n"
        "  light: { type: string, immutable: true }\n"
    )
    assert codes(make(tmp_path, TYPE + "identity: [heavy, light]\n" + pair)) == []
    result = make(tmp_path, TYPE + "identity: [light]\n" + pair)
    assert codes(result) == ["identity-not-required"]
    assert result.problems[0].message == (
        "The identity needs a required field: each of its fields could be empty."
    )


SEQUENCE = """\
identity: [bases, topology, strands]
fields:
  bases:
    type: string
    format: dna_sequence
    topology: topology
    strands: strands
    required: true
    immutable: true
  topology: { type: enum, values: [linear, circular], required: true, immutable: true }
  strands: { type: enum, values: [double, single], required: true, immutable: true }
"""


def test_a_sequences_identity_reads_it_by_its_topology_and_strands(tmp_path: Path) -> None:
    """ADR 0138: each names an enum field beside it, itself part of the identity."""
    assert codes(make(tmp_path, TYPE + SEQUENCE)) == []
    built = make(tmp_path, TYPE + SEQUENCE).plugin
    assert built is not None
    bases = built.types["acme/lab.Assay"].fields["bases"]
    assert (bases.topology, bases.strands) == ("topology", "strands")

    def problems(*changes: tuple[str, str]) -> list[str]:
        text = SEQUENCE
        for change in changes:
            text = text.replace(*change)
        return codes(make(tmp_path, TYPE + text))

    assert problems(("[linear, circular]", "[linear, ring]")) == ["sequence-option"]
    assert problems(("topology: topology", "topology: shape")) == ["sequence-option"]
    assert problems(("format: dna_sequence", "format: protein_sequence")) == ["sequence-option"]
    assert problems(("format: dna_sequence", "pattern: '^[A-Z]*$'")) == [
        "sequence-option",
        "sequence-option",
    ]
    assert problems(("[bases, topology, strands]", "[bases, topology]")) == ["identity-incomplete"]


def test_an_identity_can_be_a_set_of_parts(tmp_path: Path) -> None:
    """ADR 0138: a list of references, values or rows is a multiset, a row by its
    identity fields, each a value compared exactly."""
    # Its parts are entries too: an entry refers only to entries.
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC") + (
        "identity: [code]\nfields:\n  code: { type: string, required: true, immutable: true }\n"
    )
    rows = ROWS.replace("    by: role", "    identity: [part, role]\n    by: role").replace(
        "  type: list\n", "  type: list\n  required: true\n  immutable: true\n"
    )
    assert (
        codes(
            make(
                tmp_path, TYPE + "identity: [parts]\n" + fields(rows)[len(TYPE) :] + "---\n" + batch
            )
        )
        == []
    )
    refs = (
        "parts: { type: list, items: { type: ref, to: Batch }, required: true, immutable: true }\n"
    )
    assert (
        codes(
            make(
                tmp_path, TYPE + "identity: [parts]\n" + fields(refs)[len(TYPE) :] + "---\n" + batch
            )
        )
        == []
    )
    weights = rows.replace("copies: { type: int, min: 1, default: 1 }", "copies: { type: float }")
    with_float = weights.replace("identity: [part, role]", "identity: [part, copies]")
    assert codes(
        make(
            tmp_path,
            TYPE + "identity: [parts]\n" + fields(with_float)[len(TYPE) :] + "---\n" + batch,
        )
    ) == ["identity-type"]
    record = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    a_record = TYPE + "identity: [parts]\n" + fields(rows)[len(TYPE) :] + "---\n" + record
    assert codes(make(tmp_path, a_record)) == ["registry-refers-to-record"]
    files = "parts: { type: list, items: { type: file }, required: true, immutable: true }\n"
    assert codes(make(tmp_path, TYPE + "identity: [parts]\n" + fields(files)[len(TYPE) :])) == [
        "identity-type"
    ]


def test_on_duplicate_is_gone(tmp_path: Path) -> None:
    # One record per identity per project (ADR 0115): there's nothing left to choose.
    result = make(tmp_path, TYPE + "on_duplicate: warn\n")
    assert codes(result) == ["schema"]
    assert result.problems[0].message == "`on_duplicate` isn't allowed here."


def test_an_identity_can_be_inherited(tmp_path: Path) -> None:
    parent = (
        TYPE.replace("hrid: { prefix: ASY }\n", "abstract: true\n")
        + "identity: [code]\nfields:\n  code: { type: string, required: true, immutable: true }\n"
    )
    child = TYPE.replace("Assay", "Elisa").replace("ASY", "ELI") + "extends: Assay\n"
    assert make(tmp_path, parent + "---\n" + child).problems == []


IDENTIFIED = (
    TYPE + "identity: [code]\nfields:\n  code: { type: string, required: true, immutable: true }\n"
)


def test_an_identity_names_the_types_own_fields(tmp_path: Path) -> None:
    parent = (
        TYPE.replace("hrid: { prefix: ASY }\n", "abstract: true\n")
        + "fields:\n  code: { type: string, required: true, immutable: true }\n"
    )
    child = (
        TYPE.replace("Assay", "Elisa").replace("ASY", "ELI") + "extends: Assay\nidentity: [code]\n"
    )
    result = make(tmp_path, parent + "---\n" + child)
    assert codes(result) == ["identity-inherited"]
    assert result.problems[0].message == (
        "`code` is a field of `acme/lab.Assay`, which this type extends: an identity names the "
        "type's own fields."
    )


def test_an_identity_isnt_declared_twice_or_under_a_concrete_type(tmp_path: Path) -> None:
    child = (
        TYPE.replace("Assay", "Elisa").replace("ASY", "ELI")
        + "extends: Assay\nidentity: [plate]\n"
        + "fields:\n  plate: { type: string, required: true, immutable: true }\n"
    )
    assert codes(make(tmp_path, IDENTIFIED + "---\n" + child)) == ["identity-redeclared"]
    concrete = TYPE + "fields:\n  note: { type: string }\n"
    result = make(tmp_path, concrete + "---\n" + child)
    assert codes(result) == ["identity-under-concrete"]


def test_registry_fields(tmp_path: Path) -> None:
    good = (
        IDENTIFIED
        + "  weight: { type: float, registry: derived, computed: true }\n"
        + "  canonical: { type: string, registry: derived, immutable: true }\n"
        + "  cas: { type: string, registry: fact, pattern: '^[0-9-]+$', indexed: true, "
        "max_length: 12, unique: true }\n"
        + "  hazard: { type: enum, registry: fact, values: [none, toxic] }\n"
        + "  notes: { type: text }\n"
    )
    result = make(tmp_path, good)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.types["acme/lab.Assay"].fields
    assert {n: f.registry for n, f in found.items()} == {
        "code": None,
        "weight": "derived",
        "canonical": "derived",
        "cas": "fact",
        "hazard": "fact",
        "notes": None,
    }


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ("{ type: text, registry: fact }", "registry-fact-free-text"),
        ("{ type: json, registry: fact }", "registry-fact-free-text"),
        ("{ type: string, registry: fact }", "registry-fact-free-text"),
        ("{ type: enum, values: [a], registry: fact, required: true }", "registry-fact-required"),
        ("{ type: int, registry: fact, computed: true }", "registry-fact-computed"),
    ],
)
def test_registry_facts_are_curated_values(tmp_path: Path, spec: str, code: str) -> None:
    assert codes(make(tmp_path, IDENTIFIED + f"  x: {spec}\n")) == [code]


def test_a_fixed_fact_may_be_required(tmp_path: Path) -> None:
    """A fixed fact (ADR 0142) is given when its entry is registered and never changes, so
    it may be required: a monomer's structure, which what's registered against it reads."""
    fixed = "  x: { type: enum, values: [a], registry: fact, immutable: true, required: true }\n"
    result = make(tmp_path, IDENTIFIED + fixed)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.types["acme/lab.Assay"].fields["x"]
    assert (found.registry, found.immutable, found.required) == ("fact", True, True)


def test_registry_fields_need_an_identity(tmp_path: Path) -> None:
    result = make(tmp_path, fields("weight: { type: float, registry: derived }\n"))
    message = only(result, "registry-without-identity")
    assert message == (
        "`weight` is marked `registry: derived`, but `Assay` has no identity, so it has no "
        "registry entries."
    )
    on_identity = IDENTIFIED.replace("immutable: true }", "immutable: true, registry: derived }")
    assert codes(make(tmp_path, on_identity)) == ["registry-on-identity"]


def test_an_entry_refers_only_to_entries(tmp_path: Path) -> None:
    batch = TYPE.replace("Assay", "Batch").replace("ASY", "BTC")
    result = make(
        tmp_path,
        IDENTIFIED + "  batch: { type: ref, to: Batch, registry: derived }\n---\n" + batch,
    )
    assert codes(result) == ["registry-refers-to-record"]
    result = make(tmp_path, IDENTIFIED + "  same: { type: ref, to: Assay, registry: fact }\n")
    assert codes(result) == []
    result = make(
        tmp_path,
        TYPE + "identity: [batch]\nfields:\n  batch: { type: ref, to: Batch, required: true, "
        "immutable: true }\n---\n" + batch,
    )
    assert codes(result) == ["registry-refers-to-record"]


def test_an_extension_can_add_registry_fields_to_an_identity(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        extension("gc: { type: float, registry: derived, computed: true }\n"),
        DNA_PIN,
        with_=(DNA,),
    )
    assert result.problems == []


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("'{{ nme or hrid }}'", "names `nme`, which isn't a field"),
        ("'{{ name or }}'", "ends with `or`"),
        ("'{{ name name }}'", "where `or` belongs"),
        ("'{{ or name }}'", "where a field name or quoted text belongs"),
        ("'{{ }}'", "empty `{{ }}`"),
        ("'{{ name'", "without a `}}`"),
        ("'name }}'", "without a `{{`"),
        ("'Assay'", "the same text for every entity"),
        ("'{{ data }}'", "can't show `data`, a json field"),
    ],
)
def test_display_titles(tmp_path: Path, title: str, message: str) -> None:
    text = (
        fields("name: { type: string }\ndata: { type: json }\n")
        + f"display: {{ title: {title} }}\n"
    )
    assert message in only(make(tmp_path, text), "display-title")


def test_display_titles_combine_fields_and_text(tmp_path: Path) -> None:
    text = (
        fields("name: { type: string }\n")
        + "display: { title: 'Assay {{ name or \"untitled\" }} ({{ hrid }})' }\n"
    )
    assert make(tmp_path, text).problems == []


# --- Subtypes and references ---------------------------------------------------------------------


def test_subtypes(tmp_path: Path) -> None:
    parent = TYPE + "fields:\n  name: { type: string }\n"
    child = (
        TYPE.replace("Assay", "Elisa").replace("ASY", "ELI")
        + "extends: Assay\nfields:\n  name: { type: string }\n"
    )
    result = make(tmp_path, parent + "---\n" + child)
    assert "already a field of `acme/lab.Assay`" in only(result, "field-shadows-parent")


def test_subtype_cycles(tmp_path: Path) -> None:
    a = TYPE + "extends: Elisa\n"
    b = TYPE.replace("Assay", "Elisa").replace("ASY", "ELI") + "extends: Assay\n"
    result = make(tmp_path, a + "---\n" + b)
    assert codes(result) == ["extends-cycle", "extends-cycle"]
    assert "`acme/lab.Assay` → `acme/lab.Elisa` → `acme/lab.Assay`" in result.problems[0].message


def test_unknown_types_in_this_plugin(tmp_path: Path) -> None:
    result = make(tmp_path, fields("x: { type: ref, to: Asay }\n"))
    assert only(result, "unknown-type") == "This plugin has no type `Asay`."
    assert result.problems[0].hint == "Did you mean `Assay`?"


def test_core_entity_needs_no_dependency(tmp_path: Path) -> None:
    assert make(tmp_path, fields("x: { type: ref, to: iki/core.Entity }\n")).problems == []
    assert make(tmp_path, fields("x: { type: ref, to: iki/core.Collection }\n")).problems == []
    result = make(tmp_path, fields("x: { type: ref, to: iki/core.Sample }\n"))
    provides = (
        "In this version, core provides `iki/core.Alignment`, `iki/core.App`, "
        "`iki/core.Collection`, `iki/core.Complex`, `iki/core.Entity`, `iki/core.Organism`, "
        "`iki/core.Sequence`, `iki/core.View`."
    )
    assert provides in only(result, "unknown-type")


def test_other_plugins_types_need_a_declared_dependency(tmp_path: Path) -> None:
    result = make(tmp_path, fields("x: { type: ref, to: acme/dna.DNASequence }\n"), with_=(DNA,))
    assert "which plugin.yaml doesn't list under `dependencies`" in only(
        result, "undeclared-dependency"
    )
    assert 'dna: { types: { DNASequence: ">=2" } }' in (result.problems[0].hint or "")
    result = make(tmp_path, fields("x: { type: ref, to: acme/dna.DNASequence }\n"))
    assert codes(result) == ["undeclared-dependency"]


def test_dependencies_must_be_given_to_check_them(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields(
            "x: { type: ref, to: acme/dna.DNASequence }\ny: { type: ref, to: acme/dna.Primer }\n"
        ),
        plugin=PLUGIN + "dependencies:\n  acme/dna: {}\n",
    )
    # Reported once, on the dependency, not on every use.
    problem = result.problems[0]
    assert codes(result) == ["dependency-not-available"]
    assert problem.file == "plugin.yaml"
    assert problem.location == Location(6, 3)
    assert problem.hint == "Pass the acme/dna plugin's directory with `--with path/to/dna`."


def test_uses_of_a_dependency(tmp_path: Path) -> None:
    plugin = PLUGIN + "dependencies:\n  acme/dna:\n    types:\n      DNASequence: '>=2'\n"
    result = make(tmp_path, fields("x: { type: ref, to: acme/dna.Primer }\n"), plugin, with_=(DNA,))
    assert only(result, "unknown-type") == "The acme/dna plugin has no type `Primer`."
    ok = make(
        tmp_path, fields("x: { type: ref, to: acme/dna.DNASequence }\n"), plugin, with_=(DNA,)
    )
    assert ok.problems == []


def test_unpinned_dependency_types_are_a_warning(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("x: { type: ref, to: acme/dna.DNASequence }\n"),
        PLUGIN + "dependencies:\n  acme/dna: {}\n",
        with_=(DNA,),
    )
    assert codes(result) == ["dependency-unpinned"]
    assert result.problems[0].severity is Severity.WARNING
    assert result.ok
    assert result.plugin is not None


@pytest.mark.parametrize(
    ("constraint", "code"),
    [(">=3", "dependency-version"), ("<2", "dependency-version"), ("==2", None), ("!=1", None)],
)
def test_dependency_versions(tmp_path: Path, constraint: str, code: str | None) -> None:
    plugin = PLUGIN + f"dependencies:\n  acme/dna:\n    types:\n      DNASequence: '{constraint}'\n"
    result = make(tmp_path, "", plugin, with_=(DNA,))
    assert codes(result) == ([code] if code else [])


def test_a_plugin_says_which_iki_it_works_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import iki_spec

    monkeypatch.setattr(iki_spec, "__version__", "0.2.0.dev3")
    for fits in (">=0.1", ">=0.2,<0.3", "==0.2", "<1"):
        result = make(tmp_path, "", PLUGIN + f"iki: '{fits}'\n")
        assert result.ok, (fits, result.problems)
        assert result.plugin is not None
        assert result.plugin.iki == fits
    assert make(tmp_path, "").plugin.iki is None  # type: ignore[union-attr]

    result = make(tmp_path, "", PLUGIN + "iki: '>=0.3, <1'\n")
    assert (
        only(result, "iki-version") == "acme/lab works with Iki >=0.3, <1, and this is Iki 0.2.0."
    )
    problem = result.problems[0]
    assert (
        problem.hint
        == "Use a release of acme/lab made for Iki 0.2.0, or deploy an Iki it works with."
    )
    assert problem.location is not None
    assert problem.location.line == 5

    result = make(tmp_path, "", PLUGIN + "iki: '0.2'\n")
    assert "It compares Iki release numbers, such as `>=0.1`" in only(result, "schema")


def test_dependency_types_must_exist(tmp_path: Path) -> None:
    plugin = PLUGIN + "dependencies:\n  acme/dna:\n    types:\n      DnaSequence: '>=1'\n"
    result = make(tmp_path, "", plugin, with_=(DNA,))
    assert only(result, "unknown-type") == "The acme/dna plugin has no type `DnaSequence`."
    assert result.problems[0].hint == "Did you mean `DNASequence`?"


def test_dependency_problems(tmp_path: Path) -> None:
    result = make(tmp_path, "", PLUGIN + "dependencies:\n  acme/lab: {}\n")
    assert codes(result) == ["dependency-is-self"]
    result = make(tmp_path, "", with_=(tmp_path / "acme",))
    assert codes(result) == ["dependency-is-self"]
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.yaml").write_text("apiVersion: iki/v1\nkind: Plugin\nname: broken\n")
    result = make(tmp_path, "", with_=(broken,))
    assert "has 1 error(s) of its own" in only(result, "dependency-invalid")
    result = make(tmp_path, "", with_=(tmp_path / "missing",))
    assert "isn't a plugin Iki can read" in only(result, "dependency-invalid")


def test_dependency_cycles(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/other\nversion: 1.0.0\n"
        "dependencies:\n  acme/lab: {}\n"
    )
    result = make(tmp_path, "", PLUGIN + "dependencies:\n  acme/other: {}\n", with_=(other,))
    assert codes(result) == ["dependency-cycle"]


# --- Namespaces ----------------------------------------------------------------------------------


def _globex_dna(tmp_path: Path) -> Path:
    """The fixture dna plugin under another namespace: the same short name, another plugin."""
    copy = tmp_path / "globex_dna"
    shutil.copytree(DNA, copy, dirs_exist_ok=True)
    manifest = copy / "plugin.yaml"
    manifest.write_text(manifest.read_text().replace("name: acme/dna", "name: globex/dna"))
    for file in (copy / "types").glob("*.yaml"):
        file.write_text(file.read_text().replace("acme/dna.", "globex/dna."))
    return copy


def test_dependencies_are_named_in_full(tmp_path: Path) -> None:
    result = make(tmp_path, "", PLUGIN + "dependencies:\n  dna: {}\n", with_=(DNA,))
    assert only(result, "unqualified-dependency") == "`dna` needs its namespace."
    assert result.problems[0].hint == "Write `acme/dna`."


def test_other_plugins_types_are_named_in_full(tmp_path: Path) -> None:
    text = fields("construct: { type: ref, to: dna.DNASequence }\n")
    result = make(tmp_path, text, DNA_PIN, with_=(DNA,))
    assert "`dna.DNASequence`" in only(result, "unqualified-reference")
    assert result.problems[0].hint == "Write `acme/dna.DNASequence`."
    # The plugin's own types are named without it, or in full.
    own = {
        "types/assay.yaml": TYPE,
        "types/run.yaml": TYPE.replace("Assay", "Run").replace("ASY", "RN")
        + "fields:\n  assay: { type: ref, to: Assay }\n"
        + "  again: { type: ref, to: acme/lab.Assay }\n",
    }
    assert make(tmp_path, own).problems == []


def test_the_same_name_in_two_namespaces(tmp_path: Path) -> None:
    globex = _globex_dna(tmp_path)
    plugin = (
        PLUGIN
        + "dependencies:\n  acme/dna: { types: { DNASequence: '>=2' } }\n"
        + "  globex/dna: { types: { DNASequence: '>=2' } }\n"
    )
    text = fields(
        "ours: { type: ref, to: acme/dna.DNASequence }\n"
        "theirs: { type: ref, to: globex/dna.DNASequence }\n"
    )
    result = make(tmp_path, text, plugin, with_=(DNA, globex))
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    assert assay.fields["ours"].to == ("acme/dna.DNASequence",)
    assert assay.fields["theirs"].to == ("globex/dna.DNASequence",)
    # Without the namespace, it could be either: the hint says to choose.
    vague = fields("which: { type: ref, to: dna.DNASequence }\n")
    result = make(tmp_path, vague, plugin, with_=(DNA, globex))
    assert codes(result) == ["unqualified-reference"]
    assert result.problems[0].hint == "Write `<namespace>/dna.DNASequence`."


# --- Extensions ----------------------------------------------------------------------------------

EXTENSION = """\
apiVersion: iki/v1
kind: EntityTypeExtension
extends_type: acme/dna.DNASequence
fields:
"""
DNA_PIN = PLUGIN + "dependencies:\n  acme/dna:\n    types:\n      DNASequence: '>=2'\n"


def extension(text: str) -> str:
    return EXTENSION + textwrap.indent(textwrap.dedent(text), "  ")


def test_extensions(tmp_path: Path) -> None:
    result = make(tmp_path, extension("codon_usage: { type: json }\n"), DNA_PIN, with_=(DNA,))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.extensions[0].table == "acme__lab__acme__dna_dnasequence"


def test_extension_fields_cant_clash_with_the_types(tmp_path: Path) -> None:
    result = make(tmp_path, extension("name: { type: string }\n"), DNA_PIN, with_=(DNA,))
    message = only(result, "extension-field-conflict")
    assert (
        message == "`name` is already a field of `acme/dna.DNASequence` (from `iki/core.Sequence`)."
    )
    assert result.problems[0].hint == "Choose another name, for example `lab_name`."


def test_extension_fields_cant_clash_with_other_extensions(tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / "types").mkdir(parents=True)
    (other / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/other\nversion: 1.0.0\n"
        "dependencies:\n  acme/dna: {}\n"
    )
    (other / "types" / "x.yaml").write_text(extension("tm: { type: float }\n"))
    result = make(tmp_path, extension("tm: { type: float }\n"), DNA_PIN, with_=(DNA, other))
    assert "(from the acme/other plugin's extension)" in only(result, "extension-field-conflict")


def test_extension_fields_cant_be_required(tmp_path: Path) -> None:
    result = make(tmp_path, extension("x: { type: int, required: true }\n"), DNA_PIN, with_=(DNA,))
    assert codes(result) == ["extension-field-required"]


def test_extension_targets(tmp_path: Path) -> None:
    own = EXTENSION.replace("acme/dna.DNASequence", "Assay") + "  x: { type: int }\n"
    result = make(tmp_path, {"types/assay.yaml": TYPE, "types/ext.yaml": own})
    assert result.problems[0].hint == "Add the fields to the type itself."
    builtin = EXTENSION.replace("acme/dna.DNASequence", "iki/core.Entity") + "  x: { type: int }\n"
    assert codes(make(tmp_path, builtin)) == ["extends-builtin"]
    twice = extension("x: { type: int }\n") + "---\n" + extension("y: { type: int }\n")
    result = make(tmp_path, twice, DNA_PIN, with_=(DNA,))
    assert "already extends `acme/dna.DNASequence` (in types/assay.yaml, line 1)" in only(
        result, "duplicate-extension"
    )


def test_extension_table_names(tmp_path: Path) -> None:
    long_type = "B" + "b" * 40
    other = tmp_path / "other"
    (other / "types").mkdir(parents=True)
    (other / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/other\nversion: 1.0.0\n"
    )
    (other / "types" / "x.yaml").write_text(TYPE.replace("Assay", long_type))
    plugin = (
        PLUGIN.replace("acme", "a" * 30)
        + f"dependencies:\n  acme/other:\n    types:\n      {long_type}: '>=1'\n"
    )
    text = extension("x: { type: int }\n").replace(
        "acme/dna.DNASequence", f"acme/other.{long_type}"
    )
    result = make(tmp_path, text, plugin, with_=(other,))
    assert "PostgreSQL allows 63" in only(result, "name-too-long")


def test_list_tables(tmp_path: Path) -> None:
    text = fields(
        """\
        parents: { type: list, items: { type: ref, to: iki/core.Entity } }
        tags: { type: list, items: { type: string } }
        """
    )
    assert make(tmp_path, text).problems == []
    long = "p" + "x" * 49
    text = fields(f"{long}: {{ type: list, items: {{ type: file }} }}\n")
    message = only(make(tmp_path, text, PLUGIN.replace("acme", "acme_lab")), "name-too-long")
    assert message == (
        f"The table `acme_lab__lab_assay__{long}` for the list `{long}` of `Assay` would be 71 "
        "characters; PostgreSQL allows 63."
    )


def test_model_names_cant_clash(tmp_path: Path) -> None:
    # The type `Ext` with a list `acme_dna_dnasequence` would be the model
    # `Ext_acme_dna_DNASequence`'s twin: the extension of `acme/dna.DNASequence`.
    text = {
        "types/assay.yaml": TYPE.replace("Assay", "Ext").replace("ASY", "EXT")
        + "fields:\n  acme_dna_dnasequence: { type: list, items: { type: file } }\n",
        "types/ext.yaml": extension("x: { type: int }\n"),
    }
    message = only(make(tmp_path, text, DNA_PIN, with_=(DNA,)), "duplicate-table")
    assert "would share the model `Ext_acme_dna_DNASequence`" in message


def test_removed_fields(tmp_path: Path) -> None:
    text = fields("old: { type: string, removed: true }\n")
    result = make(tmp_path, text)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    assert assay.fields["old"].removed
    assert assay.current_fields == {}
    identity = fields("code: { type: string, required: true, immutable: true, removed: true }\n")
    message = only(make(tmp_path, identity + "identity: [code]\n"), "identity-removed")
    assert message == "`code` is part of the identity, so it can't be removed."
    title = fields("old: { type: string, removed: true }\n") + "display: { title: '{{ old }}' }\n"
    assert codes(make(tmp_path, title)) == ["display-title"]
    item = fields("x: { type: list, items: { type: int, removed: true } }\n")
    assert codes(make(tmp_path, item)) == ["schema"]


def test_parent_link_names_are_reserved(tmp_path: Path) -> None:
    assert codes(make(tmp_path, fields("sequence_ptr: { type: int }\n"))) == ["reserved-name"]


def test_a_type_named_like_its_ancestor_links_with_its_plugin() -> None:
    # acme/dna.Sequence's row links to iki/core.Sequence's as `sequence_ptr`, so
    # DNASequence's own link to acme/dna.Sequence can't be called that too.
    assert naming.parent_link("iki/core.Sequence") == "sequence_ptr"
    assert naming.parent_link("acme/dna.Sequence", ["sequence_ptr"]) == "acme_dna_sequence_ptr"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("{ type: string, default: '' }", "The default can't be empty; leave it out instead."),
        (
            '{ type: string, default: "a\\nb" }',
            "The default must be one line; use a `text` field for longer text.",
        ),
    ],
)
def test_string_values(tmp_path: Path, spec: str, message: str) -> None:
    assert only(make(tmp_path, fields(f"x: {spec}\n")), "invalid-default") == message


def test_empty_strings_need_min_length_0(tmp_path: Path) -> None:
    text = fields("x: { type: string, min_length: 0, default: '' }\n")
    assert make(tmp_path, text).problems == []
    assert make(tmp_path, fields('x: { type: text, default: "a\\nb" }\n')).problems == []


def test_plugins_round_trip_through_json() -> None:
    import json

    from iki_spec.model import plugin_from_dict, plugin_to_dict

    for directory, deps in ((DNA, ()), (PROTEIN, (DNA,)), (PREDICTION, (PROTEIN, DNA))):
        plugin = load_plugin(directory, dependencies=deps).plugin
        assert plugin is not None
        data = json.loads(json.dumps(plugin_to_dict(plugin)))
        assert plugin_from_dict(data) == plugin


# --- Settings ------------------------------------------------------------------------------------


def test_settings_rules(tmp_path: Path) -> None:
    plugin = PLUGIN + textwrap.dedent("""\
        settings:
          api_key: { type: string, required: false }
          region: { type: enum, values: [eu, us], required: true, default: eu }
          batch: { type: int, min: 1, default: 0 }
    """)
    result = make(tmp_path, "", plugin)
    assert codes(result) == ["setting-looks-secret", "required-with-default", "invalid-default"]
    assert result.problems[0].severity is Severity.WARNING


def test_settings_cant_use_entity_field_types_or_flags(tmp_path: Path) -> None:
    plugin = PLUGIN + "settings:\n  x: { type: string, immutable: true }\n"
    result = make(tmp_path, "", plugin)
    assert any("`immutable` isn't" in p.message for p in result.problems)
    plugin = PLUGIN + "settings:\n  x: { type: string, unit: mg }\n"
    (problem,) = make(tmp_path, "", plugin).problems
    assert problem.hint is not None
    assert "`immutable`" not in problem.hint


@pytest.mark.parametrize(
    "name",
    ["password", "api_key", "github_token", "client_secret", "aws_access_key", "db_credentials"],
)
def test_secret_looking_names(name: str) -> None:
    assert looks_secret(name)


@pytest.mark.parametrize("name", ["key_columns", "region", "tokenizer", "max_tokens_used"])
def test_ordinary_names(name: str) -> None:
    assert not looks_secret(name)


def test_settings_values() -> None:
    result = load_plugin(DNA)
    assert result.plugin is not None
    dna = result.plugin
    assert check_settings_text(dna, "ncbi_email: lab@example.com\n", "dna.yaml") == []
    problems = check_settings_text(
        dna, "ncbi_email: not-an-email\nmax_sequence_length: 0\nncbi_mail: x\n", "dna.yaml"
    )
    assert [p.code for p in problems] == ["invalid-setting", "invalid-setting", "unknown-setting"]
    assert problems[0].message == "`ncbi_email` isn't a valid email."
    assert problems[2].hint == "Did you mean `ncbi_email`?"
    problems = check_settings_text(dna, "", "dna.yaml")
    assert [p.code for p in problems] == ["missing-setting"]
    assert problems[0].hint == "The address NCBI asks automated clients to give."
    assert [p.code for p in check_settings_text(dna, "- a\n", "dna.yaml")] == ["settings-file"]
    assert [p.code for p in check_settings_text(dna, "a: 1\n---\nb: 2\n", "dna.yaml")] == [
        "settings-file"
    ]
    assert [p.code for p in check_settings_text(dna, "a: [\n", "dna.yaml")] == ["yaml-syntax"]


def test_settings_values_of_every_type(tmp_path: Path) -> None:
    plugin = PLUGIN + textwrap.dedent("""\
        settings:
          s: { type: string, required: false }
          i: { type: int, required: false }
          f: { type: float, required: false }
          b: { type: bool, required: false }
          e: { type: enum, values: [x, y], required: false }
          l: { type: list, items: { type: int }, max_items: 2, required: false }
    """)
    result = make(tmp_path, "", plugin)
    assert result.plugin is not None
    good = "s: a\ni: 1\nf: 1\nb: false\ne: x\nl: [1, 2]\n"
    assert check_settings_text(result.plugin, good, "acme.yaml") == []
    bad = "s: 1\ni: 1.5\nf: true\nb: 'no'\ne: z\nl: [1, 2, a]\n"
    messages = [p.message for p in check_settings_text(result.plugin, bad, "acme.yaml")]
    assert messages == [
        "`s` must be text.",
        "`i` must be a whole number.",
        "`f` must be a number.",
        "`b` must be `true` or `false`.",
        "`e` must be one of: `x`, `y`.",
        "`l` can have at most 2 items.",
        "`l` item 3 must be a number.",
    ]
    no_settings = make(tmp_path, "").plugin
    assert no_settings is not None
    (problem,) = check_settings_text(no_settings, "x: 1\n", "acme.yaml")
    assert problem.hint == "The acme/lab plugin takes no settings."


def test_settings_files(tmp_path: Path) -> None:
    dna = load_plugin(DNA).plugin
    assert dna is not None
    values = tmp_path / "dna.yaml"
    values.write_text("ncbi_email: lab@example.com\n")
    assert check_settings_values(dna, values, "dna.yaml") == []
    assert [p.code for p in check_settings_values(dna, tmp_path / "x.yaml", "x.yaml")] == [
        "unreadable-file"
    ]


# --- Building blocks -----------------------------------------------------------------------------


def test_constraints() -> None:
    assert satisfies(3, ">=2,<4")
    assert not satisfies(4, ">=2, <4")
    assert satisfies(2, "==2")
    assert not satisfies(2, "!=2")
    assert satisfies(1, "<=1")
    assert satisfies(5, ">4")


def test_formats() -> None:
    assert matches_format("ACGTNacgtn", "dna_sequence")
    assert not matches_format("ACGU", "dna_sequence")
    assert matches_format("ACGU", "rna_sequence")
    assert matches_format("MKTAYIAK*", "protein_sequence")
    assert not matches_format("MKT1", "protein_sequence")
    assert matches_format("a@b.co", "email")
    assert matches_format("https://ikisci.org/x", "uri")
    assert not matches_format("no scheme", "uri")
    assert matches_format("lims.acme-pharma.example", "hostname")
    for host in ("LIMS.example.org", "lims.example.org:8443", "https://lims.example.org", "lims"):
        assert not matches_format(host, "hostname")
    # The same patterns go into database CHECK constraints.
    for pattern in FORMATS.values():
        assert pattern_problems(pattern) == []


def test_sizes() -> None:
    assert parse_size("1B") == 1
    assert parse_size("100MiB") == 100 * 1024**2
    assert parse_size("2GB") == 2 * 1000**3
    with pytest.raises(ValueError, match="1XB"):
        parse_size("1XB")


def test_field_labels_default_from_names() -> None:
    assert Field.from_manifest("gc_content", {"type": "float"}, plugin="dna").label == "Gc content"
    assert Field.from_manifest("x", {"type": "float", "label": "X!"}, plugin="dna").label == "X!"


def test_every_schema_is_valid_draft_2020_12() -> None:
    from jsonschema import Draft202012Validator

    for name in schemas.NAMES:
        schema = schemas.load(name)
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == schemas.url(name)


def test_the_manifest_schema_dispatches_on_kind() -> None:
    assert schemas.check("manifest", {"apiVersion": "iki/v1", "kind": "Plugin"})
    assert not schemas.check(
        "manifest",
        {"apiVersion": "iki/v1", "kind": "Plugin", "name": "acme/lab", "version": "1.0.0"},
    )
    issues = schemas.check("manifest", {"apiVersion": "iki/v1", "kind": "Nope"})
    assert [i.message for i in issues] == [
        "`kind` can't be `Nope`. Choose one of: `Plugin`, `EntityType`, `EntityTypeExtension`, "
        "`Action`, `Environment`, `Workflow`, `Trigger`, `Component`, `ActionTest`, "
        "`ReviewProcedure`."
    ]


def test_the_manifest_schema_reads_every_yaml_file_in_a_plugin() -> None:
    """An editor pointed at it for a whole plugin finds each of its files valid, its
    action tests' too."""
    root = Path(__file__).resolve().parents[2] / "plugins"
    files = sorted(root.glob("*/**/*.yaml"))
    tests = [f for f in files if f.parent.name == "tests"]
    assert tests
    for path in files:
        documents, problems = read_documents(path, path.name)
        assert not problems, path
        for document in documents:
            if isinstance(document.data, dict) and document.data.get("apiVersion") == "iki/v1":
                assert not schemas.check("manifest", document.data), path


# --- Lab roles (ADR 0116) ----------------------------------------------------------------------

ROLES = (
    PLUGIN
    + """\
roles:
  analyst: { label: Analyst, description: "  Tests samples.  " }
  qa: { label: QA }
"""
)


def test_a_plugin_declares_lab_roles(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE, ROLES)
    assert result.problems == []
    assert result.plugin is not None
    roles = result.plugin.roles
    assert list(roles) == ["acme/lab.analyst", "acme/lab.qa"]
    assert roles["acme/lab.analyst"].label == "Analyst"
    assert roles["acme/lab.analyst"].description == "Tests samples."
    assert roles["acme/lab.qa"].qualified_name == "acme/lab.qa"
    assert roles["acme/lab.qa"].description is None
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    "roles",
    [
        "admin: { label: Admin }",  # a project role's name
        "Qa: { label: QA }",
        "qa: {}",  # no label
        "qa: { label: QA, may: release }",
        "qa: QA",
    ],
)
def test_lab_roles_need_a_name_and_a_label(tmp_path: Path, roles: str) -> None:
    assert codes(make(tmp_path, TYPE, PLUGIN + f"roles:\n  {roles}\n")) == ["schema"]


def test_a_lab_plugins_starters_are_warned_of(tmp_path: Path) -> None:
    """A starter is a recipe the server has (ADR 0198), and only Iki's own plugins have
    any: a lab plugin's `starters` switch nothing on."""
    result = make(tmp_path, TYPE, PLUGIN + "starters:\n  buffer-release: 1.0.0\n")
    assert result.ok
    message = only(result, "unknown-starter")
    assert "`buffer-release`" in message
    assert result.problems[0].severity.value == "warning"
    assert "enum` setting" in (result.problems[0].hint or "")


def test_lab_roles_have_labels_of_their_own(tmp_path: Path) -> None:
    plugin = PLUGIN + "roles:\n  qa: { label: QA }\n  quality: { label: qa }\n"
    assert "both called" in only(make(tmp_path, TYPE, plugin), "duplicate-role-label")


# --- State fields (ADR 0125) -------------------------------------------------------------------

STATUS = """\
fields:
  status:
    type: state
    label: Status
    initial: received
    states:
      received: { label: Received, set_by: [analyst] }
      testing: { label: In testing, set_by: [analyst, qa], reason: optional }
      released: { label: Released, set_by: [qa], locks: true, reason: required }
  custody:
    type: state
    initial: in_lab
    states:
      in_lab: { set_by: [qa] }
      quarantined: { set_by: [qa], locks: true }
"""
RELEASED = "      released: { label: Released, set_by: [qa], locks: true, reason: required }\n"


def states(body: str = STATUS) -> str:
    return TYPE + body


def test_a_type_has_state_fields_each_with_its_states(tmp_path: Path) -> None:
    result = make(tmp_path, states(), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    status = assay.fields["status"]
    assert (status.type, status.initial) == ("state", "received")
    assert status.live_states == ("received", "testing", "released")
    assert status.locking_states == {"released"}
    released = status.states["released"]
    assert (released.label, released.set_by, released.reason) == (
        "Released",
        ("acme/lab.qa",),
        "required",
    )
    assert status.states["testing"].set_by == ("acme/lab.analyst", "acme/lab.qa")
    # A state's label defaults to its name, in words.
    assert assay.fields["custody"].states["in_lab"].label == "In lab"
    assert assay.fields["custody"].locking_states == {"quarantined"}
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_a_state_returns_to_another_on_edit(tmp_path: Path) -> None:
    body = STATUS.replace("reason: required }", "reason: required, on_edit: testing }")
    result = make(tmp_path, states(body), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    status = result.plugin.types["acme/lab.Assay"].fields["status"]
    assert status.states["received"].on_edit is None
    assert status.states["released"].on_edit == "testing"
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_only_an_upgrade_probe_accepts_on_edit_on_a_signed_lock(tmp_path: Path) -> None:
    body = STATUS.replace("reason: required", "signatures: [{ meaning: review }], on_edit: testing")
    current = make(tmp_path, states(body), ROLES)
    assert "requires an explicit signed return" in only(current, "state-on-edit")
    assert current.problems[0].path == ("fields", "status", "states", "released", "on_edit")
    assert load_plugin(tmp_path / "acme", historical=True).problems == []


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (STATUS.replace("reason: required", "on_edit: gone"), "unknown-state", "`gone`"),
        (
            STATUS.replace("reason: optional", "on_edit: received"),
            "state-on-edit",
            "must lock",
        ),
        (
            STATUS.replace("reason: required", "on_edit: released"),
            "state-on-edit",
            "different state",
        ),
        (
            STATUS.replace("reason: optional", "locks: true, on_edit: released"),
            "state-on-edit",
            "locks the record",
        ),
        (
            STATUS.replace("reason: required", "on_edit: received").replace(
                "Received, set_by: [analyst]", "Received"
            ),
            "state-on-edit",
            "no lab role",
        ),
        (
            STATUS.replace("reason: required", "on_edit: testing").replace(
                "reason: optional", "signatures: [{ meaning: review }]"
            ),
            "state-on-edit",
            "requires a signature",
        ),
        (
            STATUS.replace(
                RELEASED, RELEASED + "      old: { removed: true, moved_to: testing }\n"
            ).replace("reason: required", "on_edit: old"),
            "unknown-state",
            "`old`",
        ),
        (
            STATUS.replace(
                RELEASED,
                RELEASED + "      old: { removed: true, moved_to: testing, on_edit: testing }\n",
            ),
            "state-on-edit",
            "removed",
        ),
    ],
)
def test_on_edit_names_a_state_the_editor_can_set(
    tmp_path: Path, body: str, code: str, message: str
) -> None:
    assert message in only(make(tmp_path, states(body), ROLES), code)


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (STATUS.replace("initial: received", "initial: queued"), "unknown-state", "`queued`"),
        (
            STATUS.replace("initial: received", "initial: released"),
            "state-initial",
            "can't lock them",
        ),
        (
            STATUS.replace("set_by: [analyst] }", "set_by: [chemist] }"),
            "unknown-role",
            "no lab role `chemist`",
        ),
        (
            STATUS.replace(RELEASED, RELEASED + "      lost: { label: Lost }\n"),
            "state-unreachable",
            "No one can set `status` to `lost`",
        ),
        (
            STATUS.replace(
                RELEASED,
                RELEASED + "      old: { label: Old, removed: true, moved_to: gone }\n",
            ),
            "unknown-state",
            "`gone`",
        ),
        (
            STATUS.replace(RELEASED, RELEASED + "      old: { label: Old, removed: true }\n"),
            "state-removed",
            "say which, with `moved_to`",
        ),
        (
            STATUS.replace(
                RELEASED,
                RELEASED + "      old: { removed: true, moved_to: received, locks: true }\n",
            ),
            "state-removed",
            "no record is in it to lock",
        ),
        (
            STATUS.replace(
                "{ label: Received, set_by: [analyst] }",
                "{ label: Received, removed: true, moved_to: testing }",
            ),
            "state-initial",
            "can't be removed",
        ),
        (
            STATUS.replace("set_by: [analyst] }", "set_by: [analyst], moved_to: testing }"),
            "state-removed",
            "`moved_to` is for a removed state",
        ),
        (
            STATUS.replace(
                "{ label: Received, set_by: [analyst] }",
                "{ label: Received, set_by: [analyst], signatures: [{ meaning: authorship }] }",
            ),
            "state-signatures",
            "where new records start, without signatures",
        ),
        (
            STATUS.replace(
                RELEASED,
                RELEASED
                + "      old: { removed: true, moved_to: received,"
                + " signatures: [{ meaning: review }] }\n",
            ),
            "state-signatures",
            "signed for by no one",
        ),
        (
            STATUS.replace(
                "reason: required }",
                "reason: required, signatures: [{ meaning: approval, role: chemist }] }",
            ),
            "unknown-role",
            "no lab role `chemist`",
        ),
    ],
)
def test_state_fields_are_checked(tmp_path: Path, body: str, code: str, message: str) -> None:
    assert message in only(make(tmp_path, states(body), ROLES), code)


@pytest.mark.parametrize(
    "body",
    [
        STATUS.replace("    initial: received\n", ""),  # no initial state
        STATUS.replace("set_by: [qa] }", "set_by: [] }"),  # set by no one, explicitly
        STATUS.replace("reason: optional", "reason: maybe"),
        STATUS.replace("received:", "Received:"),
        STATUS.replace(
            "{ set_by: [qa], locks: true }", "{ set_by: [qa], locks: true, not_by: [in_lab] }"
        ),
        STATUS.replace(
            "    states:\n      in_lab: { set_by: [qa] }\n", "    states:\n"
        ),  # one state
        STATUS.replace("type: state\n    label: Status", "type: state\n    required: true"),
        # Signatures: a meaning Part 11 names, at most five, nothing else.
        STATUS.replace("reason: required }", "reason: required, signatures: [{ meaning: ok }] }"),
        STATUS.replace("reason: required }", "reason: required, signatures: [] }"),
        STATUS.replace("reason: required }", "reason: required, signatures: [{ role: qa }] }"),
        STATUS.replace(
            "reason: required }",
            "reason: required, signatures: [" + ", ".join(["{ meaning: review }"] * 6) + "] }",
        ),
        STATUS.replace(
            "reason: required }",
            "reason: required, signatures: [{ meaning: review, not_by: [testing] }] }",
        ),
        STATUS.replace(
            "reason: required }",
            'reason: required, signatures: [{ meaning: review, statement: "" }] }',
        ),
    ],
)
def test_state_fields_must_be_well_formed(tmp_path: Path, body: str) -> None:
    assert "schema" in codes(make(tmp_path, states(body), ROLES))


def test_on_edit_requires_a_state_name(tmp_path: Path) -> None:
    body = STATUS.replace("reason: optional", "on_edit: [received]")
    assert "schema" in codes(make(tmp_path, states(body), ROLES))


def test_a_state_can_need_signatures_each_with_its_meaning(tmp_path: Path) -> None:
    body = STATUS.replace(
        "reason: required }",
        "reason: required, signatures: [{ meaning: review }, { meaning: approval, role: "
        "analyst, label: Analyst approval, statement: I approve it. }] }",
    )
    result = make(tmp_path, states(body), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    released = result.plugin.types["acme/lab.Assay"].fields["status"].states["released"]
    review, approval = released.signatures
    # A meaning's label and statement are its own where the plugin gives none (21 CFR
    # 11.50), and a signature without a role is given by one of the state's.
    assert (review.meaning, review.role, review.label) == ("review", None, "Review")
    assert review.statement == SIGNATURE_MEANINGS["review"].statement
    assert (approval.role, approval.label, approval.statement) == (
        "acme/lab.analyst",
        "Analyst approval",
        "I approve it.",
    )
    assert result.plugin.types["acme/lab.Assay"].fields["status"].states["testing"].signatures == ()
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_a_removed_state_says_where_its_records_go(tmp_path: Path) -> None:
    body = STATUS.replace(
        RELEASED, RELEASED + "      queued: { label: Queued, removed: true, moved_to: received }\n"
    )
    result = make(tmp_path, states(body), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    status = result.plugin.types["acme/lab.Assay"].fields["status"]
    assert "queued" not in status.live_states
    assert status.states["queued"].moved_to == "received"


CUSTODY_MOVED = """\
    removed: true
    moved_to:
      field: status
      states: { in_lab: received, quarantined: released }
"""


def test_a_removed_state_field_says_where_its_records_states_go(tmp_path: Path) -> None:
    body = STATUS + "    " + CUSTODY_MOVED.strip().replace("\n", "\n    ") + "\n"
    body = STATUS.replace(
        "  custody:\n    type: state\n", "  custody:\n    type: state\n" + CUSTODY_MOVED
    )
    result = make(tmp_path, states(body), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    custody = result.plugin.types["acme/lab.Assay"].fields["custody"]
    assert custody.removed
    assert custody.moved_to is not None
    assert custody.moved_to.field == "status"
    assert dict(custody.moved_to.states) == {"in_lab": "received", "quarantined": "released"}
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin
    # Without moved_to it's allowed: the migration refuses while records have it.
    alone = STATUS.replace(
        "  custody:\n    type: state\n", "  custody:\n    type: state\n    removed: true\n"
    )
    assert make(tmp_path, states(alone), ROLES).problems == []


@pytest.mark.parametrize(
    ("moved", "code", "message"),
    [
        ("{ in_lab: received }", "state-removed", "records in `quarantined`"),
        ("{ in_lab: received, quarantined: lost }", "unknown-state", "`lost`"),
        (
            "{ in_lab: received, quarantined: released, gone: received }",
            "unknown-state",
            "`gone`",
        ),
    ],
)
def test_a_removed_state_fields_moves_are_checked(
    tmp_path: Path, moved: str, code: str, message: str
) -> None:
    body = STATUS.replace(
        "  custody:\n    type: state\n",
        "  custody:\n    type: state\n"
        + CUSTODY_MOVED.replace("{ in_lab: received, quarantined: released }", moved),
    )
    assert message in only(make(tmp_path, states(body), ROLES), code)


def test_a_removed_state_field_moves_to_another_of_its_types(tmp_path: Path) -> None:
    body = STATUS.replace(
        "  custody:\n    type: state\n",
        "  custody:\n    type: state\n" + CUSTODY_MOVED.replace("field: status", "field: custody"),
    )
    assert "own state fields" in only(make(tmp_path, states(body), ROLES), "unknown-field")
    body = (
        STATUS.replace(
            "  custody:\n    type: state\n",
            "  custody:\n    type: state\n"
            + CUSTODY_MOVED.replace("field: status", "field: notes"),
        )
        + "  notes: { type: text }\n"
    )
    assert "own state fields" in only(make(tmp_path, states(body), ROLES), "unknown-field")


def test_state_fields_are_a_types_own(tmp_path: Path) -> None:
    items = "{ type: state, initial: one, states: { one: {}, two: { set_by: [qa] } } }"
    listed = TYPE + f"fields:\n  stages:\n    type: list\n    items: {items}\n"
    assert "can't be states" in only(make(tmp_path, listed, ROLES), "state-field-in-list")
    stage = "stage:\n  type: state\n  initial: one\n  states:\n    one: {}\n    two: {}\n"
    result = make(tmp_path, extension(stage), DNA_PIN, with_=(DNA,))
    assert "an extension can't add one" in only(result, "state-field-in-extension")


def test_a_subtype_has_its_parents_state_fields(tmp_path: Path) -> None:
    child = (
        "apiVersion: iki/v1\nkind: EntityType\nname: Elisa\nextends: Assay\n"
        "schema_version: 1\nhrid: { prefix: ELI }\n"
        "fields:\n  plate:\n    type: state\n    initial: open\n"
        "    states:\n      open: {}\n      read: { set_by: [analyst] }\n"
    )
    result = make(tmp_path, {"types/assay.yaml": states(), "types/elisa.yaml": child}, ROLES)
    assert result.problems == []


# --- People in records (ADR 0122) --------------------------------------------------------------

PEOPLE = """\
fields:
  due_on: { type: date }
  analyst:
    type: person
    role: analyst
    assignment: { field: status, states: [received, testing] }
  helpers: { type: list, items: { type: person }, assignment: true, max_items: 5 }
  received_by: { type: person }
due: due_on
"""
#: The state field (ADR 0125) the analyst's work follows.
PEOPLE_STATES = STATUS.removeprefix("fields:\n")


def people(body: str = PEOPLE, machine: str = PEOPLE_STATES) -> str:
    """A type of people fields, with its state fields after them."""
    head, due = body.split("due: ", 1)
    return TYPE + head + machine + "due: " + due


def test_fields_name_people_and_assign_records_to_them(tmp_path: Path) -> None:
    result = make(tmp_path, people(), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    analyst = assay.fields["analyst"]
    assert (analyst.type, analyst.role, analyst.assignment) == ("person", "acme/lab.analyst", True)
    assert (analyst.assignment_field, analyst.assignment_states) == (
        "status",
        ("received", "testing"),
    )
    helpers = assay.fields["helpers"]
    assert helpers.items is not None and helpers.items.type == "person"
    assert (helpers.assignment, helpers.assignment_field, helpers.assignment_states) == (
        True,
        None,
        (),
    )
    assert not assay.fields["received_by"].assignment
    assert assay.due == "due_on"
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    ("body", "machine", "code", "message"),
    [
        (
            PEOPLE.replace("role: analyst", "role: chemist"),
            PEOPLE_STATES,
            "unknown-role",
            "no lab role `chemist`",
        ),
        (
            PEOPLE.replace("states: [received, testing]", "states: [received, tested]"),
            PEOPLE_STATES,
            "unknown-state",
            "`tested`",
        ),
        (PEOPLE, "", "assignment-without-states", "can't be assigned only in some"),
        (
            PEOPLE.replace("field: status", "field: stats"),
            PEOPLE_STATES,
            "assignment-without-states",
            "`stats` isn't a state field",
        ),
        (
            PEOPLE.replace("field: status", "field: due_on"),
            PEOPLE_STATES,
            "assignment-without-states",
            "`due_on` isn't a state field",
        ),
        (
            PEOPLE.replace(
                "due: due_on",
                "  tags: { type: list, items: { type: string }, assignment: true }\ndue: due_on",
            ),
            PEOPLE_STATES,
            "assignment-not-people",
            "only a field naming people",
        ),
        (
            PEOPLE.replace("due: due_on", "due: due_by"),
            PEOPLE_STATES,
            "unknown-field",
            "`due_by`",
        ),
        (
            PEOPLE.replace("due: due_on", "due: received_by"),
            PEOPLE_STATES,
            "due-not-a-date",
            "what's due is due on a date",
        ),
    ],
)
def test_people_fields_are_checked(
    tmp_path: Path, body: str, machine: str, code: str, message: str
) -> None:
    assert message in only(make(tmp_path, people(body, machine), ROLES), code)


@pytest.mark.parametrize(
    "body",
    [
        PEOPLE.replace("assignment: true", "assignment: { field: status, states: [] }"),
        PEOPLE.replace("{ field: status, states", "{ states"),
        PEOPLE.replace("assignment: true", "assignment: always"),
        PEOPLE.replace("due: due_on", "  code: { type: string, assignment: true }\ndue: due_on"),
        PEOPLE.replace("received_by: { type: person }", "received_by: { type: string, role: qa }"),
        PEOPLE.replace("{ type: person }", "{ type: person, min_length: 1 }"),
        PEOPLE.replace(
            "received_by: { type: person }", "received_by: { type: person, default: x }"
        ),
    ],
)
def test_people_fields_must_be_well_formed(tmp_path: Path, body: str) -> None:
    assert "schema" in codes(make(tmp_path, people(body), ROLES))


def test_people_are_each_projects_not_a_registrys(tmp_path: Path) -> None:
    body = (
        TYPE
        + "identity: [code]\nfields:\n  code: { type: string, required: true, immutable: true }\n"
        + "  owner: { type: person, registry: fact }\n"
    )
    assert "can't be its registry entry's" in only(make(tmp_path, body), "person-in-registry")


def test_fields_named_iki_are_reserved(tmp_path: Path) -> None:
    body = TYPE + "fields:\n  iki_state: { type: string }\n"
    assert "`iki_state` is reserved" in only(make(tmp_path, body), "reserved-name")


# --- Notifications a type declares (ADR 0129) --------------------------------------------------
#: A type with people, states and a reference, then the notifications under test.
BASE = PEOPLE.replace(
    "  received_by: { type: person }\n",
    "  received_by: { type: person }\n"
    "  result: { type: enum, values: [pass, fail] }\n"
    "  lot: { type: string }\n"
    "  sent_at: { type: datetime }\n"
    "  passed: { type: bool }\n"
    "  notes: { type: json }\n"
    "  parent: { type: ref, to: Assay }\n",
)
NOTIFICATIONS = """\
notifications:
  released:
    label: An assay is released
    description: QA and its analyst hear when QA releases an assay.
    when: { field: status, to: released }
    tell: { roles: [qa], fields: [analyst, helpers], creator: true }
    message:
      title: "{{ hrid }} is released"
      body: "Lot {{ lot or 'unknown' }}: its result is {{ result }}, by {{ analyst }}."
    required: true
  out_of_testing:
    label: Out of testing
    when: { field: status, from: [testing] }
    where:
      all:
        - { field: sent_at, op: gte, value: "{{ now }}" }
        - { field: result, op: in, value: [fail] }
        - { field: received_by, op: is_set, value: true }
    tell: { watchers: true, actor: true }
    email: false
  failed:
    label: A result failed
    when: { field: result, to: [fail] }
    tell: { fields: [received_by] }
  parent_moved:
    label: Its parent moved
    when: { field: parent.custody, to: quarantined }
    tell: { fields: [received_by] }
  made:
    label: An assay is made
    when: { created: true }
    tell: { roles: [analyst] }
"""


def declared(body: str = NOTIFICATIONS) -> str:
    return people(BASE.replace("due: due_on", "due: due_on\n" + body.rstrip("\n")))


def test_a_type_declares_its_notifications(tmp_path: Path) -> None:
    result = make(tmp_path, declared(), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    released = assay.notifications["released"]
    assert (released.label, released.field, released.to_values) == (
        "An assay is released",
        "status",
        ("released",),
    )
    assert released.tell_roles == ("acme/lab.qa",)
    assert released.tell_fields == ("analyst", "helpers")
    assert (released.tell_creator, released.tell_watchers, released.tell_actor) == (
        True,
        False,
        False,
    )
    assert (released.email, released.required) == (True, True)
    assert released.title == "{{ hrid }} is released"
    leaving = assay.notifications["out_of_testing"]
    assert (leaving.from_values, leaving.to_values, leaving.email) == (("testing",), (), False)
    assert leaving.where is not None
    moved = assay.notifications["parent_moved"]
    assert (moved.ref, moved.target) == ("parent", "custody")
    assert assay.notifications["made"].created
    # The label defaults to the name, in words.
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


# --- Requests between projects (ADR 0132) ------------------------------------------------------

REQUEST = """\
apiVersion: iki/v1
kind: EntityType
name: AssayRequest
schema_version: 1
hrid: { prefix: REQ }
request:
  service_fields: [status, priority, scheduled_on, technician]
  sends: [samples]
fields:
  title: { type: string, required: true, max_length: 200 }
  samples: { type: list, items: { type: ref, to: Sample }, max_items: 96 }
  needed_by: { type: date }
  priority: { type: enum, values: [routine, urgent], default: routine }
  scheduled_on: { type: date }
  technician: { type: person, role: analyst, assignment: { field: status, states: [accepted] } }
  status:
    type: state
    initial: submitted
    states:
      submitted: { label: Submitted, set_by: [qa] }
      accepted: { label: Accepted, set_by: [analyst] }
      delivered: { label: Delivered, set_by: [analyst], locks: true }
"""
RESULT = """\
apiVersion: iki/v1
kind: EntityType
name: AssayResult
schema_version: 1
hrid: { prefix: RES }
result:
  of: request
  released: { field: review, states: [released] }
fields:
  request: { type: ref, to: AssayRequest, required: true, immutable: true }
  value: { type: float }
  review:
    type: state
    initial: draft
    states:
      draft: { label: Draft, set_by: [analyst] }
      released: { label: Released, set_by: [qa], locks: true }
"""
SAMPLE_TYPE = """\
apiVersion: iki/v1
kind: EntityType
name: Sample
schema_version: 1
hrid: { prefix: SAM }
fields:
  label: { type: string }
"""
#: The request's and result's references as a result names them.
REQUEST_REF = "to: AssayRequest, required: true, immutable: true"


def requests(request: str = REQUEST, result: str = RESULT) -> dict[str, str]:
    return {
        "types/request.yaml": request,
        "types/result.yaml": result,
        "types/sample.yaml": SAMPLE_TYPE,
    }


def test_signed_release_after_review_requires_a_lock(tmp_path: Path) -> None:
    body = RESULT.replace(
        "      released: { label: Released, set_by: [qa], locks: true }",
        "      reviewed: { set_by: [analyst], signatures: [{ meaning: review }] }\n"
        "      released: { set_by: [qa], signatures: [{ meaning: approval }], locks: true }",
    )
    result = make(tmp_path, requests(result=body), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.AssayResult"]
    assert assay.result is not None
    assert assay.result.requires_review(assay.fields["review"], "released")
    assert not assay.result.requires_review(assay.fields["review"], "reviewed")
    assert not assay.result.requires_review(assay.fields["value"], "released")
    unlocked = body.replace(", locks: true", "")
    assert "must lock" in only(
        make(tmp_path, requests(result=unlocked), ROLES), "result-release-lock"
    )


def test_on_edit_cannot_change_or_release_a_result(tmp_path: Path) -> None:
    released = RESULT.replace("locks: true", "locks: true, on_edit: draft")
    assert "already released" in only(
        make(tmp_path, requests(result=released), ROLES), "result-release-on-edit"
    )
    into_release = RESULT.replace("locks: true", "locks: false").replace(
        "      draft:",
        "      reviewed: { set_by: [qa], locks: true, on_edit: released }\n      draft:",
    )
    assert "release it" in only(
        make(tmp_path, requests(result=into_release), ROLES), "result-release-on-edit"
    )


def test_a_request_type_names_its_services_fields_and_what_it_sends(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        requests(
            REQUEST.replace(
                "  sends:", "  completed: { field: status, state: delivered }\n  sends:"
            )
        ),
        ROLES,
    )
    assert result.problems == []
    assert result.plugin is not None
    request = result.plugin.types["acme/lab.AssayRequest"].request
    assert request is not None
    assert request.service_fields == ("status", "priority", "scheduled_on", "technician")
    assert request.sends == ("samples",)
    assert (request.completed_field, request.completed_state) == ("status", "delivered")
    found = result.plugin.types["acme/lab.AssayResult"].result
    assert found is not None
    assert (found.of, found.released_field, found.released_states) == (
        "request",
        "review",
        ("released",),
    )
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


WORK_ITEM = """\
apiVersion: iki/v1
kind: EntityType
name: WorkItem
schema_version: 1
hrid: { prefix: WRK }
work_item: { of: request }
fields:
  request: { type: ref, to: AssayRequest, required: true, immutable: true }
"""


def test_work_items_round_trip_without_a_release_policy(tmp_path: Path) -> None:
    result = make(tmp_path, requests() | {"types/work_item.yaml": WORK_ITEM}, ROLES)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.types["acme/lab.WorkItem"]
    assert found.work_item is not None and found.work_item.of == "request"
    assert found.result is None
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            WORK_ITEM.replace(REQUEST_REF, "to: Sample, required: true, immutable: true"),
            "work-item-of-not-a-request",
        ),
        (WORK_ITEM.replace(REQUEST_REF, "to: AssayRequest"), "work-item-of-changes"),
        (
            WORK_ITEM.replace(
                "work_item: { of: request }", "extends: AssayResult\nwork_item: { of: request }"
            ).replace(
                f"fields:\n  request: {{ type: ref, {REQUEST_REF} }}\n",
                "",
            ),
            "work-item-conflict",
        ),
    ],
)
def test_work_items_require_one_request_and_cannot_be_results(
    tmp_path: Path, body: str, code: str
) -> None:
    assert only(make(tmp_path, requests() | {"types/work_item.yaml": body}, ROLES), code)


@pytest.mark.parametrize(
    ("old", "new", "code", "message"),
    [
        ("field: status, to: released", "field: stats", "unknown-field", "`stats`"),
        ("to: released }", "to: relased }", "unknown-state", "`relased`"),
        ("from: [testing]", "from: [tested]", "unknown-state", "`tested`"),
        ("to: [fail]", "to: [failed]", "notification-values", "`failed`"),
        ("result, to: [fail]", "lot, to: [fail]", "notification-values", "only a state field"),
        ("roles: [qa], fields", "roles: [chemist], fields", "unknown-role", "`chemist`"),
        ("fields: [analyst, helpers]", "fields: [analyst, lot]", "notification-recipients", "lot"),
        ("fields: [received_by]", "fields: [reciever]", "unknown-field", "`reciever`"),
        ("parent.custody", "parent.custard", "unknown-field", "`custard`"),
        ("parent.custody", "lot.custody", "notification-field", "a reference to one record"),
        ("{{ hrid }} is released", "{{ hird }} is released", "notification-message", "`hird`"),
        ("by {{ analyst }}", "by {{ notes }}", "notification-message", "a json field"),
        ("{{ result }}", "{{ result, }}", "notification-message", "where `or` belongs"),
        (
            'value: "{{ now }}" }',
            'value: "{{ me }}" }',
            "notification-where",
            "`{{ now }}`",
        ),
        (
            "{ field: result, op: in, value: [fail] }",
            "{ field: result, op: contains, value: fail }",
            "unknown-operator",
            "`result` takes",
        ),
        (
            "{ field: result, op: in, value: [fail] }",
            "{ field: result, op: in, value: [failed] }",
            "notification-where",
            "no value `failed`",
        ),
        (
            "{ field: received_by, op: is_set, value: true }",
            "{ field: received_by, op: eq, value: x }",
            "notification-where",
            "only with `is_set`",
        ),
        (
            "{ field: sent_at, op: gte",
            "{ field: lot, op: gte",
            "unknown-operator",
            "`lot` takes",
        ),
        ("when: { created: true }", "when: { created: true, field: lot }", "notification-when", ""),
        ("when: { created: true }", "when: {}", "notification-when", "Say when"),
    ],
)
def test_notifications_are_checked(
    tmp_path: Path, old: str, new: str, code: str, message: str
) -> None:
    assert old in NOTIFICATIONS
    assert message in only(
        make(tmp_path, declared(NOTIFICATIONS.replace(old, new, 1)), ROLES), code
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("tell: { roles: [analyst] }", "tell: {}"),
        ("    label: An assay is made\n", ""),
        ("email: false", "email: no"),
        ("  made:", "  Made:"),
        ("when: { created: true }", "when: { created: false }"),
        ("{{ hrid }} is released", ""),
        ("tell: { roles: [analyst] }", "tell: { everyone: true }"),
    ],
)
def test_notifications_must_be_well_formed(tmp_path: Path, old: str, new: str) -> None:
    assert old in NOTIFICATIONS
    assert "schema" in codes(make(tmp_path, declared(NOTIFICATIONS.replace(old, new, 1)), ROLES))


def test_a_notification_repeating_its_ready_for_you_is_warned_of(tmp_path: Path) -> None:
    body = """\
notifications:
  testing:
    label: Testing
    when: { field: status, to: testing }
    tell: { fields: [analyst] }
"""
    result = make(tmp_path, declared(body), ROLES)
    (problem,) = result.problems
    assert (problem.code, problem.severity) == ("notification-repeats-ready", Severity.WARNING)
    assert "told it's ready for them already" in problem.message


def test_a_subtype_cant_redeclare_its_parents_notification(tmp_path: Path) -> None:
    child = """\
apiVersion: iki/v1
kind: EntityType
name: Repeat
extends: Assay
schema_version: 1
hrid: { prefix: RPT }
notifications:
  made:
    label: Made again
    when: { created: true }
    tell: { creator: true }
"""
    result = make(tmp_path, {"types/assay.yaml": declared(), "types/repeat.yaml": child}, ROLES)
    assert "`acme/lab.Assay`" in only(result, "notification-shadows-parent")


def test_registry_fields_cant_send_one(tmp_path: Path) -> None:
    body = """\
notifications:
  renamed:
    label: Renamed
    when: { field: code }
    tell: { creator: true }
"""
    text = declared(body).replace(
        "fields:\n",
        "identity: [code]\nfields:\n  code: { type: string, immutable: true, required: true }\n",
        1,
    )
    assert "registry" in only(make(tmp_path, text, ROLES), "notification-in-registry")


@pytest.mark.parametrize(
    ("request_", "result_", "code", "message"),
    [
        (
            REQUEST.replace("[status, priority", "[status, prio"),
            RESULT,
            "unknown-field",
            "`prio` isn't a field",
        ),
        (
            REQUEST.replace("sends: [samples]", "sends: [title]"),
            RESULT,
            "request-sends-not-a-reference",
            "a string field",
        ),
        (
            REQUEST.replace("sends: [samples]", "sends: [samples, technician]"),
            RESULT,
            "request-sends-not-a-reference",
            "a person field",
        ),
        (
            REQUEST.replace("[status, priority,", "[status, samples, priority,").replace(
                "sends: [samples]", "sends: []"
            ),
            RESULT,
            "service-field-refers",
            "the service sends what it makes back as results",
        ),
        (
            REQUEST.replace("[status, priority,", "[status, title, priority,"),
            RESULT,
            "service-field-required",
            "creates the request without the service's fields",
        ),
        (
            REQUEST.replace("[status, priority,", "[status, code, priority,").replace(
                "  needed_by:",
                "  code: { type: string, unique: true, indexed: true, max_length: 40 }\n"
                "  needed_by:",
            ),
            RESULT,
            "service-field-unique",
            "would learn what that project's other records hold",
        ),
        (
            REQUEST.replace(
                "scheduled_on: { type: date }", "scheduled_on: { type: date, immutable: true }"
            ),
            RESULT,
            "service-field-immutable",
            "no way to correct it",
        ),
        (
            REQUEST.replace("request:\n", "identity: [title]\nrequest:\n").replace(
                "required: true, max_length", "required: true, immutable: true, max_length"
            ),
            RESULT,
            "request-with-identity",
            "the asking project's own record",
        ),
        (
            REQUEST,
            RESULT.replace(
                "released: { field: review, states: [released] }",
                "released: { field: value, states: [x] }",
            ),
            "result-released-without-states",
            "`value` isn't a state field",
        ),
        (
            REQUEST,
            RESULT.replace("states: [released] }", "states: [draft] }"),
            "result-released-initial",
            "they'd go back before anyone reviewed them",
        ),
        (
            REQUEST,
            RESULT.replace("states: [released] }", "states: [gone] }"),
            "unknown-state",
            "`gone`",
        ),
        (
            REQUEST,
            RESULT.replace(REQUEST_REF, "to: Sample, required: true, immutable: true"),
            "result-of-not-a-request",
            "must refer to a request type",
        ),
        (
            REQUEST.replace(
                "  sends:", "  completed: { field: status, state: submitted }\n  sends:"
            ),
            RESULT,
            "request-completion-state",
            "non-initial state",
        ),
        (
            REQUEST.replace("  sends:", "  completed: { field: title, state: absent }\n  sends:"),
            RESULT,
            "request-completion-state",
            "service writes",
        ),
        (
            REQUEST,
            RESULT.replace(REQUEST_REF, "to: AssayRequest"),
            "result-of-changes",
            "a result is of one request, for good",
        ),
    ],
)
def test_requests_and_results_are_checked(
    tmp_path: Path, request_: str, result_: str, code: str, message: str
) -> None:
    assert message in only(make(tmp_path, requests(request_, result_), ROLES), code)


REQUEST_NOTIFICATIONS = """\
notifications:
  received:
    label: A request arrives
    when: { sent: true }
    tell: { roles: [qa] }
    message: { title: "{{ hrid }} arrived: {{ title }}" }
  answered:
    label: Its results are released
    when: { released: true }
    tell: { creator: true, watchers: true }
"""


def test_a_request_tells_its_service_and_who_asked(tmp_path: Path) -> None:
    """ADR 0142: as it's sent, its service, by the lab roles held there; as a result of it
    is released, the project that asked."""
    result = make(tmp_path, requests(REQUEST + REQUEST_NOTIFICATIONS), ROLES)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.types["acme/lab.AssayRequest"].notifications
    assert (found["received"].sent, found["received"].released) == (True, False)
    assert found["received"].tell_roles == ("acme/lab.qa",)
    assert (found["answered"].sent, found["answered"].released) == (False, True)


@pytest.mark.parametrize(
    ("old", "new", "code", "message"),
    [
        (
            "tell: { roles: [qa] }",
            "tell: { roles: [qa], creator: true }",
            "notification-recipients",
            "by the lab roles its people hold there: name them in `roles`, not `creator`.",
        ),
        (
            "tell: { roles: [qa] }",
            "tell: { watchers: true }",
            "notification-recipients",
            "name them in `roles`, not `watchers`.",
        ),
        (
            "when: { sent: true }",
            "when: { sent: true, field: title }",
            "notification-when",
            "not `sent` and `field`",
        ),
        (
            "when: { released: true }",
            "when: { released: true, created: true }",
            "notification-when",
            "not `created` and `released`",
        ),
    ],
)
def test_a_requests_notifications_are_checked(
    tmp_path: Path, old: str, new: str, code: str, message: str
) -> None:
    body = REQUEST + REQUEST_NOTIFICATIONS.replace(old, new, 1)
    assert message in only(make(tmp_path, requests(body), ROLES), code)


@pytest.mark.parametrize("event", ["sent", "released"])
def test_only_a_request_is_sent_or_has_results_released(tmp_path: Path, event: str) -> None:
    body = SAMPLE_TYPE + (
        f"notifications:\n  it:\n    label: It\n    when: {{ {event}: true }}\n"
        "    tell: { roles: [qa] }\n"
    )
    files = {**requests(), "types/sample.yaml": body}
    found = only(make(tmp_path, files, ROLES), "notification-when")
    assert f"`{event}` is a request's, and `Sample` isn't a request type." in found


def test_a_request_types_subtypes_are_requests_with_its_service(tmp_path: Path) -> None:
    sub = """\
apiVersion: iki/v1
kind: EntityType
name: UrgentRequest
schema_version: 1
extends: AssayRequest
hrid: { prefix: URQ }
request:
  service_fields: [status]
"""
    files = {**requests(), "types/urgent.yaml": sub}
    # It adds service fields of its own (ADR 0232), not its parent's.
    found = only(make(tmp_path, files, ROLES), "service-field-inherited")
    assert found.startswith("`status` is ")
    own = sub + "fields:\n  queue_rank: { type: int }\n"
    files["types/urgent.yaml"] = own.replace("[status]", "[queue_rank]")
    assert make(tmp_path, files, ROLES).problems == []
    # Its parent says the rest.
    files["types/urgent.yaml"] = own.replace("[status]", "[queue_rank]\n  sends: [samples]")
    message = only(make(tmp_path, files, ROLES), "request-redeclared")
    assert "can only add service fields of their own" in message
    files["types/urgent.yaml"] = own.replace("[status]", "[]")
    assert "request-redeclared" in codes(make(tmp_path, files, ROLES))
    # A service field of its own is held to a service field's rules.
    files["types/urgent.yaml"] = own.replace("[status]", "[queue_rank]").replace(
        "{ type: int }", "{ type: int, required: true }"
    )
    assert only(make(tmp_path, files, ROLES), "service-field-required")
    files["types/urgent.yaml"] = sub.replace("request:\n  service_fields: [status]\n", "")
    assert make(tmp_path, files, ROLES).problems == []


def test_a_service_writes_only_the_request_types_own_fields(tmp_path: Path) -> None:
    ticket = """\
apiVersion: iki/v1
kind: EntityType
name: Ticket
schema_version: 1
abstract: true
fields:
  notes: { type: text }
"""
    request = REQUEST.replace("schema_version: 1\n", "schema_version: 1\nextends: Ticket\n", 1)
    request = request.replace("[status, priority,", "[status, notes, priority,")
    files = {**requests(request), "types/ticket.yaml": ticket}
    found = only(make(tmp_path, files, ROLES), "service-field-inherited")
    assert "`notes` is `acme/lab.Ticket`'s field" in found


@pytest.mark.parametrize(
    "request_",
    [
        REQUEST.replace("sends: [samples]", "sends: samples"),
        REQUEST.replace("sends: [samples]", "sends: [samples]\n  queue: true"),
        REQUEST.replace("[status, priority, scheduled_on, technician]", "[status, status]"),
    ],
)
def test_request_blocks_must_be_well_formed(tmp_path: Path, request_: str) -> None:
    assert "schema" in codes(make(tmp_path, requests(request_), ROLES))


@pytest.mark.parametrize(
    "result_",
    [
        RESULT.replace("  released: { field: review, states: [released] }\n", ""),
        RESULT.replace("states: [released] }", "states: [] }"),
        RESULT.replace("  of: request\n", ""),
    ],
)
def test_result_blocks_must_be_well_formed(tmp_path: Path, result_: str) -> None:
    assert "schema" in codes(make(tmp_path, requests(result=result_), ROLES))


TUBE = """\
amount:
  unit: uL
  label: Volume
  concentration: concentration
  aliquots: { type: Aliquot, of: parent }
fields:
  concentration: { type: quantity, unit: mg/mL }
  status:
    type: state
    initial: quarantined
    states:
      quarantined: { holds_amount: true }
      released: { set_by: [qa] }
---
apiVersion: iki/v1
kind: EntityType
name: Aliquot
schema_version: 1
hrid: { prefix: ALQ }
amount: { unit: uL }
fields:
  parent: { type: ref, to: Assay }
"""


def test_a_draw_destination_declares_one_computed_receipt_field(tmp_path: Path) -> None:
    parent = (
        TYPE.replace("hrid: { prefix: ASY }", "abstract: true")
        + """
draws_received: material_draws
fields:
  material_draws: { type: json, computed: true }
"""
    )
    child = """
---
apiVersion: iki/v1
kind: EntityType
name: Experiment
schema_version: 1
extends: Assay
hrid: { prefix: EXPT }
"""
    result = make(tmp_path, parent + child)
    assert result.problems == []
    assert result.plugin is not None
    restored = plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin))))
    assert restored.types["acme/lab.Assay"].draws_received == "material_draws"
    assert restored.types["acme/lab.Experiment"].extends == "acme/lab.Assay"
    assert codes(make(tmp_path, parent + child + "draws_received: other\n")) == [
        "draws-received-field"
    ]
    for declaration in (
        "type: text, computed: true",
        "type: json",
        "type: json, computed: true, immutable: true",
    ):
        assert "draws-received-field" in codes(
            make(tmp_path, parent.replace("type: json, computed: true", declaration))
        )
    assert "draws-received-field" in codes(
        make(tmp_path, parent.replace("draws_received: material_draws", "draws_received: missing"))
    )


def test_stock_catalogue_checks_and_roundtrips_its_project_fields(tmp_path: Path) -> None:
    catalogue = (
        TYPE
        + """
stock_catalogue:
  name: name
  unit: unit
  threshold: threshold
  notify: [qa]
  lots: { type: Lot, item: item, number: serial, expiry: expires_on }
  orders: { type: Order, item: item, reference: reference, ordered: ordered, received: received }
fields:
  name: { type: string, required: true }
  unit: { type: enum, values: [mg], required: true, immutable: true }
  threshold: { type: float, required: true, min: 0 }
"""
    )
    item = "  item: { type: ref, to: Assay, immutable: true, required: true }"
    lot = (
        TYPE.replace("Assay", "Lot").replace("ASY", "LOT")
        + """
amount: { unit: mg, material: item }
fields:
  serial: { type: string }
  expires_on: { type: date }
"""
        + item
    )
    order = (
        TYPE.replace("Assay", "Order").replace("ASY", "ORD")
        + """
fields:
  reference: { type: string }
  ordered: { type: int, required: true, immutable: true, min: 1 }
  received: { type: int, computed: true, min: 0 }
"""
        + item
    )
    files = {"types/item.yaml": catalogue, "types/lot.yaml": lot, "types/order.yaml": order}
    result = make(tmp_path, files, ROLES)
    assert codes(result) == [], result.problems
    assert result.plugin is not None
    loaded = plugin_from_dict(plugin_to_dict(result.plugin))
    assert loaded == result.plugin
    cfg = loaded.types["acme/lab.Assay"].stock_catalogue
    assert cfg is not None and cfg.lots.type == "acme/lab.Lot" and cfg.notify == ("acme/lab.qa",)
    for before, after, expected in [
        ("threshold: threshold", "threshold: serial", "stock-catalogue-field"),
        ("required: true, immutable: true", "required: true", "stock-catalogue-unit"),
        ("min: 0", "min: -1", "stock-catalogue-threshold"),
        ("required: true, min: 0", "required: true", "stock-catalogue-threshold"),
        ("values: [mg]", "values: [mL]", "stock-catalogue-lot"),
    ]:
        assert expected in codes(
            make(tmp_path, {**files, "types/item.yaml": catalogue.replace(before, after)}, ROLES)
        )
    # A threshold is optional: an item without one isn't reordered by its level.
    optional = catalogue.replace(
        "threshold: { type: float, required: true, min: 0 }", "threshold: { type: float, min: 0 }"
    )
    assert codes(make(tmp_path, {**files, "types/item.yaml": optional}, ROLES)) == []


def test_a_type_can_keep_its_amount_in_whole_units(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + 'amount: { unit: "1", whole_units: true }\n')
    assert not result.problems
    assert result.plugin is not None
    built = plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin))))
    amount = built.types["acme/lab.Assay"].amount
    assert amount is not None and amount.whole_units


def test_a_type_can_have_an_amount_with_its_aliquots(tmp_path: Path) -> None:
    """ADR 0139: kept in a unit that adds up, drawn by its concentration, split into
    aliquots of another type, and held in a state."""
    result = make(tmp_path, TYPE + TUBE, ROLES)
    assert codes(result) == []
    assert result.plugin is not None
    amount = result.plugin.types["acme/lab.Assay"].amount
    assert amount is not None
    assert (amount.unit, amount.label, amount.concentration) == ("uL", "Volume", "concentration")
    assert (amount.aliquots, amount.aliquots_of) == ("acme/lab.Aliquot", "parent")
    status = result.plugin.types["acme/lab.Assay"].fields["status"]
    assert status.states["quarantined"].holds_amount
    # It round-trips through the built manifest.
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin

    def problems(*changes: tuple[str, str]) -> list[str]:
        text = TUBE
        for change in changes:
            text = text.replace(*change)
        return codes(make(tmp_path, TYPE + text, ROLES))

    assert problems(("unit: uL\n  label", "unit: Cel\n  label")) == ["amount-unit"]
    assert problems(("unit: uL\n  label", "unit: m\n  label")) == ["amount-unit"]
    mass = ("unit: uL\n  label", "unit: mg\n  label")
    # An aliquot holds what it was split from lost, so in the same kind of unit.
    assert problems(mass) == ["amount-aliquots"]
    assert problems(mass, ("amount: { unit: uL }\n", "amount: { unit: ug }\n")) == []
    assert problems(("unit: mg/mL }", "unit: mg }")) == ["amount-concentration"]
    assert problems(("concentration: concentration", "concentration: status")) == [
        "amount-concentration"
    ]
    assert problems(("of: parent", "of: nothing")) == ["amount-aliquots"]
    assert problems(("amount: { unit: uL }\n", "")) == ["amount-aliquots"]
    # What an amount is of is a reference no record can change: a lot's molecule.
    of_its_parent = ("amount: { unit: uL }\n", "amount: { unit: uL, material: parent }\n")
    fixed = (
        "parent: { type: ref, to: Assay }",
        "parent: { type: ref, to: Assay, required: true, immutable: true }",
    )
    self_material = ("unit: uL\n  label", "unit: uL\n  material: id\n  label")
    assert problems(self_material, of_its_parent, fixed) == []
    parent = TYPE.replace("hrid: { prefix: ASY }", "abstract: true")
    isolate = TUBE.replace(*self_material).replace(*of_its_parent).replace(*fixed)
    registered_subtype = """---
apiVersion: iki/v1
kind: EntityType
name: StockDesign
extends: Assay
schema_version: 1
hrid: { prefix: STD }
identity: [code]
fields:
  code: { type: string, required: true, immutable: true }
"""
    result = make(tmp_path, parent + isolate + registered_subtype, ROLES)
    assert codes(result) == ["amount-on-identity"]
    assert problems(self_material) == ["amount-aliquots"]
    assert problems(
        self_material,
        ("amount: { unit: uL }\n", "amount: { unit: uL, material: sibling }\n"),
        ("  parent: {", "  sibling: { type: ref, to: Aliquot, immutable: true }\n  parent: {"),
    ) == ["amount-aliquots"]
    # A new aliquot cannot inherit a source material by setting its own intrinsic ID.
    self_aliquot = ("amount: { unit: uL }\n", "amount: { unit: uL, material: id }\n")
    assert problems(self_aliquot) == ["amount-aliquots", "amount-aliquots"]
    assert problems(of_its_parent, fixed) == []
    assert problems(of_its_parent) == ["amount-material"]
    assert problems(of_its_parent, fixed, ("material: parent", "material: nothing")) == [
        "amount-material"
    ]
    assert problems(
        ("hrid: { prefix: ALQ }", "hrid: { prefix: ALQ }\nextends: Assay"),
        ("amount: { unit: uL }\n", "amount: { unit: mL }\n"),
    ) == ["amount-twice"]
    identity = (
        "fields:\n  concentration",
        "identity: [code]\nfields:\n"
        "  code: { type: string, required: true, immutable: true }\n  concentration",
    )
    assert problems(identity) == ["amount-on-identity"]
    # A state can hold only an amount the type has.
    held = make(tmp_path, TYPE + "fields:" + TUBE.split("---")[0].split("fields:", 1)[1], ROLES)
    assert codes(held) == ["holds-without-amount"]


LOT = """\
apiVersion: iki/v1
kind: EntityType
name: Lot
schema_version: 1
abstract: true
fields:
  disposition:
    type: state
    initial: quarantined
    states:
      quarantined: { set_by: [qa] }
      on_hold: { set_by: [qa], holds_amount: true }
---
apiVersion: iki/v1
kind: EntityType
name: OligoLot
schema_version: 1
extends: Lot
hrid: { prefix: OLOT }
amount: { unit: nmol }
---
apiVersion: iki/v1
kind: EntityType
name: ProteinLot
schema_version: 1
extends: Lot
hrid: { prefix: PLOT }
amount: { unit: mg }
"""


def test_an_abstract_types_state_holds_each_subtypes_own_amount(tmp_path: Path) -> None:
    """ADR 0162: one disposition for every kind of lot, holding an oligo lot's nmol and a
    protein lot's mg; a concrete subtype without an amount has nothing for it to hold."""
    result = make(tmp_path, LOT, ROLES)
    assert codes(result) == []
    assert result.plugin is not None
    assert result.plugin.types["acme/lab.Lot"].amount is None
    without = LOT.replace(
        "hrid: { prefix: PLOT }\namount: { unit: mg }\n", "hrid: { prefix: PLOT }\n"
    )
    (problem,) = make(tmp_path, without, ROLES).problems
    assert (problem.code, problem.message) == (
        "holds-without-amount",
        "`acme/lab.Lot`'s `on_hold` holds the record's amount, and `ProteinLot`, which "
        "extends it, has none.",
    )


def test_draw_purposes_require_declared_roles_and_can_narrow_an_inherited_list(
    tmp_path: Path,
) -> None:
    parent = TYPE.replace("hrid: { prefix: ASY }", "abstract: true")
    parent += "draw_purposes: {qa_sampling: {by: [qa, analyst]}}\n"
    child = """
---
apiVersion: iki/v1
kind: EntityType
name: Tube
schema_version: 1
extends: Assay
hrid: { prefix: TUB }
amount: { unit: mg }
draw_purposes: {qa_sampling: {by: [qa]}}
"""
    result = make(tmp_path, parent + child, ROLES)
    assert result.problems == []
    assert result.plugin is not None
    built = plugin_from_dict(plugin_to_dict(result.plugin))
    assert built.types["acme/lab.Assay"].draw_purposes == {
        "qa_sampling": ("acme/lab.qa", "acme/lab.analyst")
    }
    assert built.types["acme/lab.Tube"].draw_purposes == {"qa_sampling": ("acme/lab.qa",)}
    wider = make(tmp_path, parent.replace("[qa, analyst]", "[analyst]") + child, ROLES)
    assert "acme/lab.qa" in only(wider, "draw-purpose-widens-roles")
    missing_role = make(tmp_path, parent.replace("[qa, analyst]", "[missing]"), ROLES)
    assert "missing" in only(missing_role, "unknown-role")
    missing_amount = make(tmp_path, parent + child.replace("amount: { unit: mg }", ""), ROLES)
    assert "has no amount" in only(missing_amount, "draw-purposes-without-amount")


@pytest.mark.parametrize(
    "policy", ["{qa_sampling: {by: []}}", "{qa_sampling: {}}", "{qc: {by: [qa]}}"]
)
def test_invalid_draw_purpose_declarations_are_rejected(tmp_path: Path, policy: str) -> None:
    result = make(tmp_path, TYPE + f"amount: {{ unit: mg }}\ndraw_purposes: {policy}\n", ROLES)
    assert codes(result) == ["schema"]


# --- Where material is kept (ADR 0180) ---------------------------------------------------------

STORAGE = """\
apiVersion: iki/v1
kind: EntityType
name: Freezer
schema_version: 1
hrid: { prefix: FRZ }
storage: { holds: [Box] }
fields:
  name: { type: string, required: true }
---
apiVersion: iki/v1
kind: EntityType
name: Box
schema_version: 1
hrid: { prefix: BOX }
storage:
  holds: [Tube]
  grid: { rows: rows, columns: 9, labels: letter_number, order: by_column }
  barcode: barcode
fields:
  rows: { type: int, required: true, immutable: true, min: 1, max: 10 }
  barcode: { type: string, unique: true, indexed: true, max_length: 64 }
---
apiVersion: iki/v1
kind: EntityType
name: Tube
schema_version: 1
hrid: { prefix: TUB }
amount: { unit: uL, follows: [lot] }
storage: { barcode: code, label: "{{ code }} {{ hrid }}", confirm: [draw, move] }
fields:
  code: { type: string, unique: true, indexed: true, max_length: 64 }
  lot: { type: ref, to: Box }
---
apiVersion: iki/v1
kind: EntityType
name: Shipment
schema_version: 1
hrid: { prefix: SHP }
request:
  service_fields: [notes]
  sends: [tubes]
  receives: { field: tubes, by: [qa] }
fields:
  tubes: { type: list, items: { type: ref, to: Tube } }
  notes: { type: text }
"""


def test_a_type_says_what_it_holds_its_grid_its_barcode_and_what_needs_a_scan(
    tmp_path: Path,
) -> None:
    result = make(tmp_path, STORAGE, ROLES)
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    types = result.plugin.types
    box = types["acme/lab.Box"].storage
    assert box is not None
    assert box.holds == ("acme/lab.Tube",)
    assert box.grid is not None
    assert (box.grid.rows, box.grid.columns, box.grid.order) == ("rows", 9, "by_column")
    tube = types["acme/lab.Tube"]
    assert tube.storage is not None
    assert tube.storage.confirm == ("draw", "move")
    assert tube.amount is not None
    assert tube.amount.follows == ("lot",)
    request = types["acme/lab.Shipment"].request
    assert request is not None
    assert (request.receives, request.receives_by) == ("tubes", ("acme/lab.qa",))
    # It round-trips through the built manifest.
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin

    def problems(*changes: tuple[str, str]) -> list[str]:
        text = STORAGE
        for change in changes:
            assert change[0] in text, change
            text = text.replace(*change, 1)
        return codes(make(tmp_path, text, ROLES))

    # What it holds is kept somewhere too.
    unstored = (
        'storage: { barcode: code, label: "{{ code }} {{ hrid }}", confirm: [draw, move] }\n',
        "",
    )
    assert problems(unstored) == ["storage-holds-unstored", "request-receives"]
    assert problems(("holds: [Box] }", "holds: [Shipment] }")) == ["storage-holds-unstored"]
    assert problems(("holds: [Box] }", "holds: [Crate] }")) == ["unknown-type"]
    # A grid's sides are numbers within a 1536-well plate's, or fields that can't change.
    assert "schema" in problems(("columns: 9,", "columns: 49,"))
    assert problems(("max: 10 }", "max: 40 }")) == ["storage-grid"]
    assert problems(("immutable: true, min: 1", "min: 1")) == ["storage-grid"]
    assert problems(("rows: { type: int", "rows: { type: float")) == ["storage-grid"]
    assert problems(("rows: rows,", "rows: shelves,")) == ["storage-grid"]
    assert problems(("holds: [Tube]\n  grid", "grid")) == ["storage-grid-without-holds"]
    # A barcode is unique, so a scan finds one record.
    assert problems(("\n  code: { type: string, unique: true,", "\n  code: { type: string,")) == [
        "storage-barcode"
    ]
    assert problems(('label: "{{ code }} {{ hrid }}"', 'label: "{{ nothing }}"')) == [
        "storage-label"
    ]
    # A draw needs a scan only of something with an amount.
    assert problems(("amount: { unit: uL, follows: [lot] }\n", "")) == [
        "storage-confirm-without-amount"
    ]
    # A container's material follows a reference.
    assert problems(("follows: [lot]", "follows: [code]")) == ["amount-follows"]
    # What's received is sent, and kept somewhere.
    assert problems(("sends: [tubes]", "sends: []")) == ["request-receives"]
    assert problems(("receives: { field: tubes,", "receives: { field: notes,")) == [
        "request-receives"
    ]
    assert problems(
        ("items: { type: ref, to: Tube } }", "items: { type: ref, to: Shipment } }")
    ) == ["request-receives"]
    assert problems(("by: [qa]", "by: [porter]")) == ["unknown-role"]


def test_a_setting_declares_its_required_or_default_policy(tmp_path: Path) -> None:
    setting = PLUGIN + "settings:\n  endpoint: { type: string }\n"
    assert "required" in only(make(tmp_path, {}, setting), "setting-policy")
    explicit = setting.replace("type: string", "type: string, required: false")
    assert make(tmp_path, {}, explicit).ok
    default = setting.replace("type: string", "type: string, default: example")
    assert make(tmp_path, {}, default).ok


def test_invalid_types_do_not_cascade_into_missing_type_errors(tmp_path: Path) -> None:
    root = """\
apiVersion: iki/v1
kind: EntityType
name: Parent
schema_version: 1
hrid: { prefix: PAR }
fields:
  label: { type: not_a_field_type }
"""
    child = """\
apiVersion: iki/v1
kind: EntityType
name: Child
schema_version: 1
hrid: { prefix: CHD }
extends: Parent
"""
    reader = """\
apiVersion: iki/v1
kind: EntityType
name: Reader
schema_version: 1
hrid: { prefix: RDR }
fields:
  parent: { type: ref, to: Parent }
  child: { type: ref, to: Child }
  unknown: { type: ref, to: Misspelled }
"""
    result = make(
        tmp_path,
        {
            "types/parent.yaml": root,
            "types/child.yaml": child,
            "types/reader.yaml": reader,
        },
    )
    assert [(p.code, p.path) for p in result.problems] == [
        ("schema", ("fields", "label", "type")),
        ("unknown-type", ("fields", "unknown", "to")),
    ]


#: A prepared draw target (ADR 0243): its planned amount a float in the unit its record names.
DRAW_TARGET = """\
draw_target:
  from: tube
  source: lot
  state: { field: status, is: waiting }
  by: [analyst]
  ready: prepared
  amount: amount
  unit: amount_unit
fields:
  tube: { type: ref, to: iki/core.Entity, required: true, immutable: true }
  lot: { type: ref, to: iki/core.Entity, required: true, immutable: true }
  prepared: { type: int, computed: true }
  amount: { type: float, min: 0.000001, required: true, immutable: true }
  amount_unit: { type: enum, values: [uL, ug], required: true, immutable: true }
  status:
    type: state
    initial: waiting
    states:
      waiting: { label: Waiting }
      done: { label: Done, set_by: [analyst] }
"""


def test_a_draw_targets_planned_amount_can_name_its_unit(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + DRAW_TARGET, ROLES)
    assert codes(result) == []
    assert result.plugin is not None
    target = result.plugin.types["acme/lab.Assay"].draw_target
    assert target is not None and (target.amount, target.unit) == ("amount", "amount_unit")
    assert plugin_from_dict(json.loads(json.dumps(plugin_to_dict(result.plugin)))) == result.plugin

    def problems(old: str, new: str) -> list[str]:
        return codes(make(tmp_path, TYPE + DRAW_TARGET.replace(old, new), ROLES))

    # A unit beside a quantity, or a float without one, says two units or none.
    quantity = "amount: { type: quantity, unit: uL,"
    assert problems("amount: { type: float,", quantity) == ["invalid-draw-target"]
    assert problems("  unit: amount_unit\n", "") == ["invalid-draw-target"]
    # The unit is fixed with the plan, like the amount.
    assert problems(
        "required: true, immutable: true }\n  status", "required: true }\n  status"
    ) == ["invalid-draw-target"]
