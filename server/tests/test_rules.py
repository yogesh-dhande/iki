"""Plugins' rules, checked at every save (ADR 0127): CEL checks, which refuse a save or hold
the record for review, and the database's own `unique` and `no_overlap` rules."""

from typing import Any

import pytest
from api_client import problem
from django.db import IntegrityError, connection, transaction
from org_world import World
from test_api_entities import api_as, create, patch
from test_runs import protein, world
from test_states import ANALYST, QA, REVIEWER, roles, set_

from iki_server.audit.models import AuditEvent
from iki_server.entities import breaches, checking, holds, registry
from iki_server.entities import services as entity_services
from iki_server.entities.models import Entity, RuleBreach, RuleHold, RulesChecked
from iki_server.entities.values import shape
from iki_server.notifications.models import Notification
from iki_server.org.models import Project

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

BATCH = "acme/protein.Batch"


def batch(world: World, values: dict[str, Any], who: str = "editor", **kw: Any) -> Any:
    return create(api_as(world, who), world.project, {"name": "L-1", **values}, BATCH, **kw)


def made(world: World, values: dict[str, Any], who: str = "editor", **kw: Any) -> dict[str, Any]:
    answer = batch(world, values, who, **kw)
    assert answer.status_code == 201, answer.content
    body: dict[str, Any] = answer.json()
    return body


def change(world: World, ref: str, values: dict[str, Any], who: str = "editor", **kw: Any) -> Any:
    api = api_as(world, who)
    return patch(api, ref, {"values": values, **kw}, api.get(f"/entities/{ref}")["ETag"])


def errors(body: dict[str, Any]) -> list[tuple[str, str | None, bool | None]]:
    return [(e["pointer"], e.get("rule"), e.get("hold")) for e in body["errors"]]


# --- What a type says ---------------------------------------------------------------------------


def test_a_type_lists_its_rules_and_what_forms_check_them_against(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{BATCH}").json()
    rules = {r["name"]: r for r in found["rules"]}
    purity = rules["purity_in_specification"]
    assert (purity["kind"], purity["check"], purity["reads"], purity["fields"]) == (
        "check",
        "record.purity >= 90.0",
        ["purity"],
        ["purity"],
    )
    assert purity["hold"] == {"reviewed_by": ["acme/protein.qa"]}
    assert rules["one_per_well"]["unique"] == ["plate", "well"]
    assert rules["one_per_well"]["while"] == {"stored": ["true"]}
    assert rules["one_at_a_time"]["no_overlap"] == {
        "from": "starts_at",
        "to": "ends_at",
        "per": ["plate"],
    }
    variables = found["rule_variables"]
    assert variables["record"]["fields"]["purity"] == {"type": "double"}
    assert set(variables) == {"record", "now", "today"}
    # An extension's rule is its type's, named by its plugin.
    protein_type = api_as(world, "viewer").get("/entity-types/acme/protein.Protein").json()
    assert [r["name"] for r in protein_type["rules"]] == ["acme/prediction:predictor_named"]


# --- Checks that refuse -------------------------------------------------------------------------


def test_a_save_that_breaks_a_rule_is_refused_saying_which_and_where(world: World) -> None:
    body = problem(
        batch(world, {"made_on": "2026-11-10", "expires_on": "2026-11-01"}),
        422,
        "validation_failed",
    )
    assert errors(body) == [("#/values/expires_on", "expires_after_made", False)]
    assert body["errors"][0]["detail"] == "It expires on 2026-11-01, before it's made."
    assert not Entity.objects.filter(type=BATCH).exists()


def test_an_empty_value_doesnt_break_a_rule_about_it(world: World) -> None:
    body = made(world, {"made_on": "2026-11-10"})
    assert body["holds"] == [] and body["breaches"] == []


def test_a_rule_reads_what_a_reference_refers_to(world: World) -> None:
    api = api_as(world, "editor")
    prt = protein(api, world.project)
    entry = Entity.objects.get(pk=prt["entry"]["id"])
    entity_services.fill_entry(
        world.users["editor"],
        shape("acme/protein.Protein"),
        entry,
        {"hazard": "bsl3"},
        project=world.project,
    )
    body = problem(batch(world, {"protein": prt["id"]}), 422, "validation_failed")
    assert errors(body) == [("#/values/protein", "protein_not_bsl3", False)]
    assert body["errors"][0]["detail"] == f"{prt['hrid']} is BSL-3: batches aren't made of it here."


def test_an_extensions_rule_is_named_by_its_plugin(world: World) -> None:
    api = api_as(world, "editor")
    prt = protein(api, world.project)
    answer = patch(
        api,
        prt["id"],
        {"values": {"predicted_by": "unknown"}},
        api.get(f"/entities/{prt['id']}")["ETag"],
    )
    body = problem(answer, 422, "validation_failed")
    assert errors(body) == [("#/values/predicted_by", "acme/prediction:predictor_named", False)]


def test_an_edit_is_checked_only_against_rules_reading_what_it_changes(world: World) -> None:
    body = made(world, {"made_on": "2026-11-01", "expires_on": "2026-12-01"})
    # Saved before the rule, say: a deploy reports it, and other edits go on.
    _set_column(body["id"], "expires_on", "2026-10-01")
    answer = change(world, body["id"], {"name": "Renamed"})
    assert answer.status_code == 200, answer.content
    refused = problem(
        change(world, body["id"], {"made_on": "2026-11-02"}), 422, "validation_failed"
    )
    assert errors(refused) == [("#/values/expires_on", "expires_after_made", False)]
    assert change(world, body["id"], {"expires_on": "2027-01-01"}).status_code == 200


def _set_column(entity_id: str, column: str, value: Any) -> None:
    """As if saved before a rule: straight to its table."""
    table = registry.model(BATCH)._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f'UPDATE "{table}" SET "{column}" = %s WHERE entity_id = %s',  # noqa: S608 - ours
            [value, entity_id],
        )


