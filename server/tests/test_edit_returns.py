"""Corrections of reviewed values return the record for review under the edit's row lock."""

from dataclasses import replace
from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as, patch
from test_rules import BATCH, made
from test_runs import world
from test_states import ANALYST, QA, REVIEWER, roles, set_

from iki_server.entities import checking, services, states
from iki_server.entities.models import Entity, Revision
from iki_server.org.services import Refused
from iki_spec.model import Rule

pytestmark = pytest.mark.django_db

__all__ = ["world"]


def approved(world: World) -> dict[str, Any]:
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    record = made(world, {"purity": 96, "result": "pass"})
    set_(world, "editor", record["id"], "status", "in_review")
    return set_(world, "admin", record["id"], "status", "approved")


def test_a_correction_records_the_return_and_the_new_values_together(world: World) -> None:
    ref = approved(world)["id"]
    api = api_as(world, "editor")
    # Naming the same normalized value is not a correction.
    same = patch(api, ref, {"values": {"purity": 96.0, "proteins": None}}, '"3"')
    assert same.status_code == 200, same.content
    assert same["ETag"] == '"3"'

    why = "Recalculated from the instrument file"
    body = {"reason": why, "values": {"purity": 97}}
    missing = problem(patch(api, ref, {**body, "reason": "  "}, '"3"'), 422, "validation_failed")
    assert missing["errors"][0]["pointer"] == "#/reason"
    saved = patch(api, ref, body, '"3"')
    assert saved.status_code == 200, saved.content
    assert saved["ETag"] == '"5"'
    assert (saved.json()["values"]["status"], saved.json()["locked"]) == ("in_testing", False)
    revisions = Revision.objects.filter(entity_id=ref, number__gte=4).order_by("number")
    back, edit = revisions
    assert (back.state_field, back.state_from, back.state_to) == (
        "status",
        "approved",
        "in_testing",
    )
    assert [(r.kind, r.changed_fields, r.user_id, r.reason) for r in (back, edit)] == [
        ("state_changed", ["status"], world.users["editor"].pk, why),
        ("edited", ["purity"], world.users["editor"].pk, why),
    ]
    for number, status, purity in [
        (3, "approved", 96),
        (4, "in_testing", 96),
        (5, "in_testing", 97),
    ]:
        values = api.get(f"/entities/{ref}/revisions/{number}").json()["values"]
        assert (values["status"], values["purity"]) == (status, purity)
    problem(patch(api, ref, body, '"3"'), 412, "stale")


def test_a_failed_correction_keeps_the_reviewed_revision(world: World) -> None:
    ref = approved(world)["id"]
    api = api_as(world, "editor")
    # A project editor still needs a role of the destination state.
    roles(world, editor=())
    body = {"reason": "Corrected value", "values": {"purity": 97}}
    assert not api.get(f"/entities/{ref}").json()["can"]["correct"]
    problem(patch(api, ref, body, '"3"'), 403, "forbidden")
    roles(world, editor=(ANALYST,))
    problem(patch(api, ref, {**body, "values": {"purity": 80}}, '"3"'), 422, "validation_failed")
    entity = Entity.objects.get(pk=ref)
    assert (entity.revision, entity.locked_by) == (3, ["status"])
    assert states.values_of(entity)["status"] == "approved"
    # A run cannot use its launcher's role and generated reason to reopen a locked record.
    with pytest.raises(Refused, match="locked"):
        services.update(
            world.users["editor"],
            entity,
            {"purity": 97},
            expected_revision=3,
            reason="Action result",
            by_run=True,
        )


def test_a_second_lock_and_a_destination_guard_still_refuse_a_correction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = states.state_fields(BATCH)
    custody = fields["custody"].states
    monkeypatch.setitem(custody, "quarantined", replace(custody["quarantined"], locks=True))
    ref = approved(world)["id"]
    set_(world, "viewer", ref, "custody", "quarantined")
    api = api_as(world, "editor")
    body = {"reason": "Read instrument file again", "values": {"purity": 97}}
    assert not api.get(f"/entities/{ref}").json()["can"]["correct"]
    refused = problem(patch(api, ref, body, '"4"'), 409, "refused")
    assert "Custody" in refused["detail"]

    # Both locks now allow a return. A destination guard can still refuse.
    monkeypatch.setitem(custody, "quarantined", replace(custody["quarantined"], on_edit="in_lab"))
    status = fields["status"].states
    with monkeypatch.context() as guarded:
        guarded.setitem(
            status,
            "in_testing",
            replace(
                status["in_testing"],
                guards={"not_quarantined": status["released"].guards["not_quarantined"]},
            ),
        )
        assert not api.get(f"/entities/{ref}").json()["can"]["correct"]
        refused = problem(patch(api, ref, body, '"4"'), 409, "guard_refused")
        assert refused["guard"] == "not_quarantined"
        entity = Entity.objects.get(pk=ref)
        assert (entity.revision, entity.locked_by) == (4, ["status", "custody"])
    # The first return has already changed Status when the second guard runs. A later
    # refusal rolls back the earlier return, including its history.
    with monkeypatch.context() as guarded:
        still_approved = replace(
            status["approved"].guards["reviewed"],
            check='record.status == "approved"',
            reads=("status",),
        )
        guarded.setitem(
            custody, "in_lab", replace(custody["in_lab"], guards={"approved": still_approved})
        )
        refused = problem(patch(api, ref, body, '"4"'), 409, "guard_refused")
        assert refused["guard"] == still_approved.name
        entity = Entity.objects.get(pk=ref)
        assert (entity.revision, entity.locked_by) == (4, ["status", "custody"])
    # The first return can also satisfy the second one's guard. Offer that correction
    # before the first return has happened.
    back_in_testing = replace(
        still_approved, name="in_testing", check='record.status == "in_testing"'
    )
    monkeypatch.setitem(
        custody, "in_lab", replace(custody["in_lab"], guards={"in_testing": back_in_testing})
    )
    assert api.get(f"/entities/{ref}").json()["can"]["correct"]
    saved = patch(api, ref, body, '"4"')
    assert saved.status_code == 200, saved.content
    values = saved.json()["values"]
    assert (values["status"], values["custody"], values["purity"]) == ("in_testing", "in_lab", 97)
    assert saved.json()["revision"] == 7


def test_save_rules_read_the_returned_state_and_corrected_values(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = approved(world)["id"]
    # Even a rule reading just the changed state and an untouched value must run.
    rule = Rule(
        "testing_name",
        "Testing name",
        "acme/protein",
        check='record.status != "in_testing" || record.name != "L-1"',
        message="Give this batch a new testing name.",
        reads=("status", "name"),
    )
    original = checking.checks_of
    monkeypatch.setattr(
        checking, "checks_of", lambda t: (*original(t), rule) if t == BATCH else original(t)
    )
    api = api_as(world, "editor")
    body = {"reason": "Checked lot provenance", "values": {"purity": 97}}
    refused = problem(patch(api, ref, body, '"3"'), 422, "validation_failed")
    assert refused["errors"][0]["rule"] == rule.name
    saved = patch(api, ref, {**body, "values": {"purity": 97, "name": "L-2"}}, '"3"')
    assert saved.status_code == 200, saved.content
    assert saved.json()["revision"] == 5
