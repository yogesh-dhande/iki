"""Validating plugins' rules (ADR 0127) and guards (ADR 0128): their kinds and fields, and
their CEL, type-checked by iki-wasm against the type's fields, with what each reads."""

import shutil
import textwrap
import threading
from pathlib import Path

import pytest

from iki_spec import wasm
from iki_spec.model import plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

pytestmark = pytest.mark.wasm

ROLES = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
iki: ">=0.1"
roles:
  analyst: { label: Analyst }
  qa: { label: QA }
"""
TYPE = """\
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: { prefix: ASY }
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


FIELDS = """\
fields:
  purity: { type: float, min: 0, max: 100 }
  result: { type: enum, values: [pass, fail] }
  plate: { type: string, max_length: 20 }
  well: { type: string, max_length: 4 }
  stored: { type: bool, default: true }
  starts_at: { type: datetime }
  ends_at: { type: datetime }
  notes: { type: text }
  status:
    type: state
    initial: received
    states:
      received: { label: Received, set_by: [analyst] }
      in_review: { label: In review, set_by: [analyst] }
      released:
        label: Released
        set_by: [qa]
        locks: true
        guards:
          reviewed:
            check: state.from == "in_review"
            message: It's released from In review.
          four_eyes:
            check: last.status.in_review == null || last.status.in_review.by != actor.person
            message: Someone else releases it.
          passed:
            check: record.result == "pass" && "qa" in actor.roles
            message: It passed.
"""


def rules(body: str) -> str:
    return TYPE + FIELDS + "rules:\n" + body


def test_rules_and_guards_are_checked_and_say_what_they_read(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        rules(
            """\
  purity_in_spec:
    label: Purity in specification
    check: record.purity >= 95.0
    message: Purity {{ record.purity }}% is out of specification.
    hold: { reviewed_by: [qa] }
  pass_in_spec:
    check: record.result != "pass" || record.purity >= 95.0
    message: It can't pass.
    fields: [result]
  one_per_well:
    unique: [plate, well]
    while: { stored: [true] }
    message: "{{ record.well }} is taken."
  one_at_a_time:
    no_overlap: { from: starts_at, to: ends_at, per: [plate] }
    message: It overlaps.
"""
        ),
        ROLES,
    )
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    assay = result.plugin.types["acme/lab.Assay"]
    purity = assay.rules["purity_in_spec"]
    assert (purity.kind, purity.reads, purity.fields, purity.hold) == (
        "check",
        ("purity",),
        ("purity",),
        ("acme/lab.qa",),
    )
    assert assay.rules["pass_in_spec"].reads == ("purity", "result")
    assert assay.rules["pass_in_spec"].fields == ("result",)
    well = assay.rules["one_per_well"]
    assert (well.kind, well.unique, well.only_while) == (
        "unique",
        ("plate", "well"),
        (("stored", ("true",)),),
    )
    overlap = assay.rules["one_at_a_time"]
    assert (overlap.kind, overlap.no_overlap, overlap.per) == (
        "no_overlap",
        ("starts_at", "ends_at"),
        ("plate",),
    )
    released = assay.fields["status"].states["released"]
    assert list(released.guards) == ["reviewed", "four_eyes", "passed"]
    assert released.guards["passed"].reads == ("result",)


def test_a_rule_that_misnames_a_field_says_what_was_meant(tmp_path: Path) -> None:
    body = "  bad:\n    check: record.purty >= 95.0\n    message: No.\n"
    message = only(make(tmp_path, rules(body), ROLES), "invalid-expression")
    assert "purty" in message
    result = make(tmp_path, rules(body), ROLES)
    assert result.problems[0].hint is not None and "purity" in result.problems[0].hint


@pytest.mark.parametrize(
    ("check", "word"),
    [
        ('record.result == "passed"', "passed"),
        ('record.purity >= "high"', ">="),
        ("record.notes.matches('x')", "matches"),
    ],
)
def test_a_rule_that_could_never_work_is_refused(tmp_path: Path, check: str, word: str) -> None:
    body = (
        f"  bad:\n    check: '{check}'\n    message: No.\n"
        if '"' in check
        else (f'  bad:\n    check: "{check}"\n    message: No.\n')
    )
    assert word in only(make(tmp_path, rules(body), ROLES), "invalid-expression")


def test_a_guard_that_names_a_state_its_field_hasnt_is_refused(tmp_path: Path) -> None:
    body = (FIELDS).replace('state.from == "in_review"', 'state.from == "in_reveiw"')
    assert "in_reveiw" in only(make(tmp_path, TYPE + body, ROLES), "invalid-expression")