# --- Rules that hold ----------------------------------------------------------------------------


def test_breaking_a_rule_that_holds_asks_why_then_saves_and_holds_it(world: World) -> None:
    roles(world, admin=(QA,), viewer=(QA,))
    body = problem(batch(world, {"purity": 82.5}), 422, "hold_reason_required")
    assert errors(body) == [("#/values/purity", "purity_in_specification", True)]
    assert body["errors"][0]["detail"] == "Purity 82.5% is below its specification, 90%."
    assert not Entity.objects.filter(type=BATCH).exists()

    saved = made(world, {"purity": 82.5}, hold_reason="As measured on the SEC; retest booked.")
    [hold] = saved["holds"]
    assert (hold["rule"], hold["label"], hold["reason"], hold["revision"]) == (
        "purity_in_specification",
        "Purity in specification",
        "As measured on the SEC; retest booked.",
        1,
    )
    assert hold["held_by"]["email"] == "editor@example.com"
    assert hold["reviewed_by"] == ["acme/protein.qa"]
    assert (hold["can_review"], hold["why_not"]) == (
        False,
        f"It needs someone with the QA role in {world.project.name}.",
    )
    event = AuditEvent.objects.get(action="entity.held")
    assert event.data["rule"] == "purity_in_specification"
    told = Notification.objects.filter(kind="rules.held")
    assert {n.recipient.email for n in told} == {"admin@example.com", "viewer@example.com"}


def test_a_refusing_rule_isnt_saved_by_a_reason(world: World) -> None:
    body = problem(
        batch(
            world,
            {"purity": 80.0, "result": "pass"},
            hold_reason="Saving anyway.",
        ),
        422,
        "validation_failed",
    )
    assert sorted(errors(body)) == [
        ("#/values/purity", "purity_in_specification", True),
        ("#/values/result", "pass_in_specification", False),
    ]


