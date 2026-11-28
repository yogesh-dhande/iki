"""Plugins' rules, checked at every save (ADR 0127): CEL checks, which refuse a save or hold
the record for review, and the database's own `unique` and `no_overlap` rules."""

import uuid
from dataclasses import replace
from importlib import import_module
from typing import Any

import pytest
from api_client import problem
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone
from org_world import World
from test_api_entities import api_as, create, patch
from test_runs import protein, world
from test_states import ANALYST, QA, REVIEWER, put, roles, set_

from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent
from iki_server.entities import breaches, checking, holds, registry
from iki_server.entities import services as entity_services
from iki_server.entities.models import (
    Entity,
    Revision,
    RuleBreach,
    RuleHold,
    RuleHoldOccurrence,
    RulesChecked,
)
from iki_server.entities.values import shape
from iki_server.notifications.models import Notification
from iki_server.org.models import Project
from iki_server.runs.models import TriggerFiring
from iki_spec.model import Rule, Trigger

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
    assert rules["one_per_well"]["while"] == {"field": "stored", "states": ["true"]}
    assert rules["one_at_a_time"]["no_overlap"] == {
        "from": "starts_at",
        "to": "ends_at",
        "per": ["plate"],
    }
    variables = found["rule_variables"]
    assert variables["record"]["fields"]["purity"] == {"type": "double"}
    assert set(variables) == {"record", "now", "today"}
    # A unique field is a rule named for it (ADR 0082); an extension's rule is its type's,
    # named by its plugin.
    protein_type = api_as(world, "viewer").get("/entity-types/acme/protein.Protein").json()
    listed = {r["name"]: r for r in protein_type["rules"]}
    assert list(listed) == ["lims_id", "chembl_target", "acme/prediction:predictor_named"]
    assert [(r["unique"], r["within"], r["key_field"]) for r in list(listed.values())[:2]] == [
        (["lims_id"], "project", "lims_id"),
        (["chembl_target"], "registry", "chembl_target"),
    ]
    assert listed["acme/prediction:predictor_named"]["key_field"] is None


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


def _proteins(world: World, count: int, bsl3: int | None = None) -> list[str]:
    """``count`` proteins, the one at ``bsl3`` of them BSL-3."""
    out = []
    for n in range(count):
        made = Entity.objects.get(
            pk=entity_services.create(
                world.users["editor"],
                "acme/protein.Protein",
                world.project,
                {"sequence": "MKV" + "".join("ACDEFGHIKL"[int(d)] for d in f"{n:04d}")},
            ).entity.pk
        )
        if n == bsl3:
            _bsl3(world, made)
        out.append(str(made.pk))
    return out


def _bsl3(world: World, made: Entity) -> None:
    entity_services.fill_entry(
        world.users["editor"],
        shape("acme/protein.Protein"),
        Entity.objects.get(pk=str(made.entry_id)),
        {"hazard": "bsl3"},
        project=world.project,
    )


