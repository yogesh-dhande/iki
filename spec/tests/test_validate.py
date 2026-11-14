import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec import schemas
from iki_spec.model import FORMATS, Field, matches_format, parse_size
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

PLUGINS = Path(__file__).parent / "plugins"
DNA, PROTEIN, PREDICTION = PLUGINS / "dna", PLUGINS / "protein", PLUGINS / "structure_prediction"

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme
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
    assert set(dna.plugin.types) == {"dna.Sequence", "dna.DNASequence"}

    protein = load_plugin(PROTEIN, dependencies=[DNA])
    assert protein.problems == []
    prediction = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA])
    assert prediction.problems == []


def test_builds_the_model_codegen_reads() -> None:
    result = load_plugin(PROTEIN, dependencies=[DNA])
    assert result.plugin is not None
    protein = result.plugin.types["protein.Protein"]
    assert protein.qualified_name == "protein.Protein"
    assert protein.table == "protein_protein"
    assert protein.extends == "dna.Sequence"
    assert protein.hrid_prefix == "PRT"
    assert protein.identity == ("sequence",)
    assert protein.label == "Protein"
    assert protein.label_plural == "Proteins"
    weight = protein.fields["molecular_weight"]
    assert weight.type == "quantity"
    assert weight.unit == "u"
    assert weight.label == "Molecular weight"
    assert protein.fields["encoded_by"].to == "dna.DNASequence"
    assert result.plugin.dependencies == {"dna": {"Sequence": ">=1", "DNASequence": ">=2,<4"}}
    structure = result.plugin.types["protein.ProteinStructure"]
    assert structure.fields["structure"].max_size == 2 * 1024**3
    chains = structure.fields["chains"]
    assert chains.items is not None
    assert chains.items.to == "protein.Protein"
    assert result.plugin.types["protein.ProteinStructure"].label == "Protein Structure"

    prediction = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA]).plugin
    assert prediction is not None
    (extension,) = prediction.extensions
    assert extension.extends_type == "protein.ProteinStructure"
    assert extension.table == "structure_prediction__protein_proteinstructure"
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
    assert sorted(result.plugin.types) == ["acme.Assay", "acme.Plate", "acme.Sample"]


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
        plugin=PLUGIN + "dependencies:\n  dna:\n    types:\n      DNASequence: '2'\n",
    )
    assert any("Constraints compare whole schema versions" in p.message for p in result.problems)


# --- Rules across the plugin ---------------------------------------------------------------------


def test_reserved_plugin_names(tmp_path: Path) -> None:
    result = make(tmp_path, plugin=PLUGIN.replace("acme", "iki_tools"))
    assert "reserved" in only(result, "reserved-name")
    result = make(tmp_path, plugin=PLUGIN.replace("acme", "auth"))
    assert codes(result) == ["reserved-name"]
    result = make(tmp_path, plugin=PLUGIN.replace("acme", "core"))
    assert codes(result) == ["reserved-name"]


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
    assert "share the table `acme_assayrun`" in only(result, "duplicate-table")


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
    assert "already uses the HRID prefix `ASY`" in only(result, "duplicate-hrid-prefix")
    dna_clash = TYPE.replace("ASY", "DNA")
    result = make(
        tmp_path,
        dna_clash,
        plugin=PLUGIN + "dependencies:\n  dna: {}\n",
        with_=(DNA,),
    )
    assert "`dna.DNASequence` already uses" in only(result, "duplicate-hrid-prefix")


def test_reserved_field_names(tmp_path: Path) -> None:
    result = make(tmp_path, fields("type: { type: string }\nclass: { type: string }\n"))
    assert codes(result) == ["reserved-name", "reserved-name"]
    assert "`sample_type` instead of `type`" in (result.problems[0].hint or "")