def test_a_guard_names_the_state_and_values_whose_edits_it_checks(tmp_path: Path) -> None:
    check = 'edits.status.received.all(e, !("purity" in e.fields) || e.by != actor.person)'
    body = TYPE + FIELDS.replace('state.from == "in_review"', check)
    assert make(tmp_path, body, ROLES).problems == []
    for name, wrong in [(".received.", ".recieved."), ('"purity"', '"purty"')]:
        bad = make(tmp_path, body.replace(check, check.replace(name, wrong)), ROLES)
        assert wrong.strip('".') in only(bad, "invalid-expression")


def test_draw_guards_are_typed_and_survive_the_plugin_build(tmp_path: Path) -> None:
    body = (
        TYPE
        + """\
amount: { unit: mg }
fields:
  plate: { type: string, max_length: 20 }
  result: { type: enum, values: [pass, fail] }
  source: { type: ref, to: Assay }
draw_guards:
  needs_a_result:
    when: operation == "draw" && purpose == "routine"
    check: record.result == "pass"
    message: "Plate {{ record.plate }} hasn't passed."
"""
    )
    result = make(tmp_path, body, ROLES)
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    built = plugin_from_dict(plugin_to_dict(result.plugin))
    guard = built.types["acme/lab.Assay"].draw_guards["needs_a_result"]
    assert guard.reads == ("plate", "result")
    bad = make(tmp_path, body.replace("record.result", "record.reslt"), ROLES)
    assert "reslt" in only(bad, "invalid-expression")
    without = make(tmp_path, body.replace("amount: { unit: mg }\n", ""), ROLES)
    assert "has no amount" in only(without, "draw-guards-without-amount")
    # A refusal is shown to everyone who can see this record. It can use the source
    # reference itself, but its message cannot disclose values from the other record.
    foreign = make(tmp_path, body.replace("{{ record.plate }}", "{{ record.source.plate }}"), ROLES)
    assert only(foreign, "invalid-expression")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        # A rule is one kind: a check, unique, or no_overlap.
        ("  x:\n    message: No.\n", "rule-kind"),
        ("  x:\n    check: 'true'\n    unique: [plate, well]\n    message: No.\n", "rule-kind"),
        ("  x:\n    unique: [plate, nope]\n    message: No.\n", "unknown-field"),
        # Text isn't compared, nor is a string a range's end.
        ("  x:\n    unique: [plate, notes]\n    message: No.\n", "rule-field"),
        ("  x:\n    no_overlap: { from: starts_at, to: plate }\n    message: No.\n", "rule-field"),
        # `while` is an enum, state or bool field's, and one of its values.
        (
            "  x:\n    unique: [plate, well]\n    while: { plate: [a] }\n    message: No.\n",
            "rule-field",
        ),
        (
            "  x:\n    unique: [plate, well]\n    while: { stored: [maybe] }\n    message: No.\n",
            "rule-while",
        ),
        (
            "  x:\n    check: record.purity > 1.0\n    fields: [nope]\n    message: No.\n",
            "unknown-field",
        ),
    ],
)
def test_rules_that_dont_fit_their_type(tmp_path: Path, body: str, code: str) -> None:
    assert codes(make(tmp_path, rules(body), ROLES)) == [code]


def test_a_hold_names_lab_roles(tmp_path: Path) -> None:
    body = (
        "  x:\n    check: record.purity > 1.0\n    hold: { reviewed_by: [nobody] }\n"
        "    message: No.\n"
    )
    assert codes(make(tmp_path, rules(body), ROLES)) == ["unknown-role"]


def test_without_iki_wasm_rules_cant_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wasm, "WASM", tmp_path / "missing.wasm")
    monkeypatch.setattr(wasm, "_compiled", None)
    monkeypatch.setattr(wasm, "_local", threading.local())
    body = "  x:\n    check: record.purity > 1.0\n    message: No.\n"
    assert "expressions-unchecked" in codes(make(tmp_path, rules(body), ROLES))


def test_the_module_keeps_only_utc() -> None:
    """Rules' times are UTC (ADR 0127): worker/.cargo/config.toml keeps chrono-tz to that
    zone, a megabyte less of the module browsers load. A build that missed it has them all."""
    assert wasm.WASM.is_file(), "Build iki-wasm first: scripts/build-wasm.sh"
    module = wasm.WASM.read_bytes()
    assert b"Europe/London" not in module
    assert b"America/New_York" not in module
