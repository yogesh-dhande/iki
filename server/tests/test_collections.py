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


def make(api: Api, world: World, search: dict[str, Any], keep: str, **kw: Any) -> Any:
    body = {"project": str(world.project.pk), "name": "Picks", "search": search, "keep": keep}
    return api.post("/collections", {**body, **kw})


def test_making_a_fixed_list_from_a_search(world: World, secret: Project) -> None:
    admin = api_as(world, "admin")
    protein(admin, world.project, "abl1", "Homo sapiens")
    protein(admin, world.project, "egfr", "Homo sapiens")
    protein(admin, world.project, "lacz", "Escherichia coli")
    protein(admin, secret, "hidden", "Homo sapiens")
    editor = api_as(world)
    made = make(editor, world, {**HUMAN, "sort": "name", "descending": True}, "members")
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["values"]["search"] is None
    # What the editor could read, in the search's order; Secret's protein isn't among them.
    assert [body["refs"][m]["title"] for m in body["values"]["members"]] == ["egfr", "abl1"]
    revisions = editor.get(f"/entities/{body['hrid']}/revisions").json()["items"]
    assert revisions[0]["reason"] == "Frozen: the 2 entities its search found."
    # Later matches don't join a fixed list.
    protein(admin, world.project, "src", "Homo sapiens")
    assert len(members(editor, body["hrid"])) == 2

    dynamic = make(editor, world, HUMAN, "search").json()
    assert dynamic["values"]["search"] == HUMAN
    assert len(members(editor, dynamic["hrid"])) == 3


def test_freezing_a_dynamic_collection(world: World) -> None:
    editor = api_as(world)
    protein(editor, world.project, "abl1", "Homo sapiens")
    made = make(editor, world, HUMAN, "search").json()
    url = f"/collections/{made['hrid']}/freeze"
    problem(editor.post(url, {}), 428, "precondition_required")
    problem(api_as(world, "viewer").post(url, {}, headers={"If-Match": '"1"'}), 403, "forbidden")
    protein(editor, world.project, "egfr", "Homo sapiens")
    frozen = editor.post(url, {"reason": "For the paper"}, headers={"If-Match": '"1"'})
    assert frozen.status_code == 200, frozen.content
    assert frozen.json()["values"]["search"] is None
    assert len(frozen.json()["values"]["members"]) == 2
    assert frozen.json()["revision"] == 2
    # It's a fixed list now, and a stale ETag is refused either way.
    again = problem(editor.post(url, {}, headers={"If-Match": '"2"'}), 409, "conflict")
    assert again["detail"] == "It lists its members already: it has no search to freeze."
    problem(editor.post(url, {}, headers={"If-Match": '"1"'}), 409, "conflict")
    abl = protein(editor, world.project, "abl2", "Homo sapiens")
    problem(
        editor.post(f"/collections/{abl['hrid']}/freeze", {}, headers={"If-Match": '"1"'}),
        409,
        "conflict",
    )


def test_a_stale_freeze_is_refused(world: World) -> None:
    editor = api_as(world)
    made = make(editor, world, HUMAN, "search").json()
    renamed = editor.patch(
        f"/entities/{made['id']}", {"values": {"name": "Humans"}}, headers={"If-Match": '"1"'}
    )
    assert renamed.status_code == 200
    stale = editor.post(f"/collections/{made['hrid']}/freeze", {}, headers={"If-Match": '"1"'})
    assert problem(stale, 412, "stale")["detail"].startswith("Someone changed it since")


def test_a_fixed_list_has_a_limit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.entities import collections

    monkeypatch.setattr(collections, "max_members", lambda: 1)
    editor = api_as(world)
    protein(editor, world.project, "abl1", "Homo sapiens")
    protein(editor, world.project, "egfr", "Homo sapiens")
    body = problem(make(editor, world, HUMAN, "members"), 409, "too_many")
    assert body["detail"].startswith("Its search finds more than 1 entities")


def test_making_one_checks_its_search(world: World) -> None:
    editor = api_as(world)
    wrong = {"type": "protein.Protein", "where": {"field": "colour", "op": "eq", "value": 1}}
    for keep in ("search", "members"):
        body = problem(make(editor, world, wrong, keep), 422, "validation_failed")
        assert body["errors"] == [
            {
                "pointer": "#/search",
                "detail": "`where.field`: protein.Protein has no field `colour`.",
            }
        ]
    problem(make(api_as(world, "viewer"), world, HUMAN, "search"), 403, "forbidden")


def test_a_long_list_is_checked_in_one_query(
    world: World, django_assert_max_num_queries: Any
) -> None:
    from iki_server.entities import services

    editor = world.users["editor"]
    ids = [
        str(
            services.create(
                editor, "dna.DNASequence", world.project, {"bases": "ACGT" + "A" * i}
            ).entity.pk
        )
        for i in range(60)
    ]
    # However many members, their references are looked up together.
    with django_assert_max_num_queries(40):
        services.create(editor, "core.Collection", world.project, {"name": "Many", "members": ids})
