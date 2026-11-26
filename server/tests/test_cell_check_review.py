"""The three procedures for a bank check and the results each test accepts."""

from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

PLUGIN = PLUGINS / "cell_lines"


@pytest.fixture(scope="module")
def check_type() -> EntityType:
    loaded = load_plugin(PLUGIN, dependencies=FIRST_PARTY[PLUGIN])
    assert loaded.problems == []
    assert loaded.plugin is not None
    return loaded.plugin.types["iki/cell_lines.CellCheck"]


def test_lab_roles_select_the_procedure_at_project_setup(check_type: EntityType) -> None:
    review = check_type.fields["review"]
    assert review.initial == "recorded"
    accepted = review.states["accepted"]
    assert accepted.locks
    assert accepted.on_edit is None
    assert accepted.set_by == ("iki/cell_lines.recorder",)
    assert accepted.signatures == ()
    for state, role, meaning in [
        ("signed_off", "research_signer", "approval"),
        ("submitted", "analyst", "authorship"),
        ("reviewed", "reviewer", "review"),
        ("released", "qa", "approval"),
    ]:
        target = review.states[state]
        assert target.locks
        assert target.on_edit is None
        assert target.set_by == (f"iki/cell_lines.{role}",)
        assert [signature.meaning for signature in target.signatures] == [meaning]
    assert review.states["recorded"].reason == "required"


@pytest.mark.parametrize(
    ("history", "target", "actor", "allowed"),
    [
        (["recorded"], "accepted", "recorder", True),
        (["recorded", "submitted", "recorded"], "accepted", "recorder", False),
        (["recorded", "accepted"], "recorded", "editor", False),
        (["recorded"], "signed_off", "signer", True),
        (["recorded"], "submitted", "analyst", True),
        (["recorded"], "reviewed", "reviewer", False),
        (["recorded"], "released", "qa", False),
        (["recorded", "submitted"], "reviewed", "creator", False),
        (["recorded", "submitted"], "reviewed", "analyst", False),
        (["recorded", "submitted"], "reviewed", "editor", False),
        (["recorded", "submitted"], "reviewed", "reviewer", True),
        (["recorded", "submitted", "reviewed"], "released", "creator", False),
        (["recorded", "submitted", "reviewed"], "released", "analyst", False),
        (["recorded", "submitted", "reviewed"], "released", "editor", False),
        (["recorded", "submitted", "reviewed"], "released", "reviewer", False),
        (["recorded", "submitted", "reviewed"], "released", "qa", True),
        (["recorded", "submitted", "recorded"], "signed_off", "signer", False),
        (["recorded", "submitted", "recorded"], "submitted", "analyst", True),
        (["recorded", "submitted", "reviewed"], "recorded", "editor", True),
        (["recorded", "signed_off"], "recorded", "signer", False),
        (["recorded", "submitted", "reviewed", "released"], "recorded", "qa", False),
    ],
)
def test_who_can_sign_a_check_at_each_step(
    check_type: EntityType, history: list[str], target: str, actor: str, allowed: bool
) -> None:
    review = check_type.fields["review"]
    schema = rules.record_schema(check_type.label, check_type.fields, None)
    variables = rules.guard_variables(schema, {"review": review}, [], review)
    people = {
        "recorded": "creator",
        "submitted": "analyst",
        "reviewed": "reviewer",
        "released": "qa",
        "signed_off": "signer",
        "accepted": "recorder",
    }
    changes = [
        {"from": previous, "to": state, "by": people[state]}
        for previous, state in zip(["", *history[:-1]], history, strict=True)
    ]
    last: dict[str, Any] = dict.fromkeys(review.states)
    last.update({change["to"]: change for change in changes})
    context = {
        "state": {"field": "review", "from": history[-1], "to": target},
        "actor": {"person": actor},
        "history": {"review": changes},
        "last": {"review": last},
        "edits": {"review": {"recorded": [{"by": "editor", "fields": ["result"]}]}},
        "record": {"report": "a-report"},
    }
    guards = review.states[target].guards.values()
    (outcomes,) = rules.evaluate(variables, [(g.check, rules.CONDITION) for g in guards], [context])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    assert all(o.value for o in outcomes) == allowed


