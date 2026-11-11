"""Collections (core.Collection): static ones list members, dynamic ones are a search."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

HUMAN = {
    "type": "protein.Protein",
    "where": {"field": "organism", "op": "eq", "value": "Homo sapiens"},
}


@pytest.fixture
def world() -> World:
    return build_world()


def protein(api: Api, project: Project, name: str, organism: str | None = None) -> Any:
    values = {"sequence": "MK" + "".join(chr(65 + (ord(c) % 20)) for c in name), "name": name}
    if organism:
        values["organism"] = organism
    made = create(api, project, values, "protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def collection(api: Api, world: World, **values: Any) -> Any:
    return create(api, world.project, {"name": "Picks", **values}, "core.Collection")


def members(api: Api, ref: str, **search: Any) -> list[str]:
    found = api.post("/entities/search", {"collection": ref, **search})
    assert found.status_code == 200, found.content
    return [e["values"]["name"] for e in found.json()["items"]]


@pytest.fixture
def secret(world: World) -> Project:
    """A project only the Admin can read."""
    project = Project.objects.create(team=world.team, name="Secret")
    ProjectMembership.objects.create(
        project=project, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    return project


def test_collections_are_a_core_type(world: World) -> None:
    types = api_as(world, "viewer").get("/entity-types").json()
    found = next(t for t in types if t["name"] == "core.Collection")
    assert {f["name"] for f in found["fields"]} >= {"name", "description", "members", "search"}
    made = collection(api_as(world), world).json()
    assert made["hrid"].startswith("COL-")
    assert made["title"] == "Picks"


def test_a_static_collection_shows_only_members_you_can_read(world: World, secret: Project) -> None:
    admin = api_as(world, "admin")
    abl = protein(admin, world.project, "abl1")
    src = protein(admin, world.project, "src")
    hidden = protein(admin, secret, "hidden")
    protein(admin, world.project, "not-a-member")
    made = collection(admin, world, members=[src["id"], abl["id"], hidden["id"]])
    assert made.status_code == 201, made.content
    made = made.json()
    assert members(admin, made["hrid"], type="protein.Protein", sort={"field": "name"}) == [
        "abl1",
        "hidden",
        "src",
    ]
    # The viewer reads the collection, but not the Secret project's entity in it.
    viewer = api_as(world, "viewer")
    assert members(viewer, made["id"], type="protein.Protein", sort={"field": "name"}) == [
        "abl1",
        "src",
    ]
    # Filters, sorting and paging work on members like on any search.
    assert members(viewer, made["hrid"], q="src") == ["src"]
    # Without access to the collection, its members aren't anyone's business.
    body = problem(
        api_as(world, "outsider").post("/entities/search", {"collection": made["hrid"]}),
        422,
        "validation_failed",
    )
    assert body["errors"] == [
        {"pointer": "#/collection", "detail": "No such collection, or you can’t see it."}
    ]


def test_a_dynamic_collection_runs_its_search_as_the_reader(world: World, secret: Project) -> None:
    admin = api_as(world, "admin")
    protein(admin, world.project, "abl1", "Homo sapiens")
    protein(admin, world.project, "lacz", "Escherichia coli")
    protein(admin, secret, "hidden", "Homo sapiens")
    made = collection(admin, world, search=HUMAN).json()
    assert made["values"]["search"] == HUMAN
    assert members(admin, made["hrid"], type="protein.Protein", sort={"field": "name"}) == [
        "abl1",
        "hidden",
    ]
    viewer = api_as(world, "viewer")
    assert members(viewer, made["hrid"]) == ["abl1"]
    # New matches join it.
    protein(admin, world.project, "egfr", "Homo sapiens")
    assert members(viewer, made["hrid"], type="protein.Protein", sort={"field": "name"}) == [
        "abl1",
        "egfr",
    ]
    # A search scoped to a project only finds that project's entities.
    scoped = collection(admin, world, search={**HUMAN, "project": str(secret.pk)}).json()
    assert members(admin, scoped["hrid"]) == ["hidden"]
    assert members(viewer, scoped["hrid"]) == []


def test_members_or_a_search_not_both(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world.project, "abl1")
    body = problem(
        collection(editor, world, members=[abl["id"]], search=HUMAN), 422, "validation_failed"
    )
    assert body["errors"] == [
        {
            "pointer": "#/values/search",
            "detail": "A collection lists its members or has a search, not both.",
        }
    ]
    # Freezing a dynamic collection: its members now, and no search.
    made = collection(editor, world, search=HUMAN).json()
    frozen = editor.patch(
        f"/entities/{made['id']}",
        {"values": {"members": [abl["id"]], "search": None}},
        headers={"If-Match": f'"{made["revision"]}"'},
    )
    assert frozen.status_code == 200, frozen.content
    assert frozen.json()["values"]["search"] is None
    assert members(editor, made["hrid"]) == ["abl1"]


def test_a_search_is_checked_as_its_author(world: World, secret: Project) -> None:
    editor = api_as(world)
    wrong = {"type": "protein.Protein", "where": {"field": "colour", "op": "eq", "value": 1}}
    body = problem(collection(editor, world, search=wrong), 422, "validation_failed")
    assert body["errors"] == [
        {
            "pointer": "#/values/search",
            "detail": "`where.field`: protein.Protein has no field `colour`.",
        }
    ]
    # A project the author can't read is no project at all to them.
    body = problem(
        collection(editor, world, search={**HUMAN, "project": str(secret.pk)}),
        422,
        "validation_failed",
    )
    assert body["errors"][0]["detail"] == "`project`: No such project, or you can’t see it."
    # The manifest's JSON Schema allows only a search's own keys.
    body = problem(collection(editor, world, search={"cursor": "x"}), 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/values/search"


def test_a_search_that_stopped_working_says_so(world: World) -> None:
    editor = api_as(world)
    made = collection(editor, world, search=HUMAN).json()
    # A type it names is gone, as after a plugin is detached: store one directly.
    from iki_server.entities import registry

    model = registry.model("core.Collection")
    model._default_manager.filter(pk=made["id"]).update(search={"type": "gone.Thing"})
    body = problem(
        editor.post("/entities/search", {"collection": made["hrid"]}), 422, "validation_failed"
    )
    assert body["errors"] == [
        {
            "pointer": "#/collection",
            "detail": f"{made['hrid']}'s search no longer works: "
            "There’s no entity type gone.Thing.",
        }
    ]
    # Renaming it doesn't need its search to work; changing the search does.
    renamed = editor.patch(
        f"/entities/{made['id']}",
        {"values": {"name": "Humans"}},
        headers={"If-Match": f'"{made["revision"]}"'},
    )
    assert renamed.status_code == 200, renamed.content
