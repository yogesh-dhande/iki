"""People in records (ADR 0122): fields naming people, assigning records to them, and each
person's work. Only someone in a record's project (holding the field's lab role) can be
named; someone who leaves drops off their work at once, and the record needs reassigning."""

import json
from typing import Any

import pytest
from api_client import Api, problem
from django.db import IntegrityError, connection, transaction
from org_world import World, build_world
from test_api_entities import api_as, create, patch
from test_lab_roles import give, membership
from test_runs import world
from test_states import ANALYST, QA, REVIEWER, SAMPLE, imported_values, roles, set_
from test_workers import keep_connection

from iki_server.accounts import service_accounts
from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import assignments, people
from iki_server.entities.models import Entity
from iki_server.notifications.models import Notification
from iki_server.org import permissions
from iki_server.org import services as org
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers and test_runs


def sample(world: World, values: dict[str, Any] | None = None, label: str = "S-1") -> Any:
    return create(
        api_as(world, "editor"), world.project, {"label": label, **(values or {})}, SAMPLE
    )


def made(world: World, values: dict[str, Any] | None = None, label: str = "S-1") -> dict[str, Any]:
    answer = sample(world, values, label)
    assert answer.status_code == 201, answer.content
    body: dict[str, Any] = answer.json()
    return body


def pk(world: World, principal: str) -> str:
    return str(world.users[principal].pk)


def edit(world: World, ref: str, values: dict[str, Any], who: str = "editor") -> Any:
    api = api_as(world, who)
    etag = api.get(f"/entities/{ref}")["ETag"]
    return patch(api, ref, {"values": values}, etag)


def work(world: World, who: str, query: str = "", open_: bool = False) -> list[dict[str, Any]]:
    path = "/me/work/open" if open_ else "/me/work"
    answer = api_as(world, who).get(f"{path}{query}")
    assert answer.status_code == 200, answer.content
    items: list[dict[str, Any]] = answer.json()["items"]
    return items


def hrids(items: list[dict[str, Any]]) -> list[str]:
    return [i["entity"]["hrid"] for i in items]


def told(user: User, kind: str) -> list[Notification]:
    return list(Notification.objects.filter(recipient=user, kind=kind).order_by("created_at"))


# --- The type -----------------------------------------------------------------------------------


