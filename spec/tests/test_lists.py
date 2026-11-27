"""Lab lists (ADR 0270): a type marked as a list, its options' fields, and starting options."""

import textwrap
from pathlib import Path

import pytest

from iki_spec.model import LIST_FIELDS, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""

METHOD = """\
apiVersion: iki/v1
kind: EntityType
name: PurificationMethod
label: Purification method
schema_version: 1
hrid: { prefix: PUM }
list:
  options:
    - { code: hplc, label: HPLC, description: Reversed-phase HPLC. }
    - { code: iex, label: Ion exchange, category: chromatography }
    - { code: desalt, label: Desalting }
fields:
  category:
    type: ref
    to: Category
  sop:
    type: string
    format: uri
    description: The SOP the method follows.
"""

CATEGORY = """\
apiVersion: iki/v1
kind: EntityType
name: Category
schema_version: 1
hrid: { prefix: CAT }
list:
  options:
    - { code: chromatography, label: Chromatography }
"""

BATCH = """\
apiVersion: iki/v1
kind: EntityType
name: Batch
schema_version: 1
hrid: { prefix: BAT }
fields:
  name: { type: string, max_length: 100, required: true }
  method: { type: ref, to: PurificationMethod }
"""


def make(tmp_path: Path, files: dict[str, str]) -> Result:
    root = tmp_path / "acme"
    (root / "types").mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(PLUGIN)
    for name, text in files.items():
        (root / name).write_text(textwrap.dedent(text))
    return load_plugin(root)


def problems(result: Result) -> list[tuple[str, str]]:
    return [(p.code, p.message) for p in result.problems]


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def valid(tmp_path: Path) -> Result:
    return make(
        tmp_path,
        {
            "types/method.yaml": METHOD,
            "types/category.yaml": CATEGORY,
            "types/batch.yaml": BATCH,
        },
    )


def test_a_list_type_gets_its_options_fields(tmp_path: Path) -> None:
    result = valid(tmp_path)
    assert result.ok, problems(result)
    assert result.plugin is not None
    method = result.plugin.types["acme/lab.PurificationMethod"]
    assert method.is_list
    assert method.identity == ("code",)
    assert list(method.fields)[:4] == list(LIST_FIELDS)
    assert method.display_title == "{{ label }}"
    code, label, description, position = (method.fields[n] for n in LIST_FIELDS)
    assert code.required and code.immutable and code.registry is None
    assert label.registry == "fact" and label.required and label.unique
    assert description.registry == "fact" and description.type == "text"
    assert position.registry == "fact" and position.type == "int"
    # The type's other fields are its options' facts.
    assert method.fields["category"].registry == "fact"
    assert method.fields["sop"].registry == "fact"
    assert method.list_spec is not None
    assert [o["code"] for o in method.list_spec.options] == ["hplc", "iex", "desalt"]
    assert not result.plugin.types["acme/lab.Batch"].is_list


def test_a_list_round_trips_through_its_built_manifest(tmp_path: Path) -> None:
    result = valid(tmp_path)
    assert result.plugin is not None
    again = plugin_from_dict(plugin_to_dict(result.plugin))
    assert again == result.plugin
    batch = plugin_to_dict(result.plugin)["types"]["acme/lab.Batch"]
    assert "list_spec" not in batch


def test_a_list_with_no_starting_options(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {
            "types/category.yaml": CATEGORY.replace(
                "list:\n  options:\n    - { code: chromatography, label: Chromatography }\n",
                "list: true\n",
            )
        },
    )
    assert result.ok, problems(result)
    assert result.plugin is not None
    category = result.plugin.types["acme/lab.Category"]
    assert category.is_list and category.list_spec is not None
    assert category.list_spec.options == ()


@pytest.mark.parametrize(
    "extra",
    [
        "identity: [category]\n",
        "extends: Category\n",
        "abstract: true\n",
        "rules: { named: { label: Named, check: 'sop != null', message: Give its SOP. } }\n",
        "due: sop\n",
    ],
)
def test_records_things_arent_for_lists(tmp_path: Path, extra: str) -> None:
    result = make(
        tmp_path,
        {"types/method.yaml": METHOD + extra, "types/category.yaml": CATEGORY},
    )
    assert "list-key" in codes(result), problems(result)


def test_nothing_extends_a_list(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {
            "types/category.yaml": CATEGORY,
            "types/special.yaml": """\
                apiVersion: iki/v1
                kind: EntityType
                name: Special
                schema_version: 1
                extends: Category
                hrid: { prefix: SPC }
            """,
        },
    )
    assert "extends-list" in codes(result), problems(result)


