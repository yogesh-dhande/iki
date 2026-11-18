"""Record states (ADR 0118): a type's states and the transitions between them, each limited
to lab roles (ADR 0116), some needing a reason, some kept from whoever made an earlier one,
and states that lock a record against every change but a transition out."""

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


def move(
    world: World, who: str, ref: str, name: str, reason: str = "", etag: str | None = None
) -> Any:
    api = api_as(world, who)
    if etag is None:
        etag = api.get(f"/entities/{ref}")["ETag"]
    return api.post(
        f"/entities/{ref}/transitions/{name}", {"reason": reason}, headers={"If-Match": etag}
    )


def moved(world: World, who: str, ref: str, name: str, reason: str = "") -> dict[str, Any]:
    answer = move(world, who, ref, name, reason)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def reviewed(world: World) -> dict[str, Any]:
    """A sample the editor tested and submitted and the admin approved: locked."""
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    made = sample(world)
    moved(world, "editor", made["id"], "start")
    moved(world, "editor", made["id"], "submit")
    return moved(world, "admin", made["id"], "approve")


def offered(body: dict[str, Any]) -> dict[str, str | None]:
    """The transitions a record offers its reader: each one's name, and why not."""
    return {t["name"]: t["why_not"] for t in body["can"]["transitions"]}


# --- The type and a new record ------------------------------------------------------------------


