"""Requests between projects (ADR 0132): a project asks another's service for work, sending
the records it's about; the service works it in its own fields and states, and releases its
results back. Each side reads only what the request gives it, and writes only its own."""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest
from api_client import Api, problem
from django.db import DatabaseError, IntegrityError, connection, transaction
from org_world import World, build_world
from scan_fixtures import clean_blob
from test_api_entities import create, patch
from test_audit import as_app_role
from test_signatures import begin, evidence, past_the_triggers, ready, signed

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import access, requests, services, states
from iki_server.entities.models import Entity, ProjectLink, Request
from iki_server.entities.values import shape
from iki_server.files.models import File
from iki_server.org import permissions
from iki_server.org.models import (
    LabRoleGrant,
    Project,
    ProjectMembership,
    ProjectRole,
    Registry,
)
from iki_server.org.services import Refused
from iki_server.plugins.operations import unlocked
from iki_server.signatures import services as signatures
from iki_server.signatures.models import Status
from iki_spec.model import Signature

__all__ = ["as_app_role"]  # imported fixture

pytestmark = pytest.mark.django_db

ASK = "acme/protein.AssayRequest"
RESULT = "acme/protein.AssayResult"
SAMPLE = "acme/protein.Sample"
LOT = "acme/protein.Lot"
ANALYST, REVIEWER, QA = "acme/protein.analyst", "acme/protein.reviewer", "acme/protein.qa"


@dataclass
class Lab:
    """The asking project (the world's Kinase Atlas) and a core facility serving it."""

    world: World
    core: Project
    users: dict[str, User]

    def api(self, who: str) -> Api:
        return Api(self.world.browser(who))


def _member(project: Project, user: User, role: ProjectRole, *lab_roles: str) -> None:
    grant = ProjectMembership.objects.create(project=project, user=user, role=role)
    for lab_role in lab_roles:
        LabRoleGrant.objects.create(membership=grant, role=lab_role)
    permissions.forget(user)


@pytest.fixture
def lab() -> Lab:
    return build_lab(build_world())


def build_lab(world: World) -> Lab:
    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    for name, role, held in (
        ("tech", ProjectRole.EDITOR, (ANALYST,)),
        ("core_qa", ProjectRole.EDITOR, (QA, REVIEWER)),
        ("core_admin", ProjectRole.ADMIN, ()),
        ("core_viewer", ProjectRole.VIEWER, ()),
    ):
        user = User.objects.create_user(f"{name}@example.com", name.replace("_", " ").title())
        world.users[name] = user
        _member(core, user, role, *held)
    # The asking project's Editor asks, as its Analyst.
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["editor"]), role=ANALYST
    )
    permissions.forget(world.users["editor"])
    return Lab(world, core, world.users)


def takes(lab: Lab, **body: Any) -> Any:
    answer = lab.api("core_admin").put(
        f"/projects/{lab.core.pk}/intake", {"types": [ASK], "from_registry": True, **body}
    )
    assert answer.status_code == 200, answer.content
    return answer.json()


def sample(lab: Lab, label: str) -> dict[str, Any]:
    made = create(lab.api("editor"), lab.world.project, {"label": label}, SAMPLE)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def ask(lab: Lab, *samples: dict[str, Any], **values: Any) -> dict[str, Any]:
    made = create(
        lab.api("editor"),
        lab.world.project,
        {"title": "Purity of the kinases", "samples": [s["id"] for s in samples], **values},
        ASK,
    )
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def submit(lab: Lab, ref: str, who: str = "editor", service: Any = None) -> Any:
    api = lab.api(who)
    etag = api.get(f"/entities/{ref}")["ETag"]
    return api.post(
        f"/entities/{ref}/submit",
        {"service": str(service or lab.core.pk), "reason": "Q4 batch"},
        headers={"If-Match": etag},
    )


def sent(lab: Lab, *samples: dict[str, Any]) -> dict[str, Any]:
    takes(lab)
    made = ask(lab, *samples)
    answer = submit(lab, made["id"])
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def change(lab: Lab, who: str, ref: str, values: dict[str, Any]) -> Any:
    api = lab.api(who)
    return patch(api, ref, {"values": values}, api.get(f"/entities/{ref}")["ETag"])


def state(lab: Lab, who: str, ref: str, field: str, to: str, reason: str = "") -> Any:
    api = lab.api(who)
    return api.post(
        f"/entities/{ref}/states/{field}",
        {"state": to, "reason": reason},
        headers={"If-Match": api.get(f"/entities/{ref}")["ETag"]},
    )


def result(lab: Lab, request_id: str, **values: Any) -> dict[str, Any]:
    made = create(lab.api("tech"), lab.core, {"request": request_id, **values}, RESULT)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


# --- The types ----------------------------------------------------------------------------------


def test_a_type_says_its_records_are_requests_or_results(lab: Lab) -> None:
    api = lab.api("viewer")
    asked = api.get(f"/entity-types/{ASK}").json()
    assert asked["request"] == {
        "service_fields": ["status", "priority", "scheduled_on", "technician", "core_notes"],
        "sends": ["samples", "protein", "tubes"],
        "receives": "tubes",
        "received_by": [ANALYST],
    }
    assert asked["result"] is None
    found = api.get(f"/entity-types/{RESULT}").json()
    assert found["result"] == {
        "of": "request",
        "released_field": "review",
        "released_states": ["released"],
    }
    assert api.get(f"/entity-types/{SAMPLE}").json()["request"] is None


def test_a_request_names_its_assays_from_the_choices_its_list_offers(lab: Lab) -> None:
    # A list's items' choices are the list's: a form offers them, and the type's
    # reference lists them.
    api = lab.api("editor")
    fields = {f["name"]: f for f in api.get(f"/entity-types/{ASK}").json()["fields"]}
    assert fields["assays"]["type"] == "list" and fields["assays"]["items"] == "enum"
    assert fields["assays"]["values"] == ["sec_purity", "intact_mass", "peptide_map", "endotoxin"]
    made = create(
        api, lab.world.project, {"title": "Lot 12 release", "assays": ["sec_purity"]}, ASK
    )
    assert made.status_code == 201, made.content
    assert made.json()["values"]["assays"] == ["sec_purity"]
    body = problem(
        create(api, lab.world.project, {"title": "Lot 13", "assays": ["purity"]}, ASK),
        422,
        "validation_failed",
    )
    assert [e["pointer"] for e in body["errors"]] == ["#/values/assays"]


# --- Intake -------------------------------------------------------------------------------------