def test_an_unsigned_acceptance_needs_the_report(check_type: EntityType) -> None:
    review = check_type.fields["review"]
    schema = rules.record_schema(check_type.label, check_type.fields, None)
    variables = rules.guard_variables(schema, {"review": review}, [], review)
    guard = review.states["accepted"].guards["report_attached"]
    contexts = [
        {
            "state": {"field": "review", "from": "recorded", "to": "accepted"},
            "record": {"report": report},
        }
        for report in ("a-report", None)
    ]
    outcomes = rules.evaluate(variables, [(guard.check, rules.CONDITION)], contexts)
    assert [row[0].value for row in outcomes] == [True, False]


def test_test_kinds_have_distinct_result_vocabularies(check_type: EntityType) -> None:
    rule = check_type.rules["result_for_test"]
    assert rule.check is not None
    variables = rules.rule_variables(rules.record_schema(check_type.label, check_type.fields, None))
    identity = {"match", "mismatch", "inconclusive"}
    contamination = {"positive", "negative", "inconclusive"}
    for kind, valid in [
        ("str_authentication", identity),
        ("species_identity", identity),
        ("mycoplasma", contamination),
        ("sterility", contamination),
        ("viral_testing", contamination),
        ("karyotype", {"conforms", "does_not_conform", "inconclusive"}),
        ("other", set(check_type.fields["result"].values)),
    ]:
        choices = check_type.fields["result"].values
        records = [{"record": {"kind": kind, "result": choice}} for choice in choices]
        results = rules.evaluate(variables, [(rule.check, rules.CONDITION)], records)
        assert all(row[0].error is None for row in results)
        assert {
            choice for choice, row in zip(choices, results, strict=True) if row[0].value
        } == valid


def test_check_date_is_between_banking_and_the_next_due_date(check_type: EntityType) -> None:
    fields = check_type.fields
    # A rule sees one level of fields on the bank it refers to.
    schema = rules.record_schema(
        check_type.label, fields, lambda _: ("Cell bank", {"banked_on": fields["checked_on"]})
    )
    checks = []
    for name in ("existing_bank", "next_check_after_test"):
        condition = check_type.rules[name].check
        assert condition is not None
        checks.append((condition, rules.CONDITION))
    dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
    contexts = [
        {
            "record": {
                "checked_on": f"{checked}T00:00:00Z",
                "bank": {"banked_on": "2026-01-02T00:00:00Z"},
                "due_on": "2026-01-02T00:00:00Z",
            }
        }
        for checked in dates
    ]
    outcomes = rules.evaluate(rules.rule_variables(schema), checks, contexts)
    assert all(o.error is None for row in outcomes for o in row)
    assert [[o.value for o in row] for row in outcomes] == [
        [False, True],
        [True, True],
        [True, False],
    ]
    # A check without a repeat date skips the second rule.
    assert check_type.rules["next_check_after_test"].when == "record.due_on != null"


def test_another_test_is_named(check_type: EntityType) -> None:
    rule = check_type.rules["named_other_test"]
    assert rule.check is not None
    variables = rules.rule_variables(rules.record_schema(check_type.label, check_type.fields, None))
    contexts = [
        {"record": {"kind": kind, "test": test}}
        for kind, test in [
            ("other", "Endotoxin"),
            ("other", None),
            ("sterility", None),
            ("sterility", "Endotoxin"),
        ]
    ]
    outcomes = rules.evaluate(variables, [(rule.check, rules.CONDITION)], contexts)
    assert [row[0].value for row in outcomes] == [True, False, True, False]
