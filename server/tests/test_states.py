"""State fields (ADR 0125): a type's fields of `type: state`, each with its own states, each
state set by the lab roles (ADR 0116) it names, from any other, some needing a reason, and
states that lock a record against every change but its state fields'."""

from typing import Any

import pytest
from api_client import problem
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from org_world import PRINCIPALS, World, build_world
from test_api_entities import api_as, create, patch
from test_bulk_imports import finished, lines, upload
from test_lab_roles import give, membership
from test_run_imports import SHA
from test_run_results import done, output
from test_runs import launch, world
from test_triggers import on
from test_workers import keep_connection, worker

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry, states
from iki_server.entities import services as entity_services
from iki_server.entities.models import Entity, Revision
from iki_server.files import services as files
from iki_server.files.services import EMPTY_SHA256
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs.models import TriggerFiring
from iki_server.webhooks import services as webhooks
from iki_server.webhooks.models import Delivery

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers and test_runs

SAMPLE = "acme/protein.Sample"
ANALYST, REVIEWER, QA = "acme/protein.analyst", "acme/protein.reviewer", "acme/protein.qa"
MEASURE = "acme/protein.measure_purity"
IMPORT_SAMPLES = "acme/protein.import_samples"
IN_TESTING = "acme/protein.measure_samples_in_testing"
#: What the fixture's Sample says unlocks its Status.
UNLOCKS = (
    "until its Status is set to Received (Analyst), In testing (Analyst, Reviewer or QA) or "
    "In review (Analyst)"
)


def sample(world: World, label: str = "S-1", project: Project | None = None) -> dict[str, Any]:
    made = create(api_as(world, "editor"), project or world.project, {"label": label}, SAMPLE)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def roles(world: World, **held: tuple[str, ...]) -> None:
    for principal, given in held.items():
        answer = give(world, membership(world, principal), *given)
        assert answer.status_code == 200, answer.content
        permissions.forget(world.users[principal])


def put(
    world: World,
    who: str,
    ref: str,
    field: str,
    state: str,
    reason: str = "",
    etag: str | None = None,
) -> Any:
    api = api_as(world, who)
    if etag is None:
        etag = api.get(f"/entities/{ref}")["ETag"]
    return api.post(
        f"/entities/{ref}/states/{field}",
        {"state": state, "reason": reason},
        headers={"If-Match": etag},
    )


def set_(
    world: World, who: str, ref: str, field: str, state: str, reason: str = ""
) -> dict[str, Any]:
    answer = put(world, who, ref, field, state, reason)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def reviewed(world: World) -> dict[str, Any]:
    """A sample the editor tested and submitted and the admin reviewed: locked."""
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    made = sample(world)
    set_(world, "editor", made["id"], "status", "in_testing")
    set_(world, "editor", made["id"], "status", "in_review")
    done: dict[str, Any] = set_(world, "admin", made["id"], "status", "reviewed")
    return done


def offered(body: dict[str, Any], field: str = "status") -> dict[str, str | None]:
    """The states a record's state field offers its reader: each one's name, and why not."""
    return {s["name"]: s["why_not"] for s in body["can"]["states"][field]}


# --- The type and a new record ------------------------------------------------------------------