def test_a_type_says_its_states_and_transitions(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{SAMPLE}").json()
    assert [(s["name"], s["label"], s["locks"]) for s in found["states"]] == [
        ("received", "Received", False),
        ("in_testing", "In testing", False),
        ("in_review", "In review", False),
        ("reviewed", "Reviewed", True),
        ("released", "Released", True),
    ]
    by_name = {t["name"]: t for t in found["transitions"]}
    assert list(by_name) == ["start", "submit", "approve", "send_back", "release", "reopen"]
    assert by_name["release"] == {
        "name": "release",
        "label": "Release",
        "description": None,
        "from": ["reviewed"],
        "to": "released",
        "roles": [QA],
        "reason": "optional",
        "not_by": ["submit", "approve"],
    }
    assert by_name["send_back"]["roles"] == [REVIEWER, QA]
    # A type without them says so.
    protein = api_as(world, "viewer").get("/entity-types/acme/protein.Protein").json()
    assert (protein["states"], protein["transitions"]) == ([], [])


def test_a_record_starts_in_its_initial_state(world: World) -> None:
    made = sample(world)
    assert made["state"] == {
        "name": "received",
        "label": "Received",
        "description": None,
        "locks": False,
    }
    assert made["locked"] is False
    assert made["can"]["edit"] is True
    assert made["can"]["transitions"] == [
        {
            "name": "start",
            "label": "Start testing",
            "to": "in_testing",
            "to_label": "In testing",
            "reason": None,
            "allowed": False,
            "why_not": "It needs someone with the Analyst role in Kinase Atlas.",
        }
    ]
    # Records of types without states have none.
    protein = create(api_as(world), world.project, {"sequence": "MKV"}, "acme/protein.Protein")
    assert (protein.json()["state"], protein.json()["can"]["transitions"]) == (None, [])


# --- Making transitions -------------------------------------------------------------------------


def test_a_transition_needs_one_of_its_lab_roles(world: World) -> None:
    made = sample(world)
    refused = problem(move(world, "editor", made["id"], "start"), 403, "forbidden")
    assert refused["detail"] == "It needs someone with the Analyst role in Kinase Atlas."
    # A project Admin is no exception: the role is what counts.
    problem(move(world, "admin", made["id"], "start"), 403, "forbidden")
    assert states.state_of(Entity.objects.get(pk=made["id"])) == "received"

    # A Viewer with the role makes it: it changes no values.
    roles(world, viewer=(ANALYST,))
    assert offered(api_as(world, "viewer").get(f"/entities/{made['id']}").json()) == {"start": None}
    started = move(world, "viewer", made["id"], "start", reason="Bench 4")
    assert started.status_code == 200, started.content
    assert started["ETag"] == '"2"'
    body = started.json()
    assert (body["state"]["name"], body["revision"], body["locked"]) == ("in_testing", 2, False)
    assert offered(body) == {"submit": None}

    # Its history has it, with who and why.
    (latest, _) = api_as(world, "viewer").get(f"/entities/{made['id']}/revisions").json()["items"]
    assert (latest["kind"], latest["user"]["name"], latest["reason"]) == (
        "transitioned",
        "Viewer",
        "Bench 4",
    )
    assert latest["changed_fields"] == []
    assert latest["transition"] == {
        "name": "start",
        "label": "Start testing",
        "from": {"name": "received", "label": "Received", "description": None, "locks": False},
        "to": {"name": "in_testing", "label": "In testing", "description": None, "locks": False},
    }


def test_whoever_moved_it_in_cant_approve_it_out(world: World) -> None:
    roles(world, editor=(ANALYST, REVIEWER, QA), admin=(REVIEWER, QA), viewer=(QA,))
    made = sample(world)
    moved(world, "editor", made["id"], "start")
    submitted = moved(world, "editor", made["id"], "submit")
    why = "You made “Submit for review” on it, so someone else makes “Approve”: the plugin "
    why += "keeps them apart."
    # The editor holds the role, so it's offered, and says why not.
    assert offered(submitted) == {"approve": why, "send_back": None}
    refused = problem(move(world, "editor", made["id"], "approve"), 409, "refused")
    assert refused["detail"] == why

    approved = moved(world, "admin", made["id"], "approve")
    assert approved["state"]["name"] == "reviewed"
    # Release is kept from whoever submitted it and from whoever approved it.
    assert offered(approved)["release"] == (
        "You made “Approve” on it, so someone else makes “Release”: the plugin keeps them apart."
    )
    problem(move(world, "admin", made["id"], "release"), 409, "refused")
    problem(move(world, "editor", made["id"], "release"), 409, "refused")
    released = moved(world, "viewer", made["id"], "release")
    assert (released["state"]["name"], released["locked"]) == ("released", True)


def test_only_the_latest_transition_named_counts(world: World) -> None:
    """Sent back and submitted again by someone else, the first submitter may approve."""
    roles(world, editor=(ANALYST, REVIEWER), admin=(ANALYST, REVIEWER))
    made = sample(world)
    moved(world, "editor", made["id"], "start")
    moved(world, "editor", made["id"], "submit")
    moved(world, "admin", made["id"], "send_back", reason="Purity looks off")
    moved(world, "admin", made["id"], "submit")
    assert moved(world, "editor", made["id"], "approve")["state"]["name"] == "reviewed"


def test_a_transition_can_require_a_reason(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    made = sample(world)
    moved(world, "editor", made["id"], "start")
    moved(world, "editor", made["id"], "submit")
    body = problem(move(world, "admin", made["id"], "send_back", "   "), 422, "validation_failed")
    assert body["errors"] == [
        {"pointer": "#/reason", "detail": "“Send back” needs a reason: say why."}
    ]
    back = moved(world, "admin", made["id"], "send_back", "Purity looks off")
    assert back["state"]["name"] == "in_testing"


def test_a_transition_from_another_state_is_refused(world: World) -> None:
    roles(world, viewer=(QA,))
    made = sample(world)
    refused = problem(move(world, "viewer", made["id"], "release"), 409, "refused")
    assert refused["detail"] == f"{made['hrid']} is Received: “Release” leads from Reviewed."
    unknown = problem(move(world, "viewer", made["id"], "publish"), 409, "refused")
    assert unknown["detail"] == "Sample records have no transition `publish`."
    protein = create(api_as(world), world.project, {"sequence": "MKV"}, "acme/protein.Protein")
    none = problem(move(world, "viewer", protein.json()["id"], "start"), 409, "refused")
    assert none["detail"] == f"{protein.json()['hrid']} has no states."


def test_a_transition_names_the_revision_it_was_decided_on(world: World) -> None:
    roles(world, editor=(ANALYST,))
    made = sample(world)
    stale = problem(move(world, "editor", made["id"], "start", etag='"7"'), 412, "stale")
    assert stale["detail"].startswith("Someone changed it since you read it")
    api = api_as(world, "editor")
    missing = api.post(f"/entities/{made['id']}/transitions/start", {})
    problem(missing, 428, "precondition_required")


# --- Locked ---------------------------------------------------------------------------------------


def test_a_locked_record_refuses_every_change(world: World) -> None:
    body = reviewed(world)
    ref, hrid = body["id"], body["hrid"]
    assert (body["locked"], body["can"]["edit"]) == (True, False)
    why = f"{hrid} is Reviewed, which locks it: it can’t be changed until “Send back” takes it out."

    # Through the API.
    changed = patch(api_as(world), ref, {"values": {"notes": "x"}, "reason": "R"}, '"4"')
    assert problem(changed, 409, "refused")["detail"] == why
    archived = patch(api_as(world), ref, {"archived": True}, '"4"')
    assert problem(archived, 409, "refused")["detail"] == (
        f"{hrid} is Reviewed, which locks it: it can’t be archived or restored until “Send "
        "back” takes it out."
    )
    # Through the services, as runs and imports write.
    entity = Entity.objects.get(pk=ref)
    with pytest.raises(Refused, match="which locks it"):
        entity_services.update(
            world.users["editor"], entity, {"notes": "x"}, expected_revision=4, by_run=True
        )
    # And in the database, whatever writes it.
    table = registry.model(SAMPLE)._meta.db_table
    for statement in (
        f"UPDATE {table} SET notes = 'x' WHERE entity_id = %s",  # noqa: S608 - ours
        f"DELETE FROM {table} WHERE entity_id = %s",  # noqa: S608 - ours
        f"UPDATE {table} SET tags = ARRAY['x'] WHERE entity_id = %s",  # noqa: S608 - ours
    ):
        with (
            pytest.raises(IntegrityError, match="is locked by its state"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, [ref])
    assert Entity.objects.get(pk=ref).revision == 4

    # A transition out unlocks it.
    back = moved(world, "admin", ref, "send_back", "Rerun purity")
    assert (back["locked"], back["can"]["edit"]) == (False, True)
    assert patch(api_as(world), ref, {"values": {"notes": "x"}}, '"5"').status_code == 200


def test_a_released_record_reopens_with_a_reason(world: World) -> None:
    body = reviewed(world)
    released = moved(world, "viewer", body["id"], "release", "Meets spec")
    assert released["locked"] is True
    assert offered(released) == {"reopen": None}
    problem(move(world, "viewer", body["id"], "reopen"), 422, "validation_failed")
    # Only QA reopens it.
    problem(move(world, "admin", body["id"], "reopen", "Wrong lot"), 403, "forbidden")
    reopened = moved(world, "viewer", body["id"], "reopen", "Wrong lot")
    assert (reopened["state"]["name"], reopened["locked"]) == ("in_testing", False)
    history = api_as(world, "viewer").get(f"/entities/{body['id']}/revisions").json()["items"]
    assert [(r["transition"] or {}).get("name") for r in history] == [
        "reopen",
        "release",
        "approve",
        "submit",
        "start",
        None,
    ]
    # The audit log has each, who made it and why: where QA reviews releases.
    events = AuditEvent.objects.filter(action="entity.transitioned", target_id=body["id"])
    assert [
        (e.actor_label, e.data["transition"], e.data["reason"]) for e in events.order_by("id")
    ] == [
        ("editor@example.com", "start", ""),
        ("editor@example.com", "submit", ""),
        ("admin@example.com", "approve", ""),
        ("viewer@example.com", "release", "Meets spec"),
        ("viewer@example.com", "reopen", "Wrong lot"),
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
            "detail": f"This action changes it, but {body['hrid']} is Reviewed, which locks "
            "it: it can’t be changed until “Send back” takes it out.",
        }
    ]
    # ...and a run launched before it locked writes nothing.
    moved(world, "admin", body["id"], "send_back", "Rerun purity")
    run = launch(api_as(world), world.project, {"sample": body["id"]}, MEASURE)
    assert run.status_code == 201, run.content
    pod = worker(world)
    job = pod.lease().json()
    moved(world, "editor", body["id"], "submit")
    moved(world, "admin", body["id"], "approve")
    updates = {"sample": [{"entity": body["id"], "values": {"purity": 99.0}}]}
    answer = done(pod, job, output(updates=updates))
    assert (answer["run_status"], answer["code"]) == ("rejected", "conflict")
    assert "which locks it" in answer["message"]


def imported(world: World, *rows: tuple[str, float]) -> dict[str, Any]:
    """An import_samples run whose command wrote a creates file of these (label, purity)."""
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
                "values": {"label": label, "purity": purity},
                "origin": {"source": "CSV file", "id": label, "file": file_id, "sha256": SHA},
            }
            for label, purity in rows
        )
    )
    answer = pod.report(job, "complete", output=output(creates_file=upload(pod, job, data)))
    assert answer.status_code == 200, answer.content
    return finished(world, run.json())


