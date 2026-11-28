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
hrid: { prefix: BTC }
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


@pytest.mark.parametrize(
    ("rule", "warned"),
    [
        # A superseded code gives new records the replacement's: this stops applying.
        ('check: record.method.code != "hplc" || record.name != ""', True),
        ('when: record.method.code == "hplc"\n    check: record.name != ""', True),
        # Whether there's an option, and a string that only looks like a read, are fine.
        ('check: record.method != null || record.name != ""', False),
        ('check: record.name != "record.method.code"', False),
    ],
)
def test_a_rule_reading_a_list_options_code_is_warned_of(
    tmp_path: Path, rule: str, warned: bool
) -> None:
    batch = BATCH + f"rules:\n  named:\n    {rule}\n    message: Name it.\n"
    result = make(
        tmp_path,
        {"types/method.yaml": METHOD, "types/category.yaml": CATEGORY, "types/batch.yaml": batch},
    )
    assert result.ok, problems(result)
    found = [p for p in result.problems if p.code == "list-code-in-expression"]
    assert len(found) == int(warned)
    if warned:
        assert found[0].severity.value == "warning"
        assert "`method.code`" in found[0].message
        assert "acme/lab.PurificationMethod" in found[0].message


def test_a_state_guard_reading_a_list_options_code_is_warned_of(tmp_path: Path) -> None:
    batch = BATCH + (
        "  status:\n"
        "    type: state\n"
        "    initial: open\n"
        "    states:\n"
        "      open: {}\n"
        "      closed:\n"
        "        guards:\n"
        "          not_by_hplc:\n"
        "            check: record.method.code != 'hplc'\n"
        "            message: An HPLC batch closes by its own SOP.\n"
    )
    result = make(
        tmp_path,
        {"types/method.yaml": METHOD, "types/category.yaml": CATEGORY, "types/batch.yaml": batch},
    )
    # (No role sets `closed` here, which the validator says too.)
    assert codes(result).count("list-code-in-expression") == 1, problems(result)


# --- What a plugin reads of an option is fixed (ADR 0270, ADR 0143) ---------------------------

GRADE = """\
apiVersion: iki/v1
kind: EntityType
name: Grade
schema_version: 1
hrid: { prefix: GRD }
list:
  options:
    - { code: gmp, label: GMP, sterile: true }
fields:
  sterile: { type: bool, label: Sterile, IMMUTABLE }
  max_endotoxin: { type: float, label: Endotoxin limit (EU/mL), min: 0 }
  sop: { type: string, format: uri }
"""

LOT = """\
apiVersion: iki/v1
kind: EntityType
name: Lot
schema_version: 1
hrid: { prefix: LOT }
fields:
  name: { type: string, max_length: 100, required: true }
  grade: { type: ref, to: Grade }
  filtered: { type: bool }
  status:
    type: state
    initial: made
    states:
      made: { label: Made, set_by: [qa] }
      released:
        label: Released
        set_by: [qa]
        guards:
          by_grade:
            check: record.grade == null || !record.grade.sterile || record.filtered == true
            message: Filter a sterile grade's lot before releasing it.
rules:
  sterile_is_filtered:
    check: record.grade == null || !record.grade.sterile || record.filtered == true
    message: "A {{ record.grade.label }} lot is filtered: its SOP is {{ record.grade.sop }}."
"""

ROLES = PLUGIN + "roles:\n  qa: { label: QA }\n"


def graded(tmp_path: Path, fixed: bool, **more: str) -> Result:
    grade = GRADE.replace("IMMUTABLE", "immutable: true" if fixed else "registry: fact")
    return make(
        tmp_path,
        {"types/grade.yaml": grade, "types/lot.yaml": LOT, "plugin.yaml": ROLES, **more},
    )


def test_a_rule_or_guard_reads_only_an_options_fixed_facts(tmp_path: Path) -> None:
    result = graded(tmp_path, fixed=False)
    found = [p for p in result.problems if p.code == "reads-curated-fact"]
    # The rule's check and the guard's: its message may show what a curator changes.
    assert len(found) == 2, problems(result)
    assert all("`Grade.sterile`" in p.message for p in found), problems(result)
    assert graded(tmp_path / "fixed", fixed=True).problems == []


def test_a_run_reads_only_an_included_options_fixed_facts(tmp_path: Path) -> None:
    action = """\
    apiVersion: iki/v1
    kind: Action
    name: label_lot
    inputs:
      lot: { type: Lot, include: [grade] }
    effects:
      updates: [{ input: lot, fields: [filtered] }]
    runs_on: { tier: backend, environment: tools }
    command: [python, ./run.py]
    """
    environment = """\
    apiVersion: iki/v1
    kind: Environment
    name: tools
    tiers: [backend]
    python: { version: "3.12", requirements: requirements.txt }
    """
    files = {"actions/label.yaml": action, "environments/tools.yaml": environment}
    for root in (tmp_path / "curated", tmp_path / "fixed"):
        (root / "acme" / "actions").mkdir(parents=True)
        (root / "acme" / "environments").mkdir()
        (root / "acme" / "requirements.txt").write_text(
            "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n"
        )
        (root / "acme" / "run.py").write_text("")
    result = graded(tmp_path / "curated", fixed=True, **files)
    # Its endotoxin limit is a number the run may work from; its SOP is the curators' text.
    (problem,) = [p for p in result.problems if p.code == "include-curated-fact"]
    assert "Grade's `max_endotoxin`" in problem.message, problems(result)
    assert "`sop`" not in problem.message
    limit = "  max_endotoxin: { type: float, label: Endotoxin limit (EU/mL), min: 0 }\n"
    fixed = graded(
        tmp_path / "fixed",
        fixed=True,
        **files,
        **{
            "types/grade.yaml": GRADE.replace("IMMUTABLE", "immutable: true").replace(
                limit, limit.replace("min: 0 }", "min: 0, immutable: true }")
            )
        },
    )
    assert fixed.problems == [], problems(fixed)