def test_a_held_record_stays_held_until_its_reviewed(world: World) -> None:
    roles(world, admin=(QA,), editor=(QA, ANALYST))
    saved = made(world, {"purity": 82.0}, hold_reason="As measured.")
    # Breaking it again adds to the same hold; editing it back doesn't clear it.
    assert change(world, saved["id"], {"purity": 81.0}, hold_reason="Retested.").status_code == 200
    body = change(world, saved["id"], {"purity": 95.0}).json()
    [hold] = body["holds"]
    assert (hold["revision"], hold["last_revision"]) == (1, 2)
    # Whoever saved it doesn't review it, nor does a token.
    mine = list(api_as(world, "editor").get(f"/entities/{saved['id']}").json()["holds"])
    assert mine[0]["why_not"] == "Someone else reviews it: you saved what held it."
    review = f"/entities/{saved['id']}/holds/{hold['id']}/review"
    problem(api_as(world, "editor").post(review, {"reason": "Fine."}), 403, "forbidden")
    problem(api_as(world, "admin").post(review, {"reason": ""}), 422, "validation_failed")
    answer = api_as(world, "admin").post(review, {"reason": "Retest within specification."})
    assert answer.status_code == 200, answer.content
    assert answer.json()["holds"] == []
    reviewed = RuleHold.objects.get(pk=hold["id"])
    assert reviewed.reviewed_by == world.users["admin"]
    assert reviewed.review_reason == "Retest within specification."
    assert AuditEvent.objects.filter(action="entity.hold_reviewed").count() == 1
    problem(api_as(world, "admin").post(review, {"reason": "Again."}), 409, "refused")


def test_a_held_record_cant_be_set_to_a_state_that_locks_it(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER, QA), viewer=(QA,))
    saved = made(world, {"purity": 82.0, "result": "fail"}, hold_reason="As measured.")
    set_(world, "editor", saved["id"], "status", "in_review")
    body = api_as(world, "admin").get(f"/entities/{saved['id']}").json()
    why = {s["name"]: s["why_not"] for s in body["can"]["states"]["status"]}
    assert why["approved"] == (
        f"{saved['hrid']} is held for review (“Purity in specification”): it can't be set to a "
        "state that locks it until its holds are reviewed."
    )
    api = api_as(world, "admin")
    answer = api.post(
        f"/entities/{saved['id']}/states/status",
        {"state": "approved"},
        headers={"If-Match": api.get(f"/entities/{saved['id']}")["ETag"]},
    )
    problem(answer, 409, "refused")
    hold = body["holds"][0]["id"]
    review = api_as(world, "viewer").post(
        f"/entities/{saved['id']}/holds/{hold}/review", {"reason": "Investigated: lab error."}
    )
    assert review.status_code == 200, review.content
    assert set_(world, "admin", saved["id"], "status", "approved")["locked"]


def test_a_runs_results_are_held_with_a_reason_naming_the_run() -> None:
    class Run:
        hrid = "RUN-000042"
        action = "acme/protein.measure_purity"

    assert holds.run_reason(Run()) == (
        "Recorded by RUN-000042, a run of acme/protein.measure_purity: its results are kept as "
        "they are."
    )


# --- Rules the database holds -------------------------------------------------------------------


def test_no_two_stored_records_share_what_a_unique_rule_names(world: World) -> None:
    first = made(world, {"plate": "P1", "well": "A1"})
    body = problem(batch(world, {"plate": "P1", "well": "A1"}), 422, "validation_failed")
    assert sorted(errors(body)) == [
        ("#/values/plate", "one_per_well", False),
        ("#/values/well", "one_per_well", False),
    ]
    assert body["errors"][0]["detail"] == (
        f"Well A1 of P1 has a batch already. {first['hrid']} has the same."
    )
    # One that isn't stored doesn't count, and another well is free.
    assert batch(world, {"plate": "P1", "well": "A1", "stored": False}).status_code == 201
    assert batch(world, {"plate": "P1", "well": "A2"}).status_code == 201
    # Taking the first out frees its well.
    assert change(world, first["id"], {"stored": False}).status_code == 200
    assert batch(world, {"plate": "P1", "well": "A1"}).status_code == 201


