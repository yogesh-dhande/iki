"""Saved searches: personal and shared, who may change them, checks, and the audit log."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.audit.models import AuditEvent
from iki_server.searches import services

pytestmark = pytest.mark.django_db

HUMAN = {
    "type": "acme/protein.Protein",
    "archived": False,
    "where": {"field": "host", "op": "eq", "value": "Homo sapiens"},
    "sort": {"field": "name", "descending": True},
}


@pytest.fixture
def world() -> World:
    return build_world()


def save(api: Api, world: World, name: str = "Human", **kw: Any) -> Any:
    body = {"project": str(world.project.pk), "name": name, "query": HUMAN, **kw}
    return api.post("/saved-searches", body)


def names(api: Api, world: World) -> list[str]:
    found = api.get("/saved-searches", project=str(world.project.pk))
    assert found.status_code == 200, found.content
    return [s["name"] for s in found.json()["items"]]


def test_saving_a_search_and_running_it(world: World) -> None:
    editor = api_as(world, "editor")
    create(
        editor,
        world.project,
        {"sequence": "MKVL", "name": "abl1", "host": "Homo sapiens"},
        "acme/protein.Protein",
    )
    create(editor, world.project, {"sequence": "MTMI", "name": "lacz"}, "acme/protein.Protein")
    viewer = api_as(world, "viewer")
    made = save(viewer, world)
    assert made.status_code == 201, made.content
    saved = made.json()
    assert saved["query"] == HUMAN and saved["shared"] is False
    assert saved["created_by"]["name"] == "Viewer"
    assert saved["can"] == {"change": True, "delete": True}
    # Its query is ready to run.
    body = {**saved["query"], "project": saved["project"]}
    found = viewer.post("/entities/search", body).json()["items"]
    assert [e["values"]["name"] for e in found] == ["abl1"]
    # Personal searches are their owner's alone, and aren't audited.
    assert names(viewer, world) == ["Human"]
    assert names(editor, world) == []
    assert not AuditEvent.objects.filter(action__startswith="saved_search.").exists()


def test_shared_searches_are_the_projects(world: World) -> None:
    editor = api_as(world, "editor")
    shared = save(editor, world, "Human proteins", shared=True).json()
    save(editor, world, "Mine only")
    viewer = api_as(world, "viewer")
    assert names(viewer, world) == ["Human proteins"]
    assert names(editor, world) == ["Human proteins", "Mine only"]
    seen = viewer.get(f"/saved-searches/{shared['id']}").json()
    assert seen["can"] == {"change": False, "delete": False}
    event = AuditEvent.objects.get(action="saved_search.shared")
    assert event.project_id == world.project.pk and event.data == {"name": "Human proteins"}

    # Only the owner changes it; the project's Admins can remove it, to keep the list tidy.
    admin = api_as(world, "admin")
    assert admin.get(f"/saved-searches/{shared['id']}").json()["can"] == {
        "change": False,
        "delete": True,
    }
    problem(admin.patch(f"/saved-searches/{shared['id']}", {"name": "Mine"}), 403, "forbidden")
    problem(viewer.delete(f"/saved-searches/{shared['id']}"), 403, "forbidden")
    renamed = editor.patch(f"/saved-searches/{shared['id']}", {"name": "Humans"})
    assert renamed.json()["name"] == "Humans"
    assert AuditEvent.objects.get(action="saved_search.updated").data == {
        "name": "Humans",
        "previous_name": "Human proteins",
    }
    assert admin.delete(f"/saved-searches/{shared['id']}").status_code == 204
    removed = AuditEvent.objects.get(action="saved_search.removed")
    assert removed.data == {"name": "Humans", "owner": "editor@example.com"}
    assert names(viewer, world) == []


def test_sharing_takes_editor(world: World) -> None:
    viewer = api_as(world, "viewer")
    body = problem(save(viewer, world, shared=True), 403, "forbidden")
    assert body["detail"] == "Sharing a search with the project takes the Editor role there."
    mine = save(viewer, world).json()
    problem(viewer.patch(f"/saved-searches/{mine['id']}", {"shared": True}), 403, "forbidden")

    editor = api_as(world, "editor")
    own = save(editor, world).json()
    assert editor.patch(f"/saved-searches/{own['id']}", {"shared": True}).json()["shared"]
    assert editor.patch(f"/saved-searches/{own['id']}", {"shared": False}).json()["shared"] is False
    assert [e.action for e in AuditEvent.objects.order_by("occurred_at", "id")][-2:] == [
        "saved_search.shared",
        "saved_search.unshared",
    ]


def test_searches_are_checked_when_saved(world: World) -> None:
    viewer = api_as(world, "viewer")
    wrong = {
        "type": "acme/protein.Protein",
        "where": {"field": "colour", "op": "eq", "value": "red"},
    }
    body = problem(
        viewer.post(
            "/saved-searches", {"project": str(world.project.pk), "name": "x", "query": wrong}
        ),
        422,
        "validation_failed",
    )
    assert body["errors"] == [
        {"pointer": "#/query/where/field", "detail": "acme/protein.Protein has no field `colour`."}
    ]
    blank = problem(save(viewer, world, "  "), 422, "validation_failed")
    assert blank["errors"] == [{"pointer": "#/name", "detail": "can't be blank."}]
    assert save(viewer, world).status_code == 201
    taken = problem(save(viewer, world), 409, "conflict")
    assert taken["detail"] == "You already have a saved search with this name here."
    # Another person can use the same name.
    assert save(api_as(world, "editor"), world).status_code == 201


def test_there_are_limits(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "MAX_OWN", 1)
    viewer = api_as(world, "viewer")
    assert save(viewer, world, "One").status_code == 201
    assert problem(save(viewer, world, "Two"), 409, "conflict")["detail"] == (
        "You have 1 saved searches here. Delete one first."
    )


def test_only_people_who_can_read_the_project(world: World) -> None:
    mine = save(api_as(world, "editor"), world, shared=True).json()
    outsider = api_as(world, "outsider")
    problem(outsider.get("/saved-searches", project=str(world.project.pk)), 404, "not_found")
    problem(outsider.get(f"/saved-searches/{mine['id']}"), 404, "not_found")
    problem(outsider.delete(f"/saved-searches/{mine['id']}"), 404, "not_found")


def test_only_your_own_are_the_sidebars_saved_views(world: World) -> None:
    """The sidebar's saved views (ADR 0176): your own, of every project or of one you can
    read, not those others shared, and none in a project you can no longer read, not even
    one you saved there."""
    from iki_server.org.models import Project, ProjectMembership, ProjectRole
    from iki_server.searches.models import SavedSearch

    viewer = api_as(world, "viewer")
    save(viewer, world, "Human")
    save(api_as(world, "editor"), world, "Shared", shared=True)
    assert viewer.post("/saved-searches", {"name": "Everywhere", "query": HUMAN}).status_code == 201
    other = Project.objects.create(team=world.team, name="Variant Calls")
    membership = ProjectMembership.objects.create(
        project=other, user=world.users["viewer"], role=ProjectRole.VIEWER
    )
    SavedSearch.objects.create(
        project=other, name="Another", query={}, created_by=world.users["viewer"]
    )
    mine = viewer.get("/saved-searches", mine=True)
    assert mine.status_code == 200, mine.content
    assert [(s["name"], s["project"]) for s in mine.json()["items"]] == [
        ("Another", str(other.pk)),
        ("Everywhere", None),
        ("Human", str(world.project.pk)),
    ]
    first = viewer.get("/saved-searches", mine=True, limit=1).json()
    assert [s["name"] for s in first["items"]] == ["Another"]
    rest = viewer.every("/saved-searches", mine=True, cursor=first["next_cursor"])
    assert [s["name"] for s in rest] == ["Everywhere", "Human"]
    in_the_project = viewer.get("/saved-searches", project=str(world.project.pk), mine=True)
    assert [s["name"] for s in in_the_project.json()["items"]] == ["Human"]
    membership.delete()
    assert [s["name"] for s in viewer.every("/saved-searches", mine=True)] == [
        "Everywhere",
        "Human",
    ]
    assert api_as(world, "outsider").every("/saved-searches", mine=True) == []


def test_a_search_of_every_project_is_its_owners_alone(world: World) -> None:
    """Saved with no project (ADR 0176), it finds records in every project its reader can
    read; nobody else sees it, and it can't be shared, having nobody to share it with."""
    from iki_server.org.models import Project, ProjectMembership, ProjectRole

    editor = api_as(world, "editor")
    other = Project.objects.create(team=world.other_team, name="Vector Reference QC")
    ProjectMembership.objects.create(
        project=other, user=world.users["viewer"], role=ProjectRole.EDITOR
    )
    create(
        editor,
        world.project,
        {"sequence": "MKVL", "name": "abl1", "host": "Homo sapiens"},
        "acme/protein.Protein",
    )
    create(
        api_as(world, "viewer"),
        other,
        {"sequence": "MSTK", "name": "src", "host": "Homo sapiens"},
        "acme/protein.Protein",
    )
    viewer = api_as(world, "viewer")
    made = viewer.post("/saved-searches", {"name": "Human", "query": HUMAN})
    assert made.status_code == 201, made.content
    saved = made.json()
    assert saved["project"] is None
    found = viewer.post("/entities/search", saved["query"]).json()["items"]
    assert [e["values"]["name"] for e in found] == ["src", "abl1"]

    # Listed without a project, beside the project searches the viewer sees.
    shared = save(editor, world, "Shared", shared=True).json()
    save(editor, world, "Editor's own")
    everywhere = viewer.every("/saved-searches")
    assert [(s["name"], s["project"]) for s in everywhere] == [
        ("Human", None),
        ("Shared", shared["project"]),
    ]
    # Not in a project's list, and nobody else's.
    assert names(viewer, world) == ["Shared"]
    assert [s["name"] for s in editor.every("/saved-searches")] == ["Editor's own", "Shared"]
    problem(editor.get(f"/saved-searches/{saved['id']}"), 404, "not_found")
    problem(editor.delete(f"/saved-searches/{saved['id']}"), 404, "not_found")

    refused = problem(
        viewer.patch(f"/saved-searches/{saved['id']}", {"shared": True}), 422, "validation_failed"
    )
    assert refused["errors"] == [{"pointer": "#/shared", "detail": "needs a project."}]
    problem(
        viewer.post("/saved-searches", {"name": "Shared", "query": HUMAN, "shared": True}),
        422,
        "validation_failed",
    )
    # One name each among them, as in a project.
    taken = viewer.post("/saved-searches", {"name": "Human", "query": HUMAN})
    assert problem(taken, 409, "conflict")["detail"] == (
        "You already have a saved search with this name here."
    )
    assert viewer.patch(f"/saved-searches/{saved['id']}", {"name": "Humans"}).json()["name"] == (
        "Humans"
    )
    assert viewer.delete(f"/saved-searches/{saved['id']}").status_code == 204
    assert not AuditEvent.objects.filter(action__startswith="saved_search.", project=None).exists()


def test_searches_in_projects_no_longer_read_arent_listed(world: World) -> None:
    viewer = api_as(world, "viewer")
    save(viewer, world, "Mine")
    assert [s["name"] for s in viewer.every("/saved-searches")] == ["Mine"]
    world.project.memberships.filter(user=world.users["viewer"]).delete()
    assert viewer.every("/saved-searches") == []
