"""Requests between projects (ADR 0132): a project asks another's service for work, sending
the records it's about; the service works it in its own fields and states, and releases its
results back. Each side reads only what the request gives it, and writes only its own."""

from dataclasses import dataclass
from typing import Any

import pytest
from api_client import Api, problem
from django.db import DatabaseError, connection, transaction
from org_world import World, build_world
from test_api_entities import create, patch

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import access, requests
from iki_server.entities.models import Entity, ProjectLink, Request
from iki_server.org import permissions
from iki_server.org.models import (
    LabRoleGrant,
    Project,
    ProjectMembership,
    ProjectRole,
    Registry,
)

pytestmark = pytest.mark.django_db

ASK = "acme/protein.AssayRequest"
RESULT = "acme/protein.AssayResult"
SAMPLE = "acme/protein.Sample"
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
    world = build_world()
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
        "sends": ["samples", "protein"],
    }
    assert asked["result"] is None
    found = api.get(f"/entity-types/{RESULT}").json()
    assert found["result"] == {
        "of": "request",
        "released_field": "review",
        "released_states": ["released"],
    }
    assert api.get(f"/entity-types/{SAMPLE}").json()["request"] is None


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
        "sends": ["samples", "protein"],
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


def raw(statement: str, params: list[Any], *sides: str) -> None:
    """Runs SQL as a write of ``sides`` (none: the asking project's), as Iki's own would."""
    with transaction.atomic(), requests.writing(frozenset(sides)), connection.cursor() as cursor:
        cursor.execute(statement, params)


def test_the_database_holds_each_side_to_its_own_columns(lab: Lab) -> None:
    made = sent(lab, sample(lab, "S-1"))
    by_name = {g.table: g for g in requests.guarded_tables()}
    main = next(g for g in by_name.values() if "core_notes" in g.service_columns)
    samples = next(g for g in by_name.values() if g.whole == requests.ASKER)
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


def test_a_service_that_doesnt_share_cant_release(lab: Lab) -> None:
    made = sent(lab)
    found = result(lab, made["id"])
    Project.objects.filter(pk=lab.core.pk).update(allow_sharing=False)
    state(lab, "core_qa", found["id"], "review", "reviewed")
    refused = problem(state(lab, "core_qa", found["id"], "review", "released"), 409, "refused")
    assert "doesn’t allow sharing" in refused["detail"]
    assert Entity.objects.get(pk=found["id"]).locked_by == []


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