def test_the_database_refuses_what_a_unique_rule_names_twice(world: World) -> None:
    first = made(world, {"plate": "P1", "well": "A1"})
    second = made(world, {"plate": "P1", "well": "B1"})
    del first
    with pytest.raises(IntegrityError), transaction.atomic():
        _set_column(second["id"], "well", "A1")


def test_no_two_records_ranges_overlap(world: World) -> None:
    first = made(
        world,
        {"plate": "P1", "starts_at": "2026-11-20T09:00:00Z", "ends_at": "2026-11-20T11:00:00Z"},
    )
    body = problem(
        batch(
            world,
            {"plate": "P1", "starts_at": "2026-11-20T10:00:00Z", "ends_at": "2026-11-20T12:00:00Z"},
        ),
        422,
        "validation_failed",
    )
    assert {e["rule"] for e in body["errors"]} == {"one_at_a_time"}
    assert body["errors"][0]["detail"] == f"P1 is in use then. It overlaps {first['hrid']}."
    # One can start as another ends, and another plate is free.
    assert (
        batch(
            world,
            {"plate": "P1", "starts_at": "2026-11-20T11:00:00Z", "ends_at": "2026-11-20T12:00:00Z"},
        ).status_code
        == 201
    )
    assert (
        batch(
            world,
            {"plate": "P2", "starts_at": "2026-11-20T10:00:00Z", "ends_at": "2026-11-20T12:00:00Z"},
        ).status_code
        == 201
    )
    later = made(
        world,
        {"plate": "P1", "starts_at": "2026-11-21T09:00:00Z", "ends_at": "2026-11-21T10:00:00Z"},
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        _set_column(later["id"], "starts_at", "2026-11-20T08:00:00Z")


def test_projects_are_kept_apart(world: World) -> None:
    made(world, {"plate": "P1", "well": "A1"})
    other = Project.objects.create(team=world.project.team, name="Elsewhere")
    entity_services.create(
        world.users["org_admin"], BATCH, other, {"name": "L-2", "plate": "P1", "well": "A1"}
    )


# --- Records saved before a rule ----------------------------------------------------------------


def test_a_deploy_reports_records_that_break_rules_and_holds_those_that_hold(world: World) -> None:
    roles(world, admin=(QA,))
    saved = made(world, {"made_on": "2026-11-01", "expires_on": "2026-12-01", "purity": 95.0})
    _set_column(saved["id"], "expires_on", "2026-10-01")
    _set_column(saved["id"], "purity", 50.0)
    RulesChecked.objects.all().delete()
    found = {c.type: c for c in breaches.recheck()}
    assert found[BATCH].broken == {"expires_after_made": 1}
    assert found[BATCH].held == {"purity_in_specification": 1}
    body = api_as(world, "editor").get(f"/entities/{saved['id']}").json()
    assert [(b["rule"], b["message"]) for b in body["breaches"]] == [
        ("expires_after_made", "It expires on 2026-10-01, before it's made.")
    ]
    [hold] = body["holds"]
    assert hold["held_by"] is None and hold["reason"].startswith(
        "Found by the deploy of acme/protein"
    )
    assert AuditEvent.objects.filter(action="plugin.rules_checked", data__type=BATCH).exists()
    # Nothing changed: the next deploy doesn't check them again.
    assert BATCH not in {c.type for c in breaches.recheck()}
    # A save that checks the rule, and finds it kept, clears it.
    assert change(world, saved["id"], {"expires_on": "2027-01-01"}).status_code == 200
    assert not RuleBreach.objects.filter(entity_id=saved["id"]).exists()


def test_the_rules_a_type_keeps_are_its_own_and_its_extensions() -> None:
    keys = [r.key for r in checking.rules_of("acme/protein.Protein")]
    assert keys == ["acme/prediction:predictor_named"]
    assert registry.types()[BATCH].rules["one_per_well"].kind == "unique"