def test_ref_columns_dont_collide(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("parent: { type: ref, to: Assay }\nparent_id: { type: string }\n"),
    )
    assert "column named `parent_id`" in only(result, "column-conflict")


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


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("{ type: enum, values: [a, b], default: c }", "must be one of: `a`, `b`."),
        ("{ type: int, min: 0, default: -1 }", "must be at least 0."),
        ("{ type: float, max: 1, default: 2 }", "must be at most 1."),
        ("{ type: string, max_length: 2, default: abc }", "at most 2 characters"),
        ("{ type: string, format: dna_sequence, default: ACGZ }", "isn't a valid dna sequence."),
        ("{ type: string, pattern: '^[a-z]+$', default: A1 }", "doesn't match the pattern"),
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
    assert result.problems[1].message == (
        "`name` is part of the identity, so it must be required and immutable."
    )


def test_on_duplicate_needs_an_identity(tmp_path: Path) -> None:
    result = make(tmp_path, TYPE + "on_duplicate: warn\n")
    assert codes(result) == ["on-duplicate-without-identity"]


def test_identity_and_on_duplicate_can_be_inherited(tmp_path: Path) -> None:
    parent = (
        TYPE.replace("hrid: { prefix: ASY }\n", "abstract: true\n")
        + "identity: [code]\nfields:\n  code: { type: string, required: true, immutable: true }\n"
    )
    child = (
        TYPE.replace("Assay", "Elisa").replace("ASY", "ELI")
        + "extends: Assay\non_duplicate: warn\n"
    )
    assert make(tmp_path, parent + "---\n" + child).problems == []


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
    assert "already a field of `acme.Assay`" in only(result, "field-shadows-parent")


def test_subtype_cycles(tmp_path: Path) -> None:
    a = TYPE + "extends: Elisa\n"
    b = TYPE.replace("Assay", "Elisa").replace("ASY", "ELI") + "extends: Assay\n"
    result = make(tmp_path, a + "---\n" + b)
    assert codes(result) == ["extends-cycle", "extends-cycle"]
    assert "`acme.Assay` → `acme.Elisa` → `acme.Assay`" in result.problems[0].message


def test_unknown_types_in_this_plugin(tmp_path: Path) -> None:
    result = make(tmp_path, fields("x: { type: ref, to: Asay }\n"))
    assert only(result, "unknown-type") == "This plugin has no type `Asay`."
    assert result.problems[0].hint == "Did you mean `Assay`?"


def test_core_entity_needs_no_dependency(tmp_path: Path) -> None:
    assert make(tmp_path, fields("x: { type: ref, to: core.Entity }\n")).problems == []
    assert make(tmp_path, fields("x: { type: ref, to: core.Collection }\n")).problems == []
    result = make(tmp_path, fields("x: { type: ref, to: core.Sample }\n"))
    provides = "In this version, core provides `core.Collection`, `core.Entity`, `core.Sequence`."
    assert provides in only(result, "unknown-type")


def test_other_plugins_types_need_a_declared_dependency(tmp_path: Path) -> None:
    result = make(tmp_path, fields("x: { type: ref, to: dna.DNASequence }\n"), with_=(DNA,))
    assert "which plugin.yaml doesn't list under `dependencies`" in only(
        result, "undeclared-dependency"
    )
    assert 'dna: { types: { DNASequence: ">=2" } }' in (result.problems[0].hint or "")
    result = make(tmp_path, fields("x: { type: ref, to: dna.DNASequence }\n"))
    assert codes(result) == ["undeclared-dependency"]


def test_dependencies_must_be_given_to_check_them(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("x: { type: ref, to: dna.DNASequence }\ny: { type: ref, to: dna.Primer }\n"),
        plugin=PLUGIN + "dependencies:\n  dna: {}\n",
    )
    # Reported once, on the dependency, not on every use.
    problem = result.problems[0]
    assert codes(result) == ["dependency-not-available"]
    assert problem.file == "plugin.yaml"
    assert problem.location == Location(6, 3)
    assert problem.hint == "Pass the dna plugin's directory with `--with path/to/dna`."