def test_an_import_starts_new_records_in_the_initial_state_and_cant_change_locked_ones(
    world: World, s3: Any
) -> None:
    first = imported(world, ("S-1", 90.0), ("S-2", 91.0))
    assert first["status"] == "succeeded", first.get("error")
    made = {e.hrid: states.state_of(e) for e in Entity.objects.filter(type=SAMPLE).order_by("hrid")}
    assert set(made.values()) == {"received"}

    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    one = Entity.objects.get(type=SAMPLE, hrid=min(made))  # S-1, made first
    for name, who in (("start", "editor"), ("submit", "editor"), ("approve", "admin")):
        moved(world, who, str(one.pk), name)
    second = imported(world, (one_label := _label(one), 99.0))
    assert second["status"] == "failed", second
    assert "which locks it" in str(second["error"]), second["error"]
    assert Entity.objects.get(pk=one.pk).revision == 4
    assert one_label == "S-1"


def _label(entity: Entity) -> str:
    value: str = (
        registry.model(SAMPLE)._default_manager.values_list("label", flat=True).get(pk=entity.pk)
    )
    return value


# --- Telling of them ------------------------------------------------------------------------------


def test_transitions_are_told_to_webhooks_and_fire_triggers(world: World) -> None:
    webhooks.create(
        project=world.project,
        url="https://lims.example.com/iki",
        description="",
        chosen=["entity.transitioned"],
        by=world.users["admin"],
    )
    on(world, IN_TESTING)
    roles(world, editor=(ANALYST,))
    made = sample(world)
    moved(world, "editor", made["id"], "start", reason="Bench 4")
    (sent,) = Delivery.objects.exclude(event="ping")
    assert sent.event == "entity.transitioned"
    data = sent.payload["data"]
    assert data["transition"] == {
        "name": "start",
        "label": "Start testing",
        "from": {"name": "received", "label": "Received"},
        "to": {"name": "in_testing", "label": "In testing"},
    }
    assert (data["revision"]["kind"], data["revision"]["reason"]) == ("transitioned", "Bench 4")
    assert data["actor"]["name"] == "Editor"
    (firing,) = TriggerFiring.objects.all()
    assert (firing.event, str(firing.entity_id), firing.revision) == (
        "entity.transitioned",
        made["id"],
        2,
    )
    # It fires only on entering the states it names.
    moved(world, "editor", made["id"], "submit")
    assert TriggerFiring.objects.count() == 1


