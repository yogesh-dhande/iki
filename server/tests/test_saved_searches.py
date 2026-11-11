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
    "type": "protein.Protein",
    "archived": False,
    "where": {"field": "organism", "op": "eq", "value": "Homo sapiens"},
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
    return [s["name"] for s in found.json()]


def test_saving_a_search_and_running_it(world: World) -> None:
    editor = api_as(world, "editor")
    create(
        editor,
        world.project,
        {"sequence": "MKVL", "name": "abl1", "organism": "Homo sapiens"},
        "protein.Protein",
    )
    create(editor, world.project, {"sequence": "MTMI", "name": "lacz"}, "protein.Protein")
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
    wrong = {"type": "protein.Protein", "where": {"field": "colour", "op": "eq", "value": "red"}}
    body = problem(
        viewer.post(
            "/saved-searches", {"project": str(world.project.pk), "name": "x", "query": wrong}
        ),
        422,
        "validation_failed",
    )
    assert body["errors"] == [
        {"pointer": "#/query/where/field", "detail": "protein.Protein has no field `colour`."}
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
