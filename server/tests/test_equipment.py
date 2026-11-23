"""Equipment review and validity rules evaluated with the production CEL module."""

from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
from iki_spec.model import EntityType, Plugin
from iki_spec.validate import load_plugin

PREFIX = "iki/equipment."
SERVICED = {
    "performed_by": "engineer",
    "performed_at": "2026-01-05T09:00:00Z",
    "outcome": "in_tolerance",
    "report": "synthetic-report",
}


@pytest.fixture(scope="module")
def equipment() -> Plugin:
    plugin = PLUGINS / "equipment"
    loaded = load_plugin(plugin, dependencies=FIRST_PARTY[plugin])
    assert loaded.plugin is not None, loaded.problems
    return loaded.plugin


def allows(
    entity_type: EntityType,
    target: str,
    previous: str,
    actor: str,
    values: dict[str, Any],
    *,
    editor: str | None = None,
) -> bool:
    state = entity_type.fields["review"]
    schema = rules.record_schema(entity_type.label, entity_type.fields, None)
    variables = rules.guard_variables(schema, {"review": state}, [], state)
    initial = state.initial
    assert initial is not None
    transitions: list[dict[str, str]] = [
        {"from": "", "to": initial, "by": "scheduler"},
        {"from": initial, "to": "submitted", "by": "engineer"},
        {"from": "submitted", "to": "reviewed", "by": "reviewer"},
    ]
    (outcomes,) = rules.evaluate(
        variables,
        [(guard.check, rules.CONDITION) for guard in state.states[target].guards.values()],
        [
            {
                "record": values,
                "actor": {"person": actor},
                "state": {"field": "review", "from": previous, "to": target},
                "history": {"review": transitions},
                "last": {"review": {change["to"]: change for change in transitions}},
                "edits": {"review": {initial: [{"by": editor}] if editor else []}},
            }
        ],
    )
    assert all(outcome.error is None or outcome.empty for outcome in outcomes), outcomes
    return all(outcome.value is True for outcome in outcomes)


def test_only_the_engineer_who_performed_the_service_can_sign(equipment: Plugin) -> None:
    service = equipment.types[PREFIX + "EquipmentService"]
    research = SERVICED | {"review_flow": "research"}
    qc = SERVICED | {"review_flow": "regulated_qc"}
    assert allows(service, "signed_off", "scheduled", "engineer", research)
    assert not allows(service, "signed_off", "scheduled", "another engineer", research)
    assert not allows(service, "signed_off", "scheduled", "engineer", qc)
    assert allows(service, "submitted", "scheduled", "engineer", qc)
    assert not allows(service, "submitted", "scheduled", "engineer", research)
    assert not allows(service, "submitted", "scheduled", "engineer", qc | {"report": None})
    assert not allows(service, "scheduled", "signed_off", "engineer", research)
    assert not allows(service, "scheduled", "released", "engineer", qc)


def test_cancel_only_unperformed_appointments(equipment: Plugin) -> None:
    service = equipment.types[PREFIX + "EquipmentService"]
    planned = {"performed_at": None, "outcome": None, "report": None}
    assert allows(service, "cancelled", "scheduled", "engineer", planned)
    assert not allows(service, "cancelled", "scheduled", "engineer", SERVICED)
    assert not allows(service, "cancelled", "released", "engineer", SERVICED)


@pytest.mark.parametrize("type_name", ["EquipmentService", "CalibrationImpact"])
def test_regulated_review_and_qa_release_require_independent_signers(
    equipment: Plugin, type_name: str
) -> None:
    type_ = equipment.types[PREFIX + type_name]
    values = {"review_flow": "regulated_qc"}
    assert allows(type_, "reviewed", "submitted", "reviewer", values)
    for person in ("scheduler", "engineer"):
        assert not allows(type_, "reviewed", "submitted", person, values)
        assert not allows(type_, "released", "reviewed", person, values)
    assert not allows(type_, "reviewed", "submitted", "editor", values, editor="editor")
    assert not allows(type_, "released", "reviewed", "reviewer", values)
    assert not allows(type_, "released", "reviewed", "qa", values, editor="qa")
    assert allows(type_, "released", "reviewed", "qa", values)
    assert not allows(type_, "released", "submitted", "qa", values)


def test_research_impact_needs_a_reasoned_disposition(equipment: Plugin) -> None:
    impact = equipment.types[PREFIX + "CalibrationImpact"]
    assessed = {
        "review_flow": "research",
        "disposition": "repeat_required",
        "reasoning": "Synthetic check outside the lab's tolerance.",
    }
    assert allows(impact, "signed_off", "recorded", "qa", assessed)
    assert not allows(impact, "signed_off", "recorded", "qa", assessed | {"reasoning": None})
    assert not allows(
        impact, "signed_off", "recorded", "qa", assessed | {"review_flow": "regulated_qc"}
    )


def test_validity_at_measurement_uses_the_selected_instrument_and_final_calibration(
    equipment: Plugin,
) -> None:
    type_ = equipment.types[PREFIX + "InstrumentUse"]
    guard = type_.rules["valid_for_measurement"]
    assert guard.check is not None
    references = {
        "iki/example_lims.Instrument": ("Instrument", {}),
        "iki/core.Entity": ("Entity", {}),
        PREFIX + "EquipmentService": (
            "Equipment service",
            equipment.types[PREFIX + "EquipmentService"].fields,
        ),
    }
    schema = rules.record_schema(type_.label, type_.fields, references.get)
    passing = SERVICED | {
        "instrument": "spectrophotometer",
        "kind": "calibration",
        "review": "released",
        "valid_until": "2026-02-05T09:00:00Z",
    }
    values = {
        "instrument": {"id": "spectrophotometer"},
        "calibration": passing,
        "measurement_at": SERVICED["performed_at"],
    }
    cases = [
        values,
        values | {"measurement_at": passing["valid_until"]},
        values | {"calibration": passing | {"instrument": "other"}},
        values | {"calibration": passing | {"review": "reviewed"}},
        values | {"calibration": passing | {"outcome": "out_of_tolerance"}},
    ]
    outcomes = rules.evaluate(
        rules.rule_variables(schema),
        [(guard.check, rules.CONDITION)],
        [{"record": record, "now": "2026-02-07T09:00:00Z"} for record in cases],
    )
    assert all(result.error is None for [result] in outcomes), outcomes
    assert [result.value for [result] in outcomes] == [True, False, False, False, False]
