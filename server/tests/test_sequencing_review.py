"""A FASTQ summary's review: Research, Regulated QC, or Record only (ADR 0255)."""

from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.model import EntityType, Plugin
from iki_spec.validate import load_plugin

PLUGIN = PLUGINS / "sequencing"
SUMMARY = "iki/sequencing.FASTQSummary"
SIGNED = ("signed_off", "submitted", "reviewed", "released")
#: Created by the analyst, submitted by them and independently reviewed.
REVIEWED = [
    {"from": "", "to": "recorded", "by": "analyst"},
    {"from": "recorded", "to": "submitted", "by": "analyst"},
    {"from": "submitted", "to": "reviewed", "by": "reviewer"},
]


@pytest.fixture(scope="module")
def summary() -> EntityType:
    loaded = load_plugin(PLUGIN, dependencies=FIRST_PARTY[PLUGIN])
    plugin: Plugin | None = loaded.plugin
    assert plugin is not None, loaded.problems
    return plugin.types[SUMMARY]


def refusals(
    type_: EntityType,
    target: str,
    previous: str,
    actor: str,
    flow: str,
    history: list[dict[str, str]],
    editor: str | None = None,
) -> list[str]:
    """The names of the guards that refuse ``actor`` setting ``target`` now."""
    state = type_.fields["review"]
    schema = rules.record_schema(type_.label, type_.fields, None)
    variables = rules.guard_variables(schema, {"review": state}, [], state)
    guards = state.states[target].guards
    (outcomes,) = rules.evaluate(
        variables,
        [(guard.check, rules.CONDITION) for guard in guards.values()],
        [
            {
                "record": {"review_flow": flow, "statistics": [], "notes": None},
                "actor": {"person": actor},
                "state": {"field": "review", "from": previous, "to": target},
                "history": {"review": history},
                "last": {"review": {change["to"]: change for change in history}},
                "edits": {"review": {"recorded": [{"by": editor}] if editor else []}},
            }
        ],
    )
    refused: list[str] = []
    for name, outcome in zip(guards, outcomes, strict=True):
        assert outcome.error is None or outcome.empty, outcome
        if outcome.value is not True:
            refused.append(name)
    return refused


def test_research_is_one_reviewers_sign_off(summary: EntityType) -> None:
    created = REVIEWED[:1]
    assert refusals(summary, "signed_off", "recorded", "reviewer", "research", created) == []
    assert refusals(summary, "submitted", "recorded", "analyst", "research", created) == ["qc"]
    assert "research" in refusals(summary, "signed_off", "recorded", "x", "regulated_qc", created)


def test_regulated_qc_needs_independent_review_and_release(summary: EntityType) -> None:
    submitted = REVIEWED[:2]
    qc = "regulated_qc"
    assert refusals(summary, "submitted", "recorded", "analyst", qc, REVIEWED[:1]) == []
    assert refusals(summary, "reviewed", "submitted", "reviewer", qc, submitted) == []
    assert refusals(summary, "reviewed", "submitted", "analyst", qc, submitted) == ["independent"]
    edited = refusals(summary, "reviewed", "submitted", "reviewer", qc, submitted, "reviewer")
    assert edited == ["independent"]
    assert refusals(summary, "released", "reviewed", "qa", qc, REVIEWED) == []
    for person in ("analyst", "reviewer"):
        assert refusals(summary, "released", "reviewed", person, qc, REVIEWED) == ["independent"]
    assert "reviewed" in refusals(summary, "released", "submitted", "qa", qc, submitted)


@pytest.mark.parametrize("target", SIGNED)
def test_a_record_only_summary_offers_no_sign_off(summary: EntityType, target: str) -> None:
    for previous in ("recorded", "submitted", "reviewed"):
        refused = refusals(summary, target, previous, "qa", "record_only", REVIEWED)
        assert refused[0] == "reviewed_procedure", (target, previous, refused)
    message = summary.fields["review"].states[target].guards["reviewed_procedure"].message
    assert message == "This summary was recorded under Record only, which has no sign-off."


def test_every_summary_pins_its_procedure(summary: EntityType) -> None:
    field: Any = summary.fields["review_flow"]
    assert tuple(field.values) == ("research", "regulated_qc", "record_only")
    assert field.computed and field.immutable
    assert "record.review_flow != null" in (summary.rules["action_evidence"].check or "")