def test_uses_of_a_dependency(tmp_path: Path) -> None:
    plugin = PLUGIN + "dependencies:\n  dna:\n    types:\n      DNASequence: '>=2'\n"
    result = make(tmp_path, fields("x: { type: ref, to: dna.Primer }\n"), plugin, with_=(DNA,))
    assert only(result, "unknown-type") == "The dna plugin has no type `Primer`."
    ok = make(tmp_path, fields("x: { type: ref, to: dna.DNASequence }\n"), plugin, with_=(DNA,))
    assert ok.problems == []


def test_unpinned_dependency_types_are_a_warning(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        fields("x: { type: ref, to: dna.DNASequence }\n"),
        PLUGIN + "dependencies:\n  dna: {}\n",
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
    plugin = PLUGIN + f"dependencies:\n  dna:\n    types:\n      DNASequence: '{constraint}'\n"
    result = make(tmp_path, "", plugin, with_=(DNA,))
    assert codes(result) == ([code] if code else [])


def test_dependency_types_must_exist(tmp_path: Path) -> None:
    plugin = PLUGIN + "dependencies:\n  dna:\n    types:\n      DnaSequence: '>=1'\n"
    result = make(tmp_path, "", plugin, with_=(DNA,))
    assert only(result, "unknown-type") == "The dna plugin has no type `DnaSequence`."
    assert result.problems[0].hint == "Did you mean `DNASequence`?"


def test_dependency_problems(tmp_path: Path) -> None:
    result = make(tmp_path, "", PLUGIN + "dependencies:\n  acme: {}\n")
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
        "apiVersion: iki/v1\nkind: Plugin\nname: other\nversion: 1.0.0\ndependencies:\n  acme: {}\n"
    )
    result = make(tmp_path, "", PLUGIN + "dependencies:\n  other: {}\n", with_=(other,))
    assert codes(result) == ["dependency-cycle"]


# --- Extensions ----------------------------------------------------------------------------------

EXTENSION = """\
apiVersion: iki/v1
kind: EntityTypeExtension
extends_type: dna.DNASequence
fields:
"""
DNA_PIN = PLUGIN + "dependencies:\n  dna:\n    types:\n      DNASequence: '>=2'\n"


def extension(text: str) -> str:
    return EXTENSION + textwrap.indent(textwrap.dedent(text), "  ")


def test_extensions(tmp_path: Path) -> None:
    result = make(tmp_path, extension("codon_usage: { type: json }\n"), DNA_PIN, with_=(DNA,))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.extensions[0].table == "acme__dna_dnasequence"


def test_extension_fields_cant_clash_with_the_types(tmp_path: Path) -> None:
    result = make(tmp_path, extension("name: { type: string }\n"), DNA_PIN, with_=(DNA,))
    message = only(result, "extension-field-conflict")
    assert message == "`name` is already a field of `dna.DNASequence` (from `dna.Sequence`)."
    assert result.problems[0].hint == "Choose another name, for example `acme_name`."


def test_extension_fields_cant_clash_with_other_extensions(tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / "types").mkdir(parents=True)
    (other / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: other\nversion: 1.0.0\ndependencies:\n  dna: {}\n"
    )
    (other / "types" / "x.yaml").write_text(extension("tm: { type: float }\n"))
    result = make(tmp_path, extension("tm: { type: float }\n"), DNA_PIN, with_=(DNA, other))
    assert "(from the other plugin's extension)" in only(result, "extension-field-conflict")


def test_extension_fields_cant_be_required(tmp_path: Path) -> None:
    result = make(tmp_path, extension("x: { type: int, required: true }\n"), DNA_PIN, with_=(DNA,))
    assert codes(result) == ["extension-field-required"]