# --- Finding them ---------------------------------------------------------------------------------


def test_search_filters_by_state(world: World) -> None:
    roles(world, editor=(ANALYST,))
    first, second, third = (sample(world, f"S-{n}") for n in (1, 2, 3))
    moved(world, "editor", second["id"], "start")
    moved(world, "editor", third["id"], "start")
    moved(world, "editor", third["id"], "submit")
    api = api_as(world, "viewer")

    def found(where: dict[str, Any]) -> list[str]:
        answer = api.post("/entities/search", {"type": SAMPLE, "where": where})
        assert answer.status_code == 200, answer.content
        return [e["hrid"] for e in answer.json()["items"]]

    assert found({"field": "state", "op": "eq", "value": "in_testing"}) == [second["hrid"]]
    assert found({"field": "state", "op": "in", "value": ["received", "in_review"]}) == [
        first["hrid"],
        third["hrid"],
    ]
    assert found({"field": "state", "op": "ne", "value": "received"}) == [
        second["hrid"],
        third["hrid"],
    ]
    wrong = api.post(
        "/entities/search",
        {"type": SAMPLE, "where": {"field": "state", "op": "eq", "value": "shipped"}},
    )
    assert problem(wrong, 422, "validation_failed")["errors"][0]["pointer"] == "#/where/value"
    # Only a type with states has them.
    other = api.post(
        "/entities/search",
        {"type": "acme/protein.Protein", "where": {"field": "state", "op": "is_set"}},
    )
    problem(other, 422, "validation_failed")


# --- Who may: every principal, in and out of the project ------------------------------------------


def test_who_may_make_a_transition(world: World) -> None:
    """Each principal, given the role where they can be, against what permissions say."""
    elsewhere = build_world("b-")
    made = sample(world)
    theirs = create(api_as(elsewhere, "editor"), elsewhere.project, {"label": "B-1"}, SAMPLE).json()
    direct = {"viewer", "editor", "runner", "admin", "mixed"}
    for principal in direct:
        roles(world, **{principal: (ANALYST,)})
    for principal in PRINCIPALS:
        user = world.user(principal)
        # Their own world's record is out of reach of this one, and the other way round.
        api = api_as(world, principal)
        assert api.get(f"/entities/{theirs['id']}").status_code in (401, 404), principal
        etag = '"1"'
        answer = api.post(
            f"/entities/{theirs['id']}/transitions/start", {}, headers={"If-Match": etag}
        )
        assert answer.status_code in (401, 404), (principal, answer.status_code)
        if user is None or not user.is_active:
            continue
        reads = permissions.can(user, Action.PROJECT_READ, world.project)
        expected = 200 if principal in direct else 403 if reads else 404
        entity = Entity.objects.get(pk=made["id"])
        answer = move(world, principal, made["id"], "start", etag=f'"{entity.revision}"')
        if answer.status_code == 200:
            # Back where it was, by a reviewer, for the next principal.
            Entity.objects.filter(pk=made["id"]).update(locked=False)
            registry.model(SAMPLE)._default_manager.filter(pk=made["id"]).update(
                iki_state="received"
            )
        assert answer.status_code == expected, (principal, answer.status_code, answer.content)

    # In an archived project nobody may.
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    for principal in direct:
        permissions.forget(world.users[principal])
        entity = Entity.objects.get(pk=made["id"])
        answer = move(world, principal, made["id"], "start", etag=f'"{entity.revision}"')
        assert answer.status_code in (403, 409), (principal, answer.status_code)
    assert Revision.objects.filter(entity_id=theirs["id"]).count() == 1
