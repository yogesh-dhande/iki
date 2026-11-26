"""Mass spectrometry results' review: Research, Regulated QC, or Record only (ADR 0258)."""

from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

RESULTS = [
    ("proteomics", "iki/proteomics.ProteomicsResult"),
    ("biologics_ms", "iki/biologics_ms.MSResult"),
]
SIGNED = ("signed_off", "submitted", "reviewed", "released")
#: Created by the analyst, submitted by them and independently reviewed.
REVIEWED = [
    {"from": "", "to": "recorded", "by": "analyst"},
    {"from": "recorded", "to": "submitted", "by": "analyst"},
    {"from": "submitted", "to": "reviewed", "by": "reviewer"},
]


@pytest.fixture(scope="module", params=RESULTS, ids=[plugin for plugin, _ in RESULTS])
def result(request: pytest.FixtureRequest) -> EntityType:
    plugin, name = request.param
    directory = PLUGINS / plugin
    loaded = load_plugin(directory, dependencies=FIRST_PARTY[directory])
    assert loaded.plugin is not None, loaded.problems
    return loaded.plugin.types[name]


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
                "record": {"review_flow": flow, "notes": None},
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


def test_research_is_one_reviewers_sign_off(result: EntityType) -> None:
    created = REVIEWED[:1]
    assert refusals(result, "signed_off", "recorded", "reviewer", "research", created) == []
    assert refusals(result, "submitted", "recorded", "analyst", "research", created) == ["qc"]
    assert "research" in refusals(result, "signed_off", "recorded", "x", "regulated_qc", created)


def test_regulated_qc_needs_independent_review_and_release(result: EntityType) -> None:
    submitted = REVIEWED[:2]
    qc = "regulated_qc"
    assert refusals(result, "submitted", "recorded", "analyst", qc, REVIEWED[:1]) == []
    assert refusals(result, "reviewed", "submitted", "reviewer", qc, submitted) == []
    assert refusals(result, "reviewed", "submitted", "analyst", qc, submitted) == ["independent"]
    edited = refusals(result, "reviewed", "submitted", "reviewer", qc, submitted, "reviewer")
    assert edited == ["independent"]
    assert refusals(result, "released", "reviewed", "qa", qc, REVIEWED) == []
    for person in ("analyst", "reviewer"):
        assert refusals(result, "released", "reviewed", person, qc, REVIEWED) == ["independent"]
    assert "reviewed" in refusals(result, "released", "submitted", "qa", qc, submitted)


@pytest.mark.parametrize("target", SIGNED)
def test_a_record_only_result_offers_no_sign_off(result: EntityType, target: str) -> None:
    for previous in ("recorded", "submitted", "reviewed"):
        refused = refusals(result, target, previous, "qa", "record_only", REVIEWED)
        assert refused[0] == "reviewed_procedure", (target, previous, refused)
    message = result.fields["review"].states[target].guards["reviewed_procedure"].message
    assert message == "This result was recorded under Record only, which has no sign-off."


def test_every_result_pins_its_procedure_and_keeps_its_evidence(result: EntityType) -> None:
    field: Any = result.fields["review_flow"]
    assert tuple(field.values) == ("research", "regulated_qc", "record_only")
    assert field.computed and field.immutable
    assert "record.review_flow != null" in (result.rules["action_evidence"].check or "")


FORMATS = {
    "iki/proteomics.ProteomicsResult": (
        "format",
        ("mztab_1_0", "maxquant_txt", "diann_tsv", "fragpipe_tsv"),
    ),
    "iki/biologics_ms.MSResult": ("report_format", ("iki_mass_table_1", "iki_peptide_map_1")),
}


def test_every_result_names_its_versioned_report_format(result: EntityType) -> None:
    name, values = FORMATS[result.qualified_name]
    field: Any = result.fields[name]
    assert tuple(field.values) == values
    assert field.computed and field.immutable
