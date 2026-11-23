"""The two signed review paths for a bank check and the results each test accepts."""

from pathlib import Path
from typing import Any

import pytest

from iki_spec import rules
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


@pytest.fixture(scope="module")
def check_type() -> EntityType:
    dependencies = ["example_lims", "lots", "plasmid", "protein", "dna", "oligo"]
    loaded = load_plugin(PLUGINS / "cell_lines", dependencies=[PLUGINS / n for n in dependencies])
    assert loaded.problems == []
    assert loaded.plugin is not None
    return loaded.plugin.types["iki/cell_lines.CellCheck"]


def test_lab_roles_select_the_signed_flow_at_project_setup(check_type: EntityType) -> None:
    review = check_type.fields["review"]
    assert review.initial == "recorded"
    for state, role, meaning in [
        ("signed_off", "research_signer", "approval"),
        ("submitted", "analyst", "authorship"),
        ("reviewed", "reviewer", "review"),
        ("released", "qa", "approval"),
    ]:
        target = review.states[state]
        assert target.locks
        assert target.set_by == (f"iki/cell_lines.{role}",)
        assert [signature.meaning for signature in target.signatures] == [meaning]
    assert review.states["submitted"].on_edit == review.states["reviewed"].on_edit == "recorded"
    assert review.states["recorded"].reason == "required"
    assert review.states["signed_off"].on_edit is None
    assert review.states["released"].on_edit is None


@pytest.mark.parametrize(
    ("history", "target", "actor", "allowed"),
    [
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
    }
    guards = review.states[target].guards.values()
    (outcomes,) = rules.evaluate(variables, [(g.check, rules.CONDITION) for g in guards], [context])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    assert all(o.value for o in outcomes) == allowed


def test_test_kinds_have_distinct_result_vocabularies(check_type: EntityType) -> None:
    rule = check_type.rules["result_for_test"]
    assert rule.check is not None
    variables = rules.rule_variables(rules.record_schema(check_type.label, check_type.fields, None))
    for kind, valid in [
        ("str_authentication", {"match", "mismatch", "inconclusive"}),
        ("mycoplasma", {"positive", "negative", "inconclusive"}),
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