def test_a_rule_over_a_list_reads_every_member(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However long the list: a rule worked out on part of one could keep it, or break it,
    wrongly. The BSL-3 protein is the 200th, then the 201st."""
    listed = _proteins(world, 201, bsl3=200)
    safe, bsl3 = listed[:200], listed[200]
    assert made(world, {"proteins": safe})["breaches"] == []
    for proteins in ([*safe[1:], bsl3], [*safe, bsl3]):
        body = problem(batch(world, {"proteins": proteins}), 422, "validation_failed")
        assert errors(body) == [("#/values/proteins", "proteins_not_bsl3", False)]
    # Checked together, as a bulk import checks its records, each reads its own list whole.
    ids = [uuid.UUID(i) for i in listed]  # as the database stores them
    savings = [
        checking.Saving({"name": "L-1", "proteins": ids}.get),
        checking.Saving({"name": "L-2", "proteins": ids[:10]}.get),
    ]
    found = checking.broken(BATCH, savings)
    assert [[b.rule.key for b in one] for one in found] == [["proteins_not_bsl3"], []]
    # Past what one lookup loads, they're checked in groups, each record's list still whole.
    monkeypatch.setattr(checking, "MAX_LOOKUP", 50)
    found = checking.broken(BATCH, [*savings, checking.Saving({"proteins": ids[150:]}.get)])
    assert [[b.rule.key for b in one] for one in found] == [
        ["proteins_not_bsl3"],
        [],
        ["proteins_not_bsl3"],
    ]


def test_a_rule_over_a_list_it_cant_read_all_of_isnt_worked_out(world: World) -> None:
    """A member it can't read (say, of a type no longer installed) isn't left out: the rule
    can't be checked, rather than worked out on the rest."""
    safe = [uuid.UUID(i) for i in _proteins(world, 2)]
    gone = uuid.UUID("019a0000-0000-7000-8000-000000000000")
    [[broken]] = checking.broken(
        BATCH, [checking.Saving({"name": "L-1", "proteins": [*safe, gone]}.get)]
    )
    assert broken.rule.key == "proteins_not_bsl3"
    assert broken.message == (
        "“None of its proteins is BSL-3” couldn’t be checked: proteins refers to 1 record it "
        "can’t read, so it isn’t worked out on the rest."
    )
    # A record kept as a tombstone, of a type whose plugin was purged (ADR 0092): the same,
    # for a list and for one reference.
    purged(safe[1])
    [[broken]] = checking.broken(BATCH, [checking.Saving({"name": "L-1", "proteins": safe}.get)])
    assert (broken.rule.key, broken.message) == (
        "proteins_not_bsl3",
        "“None of its proteins is BSL-3” couldn’t be checked: proteins refers to 1 record it "
        "can’t read, so it isn’t worked out on the rest.",
    )
    [[broken]] = checking.broken(BATCH, [checking.Saving({"name": "L-1", "protein": safe[1]}.get)])
    assert broken.rule.key == "protein_not_bsl3"
    assert "protein refers to 1 record it can’t read" in broken.message


def purged(entity_id: uuid.UUID | str) -> None:
    """As a plugin's purge leaves a record of its type, and its registry entry: each kept as
    a tombstone, and their type, with the plugin, gone."""
    made = Entity.objects.get(pk=entity_id)
    Entity.objects.filter(pk__in=[made.pk, made.entry_id]).update(
        type="acme/purged.Protein", purged_at=timezone.now()
    )
    assert "acme/purged.Protein" not in registry.types()


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
    payload = {"reason": "Retest within specification.", "review_version": hold["review_version"]}
    headers = {"If-Match": f'"{body["revision"]}"'}
    problem(api_as(world, "editor").post(review, payload, headers=headers), 403, "forbidden")
    problem(
        api_as(world, "admin").post(review, {**payload, "reason": ""}, headers=headers),
        422,
        "validation_failed",
    )
    problem(api_as(world, "admin").post(review, payload), 428, "precondition_required")
    answer = api_as(world, "admin").post(review, payload, headers=headers)
    assert answer.status_code == 200, answer.content
    assert answer.json()["holds"] == []
    refreshed = api_as(world, "admin").call(
        "GET", f"/entities/{saved['id']}", headers={"If-None-Match": headers["If-Match"]}
    )
    assert refreshed.status_code == 200, refreshed.content
    assert refreshed.json()["holds"] == []
    reviewed = RuleHold.objects.get(pk=hold["id"])
    assert reviewed.reviewed_by == world.users["admin"]
    assert reviewed.review_reason == "Retest within specification."
    event = AuditEvent.objects.get(action="entity.hold_reviewed")
    assert event.data["revision"] == body["revision"]
    assert event.data["hold_last_revision"] == hold["last_revision"]
    assert event.data["review_version"] == hold["review_version"]
    problem(api_as(world, "admin").post(review, payload, headers=headers), 409, "refused")


def test_hold_review_requires_the_record_revision_seen(world: World) -> None:
    roles(world, admin=(QA,))
    saved = made(world, {"purity": 82.0}, hold_reason="As measured.")
    api = api_as(world, "admin")
    record = f"/entities/{saved['id']}"
    seen = api.get(record)
    [hold] = seen.json()["holds"]
    edited = change(world, saved["id"], {"purity": 95.0})
    assert edited.status_code == 200, edited.content
    [current] = edited.json()["holds"]
    assert current["id"] == hold["id"]
    assert current["last_revision"] == hold["last_revision"]
    assert current["review_version"] != hold["review_version"]
    rejected = api.post(
        f"{record}/holds/{hold['id']}/review",
        {"reason": "I reviewed the measurement.", "review_version": hold["review_version"]},
        headers={"If-Match": seen["ETag"]},
    )
    problem(rejected, 412, "stale")
    assert rejected["ETag"] == edited["ETag"]
    assert not AuditEvent.objects.filter(action="entity.hold_reviewed").exists()
    assert RuleHold.objects.get(pk=hold["id"]).reviewed_at is None


@pytest.mark.parametrize("removed", [False, True], ids=["replaced", "removed"])
def test_hold_review_requires_the_deployed_rule_version_seen(
    world: World, monkeypatch: pytest.MonkeyPatch, removed: bool
) -> None:
    roles(world, admin=(QA,))
    saved = made(world, {"purity": 82.0}, hold_reason="As measured.")
    api = api_as(world, "admin")
    record = f"/entities/{saved['id']}"
    RulesChecked.objects.update_or_create(
        type=BATCH, defaults={"digest": checking.digest_of(BATCH)}
    )
    seen = api.get(record)
    [hold] = seen.json()["holds"]
    checks = checking.checks_of
    (matching,) = [rule for rule in checks(BATCH) if rule.key == hold["rule"]]
    updated = [rule for rule in checks(BATCH) if rule.key != hold["rule"]]
    if not removed:
        updated.append(replace(matching, check="record.purity >= 92.0"))
    with monkeypatch.context() as deployment:
        deployment.setattr(
            checking, "checks_of", lambda t: tuple(updated) if t == BATCH else checks(t)
        )
        checking.digest_of.cache_clear()
        try:
            breaches.recheck()
            generation = RulesChecked.objects.get(pk=BATCH)
        finally:
            checking.digest_of.cache_clear()

    review = f"{record}/holds/{hold['id']}/review"
    headers = {"If-Match": seen["ETag"]}
    payload = {"reason": "I reviewed the hold.", "review_version": hold["review_version"]}
    problem(api.post(review, payload, headers=headers), 412, "stale")
    assert not AuditEvent.objects.filter(action="entity.hold_reviewed").exists()
    refreshed = api.call("GET", record, headers={"If-None-Match": seen["ETag"]})
    assert refreshed.status_code == 200, refreshed.content
    assert "no-store" in refreshed["Cache-Control"]
    [current] = refreshed.json()["holds"]
    assert refreshed["ETag"] == seen["ETag"]
    assert current["id"] == hold["id"]
    assert current["message"] == hold["message"]
    assert current["review_version"] != hold["review_version"]
    approved = api.post(
        review, {**payload, "review_version": current["review_version"]}, headers=headers
    )
    assert approved.status_code == 200, approved.content
    event = AuditEvent.objects.get(action="entity.hold_reviewed")
    assert event.data["rule_digest"] == generation.digest
    assert event.data["rules_checked_at"] == generation.checked_at.isoformat()


def test_a_records_history_keeps_each_hold_and_its_review(world: World) -> None:
    # As the review dialog says, and ADR 0127 promises: an out-of-specification result
    # stays visible in the record's history, with who saved it, why, and who reviewed it.
    roles(world, admin=(QA,), editor=(QA, ANALYST))
    saved = made(world, {"purity": 82.4}, hold_reason="Kept: the measurement is valid.")
    change(world, saved["id"], {"purity": 95.0})
    history = f"/entities/{saved['id']}/revisions"
    newest, first = api_as(world, "viewer").get(history).json()["items"]
    assert newest["holds"] == []
    [held] = first["holds"]
    assert (held["label"], held["reason"], held["held_by"]["name"]) == (
        "Purity in specification",
        "Kept: the measurement is valid.",
        world.users["editor"].name,
    )
    assert held["held_at"] is not None and held["last_revision"] == 1
    assert held["reviewer"] is None and held["reviewed_at"] is None
    review = f"/entities/{saved['id']}/holds/{held['id']}/review"
    api = api_as(world, "admin")
    seen = api.get(f"/entities/{saved['id']}")
    [hold] = seen.json()["holds"]
    reviewed = api.post(
        review,
        {"reason": "SEC rerun within limits.", "review_version": hold["review_version"]},
        headers={"If-Match": seen["ETag"]},
    )
    assert reviewed.status_code == 200, reviewed.content
    # The hold is kept, reviewed: who, when and why.
    [kept] = api_as(world, "viewer").get(history).json()["items"][1]["holds"]
    assert kept["id"] == held["id"]
    assert kept["reviewer"]["name"] == world.users["admin"].name
    assert kept["review_reason"] == "SEC rerun within limits."
    assert kept["reviewed_at"] is not None
    one = api_as(world, "viewer").get(f"{history}/1").json()
    assert [h["id"] for h in one["holds"]] == [held["id"]]


def test_a_hold_keeps_each_failing_observation_and_excludes_every_contributor(world: World) -> None:
    roles(world, admin=(QA,), editor=(QA,), viewer=(QA,))
    saved = made(world, {"purity": 82.0}, hold_reason="First measurement kept.")
    path = f"/entities/{saved['id']}"
    repeated = change(
        world, saved["id"], {"purity": 81.0}, "admin", hold_reason="Confirmed by the retest."
    )
    assert repeated.status_code == 200, repeated.content
    [hold] = repeated.json()["holds"]
    first, second = hold["occurrences"]
    assert hold["id"] == saved["holds"][0]["id"]
    assert (first["reason"], second["reason"]) == (
        "First measurement kept.",
        "Confirmed by the retest.",
    )
    assert "82" in first["message"] and "81" in second["message"]
    assert (first["held_by"]["name"], second["held_by"]["name"]) == (
        world.users["editor"].name,
        world.users["admin"].name,
    )
    assert (first["revision"], second["revision"]) == (1, 2)
    assert not hold["can_review"]
    review = f"{path}/holds/{hold['id']}/review"
    payload = {"reason": "Investigation documented.", "review_version": hold["review_version"]}
    headers = {"If-Match": repeated["ETag"]}
    problem(api_as(world, "admin").post(review, payload, headers=headers), 403, "forbidden")
    assert api_as(world, "viewer").post(review, payload, headers=headers).status_code == 200
    api = api_as(world, "viewer")
    for observation in (first, second):
        [evidence] = api.get(f"{path}/revisions/{observation['revision']}").json()["holds"]
        assert evidence["occurrence_id"] == observation["id"]
        assert evidence["message"] == observation["message"]
        assert evidence["reason"] == observation["reason"]
        assert evidence["reviewer"]["name"] == world.users["viewer"].name
    events = AuditEvent.objects.filter(action="entity.held", target_id=saved["id"]).order_by(
        "occurred_at"
    )
    assert [e.data["reason"] for e in events] == [first["reason"], second["reason"]]
    with pytest.raises(DatabaseError, match="history is append-only"), transaction.atomic():
        RuleHoldOccurrence.objects.filter(pk=first["id"]).update(reason="Changed.")


def test_legacy_hold_backfill_preserves_known_evidence_and_marks_the_gap(world: World) -> None:
    roles(world, admin=(QA,), viewer=(QA,))
    saved = made(world, {"name": "Initial name"})
    edited = change(world, saved["id"], {"name": "Changed by second recorder"}, "admin")
    assert edited.status_code == 200, edited.content
    entity = Entity.objects.get(pk=saved["id"])
    hold = RuleHold.objects.create(
        entity=entity,
        rule="purity_in_specification",
        label="Purity in specification",
        roles=[QA],
        message="Latest reading: 81%.",
        reason="First reading kept.",
        held_by=world.users["editor"],
        revision=1,
        last_revision=2,
    )
    audit.record(
        "entity.held",
        target=entity,
        project=world.project,
        actor=world.users["editor"],
        data={"hold": str(hold.pk), "revision": 1, "message": "Original reading: 82%."},
    )
    registered = protein(api_as(world, "editor"), world.project)
    record = Entity.objects.get(pk=registered["id"])
    registered_hold = RuleHold.objects.create(
        entity=record,
        rule="registry_facts",
        label="Registry facts",
        roles=[QA],
        message="A registry fact was entered.",
        reason="Opening registry observation.",
        held_by=world.users["editor"],
        revision=record.revision,
        last_revision=record.revision,
    )
    audit.record(
        "entity.held",
        target=record,
        project=world.project,
        actor=world.users["editor"],
        data={"hold": str(registered_hold.pk), "message": registered_hold.message},
    )
    entry_edit = change(world, registered["id"], {"ec_number": "1.1.1.1"}, "admin")
    assert entry_edit.status_code == 200, entry_edit.content
    assert entry_edit.json()["revision"] == record.revision
    migration = import_module("iki_server.entities.migrations.0023_ruleholdoccurrence")
    with connection.cursor() as cursor:
        cursor.execute(migration.BACKFILL)

    path = f"/entities/{entity.pk}"
    admin = api_as(world, "admin")
    fetched = admin.get(path)
    [held] = fetched.json()["holds"]
    original, incomplete = held["occurrences"]
    assert held["message"] == original["message"] == "Original reading: 82%."
    assert original["reason"] == "First reading kept."
    assert incomplete["message"] == "Latest reading: 81%."
    assert incomplete["reason"] is None and incomplete["held_by"] is None
    assert incomplete["held_at"] is None and incomplete["legacy_gap"]
    assert not held["can_review"] and "earlier evidence was incomplete" in held["why_not"]
    [later] = admin.get(f"{path}/revisions/2").json()["holds"]
    assert later["message"] == incomplete["message"] and later["reason"] is None
    [earlier] = admin.get(f"{path}/revisions/1").json()["holds"]
    assert earlier["message"] == original["message"] and earlier["reason"] == original["reason"]
    assert api_as(world, "viewer").get(path).json()["holds"][0]["can_review"]
    [registered_output] = admin.get(f"/entities/{record.pk}").json()["holds"]
    original, incomplete = registered_output["occurrences"]
    assert original["message"] == registered_hold.message
    assert incomplete["legacy_gap"] and incomplete["message"] is None
    assert incomplete["revision"] == original["revision"]
    assert not registered_output["can_review"]


def test_hold_keeps_distinct_observations_of_the_same_record_revision(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = protein(api_as(world, "editor"), world.project)
    type_name = saved["type"]
    rule = Rule(
        name="registry_facts",
        label="Registry facts",
        plugin="acme/protein",
        check="record.ec_number == null && record.chembl_target == null",
        message="A registry fact was entered.",
        hold=(QA,),
        reads=("ec_number", "chembl_target"),
    )
    checks = checking.checks_of
    monkeypatch.setattr(
        checking, "checks_of", lambda t: (*checks(t), rule) if t == type_name else checks(t)
    )
    first = change(world, saved["id"], {"ec_number": "1.1.1.1"}, hold_reason="First fact.")
    assert first.status_code == 200, first.content
    second = change(world, saved["id"], {"chembl_target": "CHEMBL42"}, hold_reason="Second fact.")
    assert second.status_code == 200, second.content
    [hold] = second.json()["holds"]
    one, two = hold["occurrences"]
    assert one["id"] != two["id"]
    assert one["revision"] == two["revision"] == saved["revision"]
    assert two["entry_revision"] == one["entry_revision"] + 1
    assert one["entry_id"] == two["entry_id"] == saved["entry"]["id"]
    assert first["ETag"] == second["ETag"]
    assert first.json()["holds"][0]["review_version"] != hold["review_version"]
    path = f"/entities/{saved['id']}/revisions/{saved['revision']}"
    history = api_as(world, "viewer").get(path).json()
    assert [o["reason"] for o in history["holds"]] == ["First fact.", "Second fact."]


def test_hold_excludes_editors_in_a_gap_left_by_an_old_web_process(world: World) -> None:
    roles(world, admin=(QA,), viewer=(QA,))
    saved = made(world, {"purity": 82.0}, hold_reason="First measurement kept.")
    edited = change(world, saved["id"], {"name": "New label"}, "admin")
    [hold] = edited.json()["holds"]
    assert hold["can_review"]
    # An older process still updates the aggregate while the new workers roll out.
    RuleHold.objects.filter(pk=hold["id"]).update(last_revision=edited.json()["revision"])
    path = f"/entities/{saved['id']}"
    [held] = api_as(world, "admin").get(path).json()["holds"]
    assert not held["can_review"] and "earlier evidence was incomplete" in held["why_not"]
    assert api_as(world, "viewer").get(path).json()["holds"][0]["can_review"]


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
    [hold] = body["holds"]
    review = api_as(world, "viewer").post(
        f"/entities/{saved['id']}/holds/{hold['id']}/review",
        {"reason": "Investigated: lab error.", "review_version": hold["review_version"]},
        headers={"If-Match": f'"{body["revision"]}"'},
    )
    assert review.status_code == 200, review.content
    assert set_(world, "admin", saved["id"], "status", "approved")["locked"]


def _purity_rule(*, hold: bool = False, states: tuple[str, ...] = ("in_review",)) -> Rule:
    return Rule(
        name="reviewed_with_a_purity",
        label="Reviewed with a purity",
        plugin="acme/protein",
        check="record.purity != null",
        when=f"record.status in {list(states)!r}".replace("'", '"'),
        message="Measure its purity before review.",
        hold=(QA,) if hold else (),
        fields=("purity",),
        reads=("purity", "status"),
    )


def test_a_state_change_is_checked_against_the_rules_reading_its_field(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state is a value the record saves (ADR 0125): the rules reading it are checked
    as an edit of it checks them, and its controls say why not."""
    roles(world, editor=(ANALYST,))
    _with_rule(monkeypatch, BATCH, _purity_rule())
    saved = made(world, {})
    answer = api_as(world).get(f"/entities/{saved['id']}").json()
    why = {s["name"]: s["why_not"] for s in answer["can"]["states"]["status"]}
    assert why["in_review"] == "Measure its purity before review."
    body = problem(
        put(world, "editor", saved["id"], "status", "in_review"), 422, "validation_failed"
    )
    assert errors(body) == [("#/values/purity", "reviewed_with_a_purity", False)]
    assert Entity.objects.get(pk=saved["id"]).revision == saved["revision"]
    # Kept, it's set.
    assert change(world, saved["id"], {"purity": 95.0}).status_code == 200
    set_(world, "editor", saved["id"], "status", "in_review")


def test_a_state_change_that_breaks_a_rule_that_holds_is_held_for_its_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    _with_rule(monkeypatch, BATCH, _purity_rule(hold=True, states=("in_review", "approved")))
    saved = made(world, {})
    body = problem(
        put(world, "editor", saved["id"], "status", "in_review"), 422, "hold_reason_required"
    )
    assert errors(body) == [("#/values/purity", "reviewed_with_a_purity", True)]
    done = set_(world, "editor", saved["id"], "status", "in_review", "Sent for a purity check.")
    [hold] = done["holds"]
    assert hold["rule"] == "reviewed_with_a_purity" and hold["reason"] == "Sent for a purity check."
    # A state that locks it isn't set while it's held, nor so that it would be.
    problem(put(world, "admin", saved["id"], "status", "approved", "Approved."), 409, "refused")


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


# --- A registry entry's changes -----------------------------------------------------------------

PROTEIN = "acme/protein.Protein"


def _with_rule(monkeypatch: pytest.MonkeyPatch, type_name: str, rule: Rule) -> None:
    """``type_name``'s records keep ``rule`` too, as if its plugin declared it."""
    checks, every = checking.checks_of, checking.rules_of
    monkeypatch.setattr(
        checking, "checks_of", lambda t: (*checks(t), rule) if t == type_name else checks(t)
    )
    monkeypatch.setattr(
        checking, "rules_of", lambda t: (*every(t), rule) if t == type_name else every(t)
    )


def _hazard_rule(*, hold: bool = False, needs_host: bool = False) -> Rule:
    return Rule(
        name="bsl3_needs_a_host" if needs_host else "not_bsl3",
        label="A BSL-3 protein names its host" if needs_host else "Not BSL-3",
        plugin="acme/protein",
        check='record.hazard != "bsl3"',
        when="record.host == null" if needs_host else None,
        message="It's BSL-3.",
        hold=(QA,) if hold else (),
        reads=("hazard", "host") if needs_host else ("hazard",),
    )


def _rival(world: World, name: str = "Rival Screens") -> Project:
    """Another project in the registry, the outsider's alone."""
    from iki_server.org.models import ProjectMembership, ProjectRole

    rival = Project.objects.create(team=world.team, name=name)
    ProjectMembership.objects.create(
        project=rival, user=world.users["outsider"], role=ProjectRole.ADMIN
    )
    return rival


def _curate(world: World, hrid: str, values: dict[str, Any]) -> Any:
    from test_registries import registrar

    curator = registrar(world)
    return curator.patch(
        f"/entries/{hrid}",
        {"values": values, "reason": "Biosafety committee, 2026-11-17"},
        headers={"If-Match": curator.get(f"/entries/{hrid}")["ETag"]},
    )


def test_a_registrars_change_that_breaks_a_rule_on_the_entry_alone_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every record of the entry would break it alike."""
    _with_rule(monkeypatch, PROTEIN, _hazard_rule())
    values = {"sequence": "MKVLLLA", "hazard": "bsl1"}
    saved = create(api_as(world), world.project, values, PROTEIN)
    assert saved.status_code == 201, saved.content
    answer = _curate(world, saved.json()["hrid"], {"hazard": "bsl3"})
    body = problem(answer, 422, "validation_failed")
    assert errors(body) == [("#/values/hazard", "not_bsl3", False)]
    record = api_as(world).get(f"/entities/{saved.json()['id']}").json()
    assert record["values"]["hazard"] == "bsl1"


def test_a_registrars_change_holds_every_record_a_rule_that_holds_reads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rival = _rival(world)
    _with_rule(monkeypatch, PROTEIN, _hazard_rule(hold=True))
    values = {"sequence": "MKVLLLA", "hazard": "bsl1"}
    mine = create(api_as(world), world.project, values, PROTEIN).json()
    theirs = create(api_as(world, "outsider"), rival, {"sequence": "MKVLLLA"}, PROTEIN).json()
    done = _curate(world, mine["hrid"], {"hazard": "bsl3"})
    assert done.status_code == 200, done.content
    entry_revision = done.json()["revision"]
    for record in (mine, theirs):
        [hold] = RuleHold.objects.filter(entity_id=record["id"])
        assert (hold.rule, hold.reason) == ("not_bsl3", "Biosafety committee, 2026-11-17")
        assert hold.held_by == world.users["auditor"]
        [seen] = hold.occurrences.all()
        assert (seen.entry_revision, seen.revision) == (entry_revision, record["revision"])
    assert not RuleBreach.objects.exists()


def test_a_registrars_change_is_reported_on_each_record_that_breaks_its_own_rule(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule that also reads a record's own values isn't the registrar's to know of: the
    change stands, and each record that breaks it says so."""
    rival = _rival(world)
    _with_rule(monkeypatch, PROTEIN, _hazard_rule(needs_host=True))
    hostless = create(api_as(world), world.project, {"sequence": "MKVLLLA"}, PROTEIN).json()
    hosted = create(
        api_as(world, "outsider"), rival, {"sequence": "MKVLLLA", "host": "E. coli"}, PROTEIN
    ).json()
    done = _curate(world, hostless["hrid"], {"hazard": "bsl3"})
    assert done.status_code == 200, done.content
    assert "Kinase Atlas" not in done.content.decode()
    [breach] = RuleBreach.objects.filter(entity_id=hostless["id"])
    assert breach.rule == "bsl3_needs_a_host"
    assert not RuleBreach.objects.filter(entity_id=hosted["id"]).exists()
    shown = api_as(world).get(f"/entities/{hostless['id']}").json()
    assert [b["rule"] for b in shown["breaches"]] == ["bsl3_needs_a_host"]
    # Keeping the rule again clears it.
    kept = change(world, hostless["id"], {"host": "E. coli"})
    assert kept.status_code == 200, kept.content
    assert not RuleBreach.objects.filter(entity_id=hostless["id"]).exists()


@pytest.mark.parametrize("by", ["registering", "editing"])
@pytest.mark.parametrize("hold", [False, True])
def test_a_holders_new_fact_is_checked_against_the_other_records_of_the_entry(
    world: World, monkeypatch: pytest.MonkeyPatch, hold: bool, by: str
) -> None:
    """Their own save is checked against their record, as any save is; the entry's other
    records are held or reported. Who gave the fact isn't told to the other holders (ADR
    0115): the hold names no one, and no project."""
    rival = _rival(world)
    _with_rule(monkeypatch, PROTEIN, _hazard_rule(hold=hold, needs_host=True))
    mine = create(api_as(world), world.project, {"sequence": "MKVLLLA"}, PROTEIN).json()
    hosted = {"sequence": "MKVLLLA", "host": "E. coli"}
    outsider = api_as(world, "outsider")
    if by == "registering":
        theirs = create(outsider, rival, {**hosted, "hazard": "bsl3"}, PROTEIN)
        assert theirs.status_code == 201, theirs.content
    else:
        theirs = create(outsider, rival, hosted, PROTEIN)
        filled = change(world, theirs.json()["id"], {"hazard": "bsl3"}, "outsider")
        assert filled.status_code == 200, filled.content
    assert api_as(world).get(f"/entities/{mine['id']}").json()["values"]["hazard"] == "bsl3"
    assert not RuleBreach.objects.filter(entity_id=theirs.json()["id"]).exists()
    assert not RuleHold.objects.filter(entity_id=theirs.json()["id"]).exists()
    if hold:
        [held] = RuleHold.objects.filter(entity_id=mine["id"])
        assert held.held_by is None and held.held_agent is None
        assert held.reason == (f"Its registry entry {mine['hrid']} was given Hazard.")
        assert not RuleBreach.objects.filter(entity_id=mine["id"]).exists()
    else:
        [breach] = RuleBreach.objects.filter(entity_id=mine["id"])
        assert breach.rule == "bsl3_needs_a_host"
        assert not RuleHold.objects.filter(entity_id=mine["id"]).exists()


def _superseded(world: World, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The editor's record of a mistaken entry, which a registrar supersedes by another
    project's, registered with ``values``."""
    from test_registries import registrar

    wrong = create(api_as(world), world.project, {"sequence": "MKVLLLAK"}, PROTEIN).json()
    right = create(
        api_as(world, "outsider"), _rival(world), {"sequence": "MKVLLLA", **values}, PROTEIN
    )
    assert right.status_code == 201, right.content
    curator = registrar(world)
    entry = f"/entries/{wrong['hrid']}"
    done = curator.post(
        f"{entry}/supersede",
        {"replacement": right.json()["hrid"], "reason": "Mistyped residue"},
        headers={"If-Match": curator.get(entry)["ETag"]},
    )
    assert done.status_code == 200, done.content
    return wrong, right.json()


def _move(world: World, record: dict[str, Any]) -> Any:
    api, ref = api_as(world), f"/entities/{record['id']}"
    read = {"If-Match": api.get(ref)["ETag"]}
    return api.post(f"{ref}/move", {"reason": "Superseded"}, headers=read)


def test_a_move_that_breaks_a_rule_by_its_new_entrys_values_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's values are its new entry's: it's checked by those that differ."""
    _with_rule(monkeypatch, PROTEIN, _hazard_rule(needs_host=True))
    wrong, right = _superseded(world, {"hazard": "bsl3", "host": "E. coli"})
    body = problem(_move(world, wrong), 422, "validation_failed")
    assert errors(body) == [
        ("#/values/hazard", "bsl3_needs_a_host", False),
        ("#/values/host", "bsl3_needs_a_host", False),
    ]
    assert Entity.objects.get(pk=wrong["id"]).entry_id != uuid.UUID(right["entry"]["id"])


def test_a_move_that_breaks_a_rule_that_holds_holds_the_record_for_its_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_rule(monkeypatch, PROTEIN, _hazard_rule(hold=True, needs_host=True))
    wrong, right = _superseded(world, {"hazard": "bsl3", "host": "E. coli"})
    moved = _move(world, wrong)
    assert moved.status_code == 200, moved.content
    [hold] = RuleHold.objects.filter(entity_id=wrong["id"])
    assert (hold.reason, hold.held_by) == ("Superseded", world.users["editor"])
    [seen] = hold.occurrences.all()
    assert seen.entry_id == uuid.UUID(right["entry"]["id"])
    # The revision that moved it.
    assert seen.revision == wrong["revision"] + 1 == moved.json()["revision"]


#: A check of a protein's weight that reads its hazard (ADR 0080): an edit of its hazard
#: clears the weight it worked out, notes that it needs re-verification, and runs it again.
WEIGH_BY_HAZARD = Trigger(
    plugin="acme/protein",
    name="weigh_by_hazard",
    label="Weigh a protein again when its hazard changes",
    on="entity.updated",
    type=PROTEIN,
    fields=("hazard",),
    action="compute_properties",
    input="protein",
    recheck="summary",
)


def _with_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proteins are checked by :data:`WEIGH_BY_HAZARD` too, as if their plugin declared it,
    its action noting its result in their summary."""
    from iki_server.runs import catalog, triggers

    installed, actions = triggers.triggers, catalog.actions
    monkeypatch.setattr(
        triggers,
        "triggers",
        lambda: {**installed(), WEIGH_BY_HAZARD.qualified_name: WEIGH_BY_HAZARD},
    )

    def noted() -> dict[str, Any]:
        found = dict(actions())
        weigh = found["acme/protein.compute_properties"]
        updates = tuple(replace(u, fields=(*u.fields, "summary")) for u in weigh.updates)
        return {**found, weigh.qualified_name: replace(weigh, updates=updates)}

    monkeypatch.setattr(catalog, "actions", noted)


def _weighed(world: World, record: dict[str, Any]) -> None:
    """``record``'s weight, as its check worked it out."""
    entity = Entity.objects.get(pk=record["id"])
    entity_services.update(
        world.users["editor"],
        entity,
        {"molecular_weight": 30123.4, "summary": "Weighed"},
        expected_revision=entity.revision,
        by_run=True,
    )


def _rechecked(record: dict[str, Any]) -> list[TriggerFiring]:
    return list(
        TriggerFiring.objects.filter(
            trigger__trigger=WEIGH_BY_HAZARD.qualified_name, entity_id=record["id"]
        )
    )


def _last(record: dict[str, Any]) -> Revision:
    return Revision.objects.filter(entity_id=record["id"]).latest("number")


def test_a_move_clears_what_a_check_read_of_the_entry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As an edit of what a check reads clears its result (ADR 0080), so does a move to an
    entry that gives it another value: it's run again, never kept from the old one."""
    _with_recheck(monkeypatch)
    wrong, _ = _superseded(world, {"hazard": "bsl3"})
    _weighed(world, wrong)
    moved = _move(world, wrong)
    assert moved.status_code == 200, moved.content
    values = moved.json()["values"]
    assert (values["molecular_weight"], values["summary"]) == (None, "Needs re-verification")
    [firing] = _rechecked(wrong)
    revision = _last(wrong)
    assert (firing.revision, firing.editor) == (revision.number, world.users["editor"])
    assert revision.changed_fields == ["molecular_weight", "summary"]
    assert revision.reason == "Superseded"


def test_a_holders_new_fact_clears_what_their_check_read_of_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filling in a fact of the record's entry is an edit of what its check reads: its
    result is cleared, and it's run again, as whoever filled it in."""
    _with_recheck(monkeypatch)
    mine = create(api_as(world), world.project, {"sequence": "MKVLLLA"}, PROTEIN).json()
    _weighed(world, mine)
    filled = change(world, mine["id"], {"hazard": "bsl2"}, reason="Biosafety review")
    assert filled.status_code == 200, filled.content
    values = filled.json()["values"]
    assert (values["hazard"], values["molecular_weight"]) == ("bsl2", None)
    assert values["summary"] == "Needs re-verification"
    [firing] = _rechecked(mine)
    revision = _last(mine)
    assert (firing.revision, firing.editor) == (revision.number, world.users["editor"])
    assert (revision.changed_fields, revision.reason) == (
        ["molecular_weight", "summary"],
        "Biosafety review",
    )


def test_a_registrars_change_clears_what_each_holders_check_read_of_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registrar's change of a fact is every holder's (ADR 0115): on each record of the
    entry, the result of a check reading it is cleared, by the registrar for their reason.
    It isn't run there, as a recheck runs as whoever edited the record and no one did in
    its project: the record says it needs re-verification."""
    from test_registries import registrar

    _with_recheck(monkeypatch)
    rival = _rival(world)
    values = {"sequence": "MKVLLLA", "hazard": "bsl1"}
    mine = create(api_as(world), world.project, values, PROTEIN).json()
    theirs = create(api_as(world, "outsider"), rival, values, PROTEIN).json()
    for record in (mine, theirs):
        _weighed(world, record)
    registrar(world)
    curated = _curate(world, mine["hrid"], {"hazard": "bsl2"})
    assert curated.status_code == 200, curated.content
    for record, who in ((mine, "editor"), (theirs, "outsider")):
        now = api_as(world, who).get(f"/entities/{record['id']}").json()["values"]
        assert (now["hazard"], now["molecular_weight"]) == ("bsl2", None)
        assert now["summary"] == "Needs re-verification"
        revision = _last(record)
        assert (revision.changed_fields, revision.user, revision.reason) == (
            ["molecular_weight", "summary"],
            world.users["auditor"],
            "Biosafety committee, 2026-11-17",
        )
        assert _rechecked(record) == []


def test_a_registrars_change_clears_every_holders_check_in_the_same_queries(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However many records the entry has: a type's are cleared together."""
    from django.test.utils import CaptureQueriesContext
    from test_registries import registrar

    _with_recheck(monkeypatch)
    curator = registrar(world)
    outsider = api_as(world, "outsider")
    projects = [_rival(world, f"Screens {n}") for n in range(1, 6)]
    counts = []
    for sequence, holding in (("MKVLLLA", projects[:2]), ("MKVLLLG", projects)):
        values = {"sequence": sequence, "hazard": "bsl1"}
        records = [create(outsider, p, values, PROTEIN).json() for p in holding]
        for record in records:
            _weighed(world, record)
        url = f"/entries/{records[0]['hrid']}"
        tag = curator.get(url)["ETag"]
        with CaptureQueriesContext(connection) as queries:
            curated = curator.patch(
                url,
                {"values": {"hazard": "bsl2"}, "reason": "Biosafety committee, 2026-11-17"},
                headers={"If-Match": tag},
            )
        assert curated.status_code == 200, curated.content
        counts.append(len(queries))
        weights = {
            e.pk: entity_services.current_values(entity_services.specific(e))["molecular_weight"]
            for e in Entity.objects.filter(pk__in=[r["id"] for r in records])
        }
        assert set(weights.values()) == {None} and len(weights) == len(holding)
    assert counts[0] == counts[1]


@pytest.mark.parametrize("by", ["editing", "registering"])
def test_a_holders_new_fact_clears_what_another_projects_check_read_of_it(
    world: World, monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    """The entry's records in other projects have the results of their checks reading it
    cleared too, by no one, as whoever gave the fact isn't told to the other holders (ADR
    0115). A record that can't change now keeps its result, flagged to be worked out
    again."""
    _with_recheck(monkeypatch)
    rival = _rival(world)
    third = _rival(world, "Third Screens")
    outsider = api_as(world, "outsider")
    theirs = create(outsider, rival, {"sequence": "MKVLLLA"}, PROTEIN).json()
    archived = create(outsider, third, {"sequence": "MKVLLLA"}, PROTEIN).json()
    for record in (theirs, archived):
        _weighed(world, record)
    entity_services.set_archived(
        world.users["outsider"], Entity.objects.get(pk=archived["id"]), True, reason="Done"
    )
    if by == "registering":
        given = create(
            api_as(world), world.project, {"sequence": "MKVLLLA", "hazard": "bsl2"}, PROTEIN
        )
        assert given.status_code == 201, given.content
    else:
        mine = create(api_as(world), world.project, {"sequence": "MKVLLLA"}, PROTEIN).json()
        filled = change(world, mine["id"], {"hazard": "bsl2"}, reason="Biosafety review")
        assert filled.status_code == 200, filled.content
    now = outsider.get(f"/entities/{theirs['id']}").json()["values"]
    assert (now["hazard"], now["molecular_weight"]) == ("bsl2", None)
    assert now["summary"] == "Needs re-verification"
    revision = _last(theirs)
    assert (revision.changed_fields, revision.user, revision.via, revision.agent) == (
        ["molecular_weight", "summary"],
        None,
        "system",
        None,
    )
    assert revision.reason == f"Its registry entry {theirs['hrid']} was given Hazard."
    assert _rechecked(theirs) == []
    kept = outsider.get(f"/entities/{archived['id']}").json()
    assert kept["values"]["molecular_weight"] == 30123.4
    assert kept["needs_recalculation"] == ["acme/protein.compute_properties"]


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
    assert keys == ["lims_id", "chembl_target", "acme/prediction:predictor_named"]
    assert registry.types()[BATCH].rules["one_per_well"].kind == "unique"


# --- Finding what rules caught (ADR 0135) -------------------------------------------------------


def found(world: World, where: dict[str, Any], who: str = "editor", **body: Any) -> list[str]:
    """The names of the batches a search finds."""
    answer = api_as(world, who).post(
        "/entities/search", {"type": BATCH, "where": where, "limit": 100, **body}
    )
    assert answer.status_code == 200, answer.content
    return sorted(e["values"]["name"] for e in answer.json()["items"])


def caught(world: World) -> dict[str, dict[str, Any]]:
    """Three batches: one a save held, one a deploy found breaking a rule, and one fine."""
    held = made(world, {"name": "Held", "purity": 82.5}, hold_reason="As measured on the SEC.")
    broken = made(world, {"name": "Broken", "made_on": "2026-11-01", "expires_on": "2026-12-01"})
    fine = made(world, {"name": "Fine", "purity": 97.0})
    _set_column(broken["id"], "expires_on", "2026-10-01")
    RulesChecked.objects.all().delete()
    breaches.recheck()
    return {"held": held, "broken": broken, "fine": fine}


def test_a_search_finds_what_rules_hold_and_break_and_says_which(world: World) -> None:
    roles(world, admin=(QA,))
    records = caught(world)
    held = {"field": "held", "op": "is_set", "value": True}
    assert found(world, held) == ["Held"]
    assert found(world, {"field": "held", "op": "eq", "value": "purity_in_specification"}) == [
        "Held"
    ]
    assert found(world, {"field": "held", "op": "is_set", "value": False}) == ["Broken", "Fine"]
    assert found(world, {"field": "held", "op": "ne", "value": "purity_in_specification"}) == [
        "Broken",
        "Fine",
    ]
    assert found(world, {"field": "breaks", "op": "is_set", "value": True}) == ["Broken"]
    assert found(
        world,
        {"field": "breaks", "op": "in", "value": ["expires_after_made", "pass_in_specification"]},
    ) == ["Broken"]
    # Either: what a rule caught, of any kind.
    either = {"any": [held, {"field": "breaks", "op": "is_set", "value": True}]}
    assert found(world, either) == ["Broken", "Held"]
    # Each result says why: which rule holds it, and which it breaks.
    answer = api_as(world, "editor").post(
        "/entities/search", {"type": BATCH, "where": either, "sort": {"field": "name"}}
    )
    broken_out, held_out = answer.json()["items"]
    assert [(h["rule"], h["label"]) for h in held_out["holds"]] == [
        ("purity_in_specification", "Purity in specification")
    ]
    assert [(b["rule"], b["label"]) for b in broken_out["breaches"]] == [
        ("expires_after_made", "Expires after it's made")
    ]
    # Reviewed, it isn't held any more.
    [hold] = held_out["holds"]
    review = f"/entities/{records['held']['id']}/holds/{hold['id']}/review"
    clearance = api_as(world, "admin").post(
        review,
        {"reason": "Investigated.", "review_version": hold["review_version"]},
        headers={"If-Match": f'"{held_out["revision"]}"'},
    )
    assert clearance.status_code == 200, clearance.content
    assert found(world, held) == []
    # Without a type, any entity: a rule's name is any rule's of that name.
    assert found(world, held, type=None) == []
    named = {"field": "breaks", "op": "eq", "value": "expires_after_made"}
    assert found(world, named, type=None) == ["Broken"]


def test_a_search_by_rule_names_one_of_its_types(world: World) -> None:
    caught(world)
    api = api_as(world, "viewer")

    def refused(where: dict[str, Any]) -> list[tuple[str, str]]:
        body = problem(
            api.post("/entities/search", {"type": BATCH, "where": where}), 422, "validation_failed"
        )
        return [(e["pointer"], e["detail"]) for e in body["errors"]]

    assert refused({"field": "held", "op": "eq", "value": "expires_after_made"}) == [
        (
            "#/where/value",
            "isn't a rule that holds them for review: Batches have `purity_in_specification`.",
        )
    ]
    assert refused({"field": "breaks", "op": "in", "value": ["expires_after_made", "nope"]}) == [
        (
            "#/where/value/1",
            "isn't a rule they can break once saved: Batches have `pass_in_specification`, "
            "`reviewer_isnt_analyst`, `expires_after_made`, `protein_not_bsl3` or "
            "`proteins_not_bsl3`.",
        )
    ]
    assert refused({"field": "held", "op": "is_set", "value": "yes"}) == [
        ("#/where/value", "must be true or false.")
    ]
    assert refused({"field": "held", "op": "eq", "value": ""}) == [
        ("#/where/value", "must name a rule, such as `purity_in_specification`.")
    ]
    assert refused({"field": "breaks", "op": "gt", "value": "x"})[0][0] == "#/where/op"
    sort = api.post("/entities/search", {"type": BATCH, "sort": {"field": "held"}})
    problem(sort, 422, "validation_failed")
    # A rule its plugin no longer has still names what it holds.
    RuleHold.objects.update(rule="purity_at_least_90")
    assert found(world, {"field": "held", "op": "eq", "value": "purity_at_least_90"}) == ["Held"]
    # Another type has none that hold.
    protein_search = api.post(
        "/entities/search",
        {
            "type": "acme/protein.Protein",
            "where": {"field": "held", "op": "eq", "value": "purity_in_specification"},
        },
    )
    body = problem(protein_search, 422, "validation_failed")
    assert body["errors"][0]["detail"] == "Proteins have no rules that hold them for review."


def test_a_search_finds_only_what_you_can_read_held(world: World) -> None:
    caught(world)
    other = Project.objects.create(team=world.team, name="Elsewhere")
    entity_services.create(
        world.users["org_admin"],
        BATCH,
        other,
        {"name": "Elsewhere", "purity": 50.0},
        hold_reason="As measured.",
    )
    assert RuleHold.objects.filter(entity__owner_project=other).count() == 1
    held = {"field": "held", "op": "is_set", "value": True}
    # Members of the project read its held batch, and nobody's reads the other's.
    for who in ("viewer", "editor", "admin"):
        assert found(world, held, who) == ["Held"], who
    assert found(world, held, "outsider") == []
    assert found(world, held, "outsider", type=None) == []
    # Naming the project you can't read is refused, as it is for any search.
    elsewhere = api_as(world, "viewer").post(
        "/entities/search", {"type": BATCH, "project": str(other.pk), "where": held}
    )
    assert elsewhere.status_code == 404 or elsewhere.json()["items"] == [], elsewhere.content


def test_a_saved_search_keeps_what_rules_caught(world: World) -> None:
    caught(world)
    api = api_as(world, "editor")
    held = {"field": "held", "op": "eq", "value": "purity_in_specification"}
    saved = api.post(
        "/saved-searches",
        {
            "project": str(world.project.pk),
            "name": "Held for QA",
            "query": {"type": BATCH, "where": held},
        },
    )
    assert saved.status_code == 201, saved.content
    query = saved.json()["query"]
    assert query["where"] == held
    assert found(world, query["where"]) == ["Held"]
    wrong = api.post(
        "/saved-searches",
        {
            "project": str(world.project.pk),
            "name": "Typo",
            "query": {"type": BATCH, "where": {**held, "value": "purity"}},
        },
    )
    body = problem(wrong, 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/query/where/value"


def test_the_searches_columns_are_the_specs() -> None:
    from iki_server.entities import search
    from iki_spec import conditions

    assert set(search._BUILT_IN) == set(conditions.BUILT_IN)
    assert {n: c.kind for n, c in search._BUILT_IN.items()} == dict(conditions.BUILT_IN)