def test_extension_targets(tmp_path: Path) -> None:
    own = EXTENSION.replace("dna.DNASequence", "Assay") + "  x: { type: int }\n"
    result = make(tmp_path, {"types/assay.yaml": TYPE, "types/ext.yaml": own})
    assert result.problems[0].hint == "Add the fields to the type itself."
    builtin = EXTENSION.replace("dna.DNASequence", "core.Entity") + "  x: { type: int }\n"
    assert codes(make(tmp_path, builtin)) == ["extends-builtin"]
    twice = extension("x: { type: int }\n") + "---\n" + extension("y: { type: int }\n")
    result = make(tmp_path, twice, DNA_PIN, with_=(DNA,))
    assert "already extends `dna.DNASequence` (in types/assay.yaml, line 1)" in only(
        result, "duplicate-extension"
    )


def test_extension_table_names(tmp_path: Path) -> None:
    long_type = "B" + "b" * 40
    other = tmp_path / "other"
    (other / "types").mkdir(parents=True)
    (other / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: other\nversion: 1.0.0\n"
    )
    (other / "types" / "x.yaml").write_text(TYPE.replace("Assay", long_type))
    plugin = (
        PLUGIN.replace("acme", "a" * 30)
        + f"dependencies:\n  other:\n    types:\n      {long_type}: '>=1'\n"
    )
    text = extension("x: { type: int }\n").replace("dna.DNASequence", f"other.{long_type}")
    result = make(tmp_path, text, plugin, with_=(other,))
    assert "PostgreSQL allows 63" in only(result, "name-too-long")


def test_list_tables(tmp_path: Path) -> None:
    text = fields(
        """\
        parents: { type: list, items: { type: ref, to: core.Entity } }
        tags: { type: list, items: { type: string } }
        """
    )
    assert make(tmp_path, text).problems == []
    long = "p" + "x" * 49
    text = fields(f"{long}: {{ type: list, items: {{ type: file }} }}\n")
    message = only(make(tmp_path, text, PLUGIN.replace("acme", "acme_lab")), "name-too-long")
    assert message == (
        f"The table `acme_lab_assay__{long}` for the list `{long}` of `Assay` would be 66 "
        "characters; PostgreSQL allows 63."
    )


def test_model_names_cant_clash(tmp_path: Path) -> None:
    # The type `Ext` with a list `dna_dnasequence` would be the model `Ext_dna_DNASequence`'s
    # twin: the extension of `dna.DNASequence`.
    text = {
        "types/assay.yaml": TYPE.replace("Assay", "Ext").replace("ASY", "EXT")
        + "fields:\n  dna_dnasequence: { type: list, items: { type: file } }\n",
        "types/ext.yaml": extension("x: { type: int }\n"),
    }
    message = only(make(tmp_path, text, DNA_PIN, with_=(DNA,)), "duplicate-table")
    assert "would share the model `Ext_dna_DNASequence`" in message


def test_removed_fields(tmp_path: Path) -> None:
    text = fields("old: { type: string, removed: true }\n")
    result = make(tmp_path, text)
    assert result.problems == []
    assert result.plugin is not None
    assay = result.plugin.types["acme.Assay"]
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
          api_key: { type: string }
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
          s: { type: string }
          i: { type: int }
          f: { type: float }
          b: { type: bool }
          e: { type: enum, values: [x, y] }
          l: { type: list, items: { type: int }, max_items: 2 }
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
    assert problem.hint == "The acme plugin takes no settings."


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
        "manifest", {"apiVersion": "iki/v1", "kind": "Plugin", "name": "acme", "version": "1.0.0"}
    )
    issues = schemas.check("manifest", {"apiVersion": "iki/v1", "kind": "Nope"})
    assert [i.message for i in issues] == [
        "`kind` can't be `Nope`. Choose one of: `Plugin`, `EntityType`, `EntityTypeExtension`, "
        "`Action`, `Environment`, `Workflow`, `Trigger`."
    ]
