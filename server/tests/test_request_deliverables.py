"""A deliverable returned under Regulated QC needs independent signatures, and its material
still on the request: core's result review, compiled with the deliverable's own guards
(ADRs 0301, 0305). An analytical service's own measurements are its result type's: see
examples/plugins/analytical_testing."""

import dataclasses
from collections.abc import Mapping
from functools import cache
from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import procedures, rules
from iki_spec.builtin import core_plugin
from iki_spec.model import EntityType, Field, Plugin
from iki_spec.validate import load_plugin

PLUGIN = PLUGINS / "requests"


@cache
def request_plugin() -> Plugin:
    loaded = load_plugin(PLUGIN, dependencies=FIRST_PARTY[PLUGIN])
    assert loaded.problems == []
    assert loaded.plugin is not None
    return loaded.plugin


@pytest.fixture(scope="module")
def analysis_result() -> EntityType:
    """The deliverable, as one pinned to Regulated QC has it."""
    deliverable = request_plugin().types["iki/requests.Deliverable"]
    assert deliverable.review is not None
    procedure = core_plugin().procedures["iki/core.result_review@1"]
    binding = {slot: f"{procedures.SHARED_ROLES}.{slot}" for slot in procedure.slots}
    review = procedures.review_field(deliverable.review, procedure, "regulated_qc", binding=binding)
    return dataclasses.replace(deliverable, fields={**deliverable.fields, "review": review})


def allows(
    result_type: EntityType,
    current: str,
    target: str,
    person: str,
    values: dict[str, Any],
    *,
    value_editor: str | None = None,
    covered: bool = True,
) -> bool:
    """Evaluate the example's guards with the people who signed the latest transitions."""
    review = result_type.fields["review"]
    guards = review.states[target].guards.values()
    types = request_plugin().types
    fields = result_type.fields
    request = types["iki/requests.Request"]

    def refs(name: str) -> tuple[str, Mapping[str, Field]]:
        return (request.label, request.fields) if name == "iki/requests.Request" else (name, {})

    record = rules.record_schema(result_type.label, fields, refs)
    variables = rules.guard_variables(record, {"review": review}, [], review)
    history = [{"from": "", "to": "recorded", "by": "creator"}]
    if current != "recorded":
        history.append({"from": "recorded", "to": "submitted", "by": "analyst"})
    if current in ("reviewed", "released"):
        history.append({"from": "submitted", "to": "reviewed", "by": "reviewer"})
    if current == "released":
        history.append({"from": "reviewed", "to": "released", "by": "qa"})
    context = {
        "state": {"field": "review", "from": current, "to": target},
        "record": {
            **values,
            "review": current,
            "request": {
                "standing": "open",
                "status": "in_progress",
                "materials": ["sample-id"],
            },
        },
        "covered_subjects": ["sample-id"] if values.get("material") and covered else [],
        "actor": {"person": person},
        "last": {"review": {change["to"]: change for change in history}},
        "history": {"review": history},
        "edits": {
            "review": {
                "recorded": [{"by": value_editor, "fields": ["purity"]}] if value_editor else []
            }
        },
    }
    checks = [
        (f"!({g.when}) || ({g.check})" if g.when else g.check, rules.CONDITION) for g in guards
    ]
    (outcomes,) = rules.evaluate(variables, checks, [context])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    return all(o.value for o in outcomes)


@pytest.mark.parametrize("target", ["submitted", "reviewed", "released"])
def test_each_signature_needs_the_material_still_on_the_request(
    analysis_result: EntityType, target: str
) -> None:
    current = {"submitted": "recorded", "reviewed": "submitted", "released": "reviewed"}[target]
    # Its maker submits it: whoever recorded it (ADR 0301).
    signer = {"submitted": "creator", "reviewed": "reviewer", "released": "qa"}[target]
    on_request = {"material": {"id": "sample-id"}}
    assert allows(analysis_result, current, target, signer, on_request)
    assert not allows(analysis_result, current, target, signer, on_request, covered=False)
    # A deliverable for no material, of a request that commissions none, covers nothing.
    assert allows(analysis_result, current, target, signer, {}, covered=False)


def test_qa_cannot_release_an_unreviewed_analysis(analysis_result: EntityType) -> None:
    for state in ("recorded", "submitted"):
        assert not allows(analysis_result, state, "released", "qa", {})


@pytest.mark.parametrize("person", ["creator", "analyst"])
def test_qa_cannot_release_a_result_they_created_or_submitted(
    analysis_result: EntityType, person: str
) -> None:
    values = {"material": {"id": "sample-id"}}
    assert not allows(analysis_result, "reviewed", "released", person, values)


def test_a_review_follows_a_submission_and_a_correction_needs_both_again(
    analysis_result: EntityType,
) -> None:
    values = {"material": {"id": "sample-id"}}
    assert not allows(analysis_result, "recorded", "reviewed", "reviewer", values)
    assert allows(analysis_result, "reviewed", "recorded", "analyst", values)
    assert not allows(analysis_result, "recorded", "released", "qa", values)


@pytest.mark.parametrize(
    ("current", "target"), [("submitted", "reviewed"), ("reviewed", "released")]
)
def test_a_value_editor_cannot_sign_for_someone_elses_work(
    analysis_result: EntityType, current: str, target: str
) -> None:
    assert not allows(analysis_result, current, target, "editor", {}, value_editor="editor")
    assert allows(analysis_result, current, target, "independent", {}, value_editor="editor")


def test_correcting_a_signed_result_requires_an_explicit_return(
    analysis_result: EntityType,
) -> None:
    states = analysis_result.fields["review"].states
    for name in ("submitted", "reviewed"):
        assert states[name].on_edit is None
        assert allows(analysis_result, name, "recorded", "analyst", {})
    assert not allows(analysis_result, "released", "recorded", "qa", {})