def test_a_type_says_its_state_fields_and_their_states(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{SAMPLE}").json()
    fields = {f["name"]: f for f in found["fields"]}
    status = fields["status"]
    assert (status["type"], status["initial"]) == ("state", "received")
    assert [(s["name"], s["label"], s["locks"], s["reason"]) for s in status["states"]] == [
        ("received", "Received", False, None),
        ("in_testing", "In testing", False, "optional"),
        ("in_review", "In review", False, None),
        ("reviewed", "Reviewed", True, None),
        ("released", "Released", True, "optional"),
    ]
    assert status["states"][1]["set_by"] == [ANALYST, REVIEWER, QA]
    assert status["states"][4] == {
        "name": "released",
        "label": "Released",
        "description": "Its results can be used.",
        "set_by": [QA],
        "locks": True,
        "holds_amount": False,
        "on_edit": None,
        "reason": "optional",
        "signatures": [],
        "guards": [],
    }
    assert [s["name"] for s in fields["custody"]["states"]] == ["in_lab", "quarantined", "disposed"]
    # Its schema says a state field is set on its own.
    schema = found["values_schema"]["properties"]["status"]
    assert (schema["readOnly"], schema["x-iki-state"], schema["enum"][0]) == (
        True,
        True,
        "received",
    )
    # Other fields have no states.
    assert (fields["label"]["states"], fields["label"]["initial"]) == ([], None)


def test_type_flow_explains_edges_roles_signatures_guards_and_corrections(world: World) -> None:
    api = api_as(world, "viewer")
    reply = api.get("/entity-types/acme/protein.Batch/flow")
    assert reply.status_code == 200
    flow = reply.json()
    assert (flow["type"], flow["label"]) == ("acme/protein.Batch", "Batch")
    status, custody = flow["fields"]
    assert [f["name"] for f in (status, custody)] == ["status", "custody"]
    assert status["initial"] == "in_testing"
    assert [e for e in status["transitions"] if e["to"] == "approved"] == [
        {"from": "in_review", "to": "approved", "conditional": True}
    ]
    assert all(e["from"] != e["to"] for e in status["transitions"])
    assert custody["transitions"] == [
        {"from": "quarantined", "to": "in_lab", "conditional": False},
        {"from": "in_lab", "to": "quarantined", "conditional": False},
    ]
    nodes = {s["name"]: s for s in status["states"]}
    assert nodes["approved"]["on_edit"] == "in_testing"
    assert nodes["approved"]["set_by"] == [REVIEWER]
    assert nodes["approved"]["guards"][0]["message"] == "A batch is approved from In review."
    assert [(s["meaning"], s["roles"]) for s in nodes["certified"]["signatures"]] == [
        ("review", [REVIEWER]),
        ("approval", [QA]),
    ]
    assert api.get("/entity-types/acme%2Fdna.Sequence/flow").json()["fields"] == []
    assert api.get("/entity-types/iki/core.Entity/flow").json() == {
        "type": "iki/core.Entity",
        "label": "Entity",
        "fields": [],
    }
    problem(api.get("/entity-types/acme/dna.Unknown/flow"), 404, "not_found")


def test_a_record_starts_in_its_initial_states(world: World) -> None:
    made = sample(world)
    assert (made["values"]["status"], made["values"]["custody"]) == ("received", "in_lab")
    assert (made["locked"], made["locked_by"], made["can"]["edit"]) == (False, [], True)
    needs = "It needs someone with the {} role in Kinase Atlas."
    assert offered(made) == {
        "in_testing": needs.format("Analyst, Reviewer or QA"),
        "in_review": needs.format("Analyst"),
        "reviewed": needs.format("Reviewer"),
        "released": needs.format("QA"),
    }
    assert made["can"]["states"]["custody"][0] == {
        "name": "quarantined",
        "label": "Quarantined",
        "locks": True,
        "reason": "required",
        "allowed": False,
        "why_not": needs.format("QA"),
        "signed": False,
        "guard": None,
    }
    # Its history says where it started.
    (first,) = api_as(world).get(f"/entities/{made['id']}/revisions").json()["items"]
    assert {"status", "custody"} <= set(first["changed_fields"])
    # Records of types without state fields have none.
    protein = create(api_as(world), world.project, {"sequence": "MKV"}, "acme/protein.Protein")
    assert protein.json()["can"]["states"] == {}


def test_correctability_is_refreshed_after_a_role_change(world: World) -> None:
    from test_rules import made

    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    ref = made(world, {"purity": 95})["id"]
    set_(world, "editor", ref, "status", "in_review")
    set_(world, "admin", ref, "status", "approved")
    api = api_as(world, "editor")
    first = api.get(f"/entities/{ref}")
    assert first.json()["can"]["correct"]
    roles(world, editor=())
    refreshed = api.call("GET", f"/entities/{ref}", headers={"If-None-Match": first["ETag"]})
    assert refreshed.status_code == 200
    assert not refreshed.json()["can"]["correct"]


def test_a_state_field_is_set_on_its_own(world: World) -> None:
    alone = "is a state field: it's set on its own"
    given = create(
        api_as(world, "editor"), world.project, {"label": "S", "status": "released"}, SAMPLE
    )
    (error,) = problem(given, 422, "validation_failed")["errors"]
    assert (error["pointer"], alone in error["detail"]) == ("#/values/status", True)
    made = sample(world)
    changed = patch(api_as(world), made["id"], {"values": {"custody": "disposed"}}, '"1"')
    (error,) = problem(changed, 422, "validation_failed")["errors"]
    assert (error["pointer"], alone in error["detail"]) == ("#/values/custody", True)
    assert Entity.objects.get(pk=made["id"]).revision == 1


# --- Setting a state ------------------------------------------------------------------------------


def test_a_state_needs_one_of_its_lab_roles(world: World) -> None:
    made = sample(world)
    refused = problem(put(world, "editor", made["id"], "status", "in_review"), 403, "forbidden")
    assert refused["detail"] == "It needs someone with the Analyst role in Kinase Atlas."
    # A project Admin is no exception: the role is what counts.
    problem(put(world, "admin", made["id"], "status", "in_review"), 403, "forbidden")
    assert states.values_of(Entity.objects.get(pk=made["id"]))["status"] == "received"

    # A Viewer with the role sets it: from any state, to any its roles set.
    roles(world, viewer=(ANALYST,))
    assert offered(api_as(world, "viewer").get(f"/entities/{made['id']}").json()) == {
        "in_testing": None,
        "in_review": None,
        "reviewed": "It needs someone with the Reviewer role in Kinase Atlas.",
        "released": "It needs someone with the QA role in Kinase Atlas.",
    }
    answer = put(world, "viewer", made["id"], "status", "in_review", reason="Bench 4")
    assert answer.status_code == 200, answer.content
    assert answer["ETag"] == '"2"'
    body = answer.json()
    assert (body["values"]["status"], body["revision"], body["locked"]) == ("in_review", 2, False)
    assert "in_review" not in offered(body)
    # And back, from any state.
    assert set_(world, "viewer", made["id"], "status", "received")["values"]["status"] == "received"

    # Its history has each, with who, why, and the field and states.
    api = api_as(world, "viewer")
    (latest, middle, _) = api.get(f"/entities/{made['id']}/revisions").json()["items"]
    assert (middle["kind"], middle["user"]["name"], middle["reason"]) == (
        "state_changed",
        "Viewer",
        "Bench 4",
    )
    assert middle["changed_fields"] == ["status"]
    assert middle["state"] == {
        "field": {"name": "status", "label": "Status"},
        "from": {"name": "received", "label": "Received"},
        "to": {"name": "in_review", "label": "In review"},
        "signatures": [],
    }
    assert latest["state"]["to"]["name"] == "received"
    # The value at each revision is in its history too.
    at = api.get(f"/entities/{made['id']}/revisions/2").json()
    assert at["values"]["status"] == "in_review"
    assert at["changes"]["status"] == {"before": "received", "after": "in_review"}
    # And its provenance says who set it.
    provenance = api.get(f"/entities/{made['id']}/provenance").json()
    assert (provenance["status"]["number"], provenance["status"]["kind"]) == (3, "state_changed")


def test_a_state_can_require_a_reason(world: World) -> None:
    roles(world, viewer=(QA,))
    made = sample(world)
    body = problem(
        put(world, "viewer", made["id"], "custody", "quarantined", "  "), 422, "validation_failed"
    )
    assert body["errors"] == [
        {
            "pointer": "#/reason",
            "detail": "Setting its Custody to Quarantined needs a reason: say why.",
        }
    ]
    held = set_(world, "viewer", made["id"], "custody", "quarantined", "Seal broken")
    assert (held["values"]["custody"], held["locked_by"]) == ("quarantined", ["custody"])


def test_a_state_change_is_checked(world: World) -> None:
    roles(world, viewer=(ANALYST, QA))
    made = sample(world)
    hrid = made["hrid"]
    same = problem(put(world, "viewer", made["id"], "status", "received"), 409, "refused")
    assert same["detail"] == f"{hrid}'s Status is Received already."
    unknown = problem(
        put(world, "viewer", made["id"], "status", "shipped"), 422, "validation_failed"
    )
    assert unknown["errors"][0]["pointer"] == "#/state"
    assert "`received`, `in_testing`" in unknown["errors"][0]["detail"]
    field = problem(put(world, "viewer", made["id"], "phase", "one"), 409, "refused")
    assert field["detail"] == "Sample records have no state field `phase`."
    # A field that isn't a state field isn't set this way.
    problem(put(world, "viewer", made["id"], "label", "x"), 409, "refused")
    protein = create(api_as(world), world.project, {"sequence": "MKV"}, "acme/protein.Protein")
    none = problem(put(world, "viewer", protein.json()["id"], "status", "received"), 409, "refused")
    assert none["detail"] == "Protein records have no state field `status`."


def test_a_state_change_names_the_revision_it_was_decided_on(world: World) -> None:
    roles(world, editor=(ANALYST,))
    made = sample(world)
    stale = problem(
        put(world, "editor", made["id"], "status", "in_testing", etag='"7"'), 412, "stale"
    )
    assert stale["detail"].startswith("Someone changed it since you read it")
    api = api_as(world, "editor")
    missing = api.post(f"/entities/{made['id']}/states/status", {"state": "in_testing"})
    problem(missing, 428, "precondition_required")


def test_an_archived_record_keeps_its_states(world: World) -> None:
    roles(world, editor=(ANALYST,))
    made = sample(world)
    assert patch(api_as(world), made["id"], {"archived": True}, '"1"').status_code == 200
    body = api_as(world, "editor").get(f"/entities/{made['id']}").json()
    assert offered(body)["in_testing"] == "It's archived: restore it first."
    refused = problem(put(world, "editor", made["id"], "status", "in_testing"), 409, "refused")
    assert refused["detail"] == "It's archived: restore it first."


# --- Locked ---------------------------------------------------------------------------------------


def test_a_locked_record_refuses_every_change_but_its_states(world: World) -> None:
    body = reviewed(world)
    ref, hrid = body["id"], body["hrid"]
    assert (body["locked"], body["locked_by"], body["can"]["edit"]) == (True, ["status"], False)
    why = f"{hrid} is locked by its Status (Reviewed): it can’t be changed {UNLOCKS}."

    # Through the API.
    changed = patch(api_as(world), ref, {"values": {"notes": "x"}, "reason": "R"}, '"4"')
    assert problem(changed, 409, "refused")["detail"] == why
    archived = patch(api_as(world), ref, {"archived": True}, '"4"')
    assert problem(archived, 409, "refused")["detail"] == (
        f"{hrid} is locked by its Status (Reviewed): it can’t be archived or restored {UNLOCKS}."
    )
    # Through the services, as runs and imports write.
    entity = Entity.objects.get(pk=ref)
    with pytest.raises(Refused, match="is locked by its Status"):
        entity_services.update(
            world.users["editor"], entity, {"notes": "x"}, expected_revision=4, by_run=True
        )
    # And in the database, whatever writes it: its state fields too.
    table = registry.model(SAMPLE)._meta.db_table
    for statement in (
        f"UPDATE {table} SET notes = 'x' WHERE entity_id = %s",  # noqa: S608 - ours
        f"UPDATE {table} SET status = 'received' WHERE entity_id = %s",  # noqa: S608 - ours
        f"DELETE FROM {table} WHERE entity_id = %s",  # noqa: S608 - ours
        f"UPDATE {table} SET tags = ARRAY['x'] WHERE entity_id = %s",  # noqa: S608 - ours
    ):
        with (
            pytest.raises(IntegrityError, match="is locked by its status"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, [ref])
    assert Entity.objects.get(pk=ref).revision == 4

    # Its other state fields stay settable by their roles, locking or not.
    disposed = set_(world, "viewer", ref, "custody", "disposed", "Used up")
    assert (disposed["locked_by"], disposed["revision"]) == (["status"], 5)
    held = set_(world, "viewer", ref, "custody", "quarantined", "Seal broken")
    assert held["locked_by"] == ["status", "custody"]
    both = patch(api_as(world), ref, {"values": {"notes": "x"}}, '"6"')
    assert problem(both, 409, "refused")["detail"] == (
        f"{hrid} is locked by its Status (Reviewed) and its Custody (Quarantined): it can’t be "
        f"changed {UNLOCKS} and its Custody is set to In the lab (Analyst or QA) or Disposed "
        "(QA)."
    )
    # Setting a field to a state that doesn't lock takes its lock off: the other's stays.
    back = set_(world, "admin", ref, "status", "in_testing", "Rerun purity")
    assert (back["locked_by"], back["can"]["edit"]) == (["custody"], False)
    freed = set_(world, "viewer", ref, "custody", "in_lab")
    assert (freed["locked"], freed["locked_by"]) == (False, [])
    assert api_as(world, "editor").get(f"/entities/{ref}").json()["can"]["edit"] is True
    edit = {"values": {"notes": "x"}}
    assert patch(api_as(world), ref, edit, '"8"').status_code == 422
    assert (
        patch(api_as(world), ref, {**edit, "reason": "Corrected after review"}, '"8"').status_code
        == 200
    )


def test_a_later_lock_declaration_does_not_retroactively_lock_an_edit(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    roles(world, editor=(ANALYST,))
    ref = sample(world)["id"]
    changed = set_(world, "editor", ref, "status", "in_testing")
    assert not changed["locked"]
    field = states.state_fields(SAMPLE)["status"]
    monkeypatch.setitem(field.states, "received", replace(field.states["received"], locks=True))
    api = api_as(world, "editor")
    record = api.get(f"/entities/{ref}")
    assert "edit_reason_required" not in record.json()["can"]
    assert (
        patch(api, ref, {"values": {"notes": "First measurement"}}, record["ETag"]).status_code
        == 200
    )


def test_state_changes_are_in_the_audit_log(world: World) -> None:
    body = reviewed(world)
    released = set_(world, "viewer", body["id"], "status", "released", "Meets spec")
    assert released["locked"] is True
    # Only QA sends it back from released, as from anywhere.
    problem(put(world, "admin", body["id"], "status", "released"), 409, "refused")
    reopened = set_(world, "viewer", body["id"], "status", "in_testing", "Wrong lot")
    assert (reopened["values"]["status"], reopened["locked"]) == ("in_testing", False)
    history = api_as(world, "viewer").get(f"/entities/{body['id']}/revisions").json()["items"]
    assert [(r["state"] or {}).get("to", {}).get("name") for r in history] == [
        "in_testing",
        "released",
        "reviewed",
        "in_review",
        "in_testing",
        None,
    ]
    # The audit log has each, who set it and why: where QA reviews releases.
    events = AuditEvent.objects.filter(action="entity.state_changed", target_id=body["id"])
    assert [
        (e.actor_label, e.data["field"], e.data["from"], e.data["to"], e.data["reason"])
        for e in events.order_by("id")
    ] == [
        ("editor@example.com", "status", "received", "in_testing", ""),
        ("editor@example.com", "status", "in_testing", "in_review", ""),
        ("admin@example.com", "status", "in_review", "reviewed", ""),
        ("viewer@example.com", "status", "reviewed", "released", "Meets spec"),
        ("viewer@example.com", "status", "released", "in_testing", "Wrong lot"),
    ]
    assert events.filter(project=world.project).count() == 5


def test_a_run_cant_change_a_locked_record(world: World) -> None:
    body = reviewed(world)
    # Launching on it is refused...
    refused = launch(api_as(world), world.project, {"sample": body["id"]}, MEASURE)
    errors = problem(refused, 422, "validation_failed")["errors"]
    assert errors == [
        {
            "pointer": "#/inputs/sample",
            "detail": f"This action changes it, but {body['hrid']} is locked by its Status "
            f"(Reviewed): it can’t be changed {UNLOCKS}.",
        }
    ]
    # ...and a run launched before it locked writes nothing.
    set_(world, "admin", body["id"], "status", "in_testing", "Rerun purity")
    target = {"sample": body["id"]}
    missing = launch(api_as(world), world.project, target, MEASURE)
    assert problem(missing, 422, "validation_failed")["errors"][0]["pointer"] == "#/reason"
    run = launch(api_as(world), world.project, target, MEASURE, reason="Repeat the measurement")
    assert run.status_code == 201, run.content
    pod = worker(world)
    job = pod.lease().json()
    set_(world, "editor", body["id"], "status", "in_review")
    set_(world, "admin", body["id"], "status", "reviewed")
    updates = {"sample": [{"entity": body["id"], "values": {"purity": 99.0}}]}
    answer = done(pod, job, output(updates=updates))
    assert (answer["run_status"], answer["code"]) == ("rejected", "conflict")
    assert "is locked by its Status" in answer["message"]


def imported_values(world: World, *rows: dict[str, Any]) -> dict[str, Any]:
    """An import_samples run whose command wrote a creates file of samples of these values."""
    upload_ = files.start(
        world.users["editor"],
        world.project,
        name="samples.csv",
        media_type="text/csv",
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload_.refresh_from_db()
    file_id = str(upload_.file_id)
    run = launch(api_as(world), world.project, {}, IMPORT_SAMPLES, params={"file": file_id})
    assert run.status_code == 201, run.content
    pod = worker(world)
    job = pod.lease().json()
    data = lines(
        *(
            {
                "type": SAMPLE,
                "values": values,
                "origin": {
                    "source": "CSV file",
                    "id": values["label"],
                    "file": file_id,
                    "sha256": SHA,
                },
            }
            for values in rows
        )
    )
    answer = pod.report(job, "complete", output=output(creates_file=upload(pod, job, data)))
    assert answer.status_code == 200, answer.content
    return finished(world, run.json())


def test_an_import_starts_new_records_in_their_initial_states_and_cant_change_locked_ones(
    world: World, s3: Any
) -> None:
    first = imported_values(
        world, {"label": "S-1", "purity": 90.0}, {"label": "S-2", "purity": 91.0}
    )
    assert first["status"] == "succeeded", first.get("error")
    made = {
        e.hrid: states.values_of(e) for e in Entity.objects.filter(type=SAMPLE).order_by("hrid")
    }
    assert list(made.values()) == [{"status": "received", "custody": "in_lab"}] * 2
    # Nor can an import set a state.
    stated = imported_values(world, {"label": "S-3", "status": "released"})
    assert stated["status"] == "failed", stated
    assert "is a state field" in str(stated["error"]), stated["error"]

    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    one = Entity.objects.get(type=SAMPLE, hrid=min(made))  # S-1, made first
    for state, who in (("in_testing", "editor"), ("in_review", "editor"), ("reviewed", "admin")):
        set_(world, who, str(one.pk), "status", state)
    second = imported_values(world, {"label": _label(one), "purity": 99.0})
    assert second["status"] == "failed", second
    assert "is locked by its Status" in str(second["error"]), second["error"]
    assert Entity.objects.get(pk=one.pk).revision == 4


def _label(entity: Entity) -> str:
    value: str = (
        registry.model(SAMPLE)._default_manager.values_list("label", flat=True).get(pk=entity.pk)
    )
    return value


# --- Telling of them ------------------------------------------------------------------------------


def test_state_changes_are_told_to_webhooks_and_fire_triggers(world: World) -> None:
    webhooks.create(
        project=world.project,
        url="https://lims.example.com/iki",
        description="",
        chosen=["entity.state_changed"],
        by=world.users["admin"],
    )
    on(world, IN_TESTING)
    roles(world, editor=(ANALYST,))
    made = sample(world)
    set_(world, "editor", made["id"], "status", "in_testing", reason="Bench 4")
    (sent,) = Delivery.objects.exclude(event="ping")
    assert sent.event == "entity.state_changed"
    data = sent.payload["data"]
    assert data["state"] == {
        "field": {"name": "status", "label": "Status"},
        "from": {"name": "received", "label": "Received"},
        "to": {"name": "in_testing", "label": "In testing"},
    }
    assert (data["revision"]["kind"], data["revision"]["reason"]) == ("state_changed", "Bench 4")
    assert data["revision"]["changed_fields"] == ["status"]
    assert data["actor"]["name"] == "Editor"
    (firing,) = TriggerFiring.objects.all()
    assert (firing.event, str(firing.entity_id), firing.revision) == (
        "entity.state_changed",
        made["id"],
        2,
    )
    # It fires only for its field, set to the states it names.
    set_(world, "editor", made["id"], "status", "in_review")
    roles(world, editor=(ANALYST, QA))
    set_(world, "editor", made["id"], "custody", "disposed", "Used up")
    assert TriggerFiring.objects.count() == 1
    assert Delivery.objects.exclude(event="ping").count() == 3


# --- Finding them ---------------------------------------------------------------------------------


def test_search_filters_by_each_state_field(world: World) -> None:
    roles(world, editor=(ANALYST, QA))
    first, second, third = (sample(world, f"S-{n}") for n in (1, 2, 3))
    set_(world, "editor", second["id"], "status", "in_testing")
    set_(world, "editor", third["id"], "status", "in_review")
    set_(world, "editor", third["id"], "custody", "quarantined", "Seal broken")
    api = api_as(world, "viewer")

    def found(where: dict[str, Any]) -> list[str]:
        answer = api.post("/entities/search", {"type": SAMPLE, "where": where})
        assert answer.status_code == 200, answer.content
        return [e["hrid"] for e in answer.json()["items"]]

    assert found({"field": "status", "op": "eq", "value": "in_testing"}) == [second["hrid"]]
    assert found({"field": "status", "op": "in", "value": ["received", "in_review"]}) == [
        first["hrid"],
        third["hrid"],
    ]
    assert found({"field": "status", "op": "ne", "value": "received"}) == [
        second["hrid"],
        third["hrid"],
    ]
    assert found({"field": "custody", "op": "eq", "value": "quarantined"}) == [third["hrid"]]
    wrong = api.post(
        "/entities/search",
        {"type": SAMPLE, "where": {"field": "status", "op": "eq", "value": "shipped"}},
    )
    assert problem(wrong, 422, "validation_failed")["errors"][0]["pointer"] == "#/where/value"
    # Only a type with the field has it.
    other = api.post(
        "/entities/search",
        {"type": "acme/protein.Protein", "where": {"field": "status", "op": "is_set"}},
    )
    problem(other, 422, "validation_failed")


# --- Who may: every principal, in and out of the project, for each state field -------------------

#: For each state field, a state to set it to that doesn't lock, the role that sets it, and
#: the reason it needs.
SETTABLE = {"status": ("in_testing", ANALYST, ""), "custody": ("disposed", QA, "Used up")}


@pytest.mark.parametrize("field", list(SETTABLE))
def test_who_may_set_a_state(world: World, field: str) -> None:
    """Each principal, given the role where they can be, against what permissions say."""
    state, role, reason = SETTABLE[field]
    initial = {"status": "received", "custody": "in_lab"}[field]
    elsewhere = build_world("b-")
    made = sample(world)
    theirs = create(api_as(elsewhere, "editor"), elsewhere.project, {"label": "B-1"}, SAMPLE).json()
    direct = {"viewer", "editor", "runner", "admin", "mixed"}
    for principal in direct:
        roles(world, **{principal: (role,)})
    model = registry.model(SAMPLE)._default_manager
    for principal in PRINCIPALS:
        user = world.user(principal)
        # Their own world's record is out of reach of this one, and the other way round.
        api = api_as(world, principal)
        assert api.get(f"/entities/{theirs['id']}").status_code in (401, 404), principal
        answer = api.post(
            f"/entities/{theirs['id']}/states/{field}",
            {"state": state, "reason": reason},
            headers={"If-Match": '"1"'},
        )
        assert answer.status_code in (401, 404), (principal, answer.status_code)
        if user is None or not user.is_active:
            continue
        reads = permissions.can(user, Action.PROJECT_READ, world.project)
        expected = 200 if principal in direct else 403 if reads else 404
        entity = Entity.objects.get(pk=made["id"])
        answer = put(
            world, principal, made["id"], field, state, reason, etag=f'"{entity.revision}"'
        )
        assert answer.status_code == expected, (principal, answer.status_code, answer.content)
        if answer.status_code == 200:
            # Back where it was, for the next principal.
            model.filter(pk=made["id"]).update(**{field: initial})
        else:
            assert states.values_of(entity)[field] == initial

    # Nobody may while it's archived, nor in an archived project.
    Entity.objects.filter(pk=made["id"]).update(archived_at=timezone.now())
    for principal in direct:
        entity = Entity.objects.get(pk=made["id"])
        answer = put(
            world, principal, made["id"], field, state, reason, etag=f'"{entity.revision}"'
        )
        assert problem(answer, 409, "refused")["detail"] == "It's archived: restore it first."
    Entity.objects.filter(pk=made["id"]).update(archived_at=None)
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    for principal in direct:
        permissions.forget(world.users[principal])
        entity = Entity.objects.get(pk=made["id"])
        answer = put(
            world, principal, made["id"], field, state, reason, etag=f'"{entity.revision}"'
        )
        assert answer.status_code in (403, 409), (principal, answer.status_code)
    assert Revision.objects.filter(entity_id=theirs["id"]).count() == 1


def test_an_import_writes_lists_of_rows_and_points_at_a_wrong_one(world: World, s3: Any) -> None:
    """ADR 0137: a bulk import writes each row's fields to their columns and the history,
    and a row that doesn't fit is named where it is."""
    from test_runs import protein

    abl1 = protein(api_as(world), world.project)
    rows = [
        {"material": abl1["id"], "amount": {"value": 2, "unit": "mg"}, "added_on": "2026-11-01"},
        {"material": abl1["id"], "note": "Again"},
    ]
    done_ = imported_values(world, {"label": "S-1", "additions": rows})
    assert done_["status"] == "succeeded", done_.get("error")
    one = Entity.objects.get(type=SAMPLE)
    found = api_as(world).get(f"/entities/{one.hrid}").json()["values"]["additions"]
    assert [(r["amount"], r["added_on"], r["note"]) for r in found] == [
        (2000.0, "2026-11-01", None),
        (None, None, "Again"),
    ]
    first = api_as(world).get(f"/entities/{one.hrid}/revisions/1").json()["values"]
    assert first["additions"] == found
    wrong = imported_values(world, {"label": "S-2", "additions": [{"note": "No material"}]})
    assert wrong["status"] == "failed", wrong
    assert "additions/0/material" in str(wrong["error"]), wrong["error"]
