"""Computed fields worked out by an expression (ADR 0297): `computed: {cel: …}`, checked
against the record's own fields by iki-wasm, as rules are, and worked out by it."""

import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec import rules
from iki_spec.model import plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

pytestmark = pytest.mark.wasm

ROLES = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""


def make(tmp_path: Path, types: str, plugin: str) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(textwrap.dedent(plugin))
    (root / "types" / "assay.yaml").write_text(textwrap.dedent(types))
    return load_plugin(root)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


#: A buffer the lab makes up, with a shelf life; and what it's worked out to.
BUFFER = """\
apiVersion: iki/v1
kind: EntityType
name: Assay
label: Buffer batch
schema_version: 1
hrid: { prefix: BUF }
fields:
  prepared_on: { type: date, required: true }
  shelf_life_days: { type: int, min: 1, default: 30 }
  volume: { type: quantity, unit: mL, min: 0 }
  aliquots: { type: int, min: 1 }
  ph: { type: float }
  lot: { type: ref, to: Assay }
  expires_on:
    type: date
    label: Expires on
    computed:
      cel: record.prepared_on + duration(string(record.shelf_life_days * 24) + "h")
  aliquot_volume:
    type: quantity
    unit: uL
    min: 0
    computed: { cel: record.volume * 1000.0 / double(record.aliquots) }
  ph_band:
    type: enum
    values: [acidic, neutral, basic]
    computed: { cel: 'record.ph < 6.5 ? "acidic" : record.ph > 7.5 ? "basic" : "neutral"' }
  label_text:
    type: string
    max_length: 60
    computed: { cel: string(record.aliquots) + " × " + string(record.aliquot_volume) + " µL" }
"""


def buffer(**replace: str) -> str:
    out = BUFFER
    for old, new in replace.items():
        out = out.replace(old, new)
    return out


def test_a_computed_field_is_checked_and_says_what_it_reads(tmp_path: Path) -> None:
    result = make(tmp_path, BUFFER, ROLES)
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    fields = result.plugin.types["acme/lab.Assay"].fields
    expires = fields["expires_on"]
    assert expires.computed and expires.worked_out and not expires.set_by_runs
    assert expires.cel_reads == ("prepared_on", "shelf_life_days")
    assert fields["label_text"].cel_reads == ("aliquot_volume", "aliquots")
    # Worked out after what they read: the label after the aliquot's volume.
    order = rules.computed_order(fields)
    assert order.index("aliquot_volume") < order.index("label_text")
    # What the server reads from the built plugin is what was checked.
    again = plugin_from_dict(plugin_to_dict(result.plugin))
    assert again.types["acme/lab.Assay"].fields["label_text"] == fields["label_text"]
    assert "cel" not in plugin_to_dict(result.plugin)["types"]["acme/lab.Assay"]["fields"]["ph"]


def test_iki_wasm_works_a_computed_value_out_as_its_field_holds_it(tmp_path: Path) -> None:
    result = make(tmp_path, BUFFER, ROLES)
    assert result.plugin is not None
    entity_type = result.plugin.types["acme/lab.Assay"]
    fields = entity_type.fields
    names = ["expires_on", "aliquot_volume", "ph_band"]
    asked = [(fields[n].cel or "", rules.VALUE, rules.computed_result(fields[n])) for n in names]
    contexts = [
        {
            "record": {
                "prepared_on": "2026-11-27T00:00:00Z",
                "shelf_life_days": 30,
                "volume": 50.0,
                "aliquots": 200,
                "ph": 7.4,
            }
        },
        {"record": {"prepared_on": "2026-11-27T00:00:00Z", "volume": 50.0, "aliquots": 0}},
    ]
    found = rules.evaluate(rules.computed_variables(entity_type.label, fields), asked, contexts)
    assert [o.result for o in found[0]] == ["2026-12-27T00:00:00Z", 250.0, "neutral"]
    # Without a shelf life or a pH, or with no aliquots, nothing is worked out.
    assert [o.result for o in found[1]] == [None, None, None]
    assert [o.empty for o in found[1]] == [True, False, True]


@pytest.mark.parametrize(
    ("old", "new", "code", "word"),
    [
        # What it reads must be the record's own values.
        ("record.ph < 6.5", "record.lot.ph < 6.5", "computed-reads", "reference"),
        (
            "record.ph < 6.5",
            "today > record.prepared_on && record.ph < 6.5",
            "computed-reads",
            "tomorrow",
        ),
        ("string(record.aliquots) +", "record.hrid +", "computed-reads", "created"),
        # It's one value of its field's kind.
        ('"neutral"', '"neutrall"', "invalid-expression", "isn't one of Ph band's values"),
        ("record.volume * 1000.0", "string(record.volume)", "invalid-expression", "number"),
        # And not what its value would clash with.
        (
            "    max_length: 60\n",
            "    max_length: 60\n    unique: true\n    indexed: true\n",
            "computed-unique",
            "clash",
        ),
        (
            "  ph_band:\n    type: enum",
            "  ph_band:\n    immutable: true\n    type: enum",
            "computed-immutable",
            "every save",
        ),
        (
            "    label: Expires on\n",
            "    label: Expires on\n    required: true\n",
            "computed-required",
            "expression",
        ),
    ],
)
def test_a_computed_field_that_couldnt_be_worked_out_again_is_refused(
    tmp_path: Path, old: str, new: str, code: str, word: str
) -> None:
    assert word in only(make(tmp_path, buffer(**{old: new}), ROLES), code)


def test_computed_fields_that_read_each_other_round_are_refused(tmp_path: Path) -> None:
    looped = buffer(
        **{"record.volume * 1000.0 / double(record.aliquots)": ("double(size(record.label_text))")}
    )
    result = make(tmp_path, looped, ROLES)
    assert codes(result) == ["computed-cycle", "computed-cycle"]
    assert "`aliquot_volume` → `label_text` → `aliquot_volume`" in result.problems[0].message


def test_only_one_value_can_be_computed(tmp_path: Path) -> None:
    listed = buffer(
        **{
            "  ph_band:\n    type: enum\n    values: [acidic, neutral, basic]\n": (
                "  ph_band:\n    type: json\n"
            )
        }
    )
    assert "json field" in only(make(tmp_path, listed, ROLES), "computed-kind")


def test_no_action_updates_a_computed_value(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    make(tmp_path, BUFFER, ROLES)
    (root / "actions").mkdir()
    (root / "environments").mkdir(exist_ok=True)
    (root / "environments" / "python.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: iki/v1
            kind: Environment
            name: python
            tiers: [backend]
            """
        )
    )
    (root / "actions" / "expire.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: iki/v1
            kind: Action
            name: expire
            inputs:
              batch: { type: Assay, schema: ">=1" }
            effects:
              updates: [{ input: batch, fields: [expires_on] }]
            runs_on: { tier: backend, environment: python }
            command: [python, ./expire.py]
            """
        )
    )
    (root / "expire.py").write_text("")
    result = load_plugin(root)
    assert "updates-cel-computed" in codes(result), [(p.code, p.message) for p in result.problems]