def test_a_list_has_its_own_fields_already(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {"types/category.yaml": CATEGORY + "fields:\n  label: { type: string, max_length: 10 }\n"},
    )
    assert codes(result) == ["list-field-reserved"], problems(result)


def test_a_lists_fields_are_curated_facts(tmp_path: Path) -> None:
    computed = """\
fields:
  weight: { type: float, registry: derived, computed: true }
  notes: { type: text, max_length: 100 }
  reviewer: { type: person }
"""
    result = make(tmp_path, {"types/category.yaml": CATEGORY + computed})
    found = codes(result)
    assert "list-computed" in found
    # Free text is a fact's problem, as for any registry fact; people are projects'.
    assert "registry-fact-free-text" in found
    assert "person-in-registry" in found


def test_a_list_refers_only_to_other_lists(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {
            "types/category.yaml": CATEGORY + "fields:\n  batch: { type: ref, to: Batch }\n",
            "types/batch.yaml": BATCH.replace(
                "  method: { type: ref, to: PurificationMethod }\n", ""
            ),
        },
    )
    assert "list-refers-to-entry" in codes(result), problems(result)


def test_a_reference_to_a_list_names_it_alone(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {
            "types/category.yaml": CATEGORY,
            "types/batch.yaml": BATCH.replace("to: PurificationMethod", "to: [Category, Batch]"),
        },
    )
    assert "list-among-types" in codes(result), problems(result)


def test_starting_options_are_each_the_lists_once(tmp_path: Path) -> None:
    twice = CATEGORY + "    - { code: chromatography, label: Again }\n"
    twice += "    - { code: other, label: chromatography }\n"
    result = make(tmp_path, {"types/category.yaml": twice})
    assert codes(result) == ["list-option-twice", "list-label-twice"], problems(result)


def test_a_starting_options_code_and_label_name_it_alone(tmp_path: Path) -> None:
    """A sheet names an option by its code or its label, in any case, so another option's
    code or label may be neither; its own may be both."""
    clash = CATEGORY + "    - { code: sec, label: Size exclusion }\n"
    clash += "    - { code: gf, label: SEC }\n"
    clash += "    - { code: CHROMATOGRAPHY, label: Other chromatography }\n"
    clash += "    - { code: desalting, label: Buffer exchange }\n"
    clash += "    - { code: buffer_exchange, label: Desalting }\n"
    result = make(tmp_path, {"types/category.yaml": clash})
    assert codes(result) == ["list-name-twice"] * 3, problems(result)
    assert "“SEC” is option 2's code, in any case" in str(problems(result))


def test_starting_options_values_are_checked(tmp_path: Path) -> None:
    bad = METHOD.replace("category: chromatography", "category: filtration")
    bad = bad.replace("label: Desalting }", "label: ' Desalting', sop: not a uri, colour: red }")
    bad = bad.replace("{ code: hplc,", "{ position: 3, code: hplc,")
    result = make(tmp_path, {"types/method.yaml": bad, "types/category.yaml": CATEGORY})
    found = codes(result)
    assert found.count("list-option-unknown") == 1, problems(result)
    assert found.count("list-option-value") == 2, problems(result)
    assert found.count("unknown-field") == 2, problems(result)


def test_a_starting_option_needs_its_code_and_label(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        {"types/category.yaml": CATEGORY.replace("{ code: chromatography, label", "{ label")},
    )
    assert not result.ok
    assert any("code" in message for _, message in problems(result)), problems(result)


def test_an_extension_of_a_list_adds_its_options_facts(tmp_path: Path) -> None:
    lab = tmp_path / "lab"
    make(lab, {"types/category.yaml": CATEGORY})
    other = tmp_path / "other"
    (other / "acme").mkdir(parents=True)
    (other / "acme" / "plugin.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: iki/v1
            kind: Plugin
            name: acme/qc
            version: 1.0.0
            dependencies:
              acme/lab: { types: { Category: ">=1" } }
            """
        )
    )
    (other / "acme" / "types").mkdir()
    (other / "acme" / "types" / "category.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: iki/v1
            kind: EntityTypeExtension
            extends_type: acme/lab.Category
            fields:
              sop: { type: string, format: uri, registry: fact }
              notes: { type: string, max_length: 100, pattern: "^.+$" }
            """
        )
    )
    result = load_plugin(other / "acme", dependencies=(lab / "acme",))
    assert codes(result) == ["list-extension-record-field"], problems(result)
