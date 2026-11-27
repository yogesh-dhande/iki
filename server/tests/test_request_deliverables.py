"""QC deliverables require independent signatures, and their material still on the request.
An analytical service's own measurements are its result type's: see
examples/plugins/analytical_testing."""

from collections.abc import Mapping
from functools import cache
from typing import Any

import pytest
from first_party_dependencies import FIRST_PARTY, PLUGINS

from iki_spec import rules
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
    return request_plugin().types["iki/requests.QCDeliverable"]


def allows(
    result_type: EntityType,
    current: str,
    target: str,
    person: str,
    values: dict[str, Any],
    *,
    reviewed_from: str = "submitted",
    value_editor: str | None = None,
    covered: bool = True,
) -> bool:
    """Evaluate the example's guards with the people who signed the latest transitions."""
    review = result_type.fields["review"]
    guards = review.states[target].guards.values()
    types = request_plugin().types
    fields = {**types["iki/requests.Deliverable"].fields, **result_type.fields}
    request = types["iki/requests.Request"]

    def refs(name: str) -> tuple[str, Mapping[str, Field]]:
        return (request.label, request.fields) if name == "iki/requests.QCRequest" else (name, {})

    record = rules.record_schema(result_type.label, fields, refs)
    variables = rules.guard_variables(record, {"review": review}, [], review)
    history = [{"from": "", "to": "recorded", "by": "creator"}]
    if current != "recorded" and reviewed_from == "submitted":
        history.append({"from": "recorded", "to": "submitted", "by": "analyst"})
    if current in ("reviewed", "released"):
        history.append({"from": reviewed_from, "to": "reviewed", "by": "reviewer"})
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
    signer = {"submitted": "analyst", "reviewed": "reviewer", "released": "qa"}[target]
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


def test_an_older_unsigned_review_needs_fresh_review(analysis_result: EntityType) -> None:
    values = {"material": {"id": "sample-id"}}
    assert allows(
        analysis_result, "reviewed", "recorded", "analyst", values, reviewed_from="recorded"
    )
    assert not allows(
        analysis_result, "reviewed", "released", "qa", values, reviewed_from="recorded"
    )


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