def test_a_services_admins_choose_what_it_takes_and_from_whom(lab: Lab) -> None:
    path = f"/projects/{lab.core.pk}/intake"
    assert lab.api("core_viewer").get(path).json() == {
        "types": [],
        "from_registry": False,
        "projects": [],
    }
    body = {"types": [ASK], "from_registry": False, "projects": [str(lab.world.project.pk)]}
    problem(lab.api("tech").put(path, body), 403, "forbidden")
    # Its Admin names only projects they can see: one they can't is no project at all.
    refused = problem(lab.api("core_admin").put(path, body), 422, "validation_failed")
    assert refused["errors"] == [
        {"pointer": "#/projects/0", "detail": "is no project you can see."}
    ]
    _member(lab.world.project, lab.users["core_admin"], ProjectRole.VIEWER)
    answer = lab.api("core_admin").put(path, body)
    assert answer.status_code == 200, answer.content
    assert answer.json() == {
        "types": [{"name": ASK, "label": "Assay request"}],
        "from_registry": False,
        "projects": [
            {
                "id": str(lab.world.project.pk),
                "name": "Kinase Atlas",
                "slug": lab.world.project.slug,
            }
        ],
    }
    # Only request types, and never itself.
    wrong = {"types": [SAMPLE, "acme/protein.Nope"], "projects": [str(lab.core.pk)]}
    refused = problem(lab.api("core_admin").put(path, wrong), 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/types/0", "#/types/1", "#/projects/0"]
    changed = AuditEvent.objects.get(action="project.intake_changed")
    assert changed.project == lab.core
    assert changed.data["types"] == [[], [ASK]]


@pytest.mark.django_db(transaction=True)
def test_two_admins_changing_what_it_takes_are_each_audited(lab: Lab) -> None:
    """One Admin opens it to every project in the registry; another, at the same moment,
    keeps it closed. The second waits for the first, and its change is audited against
    the first's: the log ends where the setting does."""
    core = Project.objects.get(pk=lab.core.pk)
    first_set, first_may_commit = threading.Event(), threading.Event()
    other = User.objects.create_user("core_admin2@example.com", "Core Admin Two")
    _member(core, other, ProjectRole.ADMIN)
    admins = [lab.users["core_admin"], other]
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with transaction.atomic():
                requests.set_intake(core, types=[ASK], from_registry=True, sources=[], by=admins[0])
                first_set.set()
                first_may_commit.wait(10)
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    def second() -> None:
        try:
            first_set.wait(10)
            requests.set_intake(core, types=[ASK], from_registry=False, sources=[], by=admins[1])
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    first_set.wait(10)
    # The second waits for the first's lock, rather than reading what was there before it.
    threads[1].join(0.5)
    assert threads[1].is_alive()
    first_may_commit.set()
    for t in threads:
        t.join(20)
    assert errors == []
    assert requests.intake_of(core).from_registry is False
    changes = [
        (e.actor_id, e.data)
        for e in AuditEvent.objects.filter(action="project.intake_changed").order_by("occurred_at")
    ]
    assert changes == [
        (admins[0].pk, {"types": [[], [ASK]], "from_registry": [False, True]}),
        (admins[1].pk, {"from_registry": [True, False]}),
    ]


def test_a_project_lists_the_services_it_may_send_to(lab: Lab) -> None:
    path = f"/projects/{lab.world.project.pk}/services"
    viewer = lab.api("viewer")
    assert viewer.get(path).json() == []
    takes(lab)
    listed = viewer.get(path).json()
    assert [(s["project"]["name"], s["description"]) for s in listed] == [
        ("Protein Core", "Assays")
    ]
    assert listed[0]["types"] == [{"name": ASK, "label": "Assay request"}]
    assert viewer.get(path, type=ASK).json() == listed
    assert viewer.get(path, type=SAMPLE).json() == []
    assert viewer.get(path, type="acme/protein.Nope").json() == []
    # Named projects alone, once it doesn't take from the whole registry.
    takes(lab, from_registry=False)
    assert viewer.get(path).json() == []
    # And never from another registry: requests stay within one (ADR 0115).
    takes(lab)
    other = Registry.objects.create(name="Partners")
    Project.objects.filter(pk=lab.core.pk).update(registry=other)
    assert viewer.get(path).json() == []


# --- Sending ------------------------------------------------------------------------------------


def test_a_request_isnt_the_services_until_its_sent(lab: Lab) -> None:
    refused = problem(
        create(lab.api("editor"), lab.world.project, {"title": "T", "core_notes": "x"}, ASK),
        422,
        "validation_failed",
    )
    assert refused["errors"][0]["pointer"] == "#/values/core_notes"
    made = ask(lab)
    assert made["request"] == {
        "asking_project": {
            "id": str(lab.world.project.pk),
            "name": "Kinase Atlas",
            "slug": lab.world.project.slug,
        },
        "service": None,
        "submitted": False,
        "submitted_at": None,
        "submitted_by": None,
        "service_fields": ["status", "priority", "scheduled_on", "technician", "core_notes"],
        "sends": ["samples", "protein", "tubes"],
        "yours": ["asker"],
    }
    assert made["values"]["status"] == "submitted"
    problem(change(lab, "editor", made["id"], {"priority": "urgent"}), 422, "validation_failed")
    # Nobody sets the service's states before it has one.
    _member(lab.world.project, lab.users["tech"], ProjectRole.EDITOR, QA)
    refused = problem(state(lab, "tech", made["id"], "status", "accepted"), 409, "refused")
    assert "the project the request is sent to sets it" in refused["detail"]


def test_sending_a_request_shares_it_and_its_samples_with_the_service(lab: Lab) -> None:
    first, second, kept = sample(lab, "S-1"), sample(lab, "S-2"), sample(lab, "S-3")
    takes(lab)
    made = ask(lab, first, second)
    tech = lab.api("tech")
    problem(tech.get(f"/entities/{made['id']}"), 404, "not_found")

    problem(submit(lab, made["id"], "viewer"), 403, "forbidden")
    answer = submit(lab, made["id"])
    assert answer.status_code == 200, answer.content
    body = answer.json()
    assert body["request"]["service"]["name"] == "Protein Core"
    assert body["request"]["submitted"] and body["request"]["submitted_by"]["name"] == "Editor"
    assert body["revision"] == 1  # Sending isn't a change of its values.

    # The service reads it and what it sent: nothing else of the asking project's.
    seen = tech.get(f"/entities/{made['id']}").json()
    assert seen["request"]["yours"] == ["service"]
    assert seen["can"]["edit"] is True
    for s in (first, second):
        assert tech.get(f"/entities/{s['id']}").status_code == 200
    problem(tech.get(f"/entities/{kept['id']}"), 404, "not_found")
    two_sides = tech.get(f"/entities/{made['id']}/request").json()
    assert [e["hrid"] for e in two_sides["sent"]] == [first["hrid"], second["hrid"]]
    assert two_sides["results"] == []

    # Audited on both sides.
    assert AuditEvent.objects.get(action="request.submitted").project == lab.world.project
    received = AuditEvent.objects.get(action="request.received")
    assert received.project == lab.core
    assert received.data["sent"] == [first["id"], second["id"]]
    assert received.data["reason"] == "Q4 batch"

    # It goes to one service, for good.
    problem(submit(lab, made["id"]), 409, "refused")


def test_a_project_lists_the_services_it_has_sent_to_by_name_alone(lab: Lab) -> None:
    path = f"/projects/{lab.world.project.pk}/sent-to"
    viewer = lab.api("viewer")
    assert viewer.get(path).json() == []
    sent(lab)
    expected = [{"id": str(lab.core.pk), "name": "Protein Core", "slug": lab.core.slug}]
    assert viewer.get(path).json() == expected
    # Once it's sent there, it stays listed, though the core takes no more of them.
    takes(lab, types=[])
    assert viewer.get(path).json() == expected
    assert viewer.get(f"/projects/{lab.world.project.pk}/services").json() == []
    # Nothing else of the core's shows its asker, nor the other way round.
    problem(viewer.get(f"/projects/{lab.core.pk}"), 404, "not_found")
    problem(viewer.get(f"/projects/{lab.core.pk}/sent-to"), 404, "not_found")
    assert lab.api("core_viewer").get(f"/projects/{lab.core.pk}/sent-to").json() == []


def test_a_service_lists_who_has_sent_it_requests_by_name_alone(lab: Lab) -> None:
    path = f"/projects/{lab.core.pk}/sent-from"
    tech = lab.api("tech")
    assert tech.get(path).json() == []
    made = sent(lab)
    asking = lab.world.project
    assert tech.get(path).json() == [
        {"id": str(asking.pk), "name": asking.name, "slug": asking.slug}
    ]
    # Its queue is searched by who asked, though its people can't see that project.
    problem(tech.get(f"/projects/{asking.pk}"), 404, "not_found")
    found = tech.post(
        "/entities/search",
        {"type": ASK, "where": {"field": "owner_project", "op": "eq", "value": str(asking.pk)}},
    )
    assert [e["hrid"] for e in found.json()["items"]] == [made["hrid"]]
    problem(tech.get(f"/projects/{asking.pk}/sent-from"), 404, "not_found")
    assert lab.api("viewer").get(f"/projects/{asking.pk}/sent-from").json() == []


def test_a_project_that_doesnt_take_it_looks_like_none_at_all(lab: Lab) -> None:
    made = ask(lab)
    nothing = problem(submit(lab, made["id"]), 404, "not_found")
    made_up = problem(
        submit(lab, made["id"], service="0190d5a4-0000-7000-8000-000000000000"), 404, "not_found"
    )
    assert nothing["detail"] == made_up["detail"]
    assert (
        nothing["detail"] == "No such service, or it doesn’t take Assay requests from Kinase Atlas."
    )
    # Nor itself, nor one in another registry.
    takes(lab)
    problem(submit(lab, made["id"], service=lab.world.project.pk), 404, "not_found")
    assert not Request.objects.exists()


def test_a_project_that_doesnt_share_cant_send_requests(lab: Lab) -> None:
    made = ask(lab, sample(lab, "S-1"))
    takes(lab)
    Project.objects.filter(pk=lab.world.project.pk).update(allow_sharing=False)
    refused = problem(submit(lab, made["id"]), 409, "refused")
    assert "doesn’t allow sharing" in refused["detail"]
    assert not ProjectLink.objects.filter(project=lab.core).exists()


def test_a_stale_request_isnt_sent(lab: Lab) -> None:
    takes(lab)
    made = ask(lab)
    answer = lab.api("editor").post(
        f"/entities/{made['id']}/submit",
        {"service": str(lab.core.pk)},
        headers={"If-Match": '"7"'},
    )
    problem(answer, 412, "stale")


# --- Two sides ----------------------------------------------------------------------------------


def test_each_side_changes_only_its_own_fields(lab: Lab) -> None:
    made = sent(lab)
    ref = made["id"]
    refused = problem(change(lab, "tech", ref, {"instructions": "Hurry"}), 422, "validation_failed")
    assert refused["errors"] == [
        {
            "pointer": "#/values/instructions",
            "detail": "is Kinase Atlas's: the project that sent the request changes it.",
        }
    ]
    answer = change(lab, "tech", ref, {"core_notes": "Bench 2", "priority": "urgent"})
    assert answer.status_code == 200, answer.content
    refused = problem(change(lab, "editor", ref, {"core_notes": "No"}), 422, "validation_failed")
    assert refused["errors"][0]["detail"] == (
        "is Protein Core's: the project serving the request changes it."
    )
    assert change(lab, "editor", ref, {"instructions": "Twice"}).status_code == 200
    # The service doesn't archive it, nor do its Viewers change it.
    api = lab.api("tech")
    archive = patch(api, ref, {"archived": True}, api.get(f"/entities/{ref}")["ETag"])
    problem(archive, 403, "forbidden")
    problem(change(lab, "core_viewer", ref, {"core_notes": "x"}), 403, "forbidden")
    values = lab.api("editor").get(f"/entities/{ref}").json()["values"]
    assert (values["core_notes"], values["priority"], values["instructions"]) == (
        "Bench 2",
        "urgent",
        "Twice",
    )


SCHEDULE = "acme/protein.schedule_request"


def schedule(lab: Lab, who: str, project: Project, ref: str) -> Any:
    """Launches the service's planning action, which writes the request's service fields."""
    from iki_server.runs import catalog

    catalog.compute.cache_clear()
    return lab.api(who).post(
        f"/actions/{SCHEDULE}/runs",
        {"project": str(project.pk), "inputs": {"request": [ref]}},
    )


def test_a_run_changes_a_request_only_as_its_own_projects_side(lab: Lab) -> None:
    """A run writes as the project it runs in, alone (ADR 0132): it's launched only if the
    fields it changes are that side's, whoever launches it and whichever sides they're on,
    not refused once it has run."""
    from iki_server.runs.models import ActionRun

    unsent = ask(lab)
    refused = problem(
        schedule(lab, "editor", lab.world.project, unsent["id"]), 422, "validation_failed"
    )
    assert refused["errors"] == [
        {
            "pointer": "#/inputs/request",
            "detail": f"This action changes `core_notes` and `scheduled_on` of "
            f"{unsent['hrid']}, the service's once it's sent: a run in the project it's sent "
            "to changes them, not one in Kinase Atlas.",
        }
    ]
    takes(lab)
    ref = unsent["id"]
    assert submit(lab, ref).status_code == 200
    side = (
        f"This action changes `core_notes` and `scheduled_on` of {unsent['hrid']}, Protein "
        "Core's side of the request: a run in Protein Core changes them, not one in Kinase Atlas."
    )
    refused = problem(schedule(lab, "editor", lab.world.project, ref), 422, "validation_failed")
    assert refused["errors"] == [{"pointer": "#/inputs/request", "detail": side}]
    # On both sides, an Editor of each: still the side of the project the run is in.
    _member(lab.core, lab.users["editor"], ProjectRole.EDITOR)
    refused = problem(schedule(lab, "editor", lab.world.project, ref), 422, "validation_failed")
    assert refused["errors"] == [{"pointer": "#/inputs/request", "detail": side}]
    # Someone of the service alone can't launch in the asking project, nor see it.
    problem(schedule(lab, "tech", lab.world.project, ref), 404, "not_found")
    assert not ActionRun.objects.exists()

    for who in ("tech", "editor"):
        launched = schedule(lab, who, lab.core, ref)
        assert launched.status_code == 201, launched.content
    assert ActionRun.objects.filter(project=lab.core).count() == 2


def test_each_side_sets_its_own_states_with_its_own_lab_roles(lab: Lab) -> None:
    made = sent(lab)
    ref = made["id"]
    # The asking project's Analyst isn't the service's.
    problem(state(lab, "editor", ref, "status", "accepted"), 403, "forbidden")
    assert state(lab, "tech", ref, "status", "accepted").status_code == 200
    problem(state(lab, "tech", ref, "standing", "withdrawn", "No"), 403, "forbidden")
    body = state(lab, "editor", ref, "standing", "withdrawn", "Changed plans").json()
    assert body["values"]["standing"] == "withdrawn"
    offered = lab.api("tech").get(f"/entities/{ref}").json()["can"]["states"]["status"]
    assert {s["name"]: s["why_not"] for s in offered}["delivered"] == (
        "It needs someone with the QA role in Protein Core."
    )


def test_the_service_assigns_its_own_people(lab: Lab) -> None:
    made = sent(lab)
    ref = made["id"]
    tech = str(lab.users["tech"].pk)
    # The asking project's people aren't the service's to name.
    refused = problem(
        change(lab, "tech", ref, {"technician": str(lab.users["editor"].pk)}),
        422,
        "validation_failed",
    )
    assert refused["errors"][0]["pointer"] == "#/values/technician"
    assert change(lab, "tech", ref, {"technician": tech}).status_code == 200
    state(lab, "tech", ref, "status", "accepted")
    work = lab.api("tech").get("/me/work").json()["items"]
    assert [w["entity"]["hrid"] for w in work] == [made["hrid"]]


def test_withdrawn_requests_leave_work_and_cannot_be_taken(lab: Lab) -> None:
    assigned, available = sent(lab), sent(lab)
    technician = lab.api("tech")

    def work_ids(path: str) -> list[str]:
        listing = technician.get(path)
        assert listing.status_code == 200, listing.content
        return [item["entity"]["id"] for item in listing.json()["items"]]

    for ref in (assigned["id"], available["id"]):
        accepted = state(lab, "tech", ref, "status", "accepted")
        assert accepted.status_code == 200, accepted.content
    changed = change(lab, "tech", assigned["id"], {"technician": str(lab.users["tech"].pk)})
    assert changed.status_code == 200, changed.content
    assert work_ids("/me/work") == [assigned["id"]]
    assert work_ids("/me/work/open") == [available["id"]]

    for ref in (assigned["id"], available["id"]):
        stopped = state(lab, "editor", ref, "standing", "withdrawn", "No longer needed")
        assert stopped.status_code == 200, stopped.content
    assert work_ids("/me/work") == []
    assert work_ids("/me/work/open") == []

    ref = available["id"]
    observed = technician.get(f"/entities/{ref}")
    assert observed.status_code == 200, observed.content
    values = observed.json()["values"]
    assert values["status"] == "accepted" and values["standing"] == "withdrawn"
    assert values["technician"] is None
    assert observed.json()["can"]["take"] == []
    refused = problem(
        technician.post(
            f"/entities/{ref}/take",
            {"field": "technician"},
            headers={"If-Match": observed["ETag"]},
        ),
        409,
        "refused",
    )
    assert "withdrawn" in refused["detail"]
    assert technician.get(f"/entities/{ref}")["ETag"] == observed["ETag"]

    reopened = state(lab, "editor", ref, "standing", "open")
    assert reopened.status_code == 200, reopened.content
    assert work_ids("/me/work/open") == [ref]
    took = technician.post(
        f"/entities/{ref}/take",
        {"field": "technician"},
        headers={"If-Match": reopened["ETag"]},
    )
    assert took.status_code == 200, took.content
    assert work_ids("/me/work") == [ref]


def raw(statement: str, params: list[Any], *sides: str) -> None:
    """Runs SQL as a write of ``sides`` (none: the asking project's), as Iki's own would."""
    with transaction.atomic(), requests.writing(frozenset(sides)), connection.cursor() as cursor:
        cursor.execute(statement, params)


def test_only_the_migrator_can_rewrite_request_values_across_sides(
    lab: Lab, as_app_role: Callable[[str], None]
) -> None:
    sent(lab, sample(lab, "S-1"))
    table = next(g.table for g in requests.guarded_tables() if "core_notes" in g.service_columns)
    rewrite = f"UPDATE {table} SET core_notes = 'Migration notes'"  # noqa: S608 - fixture table
    with connection.schema_editor() as editor, unlocked(editor):
        with pytest.raises(DatabaseError, match="the service writes its fields"):
            as_app_role(rewrite)
        editor.execute(rewrite)


def test_the_database_holds_each_side_to_its_own_columns(lab: Lab) -> None:
    made = sent(lab, sample(lab, "S-1"))
    by_name = {g.table: g for g in requests.guarded_tables()}
    main = next(g for g in by_name.values() if "core_notes" in g.service_columns)
    form = shape(ASK)
    samples = by_name[form.part_of("samples").list_model("samples")._meta.db_table]
    assert samples.whole == requests.ASKER
    update = f'UPDATE "{main.table}" SET {{}} = %s WHERE entity_id = %s'  # noqa: S608
    with pytest.raises(DatabaseError, match="the service writes its fields"):
        raw(update.format("core_notes"), ["Sneaky", made["id"]])
    with pytest.raises(DatabaseError, match="writes only its fields"):
        raw(update.format("instructions"), ["Sneaky", made["id"]], requests.SERVICE)
    with pytest.raises(DatabaseError, match="this field is the asking project's"):
        raw(
            f'DELETE FROM "{samples.table}" WHERE entity_id = %s',  # noqa: S608
            [made["id"]],
            requests.SERVICE,
        )
    # Each writes its own, and someone on both sides writes both.
    raw(update.format("core_notes"), ["Fine", made["id"]], requests.SERVICE)
    raw(update.format("instructions"), ["Fine", made["id"]])
    raw(update.format("core_notes"), ["Both", made["id"]], requests.SERVICE, requests.ASKER)


# --- What it sends ------------------------------------------------------------------------------


def request_with_private_sample(
    lab: Lab,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    editor = lab.users["editor"]
    project = lab.world.project
    shared = services.create(editor, SAMPLE, project, {"label": "Sample for the service"}).entity
    private = services.create(
        editor, SAMPLE, project, {"label": "Sample the asking project keeps"}
    ).entity
    requests.set_intake(
        lab.core, types=[ASK], from_registry=True, sources=[], by=lab.users["core_admin"]
    )
    made = services.create(
        editor,
        ASK,
        project,
        {
            "title": "Purity of the kinases",
            "samples": [str(shared.pk)],
            "private_sample": str(private.pk),
            "comparison_samples": [str(shared.pk), str(private.pk)],
            "sample_notes": [{"sample": str(private.pk), "note": "Comparison"}],
        },
    ).entity
    assert isinstance(made, Entity)
    assert isinstance(shared, Entity)
    assert isinstance(private, Entity)
    requests.submit(editor, made, lab.core, expected_revision=made.revision, reason="Q4 batch")
    return (
        {"id": str(made.pk), "hrid": made.hrid},
        {"id": str(shared.pk), "hrid": shared.hrid},
        {"id": str(private.pk), "hrid": private.hrid},
    )


def test_stored_field_spellings_require_known_reference_types(lab: Lab) -> None:
    from iki_server.api.entities import _submissions
    from iki_server.entities.models import Revision
    from iki_server.runs import output_refs

    made, shared, private = request_with_private_sample(lab)
    revision = Revision.objects.get(entity=made["id"], number=1)
    revision.submitted = {
        "former_field": private["id"],
        "sample_notes": [{"former_cell": private["id"], "sample": shared["id"], "note": "Retest"}],
    }
    shown = _submissions(lab.world.users["editor"], [revision])[revision.pk]
    assert shown == {
        "former_field": "unreadable",
        "sample_notes": [{"former_cell": "unreadable", "sample": shared["id"], "note": "Retest"}],
    }
    for name, value in revision.submitted.items():
        with pytest.raises(output_refs.Unavailable):
            output_refs.dependencies({"creates": [{"type": ASK, "values": {name: value}}]})


@pytest.mark.parametrize("surface", ["detail", "list", "search", "history"])
def test_a_request_indicates_a_reference_the_service_cannot_read(lab: Lab, surface: str) -> None:
    made, shared, private = request_with_private_sample(lab)

    def read(api: Api) -> dict[str, Any]:
        path = f"/entities/{made['id']}"
        result: dict[str, Any]
        if surface == "history":
            result = api.get(f"{path}/revisions/1").json()
        elif surface == "search":
            result = api.post("/entities/search", {"type": ASK}).json()["items"][0]
        elif surface == "list":
            result = api.get(f"/entities?type={ASK}").json()["items"][0]
        else:
            result = api.get(path).json()
        return result

    service = lab.api("tech")
    problem(service.get(f"/entities/{private['id']}"), 404, "not_found")
    shown = read(service)
    assert shown["values"]["private_sample"] == "unreadable"
    assert shown["values"]["comparison_samples"] == [shared["id"], "unreadable"]
    assert shown["values"]["sample_notes"] == [{"sample": "unreadable", "note": "Comparison"}]
    assert set(shown["refs"]) == {shared["id"]}
    assert private["id"] not in json.dumps(shown) and private["hrid"] not in json.dumps(shown)
    if surface == "history":
        assert shown["changes"]["private_sample"] == {"before": None, "after": "unreadable"}
    own = read(lab.api("editor"))
    assert own["values"]["private_sample"] == private["id"]
    assert own["refs"][private["id"]]["hrid"] == private["hrid"]


@pytest.mark.parametrize("trail", [False, True])
def test_request_csv_names_only_samples_the_reader_can_read(lab: Lab, trail: bool) -> None:
    made, shared, private = request_with_private_sample(lab)
    path = (
        f"/entities/{made['id']}/revisions.csv"
        if trail
        else f"/entities/search.csv?type={ASK}&columns=private_sample&columns=comparison_samples"
    )
    for who, readable in [("tech", False), ("editor", True)]:
        response = lab.api(who).get(path)
        assert response.status_code == 200
        csv = response.getvalue().decode()
        if readable:
            assert private["hrid"] in csv
        else:
            assert "An entity you can’t read" in csv
            assert private["hrid"] not in csv
        assert shared["hrid"] in csv
        assert private["id"] not in csv


def test_what_a_request_names_later_goes_too_and_stays_while_named(lab: Lab) -> None:
    first, later = sample(lab, "S-1"), sample(lab, "S-2")
    made = sent(lab, first)
    tech = lab.api("tech")
    problem(tech.get(f"/entities/{later['id']}"), 404, "not_found")
    assert (
        change(lab, "editor", made["id"], {"samples": [first["id"], later["id"]]}).status_code
        == 200
    )
    assert tech.get(f"/entities/{later['id']}").status_code == 200

    admin = lab.api("core_admin")
    # The request itself stays in the service, and so does what it still names.
    problem(admin.delete(f"/entities/{made['id']}/projects/{lab.core.pk}"), 409, "refused")
    refused = problem(
        admin.delete(f"/entities/{first['id']}/projects/{lab.core.pk}"), 409, "refused"
    )
    assert "take it out of the request first" in refused["detail"]
    # Taken out of the request, it stays until the service takes it out too.
    assert change(lab, "editor", made["id"], {"samples": [later["id"]]}).status_code == 200
    assert tech.get(f"/entities/{first['id']}").status_code == 200
    answer = admin.delete(f"/entities/{first['id']}/projects/{lab.core.pk}")
    assert answer.status_code == 204, answer.content
    problem(tech.get(f"/entities/{first['id']}"), 404, "not_found")


def test_what_a_request_sent_goes_no_further(lab: Lab) -> None:
    first = sample(lab, "S-1")
    sent(lab, first)
    elsewhere = Project.objects.create(team=lab.world.team, name="Core Archive")
    _member(elsewhere, lab.users["tech"], ProjectRole.EDITOR)
    answer = lab.api("tech").put(f"/entities/{first['id']}/projects/{elsewhere.pk}")
    refused = problem(answer, 409, "refused")
    assert "through a request between projects" in refused["detail"]
    assert not access.shareable_into(lab.users["tech"], elsewhere).filter(pk=first["id"]).exists()


# --- Results ------------------------------------------------------------------------------------


def test_results_are_the_services_until_it_releases_them(lab: Lab) -> None:
    first = sample(lab, "S-1")
    made = sent(lab, first)
    # Only the service makes them.
    refused = problem(
        create(lab.api("editor"), lab.world.project, {"request": made["id"]}, RESULT),
        422,
        "validation_failed",
    )
    assert refused["errors"][0]["pointer"] == "#/values/request"
    found = result(lab, made["id"], sample=first["id"], purity=97.5)
    editor = lab.api("editor")
    problem(editor.get(f"/entities/{found['id']}"), 404, "not_found")
    assert editor.get(f"/entities/{made['id']}/request").json()["results"] == []
    assert [
        r["hrid"] for r in lab.api("tech").get(f"/entities/{made['id']}/request").json()["results"]
    ] == [found["hrid"]]

    assert state(lab, "core_qa", found["id"], "review", "reviewed").status_code == 200
    problem(editor.get(f"/entities/{found['id']}"), 404, "not_found")
    released = state(lab, "core_qa", found["id"], "review", "released")
    assert released.status_code == 200, released.content
    got = editor.get(f"/entities/{found['id']}").json()
    assert (got["values"]["purity"], got["locked"], got["can"]["edit"]) == (97.5, True, False)
    assert [r["hrid"] for r in editor.get(f"/entities/{made['id']}/request").json()["results"]] == [
        found["hrid"]
    ]
    event = AuditEvent.objects.get(action="request.result_released")
    assert event.project == lab.world.project
    assert event.data["request_hrid"] == made["hrid"]
    # It stays released, wherever its state goes.
    refused = problem(
        lab.api("admin").delete(f"/entities/{found['id']}/projects/{lab.world.project.pk}"),
        409,
        "refused",
    )
    assert "released" in refused["detail"]


@pytest.fixture
def signed_result(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Run the request fixture's review and release through SSO, as in the requests plugin."""
    field = states.state_fields(RESULT)["review"]
    for state_name, meaning in [("reviewed", "review"), ("released", "approval")]:
        target = field.states[state_name]
        signature = Signature.from_manifest({"meaning": meaning}, plugin="acme/protein")
        monkeypatch.setitem(field.states, state_name, replace(target, signatures=(signature,)))
    _member(lab.core, lab.users["admin"], ProjectRole.EDITOR, REVIEWER)
    ready(lab.world)
    request = sent(lab)
    found = result(lab, request["id"], purity=97.5)
    return request["id"], found["id"]


def test_qa_signs_the_reviewed_revision_and_the_requester_receives_it(
    lab: Lab, signed_result: tuple[str, str]
) -> None:
    request, ref = signed_result
    reviewed = signed(lab.world, "admin", ref, "reviewed", field="review").signature
    assert reviewed is not None
    qa = signed(lab.world, "core_qa", ref, "released", field="review").signature
    assert qa is not None
    review_document = json.loads(reviewed.document)
    assert json.loads(qa.document)["reviewed"] == {
        "revision": reviewed.revision,
        "content_sha256": review_document["record"]["content_sha256"],
        "signatures": [str(reviewed.pk)],
    }
    requester = lab.api("editor")
    detail = requester.get(f"/entities/{request}/request").json()
    [receipt] = detail["result_releases"]
    assert receipt == {
        "entity": ref,
        "reviewed_revision": reviewed.revision,
        "signed_revision": qa.revision,
        "released_revision": qa.revision + 1,
    }
    event = AuditEvent.objects.get(action="request.result_released", target_id=ref)
    assert {name: event.data[name] for name in receipt if name != "entity"} == {
        name: value for name, value in receipt.items() if name != "entity"
    }
    signed_values = json.loads(reviewed.content)["values"]
    for rev in (
        receipt["reviewed_revision"],
        receipt["signed_revision"],
        receipt["released_revision"],
    ):
        got = requester.get(f"/entities/{ref}/revisions/{rev}").json()["values"]
        assert got["purity"] == signed_values["purity"] == 97.5
        assert got["request"] == signed_values["request"] == request
    [delivered] = detail["results"]
    assert delivered["revision"] == receipt["released_revision"]
    assert delivered["values"] == requester.get(f"/entities/{ref}").json()["values"]
    assert delivered["values"]["purity"] == 97.5
    # This fixture allows a manual return to draft. The completed release freezes it.
    refused = problem(state(lab, "tech", ref, "review", "draft"), 409, "refused")
    assert "released" in refused["detail"]
    problem(change(lab, "tech", ref, {"purity": 80}), 409, "refused")
    assert requester.get(f"/entities/{ref}").json()["revision"] == receipt["released_revision"]
    options = lab.api("tech").get(f"/entities/{ref}").json()["can"]
    assert not options["edit"]
    assert not any(t["allowed"] for t in options["states"]["review"])
    assert not any(t["allowed"] for t in options["sign"])
    assert all(check.ok for signature in (reviewed, qa) for check in signatures.verify(signature))
    assert "review" in {check.name for check in signatures.verify(qa)}
    # Migrations and bundle imports cannot unlock it or append a later revision.
    for changed in [{"locked_by": []}, {"revision": receipt["released_revision"] + 1}]:
        with (
            pytest.raises(IntegrityError, match="iki_released_review_locked"),
            transaction.atomic(),
        ):
            Entity.objects.filter(pk=ref).update(**changed)


@pytest.mark.parametrize("source", ["review_history", "live_values"])
def test_release_refuses_another_measurement_in_the_review_transition(
    lab: Lab, signed_result: tuple[str, str], source: str
) -> None:
    _, ref = signed_result
    reviewed = signed(lab.world, "admin", ref, "reviewed", field="review").signature
    assert reviewed is not None
    underway = begin(lab.world, "core_qa", ref, "released", field="review")
    part = shape(RESULT).parts[0]
    with past_the_triggers():
        if source == "review_history":
            part.history._default_manager.filter(entity_id=ref, revision=underway.revision).update(
                purity=80
            )
        else:
            part.model._default_manager.filter(pk=ref).update(purity=80)
    # Both the original signature and its transition metadata are intact. The *content*
    # of the resulting revision must still agree before QA releases it.
    assert all(check.ok for check in signatures.verify(reviewed))
    assert signatures.finish(underway, evidence(lab.world, "core_qa")).status == Status.FAILED
    problem(lab.api("editor").get(f"/entities/{ref}"), 404, "not_found")


def test_a_previously_delivered_result_cannot_be_signed_as_a_fresh_release(
    lab: Lab, signed_result: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A previously unsigned release may be read throughout a plugin upgrade."""
    _, ref = signed_result
    field = states.state_fields(RESULT)["review"]
    with monkeypatch.context() as legacy:
        target = field.states["released"]
        legacy.setitem(field.states, "released", replace(target, signatures=()))
        assert state(lab, "core_qa", ref, "review", "released").status_code == 200
        assert state(lab, "tech", ref, "review", "draft").status_code == 200
    signed(lab.world, "admin", ref, "reviewed", field="review")
    with pytest.raises(Refused, match="already delivered"):
        begin(lab.world, "core_qa", ref, "released", field="review")
    assert AuditEvent.objects.filter(action="request.result_released", target_id=ref).count() == 1


def test_release_requires_a_fresh_review_after_any_intervening_revision(
    lab: Lab, signed_result: tuple[str, str]
) -> None:
    _, ref = signed_result
    with pytest.raises(Refused, match="review"):
        begin(lab.world, "core_qa", ref, "released", field="review")
    signed(lab.world, "admin", ref, "reviewed", field="review")
    underway = begin(lab.world, "core_qa", ref, "released", field="review")
    # Reviewed on this type permits edits. Even a change back to identical values is
    # another revision, and must go through review again.
    for purity in [80, 97.5]:
        assert change(lab, "tech", ref, {"purity": purity}).status_code == 200
    assert signatures.finish(underway, evidence(lab.world, "core_qa")).status == Status.FAILED
    with pytest.raises(Refused, match="review"):
        begin(lab.world, "core_qa", ref, "released", field="review")
    problem(lab.api("editor").get(f"/entities/{ref}"), 404, "not_found")
    LabRoleGrant.objects.create(
        membership=lab.core.memberships.get(user=lab.users["admin"]), role=ANALYST
    )
    permissions.forget(lab.users["admin"])
    signed(
        lab.world, "admin", ref, "draft", field="review", reason="Review the corrected measurement"
    )
    fresh = signed(lab.world, "admin", ref, "reviewed", field="review").signature
    qa = signed(lab.world, "core_qa", ref, "released", field="review").signature
    assert qa is not None and fresh is not None
    assert json.loads(qa.document)["reviewed"]["signatures"] == [str(fresh.pk)]


def told(kind: str) -> list[tuple[str, str, str]]:
    """Who was told of a request's notification (ADR 0142): each one's email, title and the
    project it's in."""
    from iki_server.notifications.models import Notification

    return [
        (n.recipient.email, n.title, n.project.name if n.project else "")
        for n in Notification.objects.filter(kind=f"{ASK}.{kind}")
        .select_related("recipient", "project")
        .order_by("recipient__email")
    ]


def test_a_result_is_shared_only_by_its_release(lab: Lab) -> None:
    """Someone in both projects, an Analyst in the core and an Editor of the project that
    asked, can't share a result before QA releases it: not with the project that asked,
    nor anywhere else. Released, it's audited, told and kept, and the core shares it on
    as any record of its own."""
    _member(lab.world.project, lab.users["tech"], ProjectRole.EDITOR)
    archive = Project.objects.create(team=lab.world.team, name="Core Archive")
    _member(archive, lab.users["tech"], ProjectRole.EDITOR)
    first = sample(lab, "S-1")
    made = sent(lab, first)
    found = result(lab, made["id"], sample=first["id"], purity=12.0)
    tech = lab.api("tech")

    for project in (lab.world.project, archive):
        refused = problem(
            tech.put(f"/entities/{found['id']}/projects/{project.pk}"), 409, "refused"
        )
        assert refused["detail"] == (
            f"{found['hrid']} can't be shared with {project.name}: it's a request's result its "
            "service hasn't released, and it's the service's alone until then. Releasing it "
            "shares it with the project that asked (ADR 0132)."
        )
        assert not access.shareable_into(lab.users["tech"], project).filter(pk=found["id"]).exists()
    problem(lab.api("viewer").get(f"/entities/{found['id']}"), 404, "not_found")
    # Nor can it go out as what another request sends.
    assert (
        not access.shareable_into(lab.users["tech"], lab.core)
        .exclude(owner_project=lab.core)
        .filter(pk=found["id"])
        .exists()
    )

    state(lab, "core_qa", found["id"], "review", "reviewed")
    assert state(lab, "core_qa", found["id"], "review", "released").status_code == 200
    assert lab.api("viewer").get(f"/entities/{found['id']}").json()["values"]["purity"] == 12.0
    assert AuditEvent.objects.filter(action="request.result_released").count() == 1
    assert [t[0] for t in told("results_released")] == ["editor@example.com"]
    refused = problem(
        lab.api("admin").delete(f"/entities/{found['id']}/projects/{lab.world.project.pk}"),
        409,
        "refused",
    )
    assert "released" in refused["detail"]
    # The project that asked reads it, and passes it on no further.
    elsewhere = Project.objects.create(team=lab.world.team, name="Kinase Follow-up")
    _member(elsewhere, lab.users["editor"], ProjectRole.EDITOR)
    refused = problem(
        lab.api("editor").put(f"/entities/{found['id']}/projects/{elsewhere.pk}"), 409, "refused"
    )
    assert "through a request between projects" in refused["detail"]
    # The core, whose record it is, shares it on now.
    linked = tech.put(f"/entities/{found['id']}/projects/{archive.pk}")
    assert linked.status_code == 201, linked.content


def test_a_result_already_in_the_project_that_asked_is_still_released(lab: Lab) -> None:
    """However it got there, its release is audited and told, once, and it stays."""
    first = sample(lab, "S-1")
    made = sent(lab, first)
    found = result(lab, made["id"], sample=first["id"], purity=97.5)
    ProjectLink.objects.create(
        entity_id=found["id"], project=lab.world.project, linked_by=lab.users["tech"]
    )
    state(lab, "core_qa", found["id"], "review", "reviewed")
    assert state(lab, "core_qa", found["id"], "review", "released").status_code == 200

    link = ProjectLink.objects.get(entity_id=found["id"], project=lab.world.project)
    assert str(link.via_request_id) == made["id"]
    event = AuditEvent.objects.get(action="request.result_released")
    assert (event.project, event.data["request_hrid"]) == (lab.world.project, made["hrid"])
    assert [t[0] for t in told("results_released")] == ["editor@example.com"]
    problem(
        lab.api("admin").delete(f"/entities/{found['id']}/projects/{lab.world.project.pk}"),
        409,
        "refused",
    )
    # Back to reviewed and released again, it was released already: told once.
    state(lab, "core_qa", found["id"], "review", "reviewed")
    state(lab, "core_qa", found["id"], "review", "released")
    assert AuditEvent.objects.filter(action="request.result_released").count() == 1


def test_a_request_tells_its_service_as_it_arrives(lab: Lab) -> None:
    """Declared by its type (ADR 0142), as GitHub tells a repository's watchers of a new
    issue: the core's QA, in the core's project, and nobody of the project asking."""
    made = sent(lab)
    assert told("received") == [
        ("core_qa@example.com", f"{made['hrid']} arrived: Purity of the kinases", "Protein Core")
    ]
    event = AuditEvent.objects.get(action="notification.sent", data__kind=f"{ASK}.received")
    assert event.project == lab.core
    assert [p["name"] for p in event.data["told"]] == ["Core Qa"]
    assert "revision" not in event.data
    # Told as anyone is of what a type declares: they can read it, in their inbox.
    inbox = lab.api("core_qa").get("/notifications").json()["items"]
    assert [(n["kind"], n["records"][0]["hrid"]) for n in inbox] == [
        (f"{ASK}.received", made["hrid"])
    ]


def test_the_project_asking_is_told_as_its_results_are_released(lab: Lab) -> None:
    """As GitHub tells an issue's author and subscribers it's closed: whoever made the
    request, and whoever watches it, once they can read the result, and not before."""
    first = sample(lab, "S-1")
    made = sent(lab, first)
    watch = lab.api("viewer").put(f"/entities/{made['id']}/watch", {"mode": "watching"})
    assert watch.status_code == 200, watch.content
    found = result(lab, made["id"], sample=first["id"], purity=97.5)
    assert state(lab, "core_qa", found["id"], "review", "reviewed").status_code == 200
    assert told("results_released") == []

    assert state(lab, "core_qa", found["id"], "review", "released").status_code == 200
    assert told("results_released") == [
        ("editor@example.com", f"{made['hrid']}: Its results are released", "Kinase Atlas"),
        ("viewer@example.com", f"{made['hrid']}: Its results are released", "Kinase Atlas"),
    ]
    event = AuditEvent.objects.get(action="notification.sent", data__kind=f"{ASK}.results_released")
    assert str(event.target_id) == made["id"]
    assert event.data["about"] == {"id": found["id"], "hrid": found["hrid"]}


def test_each_result_released_is_told_though_the_first_notice_is_unread(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second result released while the first's notice is still open, unread and not
    emailed updates that notice, as a new comment updates a GitHub thread: it names both
    results, and the person hears of it live. Its audit says the notice was updated."""
    from iki_server.realtime import events

    first = sample(lab, "S-1")
    made = sent(lab, first)
    one = result(lab, made["id"], sample=first["id"], purity=97.5)
    two = result(lab, made["id"], sample=first["id"], purity=96.0)
    for found in (one, two):
        assert state(lab, "core_qa", found["id"], "review", "reviewed").status_code == 200
    assert state(lab, "core_qa", one["id"], "review", "released").status_code == 200
    published: list[tuple[str, str]] = []
    real = events.publish

    def publish(user: Any, kind: str, data: Any) -> None:
        published.append((str(user), kind))
        real(user, kind, data)

    monkeypatch.setattr(events, "publish", publish)
    assert state(lab, "core_qa", two["id"], "review", "released").status_code == 200

    editor = lab.users["editor"]
    assert [t[0] for t in told("results_released")] == ["editor@example.com"]  # one notice
    inbox = lab.api("editor").get("/notifications").json()["items"]
    [note] = [n for n in inbox if n["kind"] == f"{ASK}.results_released"]
    assert note["count"] == 1  # about one request
    assert [r["hrid"] for r in note["records"]] == [made["hrid"], one["hrid"], two["hrid"]]
    assert (str(editor.pk), "notification") in published
    sent_events = AuditEvent.objects.filter(
        action="notification.sent", data__kind=f"{ASK}.results_released"
    ).order_by("id")
    assert [e.data["about"]["hrid"] for e in sent_events] == [one["hrid"], two["hrid"]]
    assert [[p["notice"] for p in e.data["told"]] for e in sent_events] == [["new"], ["updated"]]


def test_a_service_that_doesnt_share_cant_release(lab: Lab) -> None:
    made = sent(lab)
    found = result(lab, made["id"])
    Project.objects.filter(pk=lab.core.pk).update(allow_sharing=False)
    state(lab, "core_qa", found["id"], "review", "reviewed")
    refused = problem(state(lab, "core_qa", found["id"], "review", "released"), 409, "refused")
    assert "doesn’t allow sharing" in refused["detail"]
    assert Entity.objects.get(pk=found["id"]).locked_by == []


# --- Files --------------------------------------------------------------------------------------


def a_file(project: Project, name: str) -> File:
    digest = hashlib.sha256(name.encode()).hexdigest()
    blob = clean_blob(sha256=digest, size=10, storage_uri=f"s3://iki/{name}")
    return File.objects.create(blob=blob, name=name, media_type="application/pdf", project=project)


CAME_WITH_A_REQUEST = (
    "refers to a file you read through a request between projects: it's read with the "
    "records it came on, and nothing else refers to it."
)


def test_a_file_that_came_with_a_request_is_read_with_it_alone(lab: Lab) -> None:
    """The asking project's form goes with its request: the core reads it there, and refers
    to it from nothing else, in its own project or another. The project whose file it is
    still does, as before."""
    form = a_file(lab.world.project, "form.pdf")
    takes(lab)
    made = ask(lab, form=str(form.pk))
    assert submit(lab, made["id"]).status_code == 200
    tech = lab.api("tech")
    assert tech.get(f"/files/{form.pk}").status_code == 200

    archive = Project.objects.create(team=lab.world.team, name="Core Archive")
    _member(archive, lab.users["tech"], ProjectRole.EDITOR)
    archivist = User.objects.create_user("archivist@example.com", "Archivist")
    lab.users["archivist"] = archivist
    _member(archive, archivist, ProjectRole.VIEWER)
    refused = problem(
        create(tech, lab.core, {"request": made["id"], "report": str(form.pk)}, RESULT),
        422,
        "validation_failed",
    )
    assert refused["errors"] == [{"pointer": "#/values/report", "detail": CAME_WITH_A_REQUEST}]
    refused = problem(
        create(tech, archive, {"label": "L-1", "report": str(form.pk)}, LOT),
        422,
        "validation_failed",
    )
    assert refused["errors"] == [{"pointer": "#/values/report", "detail": CAME_WITH_A_REQUEST}]
    problem(lab.api("archivist").get(f"/files/{form.pk}"), 404, "not_found")
    assert not access.shareable_files(lab.users["tech"], archive).filter(pk=form.pk).exists()

    # The project it's from refers to it again, here or in another project of its own.
    again = create(
        lab.api("editor"), lab.world.project, {"label": "L-1", "report": str(form.pk)}, LOT
    )
    assert again.status_code == 201, again.content
    elsewhere = Project.objects.create(team=lab.world.team, name="Kinase Follow-up")
    _member(elsewhere, lab.users["editor"], ProjectRole.EDITOR)
    there = create(lab.api("editor"), elsewhere, {"label": "L-2", "report": str(form.pk)}, LOT)
    assert there.status_code == 201, there.content
    # Someone in both projects refers to it as its own project's member.
    _member(lab.world.project, lab.users["tech"], ProjectRole.VIEWER)
    mine = create(tech, archive, {"label": "L-3", "report": str(form.pk)}, LOT)
    assert mine.status_code == 201, mine.content


def test_a_released_results_file_is_read_with_it_alone(lab: Lab) -> None:
    """The core's report comes back with its result: the project that asked reads it there,
    and refers to it from nothing else. The core, whose file it is, still does."""
    first = sample(lab, "S-1")
    made = sent(lab, first)
    report = a_file(lab.core, "report.pdf")
    found = result(lab, made["id"], sample=first["id"], purity=97.5, report=str(report.pk))
    editor = lab.api("editor")
    problem(editor.get(f"/files/{report.pk}"), 404, "not_found")
    state(lab, "core_qa", found["id"], "review", "reviewed")
    state(lab, "core_qa", found["id"], "review", "released")
    assert editor.get(f"/files/{report.pk}").status_code == 200

    refused = problem(
        create(editor, lab.world.project, {"label": "L-1", "report": str(report.pk)}, LOT),
        422,
        "validation_failed",
    )
    assert refused["errors"] == [{"pointer": "#/values/report", "detail": CAME_WITH_A_REQUEST}]
    archive = Project.objects.create(team=lab.world.team, name="Core Archive")
    _member(archive, lab.users["tech"], ProjectRole.EDITOR)
    own = create(lab.api("tech"), archive, {"label": "L-2", "report": str(report.pk)}, LOT)
    assert own.status_code == 201, own.content


# --- The queue ----------------------------------------------------------------------------------


def test_the_service_finds_its_queue(lab: Lab) -> None:
    made = sent(lab)
    ask(lab)  # Not sent: not the service's.
    found = lab.api("tech").post(
        "/entities/search",
        {"type": ASK, "where": {"field": "sent_to", "op": "eq", "value": str(lab.core.pk)}},
    )
    assert found.status_code == 200, found.content
    assert [e["hrid"] for e in found.json()["items"]] == [made["hrid"]]
    asking = lab.api("editor").post(
        "/entities/search",
        {"type": ASK, "where": {"field": "sent_to", "op": "is_set", "value": False}},
    )
    assert asking.status_code == 200, asking.content
    assert len(asking.json()["items"]) == 1
    listed = lab.api("tech").get("/entities", project=str(lab.core.pk), type=ASK).json()
    assert [e["hrid"] for e in listed["items"]] == [made["hrid"]]


# --- Who reads what -----------------------------------------------------------------------------


def test_each_side_reads_only_what_the_request_gives_it(lab: Lab) -> None:
    """Fuzzed both ways (ADR 0101): of every record either project holds, the service's
    people read only the request and what it sent, and the asking project's only the
    results released back."""
    sent_samples = [sample(lab, f"S-{i}") for i in range(3)]
    unsent = [sample(lab, f"U-{i}") for i in range(3)]
    made = sent(lab, *sent_samples[:2])
    change(lab, "editor", made["id"], {"samples": [s["id"] for s in sent_samples]})
    other = ask(lab)
    results = [result(lab, made["id"], purity=float(i)) for i in range(3)]
    for r in results[:2]:
        state(lab, "core_qa", r["id"], "review", "reviewed")
    state(lab, "core_qa", results[0]["id"], "review", "released")
    own = create(lab.api("tech"), lab.core, {"label": "Core control"}, SAMPLE).json()

    every = [*sent_samples, *unsent, made, other, *results, own]
    service_reads = {
        made["id"],
        *(s["id"] for s in sent_samples),
        *(r["id"] for r in results),
        own["id"],
    }
    asker_reads = {
        made["id"],
        other["id"],
        *(s["id"] for s in sent_samples + unsent),
        results[0]["id"],
    }
    for who, expected in (
        ("tech", service_reads),
        ("core_viewer", service_reads),
        ("viewer", asker_reads),
    ):
        api = lab.api(who)
        user = lab.users[who]
        for record in every:
            status = api.get(f"/entities/{record['id']}").status_code
            assert status == (200 if record["id"] in expected else 404), (who, record["hrid"])
        listed = {str(i) for i in access.visible(user).values_list("pk", flat=True)}
        assert listed & {r["id"] for r in every} == expected, who
        searched = api.post("/entities/search", {"limit": 100}).json()["items"]
        assert {e["id"] for e in searched} == expected, who
        # Nothing a response carries names what they can't read.
        two_sides = api.get(f"/entities/{made['id']}/request").json()
        named = {e["id"] for e in two_sides["sent"] + two_sides["results"]}
        assert named <= expected, who
        refs = set(api.get(f"/entities/{made['id']}").json()["refs"])
        assert refs <= expected, who