def test_a_type_says_which_fields_name_people(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{SAMPLE}").json()
    fields = {f["name"]: f for f in found["fields"]}
    assert {k: fields["analyst"][k] for k in ("type", "role", "role_label", "assignment")} == {
        "type": "person",
        "role": ANALYST,
        "role_label": "Analyst",
        "assignment": True,
    }
    assert (fields["analyst"]["assignment_field"], fields["analyst"]["assignment_states"]) == (
        "status",
        ["received", "in_testing"],
    )
    assert fields["helpers"]["assignment_field"] is None
    assert (fields["helpers"]["items"], fields["helpers"]["assignment"]) == ("person", True)
    assert (fields["received_by"]["role"], fields["received_by"]["assignment"]) == (None, False)
    assert found["due"] == "due_on"
    schema = found["values_schema"]["properties"]["analyst"]
    assert schema["anyOf"][0] == {"type": "string", "x-iki-person": True, "x-iki-role": ANALYST}


# --- Naming people ------------------------------------------------------------------------------


def test_only_someone_in_the_project_with_the_role_can_be_named(world: World) -> None:
    # The viewer reads the project, but doesn't hold the analyst role yet.
    refused = problem(sample(world, {"analyst": pk(world, "viewer")}), 422, "validation_failed")
    assert refused["errors"] == [
        {
            "pointer": "#/values/analyst",
            "detail": "must name someone with the Analyst role in Kinase Atlas: Viewer "
            "doesn't have it.",
        }
    ]
    # Someone outside it gets the same answer as nobody at all: who exists isn't told.
    generic = "isn't someone who can be named here: name an active person in Kinase Atlas "
    generic += "with the Analyst role."
    for who in (pk(world, "outsider"), "outsider@example.com", "nobody@example.com", "x"):
        refused = problem(sample(world, {"analyst": who}), 422, "validation_failed")
        assert refused["errors"][0]["detail"] == generic, who
    # Nor someone deactivated, though they had a role there.
    roles(world, viewer=(ANALYST,))
    for who in ("inactive", "team_member"):
        refused = problem(sample(world, {"received_by": pk(world, who)}), 422, "validation_failed")
        assert refused["errors"][0]["detail"].startswith("isn't someone who can be named here")

    # With the role, by ID or email address.
    body = made(world, {"analyst": "Viewer@Example.com", "received_by": pk(world, "via_team")})
    assert body["values"]["analyst"] == pk(world, "viewer")
    assert body["people"][pk(world, "viewer")] == {
        "id": pk(world, "viewer"),
        "name": "Viewer",
        "email": "viewer@example.com",
        "active": True,
    }
    # Someone in it by a team can be named.
    assert body["values"]["received_by"] == pk(world, "via_team")


def test_a_service_account_cant_be_named(world: World) -> None:
    bot = service_accounts.create("LIMS sync", "", by=world.users["org_admin"])
    ProjectMembership.objects.create(project=world.project, user=bot, role=ProjectRole.EDITOR)
    refused = problem(sample(world, {"received_by": str(bot.pk)}), 422, "validation_failed")
    assert refused["errors"][0]["detail"].startswith("isn't someone who can be named here")


def test_a_list_names_several_once_each(world: World) -> None:
    body = made(world, {"helpers": [pk(world, "viewer"), pk(world, "runner")]})
    assert body["values"]["helpers"] == [pk(world, "viewer"), pk(world, "runner")]
    assert set(body["people"]) == {pk(world, "viewer"), pk(world, "runner")}
    twice = sample(world, {"helpers": [pk(world, "viewer"), "viewer@example.com"]}, "S-2")
    assert problem(twice, 422, "validation_failed")["errors"][0]["detail"] == (
        "names someone twice."
    )


def test_the_picker_lists_whom_a_field_can_name(world: World) -> None:
    roles(world, viewer=(ANALYST,), editor=(ANALYST, REVIEWER))
    api = api_as(world, "editor")
    path = f"/projects/{world.project.pk}/people"
    everyone = [p["name"] for p in api.get(path).json()]
    # Active people who read it, by their own grant or a team's: no one deactivated.
    assert everyone == ["Admin", "Editor", "Mixed", "Runner", "Via Team", "Viewer"]
    analysts = api.get(f"{path}?type={SAMPLE}&field=analyst").json()
    assert [p["name"] for p in analysts] == ["Editor", "Viewer"]
    assert [p["name"] for p in api.get(f"{path}?role={REVIEWER}").json()] == ["Editor"]
    assert [p["email"] for p in api.get(f"{path}?q=vie").json()] == ["viewer@example.com"]
    # Only for those who read the project: its people aren't anyone else's to list.
    problem(api_as(world, "outsider").get(path), 404, "not_found")
    problem(api.get(f"{path}?role=acme/protein.nobody"), 404, "not_found")


# --- Assigning ------------------------------------------------------------------------------------


def test_assigning_is_audited_and_told(world: World) -> None:
    roles(world, viewer=(ANALYST,), runner=(ANALYST,))
    body = made(world, {"analyst": pk(world, "viewer")})
    (event,) = AuditEvent.objects.filter(action="entity.assigned", target_id=body["id"])
    assert event.actor_label == "editor@example.com"
    assert event.data == {
        "field": "analyst",
        "label": "Analyst",
        "added": [{"id": pk(world, "viewer"), "name": "Viewer"}],
        "removed": [],
        "revision": 1,
        "reason": "",
    }
    (note,) = told(world.users["viewer"], "assignment.assigned")
    assert note.title == f"Editor assigned you {body['hrid']} as Analyst"
    assert note.path == f"/entities/{body['id']}"

    # Reassigning tells both, and the revision has it.
    assert edit(world, body["id"], {"analyst": pk(world, "runner")}).status_code == 200
    assert len(told(world.users["runner"], "assignment.assigned")) == 1
    (off,) = told(world.users["viewer"], "assignment.unassigned")
    assert off.title == f"Editor took you off {body['hrid']} as Analyst"
    latest = api_as(world, "viewer").get(f"/entities/{body['id']}/revisions/2").json()
    assert latest["changes"]["analyst"] == {
        "before": pk(world, "viewer"),
        "after": pk(world, "runner"),
    }
    assert set(latest["people"]) == {pk(world, "viewer"), pk(world, "runner")}
    # A field that isn't an assignment field audits nothing more.
    edit(world, body["id"], {"received_by": pk(world, "viewer")})
    assert AuditEvent.objects.filter(action="entity.assigned", target_id=body["id"]).count() == 2
    # Nobody is told of their own doing.
    roles(world, editor=(ANALYST,))
    edit(world, body["id"], {"analyst": pk(world, "editor")})
    assert told(world.users["editor"], "assignment.assigned") == []


def test_my_work_follows_the_records_state_field(world: World) -> None:
    roles(world, viewer=(ANALYST,), runner=(REVIEWER,), admin=(QA,))
    first = made(world, {"analyst": pk(world, "viewer"), "due_on": "2026-12-01"}, "S-1")
    second = made(world, {"analyst": pk(world, "viewer"), "due_on": "2026-11-20"}, "S-2")
    undated = made(world, {"analyst": pk(world, "viewer")}, "S-3")
    made(world, {}, "S-4")
    mine = work(world, "viewer")
    # Soonest due first, undated last.
    assert hrids(mine) == [second["hrid"], first["hrid"], undated["hrid"]]
    assert (mine[0]["fields"], mine[0]["due"]) == (["analyst"], "2026-11-20")
    assert hrids(work(world, "viewer", "?sort=updated")) == [
        undated["hrid"],
        second["hrid"],
        first["hrid"],
    ]
    assert hrids(work(world, "viewer", "?limit=1")) == [second["hrid"]]
    page = api_as(world, "viewer").get("/me/work?limit=2").json()
    rest = api_as(world, "viewer").get(f"/me/work?limit=2&cursor={page['next_cursor']}").json()
    assert hrids(page["items"] + rest["items"]) == hrids(mine)
    assert rest["next_cursor"] is None
    assert hrids(work(world, "viewer", "?state=status.in_review")) == []
    # A state is named with its field: a bare one isn't a state field's.
    bare = problem(
        api_as(world, "viewer").get("/me/work?state=in_review"), 422, "validation_failed"
    )
    assert "`in_review` isn't a state field's state" in bare["errors"][0]["detail"]
    assert hrids(work(world, "runner")) == []

    # Once its status is In review, it's its reviewer's work, no longer its analyst's.
    edit(world, first["id"], {"reviewer": pk(world, "runner")})
    set_(world, "viewer", first["id"], "status", "in_testing")
    set_(world, "viewer", first["id"], "status", "in_review")
    assert hrids(work(world, "runner")) == [first["hrid"]]
    assert first["hrid"] not in hrids(work(world, "viewer"))
    assert hrids(work(world, "runner", "?state=status.in_review")) == [first["hrid"]]
    assert hrids(work(world, "runner", "?state=status.received")) == []
    # Several of a field's states are any of them; several fields, all of them.
    both = "?state=status.received&state=status.in_review"
    assert hrids(work(world, "runner", both)) == [first["hrid"]]
    assert hrids(work(world, "runner", both + "&state=custody.in_lab")) == [first["hrid"]]
    assert hrids(work(world, "runner", both + "&state=custody.quarantined")) == []
    # Its other state field doesn't change whose work it is.
    set_(world, "admin", first["id"], "custody", "disposed", "Used up")
    assert hrids(work(world, "runner")) == [first["hrid"]]
    # A type without the state field named has none.
    assert hrids(work(world, "runner", "?state=stage.in_review")) == []


def test_whoever_is_named_is_told_when_its_their_turn(world: World) -> None:
    roles(world, viewer=(ANALYST,), runner=(REVIEWER,))
    body = made(world, {"reviewer": pk(world, "runner")})
    # Named before its status is In review: not theirs yet, so they aren't told yet.
    assert told(world.users["runner"], "assignment.assigned") == []
    assert hrids(work(world, "runner")) == []
    set_(world, "viewer", body["id"], "status", "in_testing")
    set_(world, "viewer", body["id"], "status", "in_review")
    (ready,) = told(world.users["runner"], "assignment.ready")
    assert ready.title == f"{body['hrid']} is ready for you, as Reviewer"
    assert hrids(work(world, "runner")) == [body["hrid"]]
    # Set again to a state it's theirs in, or its other state field changing: told once.
    roles(world, viewer=(ANALYST, QA))
    set_(world, "viewer", body["id"], "custody", "quarantined", "Leaking")
    set_(world, "viewer", body["id"], "custody", "in_lab")
    assert len(told(world.users["runner"], "assignment.ready")) == 1


def test_a_locked_records_people_are_locked_with_it(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER, QA), viewer=(ANALYST, QA))
    body = made(world)
    set_(world, "editor", body["id"], "status", "in_testing")
    set_(world, "editor", body["id"], "status", "in_review")
    set_(world, "admin", body["id"], "status", "reviewed")
    refused = problem(edit(world, body["id"], {"analyst": pk(world, "viewer")}), 409, "refused")
    assert "is locked by its Status (Reviewed)" in refused["detail"]
    bulk = api_as(world, "admin").post(
        "/entities/assign",
        {"entities": [body["id"]], "field": "analyst", "person": pk(world, "viewer")},
    )
    assert bulk.json()["changed"] == []
    (skipped,) = bulk.json()["skipped"]
    assert "Reviewed" in skipped["why"]
    # The database refuses too, for a list naming people as for its other values.
    table = assignments.of(SAMPLE)[0].part.list_model("helpers")._meta.db_table
    with (
        pytest.raises(IntegrityError, match="is locked by its status"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            f"INSERT INTO {table} (entity_id, position, item_id) VALUES (%s, 0, %s)",  # noqa: S608 - ours
            [body["id"], pk(world, "viewer")],
        )
    # Out of the locking state, it can be reassigned again.
    set_(world, "viewer", body["id"], "status", "released")
    set_(world, "viewer", body["id"], "status", "in_testing", "Wrong lot")
    changed = edit(world, body["id"], {"analyst": pk(world, "viewer")})
    assert changed.status_code == 200, changed.content
    event = AuditEvent.objects.filter(action="entity.assigned", target_id=body["id"]).first()
    assert event is not None
    assert event.data["revision"] == changed.json()["revision"]


# --- Leaving ------------------------------------------------------------------------------------


def test_someone_who_leaves_drops_off_their_work(world: World) -> None:
    roles(world, viewer=(ANALYST,), runner=(ANALYST,))
    body = made(world, {"analyst": pk(world, "viewer"), "notes": "x"})
    assert hrids(work(world, "viewer")) == [body["hrid"]]
    TaskRecord.objects.all().delete()

    org.set_active(world.users["viewer"], False, by=world.users["org_admin"])
    # Its value stays, for the record; they're off its work, and it needs reassigning.
    read = api_as(world, "editor").get(f"/entities/{body['id']}").json()
    assert read["values"]["analyst"] == pk(world, "viewer")
    assert read["people"][pk(world, "viewer")]["active"] is False
    assert read["needs_reassigning"] == [
        {"field": "analyst", "person": pk(world, "viewer"), "why": "deactivated"}
    ]
    found = api_as(world, "editor").post(
        "/entities/search",
        {"type": SAMPLE, "where": {"field": "needs_reassigning", "op": "eq", "value": True}},
    )
    assert [e["hrid"] for e in found.json()["items"]] == [body["hrid"]]
    viewer_record = User.objects.get(pk=world.users["viewer"].pk)
    assert assignments.work(viewer_record, assignments.WorkFilters(), None, 10)[0] == []

    # The project's Admins are told, once the change is made.
    (task,) = TaskRecord.objects.filter(task_path__endswith="check_people")
    assert task.args == [[pk(world, "viewer")]]
    Runner(queues=["default"]).run_ready()
    (note,) = told(world.users["admin"], "assignment.needs_reassigning")
    assert note.title == "1 record in Kinase Atlas needs reassigning"
    assert note.body == "Viewer is assigned it, and can't be any more: deactivated."
    assert note.path == "/projects/kinase-atlas/entities?q=needs%3Areassigning"

    # Changing something else keeps them named; naming someone else reassigns it.
    assert edit(world, body["id"], {"notes": "y"}).status_code == 200
    again = edit(world, body["id"], {"analyst": pk(world, "runner")})
    assert again.status_code == 200, again.content
    assert again.json()["needs_reassigning"] == []
    # Once off it, they can't be named back.
    refused = edit(world, body["id"], {"analyst": pk(world, "viewer")})
    problem(refused, 422, "validation_failed")


def test_losing_the_role_or_the_project_needs_reassigning(world: World) -> None:
    roles(world, viewer=(ANALYST,), editor=(ANALYST,))
    body = made(world, {"analyst": pk(world, "viewer"), "helpers": [pk(world, "runner")]})
    answer = give(world, membership(world, "viewer"))
    assert answer.status_code == 200
    permissions.forget(world.users["viewer"])
    read = api_as(world, "editor").get(f"/entities/{body['id']}").json()
    assert read["needs_reassigning"] == [
        {
            "field": "analyst",
            "person": pk(world, "viewer"),
            "why": "no longer Analyst in Kinase Atlas",
        }
    ]
    org.remove_project_membership(membership(world, "runner"), by=world.users["admin"])
    read = api_as(world, "editor").get(f"/entities/{body['id']}").json()
    assert {(s["field"], s["why"]) for s in read["needs_reassigning"]} == {
        ("analyst", "no longer Analyst in Kinase Atlas"),
        ("helpers", "no longer in Kinase Atlas"),
    }
    # Open to the analysts again: one of them takes it.
    assert hrids(work(world, "editor", open_=True)) == [body["hrid"]]
    assert read["can"]["take"] == ["analyst"]
    taken = api_as(world, "editor").post(
        f"/entities/{body['id']}/take",
        {"field": "analyst"},
        headers={"If-Match": f'"{read["revision"]}"'},
    )
    assert taken.status_code == 200, taken.content
    assert taken.json()["values"]["analyst"] == pk(world, "editor")
    assert hrids(work(world, "editor", open_=True)) == []
    assert hrids(work(world, "editor")) == [body["hrid"]]
    event = AuditEvent.objects.filter(action="entity.assigned", target_id=body["id"]).first()
    assert event is not None
    assert event.data["took"] is True


def test_only_a_role_holder_takes_open_work(world: World) -> None:
    roles(world, viewer=(ANALYST,))
    body = made(world)
    api = api_as(world, "editor")
    headers = {"If-Match": f'"{body["revision"]}"'}
    # The editor may edit it, but taking is lab-role work.
    assert body["can"]["take"] == []
    answer = api.post(f"/entities/{body['id']}/take", {"field": "analyst"}, headers=headers)
    problem(answer, 403, "forbidden")
    viewer = api_as(world, "viewer")
    # Not helpers: a field no lab role limits is an Editor's to fill in, not open work.
    assert viewer.get(f"/entities/{body['id']}").json()["can"]["take"] == ["analyst"]
    # Fields without a role an Editor assigns.
    answer = viewer.post(f"/entities/{body['id']}/take", {"field": "helpers"}, headers=headers)
    assert (
        problem(answer, 403, "forbidden")["detail"]
        == "Helpers isn't limited to a lab role: an Editor assigns it."
    )
    # A reviewer's isn't open while the sample is received: it isn't theirs to work on yet.
    roles(world, viewer=(ANALYST, REVIEWER))
    answer = viewer.post(f"/entities/{body['id']}/take", {"field": "reviewer"}, headers=headers)
    problem(answer, 409, "refused")
    taken = viewer.post(f"/entities/{body['id']}/take", {"field": "analyst"}, headers=headers)
    assert taken.status_code == 200, taken.content
    # Then it's someone's, and stays theirs.
    roles(world, editor=(ANALYST,))
    again = api.post(
        f"/entities/{body['id']}/take", {"field": "analyst"}, headers={"If-Match": '"2"'}
    )
    assert problem(again, 409, "refused")["detail"].endswith("someone has it.")


def _open_work_queries(world: World, projects: int) -> int:
    """How many queries the viewer's open work takes, once they're in ``projects`` more
    projects, holding the analyst's lab role in each by their own grant and their team's."""
    from django.test.utils import CaptureQueriesContext

    from iki_server.org.models import LabRoleGrant

    team = world.users["viewer"].team_memberships.first()
    for n in range(projects):
        p = Project.objects.create(team=world.team, name=f"Lab {Project.objects.count()}-{n}")
        mine = ProjectMembership.objects.create(
            project=p, user=world.users["viewer"], role=ProjectRole.VIEWER
        )
        LabRoleGrant.objects.create(membership=mine, role=ANALYST)
        if team is not None:
            theirs = ProjectMembership.objects.create(
                project=p, team=team.team, role=ProjectRole.VIEWER
            )
            LabRoleGrant.objects.create(membership=theirs, role=ANALYST)
    with CaptureQueriesContext(connection) as queries:
        found = work(world, "viewer", open_=True)
    assert len(found) == 1
    return len(queries)


def test_open_work_takes_as_many_queries_however_many_projects_its_reader_is_in(
    world: World,
) -> None:
    """Their lab roles in every project they read are looked up at once."""
    roles(world, viewer=(ANALYST,))
    made(world)
    few = _open_work_queries(world, 1)
    assert _open_work_queries(world, 40) == few


# --- Reassigning many ---------------------------------------------------------------------------


def test_many_are_reassigned_at_once_and_each_person_is_told_once(world: World) -> None:
    roles(
        world, viewer=(ANALYST,), runner=(ANALYST,), editor=(ANALYST, REVIEWER), admin=(REVIEWER,)
    )
    ids = [made(world, {"analyst": pk(world, "viewer")}, f"S-{n}")["id"] for n in range(3)]
    locked = made(world, {}, "S-L")
    set_(world, "editor", locked["id"], "status", "in_testing")
    set_(world, "editor", locked["id"], "status", "in_review")
    set_(world, "admin", locked["id"], "status", "reviewed")
    Notification.objects.all().delete()
    answer = api_as(world, "editor").post(
        "/entities/assign",
        {
            "entities": [*ids, locked["id"]],
            "field": "analyst",
            "person": "runner@example.com",
            "reason": "Viewer is away",
        },
    )
    assert answer.status_code == 200, answer.content
    out = answer.json()
    assert out["changed"] == ids
    assert [s["entity"] for s in out["skipped"]] == [locked["id"]]
    (note,) = told(world.users["runner"], "assignment.assigned")
    assert note.title == "Editor assigned you 3 records as Analyst"
    assert note.path == f"/work?project={world.project.pk}"
    (off,) = told(world.users["viewer"], "assignment.unassigned")
    assert off.title == "Editor took you off 3 records as Analyst"
    events = AuditEvent.objects.filter(action="entity.assigned", target_id__in=ids)
    grouped = [e for e in events if "group" in e.data]
    assert len(grouped) == 3
    assert {e.data["group"] for e in grouped} == {out["group"]}
    assert {e.data["reason"] for e in grouped} == {"Viewer is away"}
    # Someone who can't be named skips each record, saying why.
    again = api_as(world, "editor").post(
        "/entities/assign",
        {"entities": ids[:1], "field": "analyst", "person": pk(world, "outsider")},
    )
    (skipped,) = again.json()["skipped"]
    assert skipped["why"].startswith("Analyst isn't someone who can be named here")
    # A record you can't read changes nothing, and isn't skipped as if it might exist.
    unknown = api_as(world, "editor").post(
        "/entities/assign",
        {"entities": [*ids, pk(world, "viewer")], "field": "analyst", "person": None},
    )
    assert pk(world, "viewer") in problem(unknown, 404, "not_found")["detail"]
    # Only an Editor reassigns.
    viewer = api_as(world, "viewer").post(
        "/entities/assign", {"entities": ids[:1], "field": "analyst", "person": None}
    )
    assert viewer.json()["skipped"][0]["why"] == "It needs you as an Editor in Kinase Atlas."
    # A list adds and removes.
    added = api_as(world, "editor").post(
        "/entities/assign",
        {"entities": ids[:2], "field": "helpers", "person": pk(world, "viewer"), "op": "add"},
    )
    assert added.json()["changed"] == ids[:2]
    removed = api_as(world, "editor").post(
        "/entities/assign",
        {"entities": ids[:1], "field": "helpers", "person": pk(world, "viewer"), "op": "remove"},
    )
    assert removed.json()["changed"] == ids[:1]
    assert Entity.objects.get(pk=ids[0]).revision == 4


# --- Search -------------------------------------------------------------------------------------


def find(world: World, where: dict[str, Any], who: str = "viewer") -> list[str]:
    answer = api_as(world, who).post("/entities/search", {"type": SAMPLE, "where": where})
    assert answer.status_code == 200, answer.content
    return [e["values"]["label"] for e in answer.json()["items"]]


def test_searches_find_whom_records_name(world: World) -> None:
    roles(world, viewer=(ANALYST, REVIEWER), runner=(ANALYST, REVIEWER))
    made(world, {"analyst": pk(world, "viewer")}, "mine")
    made(world, {"analyst": pk(world, "runner"), "reviewer": pk(world, "viewer")}, "theirs")
    made(world, {}, "nobody's")
    me = {"field": "assigned", "op": "eq", "value": "{{ me }}"}
    assert find(world, me) == ["mine"]
    # A reviewer named before review isn't assigned it yet.
    assert find(world, me, "runner") == ["theirs"]
    assert find(world, {"field": "reviewer", "op": "eq", "value": "{{me}}"}) == ["theirs"]
    assert find(world, {"field": "analyst", "op": "eq", "value": "Runner@example.com"}) == [
        "theirs"
    ]
    assert find(world, {"field": "analyst", "op": "eq", "value": "nobody@example.com"}) == []
    assert find(world, {"field": "assigned", "op": "is_set", "value": False}) == ["nobody's"]
    assert find(world, {"field": "assigned", "op": "ne", "value": "{{ me }}"}) == [
        "theirs",
        "nobody's",
    ]
    assert find(world, {"field": "created_by", "op": "eq", "value": "{{ me }}"}, "editor") == [
        "mine",
        "theirs",
        "nobody's",
    ]
    # Not sorted by: an ID's order means nothing, and names aren't the record's.
    answer = api_as(world, "viewer").post(
        "/entities/search", {"type": SAMPLE, "sort": {"field": "analyst"}}
    )
    problem(answer, 422, "validation_failed")


# --- Projects apart -----------------------------------------------------------------------------


def test_people_dont_cross_projects(world: World) -> None:
    other = Project.objects.create(team=world.team, name="Other Lab")
    stranger = User.objects.create_user(email="stranger@example.com", name="Stranger")
    ProjectMembership.objects.create(project=other, user=stranger, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(
        project=other, user=world.users["viewer"], role=ProjectRole.EDITOR
    )
    # A member of another project can't be named here, by ID or email.
    for who in (str(stranger.pk), "stranger@example.com"):
        refused = problem(sample(world, {"received_by": who}), 422, "validation_failed")
        assert refused["errors"][0]["detail"].startswith("isn't someone who can be named here")
    # Its picker isn't theirs to read.
    problem(api_as(world, "editor").get(f"/projects/{other.pk}/people"), 404, "not_found")
    # Their work in it isn't anyone else's to see.
    theirs = create(
        Api(world.browser("viewer")),
        other,
        {"label": "Other", "received_by": str(stranger.pk)},
        SAMPLE,
    )
    assert theirs.status_code == 201, theirs.content
    found = find(world, {"field": "received_by", "op": "eq", "value": str(stranger.pk)}, "editor")
    assert found == []
    # Someone's email is shown only to those in a project with them.
    shown = people.shown(world.users["editor"], [stranger.pk], {stranger.pk: {other.pk}})
    assert (shown[stranger.pk].email, shown[stranger.pk].name) == (None, "Stranger")
    shown = people.shown(world.users["viewer"], [stranger.pk], {stranger.pk: {other.pk}})
    assert shown[stranger.pk].email == "stranger@example.com"


def test_an_import_names_people_as_a_create_does(world: World, s3: Any) -> None:
    roles(world, viewer=(ANALYST,))
    body = imported_values(
        world,
        {"label": "S-1", "analyst": "viewer@example.com", "helpers": [pk(world, "editor")]},
    )
    assert body["status"] == "succeeded", body.get("error")
    (effect,) = body["effects"]
    record = api_as(world, "editor").get(f"/entities/{effect['hrid']}").json()
    assert record["values"]["analyst"] == pk(world, "viewer")
    assert record["values"]["helpers"] == [pk(world, "editor")]
    assert record["people"][pk(world, "viewer")]["name"] == world.users["viewer"].name
    # Its first revision has them, as any other's would.
    entity = Entity.objects.get(pk=effect["entity"])
    history = assignments.of(entity.type)[0].part.history._default_manager.get(entity_id=entity.pk)
    assert str(getattr(history, "analyst")) == pk(world, "viewer")  # noqa: B009 - a plugin's model
    # It's on their work, as soon as it's theirs.
    assert effect["hrid"] in hrids(work(world, "viewer"))
    # Someone who can't be named fails the import, which writes nothing.
    refused = imported_values(world, {"label": "S-2", "analyst": "outsider@example.com"})
    assert refused["status"] == "failed", refused
    assert "isn't someone who can be named here" in json.dumps(refused["error"])
    assert not Entity.objects.filter(type=SAMPLE, hrid__isnull=False).exclude(pk=entity.pk).exists()


def test_work_is_only_your_own(world: World) -> None:
    roles(world, viewer=(ANALYST,))
    body = made(world, {"analyst": pk(world, "viewer")})
    for who in ("editor", "admin", "org_admin", "auditor", "outsider"):
        assert body["hrid"] not in hrids(work(world, who)), who
    # Anyone signed in has work, if only none.
    problem(Api(world.browser("anonymous")).get("/me/work"), 401, "not_authenticated")
    second = build_world("second-")
    assert hrids(work(second, "viewer")) == []
