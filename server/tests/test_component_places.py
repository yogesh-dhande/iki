"""What fills a component's place (ADR 0117): an entity's tab, a search's results, a
project's page, a person's home. A component sees only what its viewer may read, found
as the grid's search finds it, and only the fields it reads; what it asks for can only
narrow its place, never widen it, across projects, registries, roles and tokens."""

import dataclasses
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.components import installed, places
from iki_server.entities import search
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_spec.model import ComponentSearch

pytestmark = pytest.mark.django_db

BOARD = "acme/protein.board"
PATH = f"/components/{BOARD}/entities"
PROTEIN = "acme/protein.Protein"
DNA = "acme/dna.DNASequence"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def rival(world: World) -> Project:
    """Another project in the same registry: the outsider is its Admin, the editor an
    Editor, and no one else is in it."""
    project = Project.objects.create(team=world.team, name="Rival Screens")
    for name, role in (("outsider", ProjectRole.ADMIN), ("editor", ProjectRole.EDITOR)):
        ProjectMembership.objects.create(project=project, user=world.users[name], role=role)
    return project


def api_as(world: World, principal: str) -> Api:
    return Api(world.browser(principal))


def create(api: Api, project: Project, values: dict[str, Any], type_name: str = PROTEIN) -> Any:
    made = api.post("/entities", {"type": type_name, "project": str(project.pk), "values": values})
    assert made.status_code == 201, made.content
    return made.json()


def shown(api: Api, body: dict[str, Any], name: str = BOARD) -> list[dict[str, Any]]:
    response = api.post(f"/components/{name}/entities", body)
    assert response.status_code == 200, response.content
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def hrids(items: list[dict[str, Any]]) -> list[str]:
    return [e["hrid"] for e in items]


@pytest.fixture
def atlas(world: World) -> dict[str, Any]:
    """In the Kinase Atlas: a DNA sequence, two proteins it encodes and one it doesn't."""
    editor = api_as(world, "editor")
    dna = create(editor, world.project, {"bases": "ATGGCC", "name": "abl1.dna"}, DNA)
    entry = dna["entry"]["id"]
    encoded = [
        create(
            editor,
            world.project,
            {"sequence": s, "name": n, "encoded_by": entry, "summary": "Notes", "lims_id": lims},
        )
        for s, n, lims in (("MKVL", "ABL1", "L-1"), ("MKAA", "ABL2", "L-2"))
    ]
    other = create(editor, world.project, {"sequence": "MSTN", "name": "SRC"})
    return {"dna": dna, "encoded": encoded, "other": other}


# --- What it gets --------------------------------------------------------------------------------


def test_an_entitys_tab_gets_what_its_search_finds_and_only_the_fields_it_reads(
    world: World, atlas: dict[str, Any]
) -> None:
    items = shown(api_as(world, "viewer"), {"entity": atlas["dna"]["id"]})
    assert hrids(items) == [e["hrid"] for e in atlas["encoded"]]  # by name: ABL1, ABL2
    first = items[0]
    assert set(first) == {
        "id",
        "hrid",
        "title",
        "type",
        "revision",
        "project",
        "values",
        "refs",
        "people",
    }
    assert first["people"] == {}
    # Only what it reads: not the summary, nor the LIMS ID.
    assert first["values"] == {
        "name": "ABL1",
        "sequence": "MKVL",
        "encoded_by": atlas["dna"]["entry"]["id"],
    }
    assert first["project"] == {
        "id": str(world.project.pk),
        "name": world.project.name,
        "slug": world.project.slug,
    }
    assert list(first["refs"]) == [atlas["dna"]["entry"]["id"]]
    assert first["refs"][atlas["dna"]["entry"]["id"]]["hrid"] == atlas["dna"]["hrid"]


def test_a_component_gets_only_display_names_from_its_declared_person_fields(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_type = "acme/protein.Sample"
    reviewer, helper, receiver = (world.users[n] for n in ("runner", "viewer", "admin"))
    helper.name = ""
    helper.save(update_fields=["name"])
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=reviewer), role="acme/protein.reviewer"
    )
    values = {
        "label": "Sample for review",
        "reviewer": str(reviewer.pk),
        "helpers": [str(helper.pk)],
        "received_by": str(receiver.pk),
    }
    editor = api_as(world, "editor")
    full = create(editor, world.project, values, sample_type)
    assert full["people"][str(helper.pk)]["name"] == helper.email
    assert full["people"][str(receiver.pk)]["name"] == receiver.name

    board = installed.components()[BOARD]
    assert board.entities is not None
    fields = dataclasses.replace(board.entities, type=sample_type, fields=("reviewer", "helpers"))
    monkeypatch.setitem(installed.components(), BOARD, dataclasses.replace(board, entities=fields))
    (item,) = shown(editor, {"search": {"type": sample_type, "project": str(world.project.pk)}})
    assert item["values"] == {"reviewer": str(reviewer.pk), "helpers": [str(helper.pk)]}
    assert item["people"] == {
        str(reviewer.pk): {"name": reviewer.name},
        str(helper.pk): {"name": "Someone without a name"},
    }


def test_a_searchs_results_are_the_grids(world: World, atlas: dict[str, Any]) -> None:
    viewer = api_as(world, "viewer")
    wanted = {
        "type": PROTEIN,
        "project": str(world.project.pk),
        "where": {"field": "name", "op": "starts_with", "value": "ABL"},
        "sort": {"field": "name", "descending": True},
    }
    grid = viewer.post("/entities/search", wanted).json()["items"]
    assert hrids(shown(viewer, {"search": wanted})) == hrids(grid)
    assert hrids(grid) == [e["hrid"] for e in reversed(atlas["encoded"])]


def test_a_projects_page_gets_its_search_in_that_project(
    world: World, atlas: dict[str, Any], rival: Project
) -> None:
    create(api_as(world, "outsider"), rival, {"sequence": "MRIV", "name": "Rival lead"})
    items = shown(api_as(world, "editor"), {"project": str(world.project.pk)})
    assert [e["values"]["name"] for e in items] == ["ABL1", "ABL2", "SRC"]


def test_a_persons_home_is_what_names_them_in_every_project_they_read(
    world: World, atlas: dict[str, Any], rival: Project
) -> None:
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    create(editor, rival, {"sequence": "MRIV", "name": "Also mine"})
    create(outsider, rival, {"sequence": "MRAA", "name": "Theirs"})
    names = [e["values"]["name"] for e in shown(editor, {"home": True})]
    assert names == ["ABL1", "ABL2", "Also mine", "SRC"]
    assert [e["values"]["name"] for e in shown(outsider, {"home": True})] == ["Theirs"]
    # Someone who added nothing has nothing there, though they read the Atlas.
    assert shown(api_as(world, "viewer"), {"home": True}) == []


# --- What they can't read ------------------------------------------------------------------------


def test_no_place_shows_another_projects_records(
    world: World, atlas: dict[str, Any], rival: Project
) -> None:
    outsider = api_as(world, "outsider")
    # The same DNA and proteins, registered in the rival project: the same entries.
    dna = create(outsider, rival, {"bases": "ATGGCC"}, DNA)
    assert dna["entry"]["id"] == atlas["dna"]["entry"]["id"]
    theirs = create(
        outsider, rival, {"sequence": "MKVL", "encoded_by": dna["entry"]["id"], "name": "Rival"}
    )
    # Their tab shows their record of ABL1, not the Atlas's, which they can't read.
    items = shown(outsider, {"entity": dna["id"]})
    assert [e["id"] for e in items] == [theirs["id"]]
    assert items[0]["values"]["name"] == "Rival"
    # The editor reads both projects; each tab shows its own project's records.
    editor = api_as(world, "editor")
    assert [e["id"] for e in shown(editor, {"entity": dna["id"]})] == [theirs["id"]]
    atlas_ids = [e["id"] for e in atlas["encoded"]]
    assert [e["id"] for e in shown(editor, {"entity": atlas["dna"]["id"]})] == atlas_ids
    # A search without a project finds only what they read; with the Atlas, nothing.
    found = shown(outsider, {"search": {"type": PROTEIN}})
    assert [e["id"] for e in found] == [theirs["id"]]
    for body in (
        {"search": {"type": PROTEIN, "project": str(world.project.pk)}},
        {"project": str(world.project.pk)},
        {"entity": atlas["dna"]["id"]},
    ):
        problem(outsider.post(PATH, body), 404, "not_found")


def test_roles_decide_as_they_do_for_the_grid(world: World, atlas: dict[str, Any]) -> None:
    body = {"project": str(world.project.pk)}
    for principal in ("viewer", "editor", "runner", "admin", "via_team", "mixed"):
        assert len(shown(api_as(world, principal), body)) == 3, principal
    # Seeing a project isn't reading it: an Org Admin who isn't a member reads none of it.
    problem(api_as(world, "org_admin").post(PATH, body), 403, "forbidden")
    org_admin = api_as(world, "org_admin")
    assert shown(org_admin, {"search": {"type": PROTEIN}}) == []
    assert shown(org_admin, {"home": True}) == []
    problem(org_admin.post(PATH, {"entity": atlas["dna"]["id"]}), 404, "not_found")
    for principal in ("outsider", "team_member", "auditor"):
        problem(api_as(world, principal).post(PATH, body), 404, "not_found")
    for principal in ("anonymous", "inactive"):
        assert api_as(world, principal).post(PATH, body).status_code == 401


def test_registries_are_kept_apart(world: World, atlas: dict[str, Any]) -> None:
    made = api_as(world, "org_admin").post(
        "/registries", {"name": "Client A", "teams": [str(world.team.pk)]}
    )
    assert made.status_code == 201, made.content
    outsider = api_as(world, "team_member")
    created = outsider.post(
        "/projects",
        {"team": str(world.team.pk), "name": "Client A work", "registry": made.json()["id"]},
    )
    assert created.status_code == 201, created.content
    theirs = Project.objects.get(pk=created.json()["id"])
    dna = create(outsider, theirs, {"bases": "ATGGCC"}, DNA)
    assert dna["entry"]["id"] != atlas["dna"]["entry"]["id"]
    create(outsider, theirs, {"sequence": "MKVL", "encoded_by": dna["entry"]["id"]})
    # The same identities, another registry's entries: neither side sees the other's.
    assert len(shown(outsider, {"entity": dna["id"]})) == 1
    assert len(shown(api_as(world, "editor"), {"entity": atlas["dna"]["id"]})) == 2
    assert len(shown(outsider, {"search": {"type": PROTEIN}})) == 1


def test_a_token_sees_only_its_projects(
    world: World, atlas: dict[str, Any], rival: Project
) -> None:
    create(api_as(world, "editor"), rival, {"sequence": "MRIV", "name": "Rival lead"})
    _, tokens = signed_in_token(world, "editor", scope="read", projects=[world.project])
    found = bearer(tokens["access_token"], "POST", PATH, {"home": True})
    assert found.status_code == 200, found.content
    assert [e["values"]["name"] for e in found.json()["items"]] == ["ABL1", "ABL2", "SRC"]
    elsewhere = bearer(tokens["access_token"], "POST", PATH, {"project": str(rival.pk)})
    assert elsewhere.status_code == 404


# --- What it can ask for -------------------------------------------------------------------------


def test_a_refinement_narrows_its_place_and_can_never_widen_it(
    world: World, atlas: dict[str, Any], rival: Project
) -> None:
    editor = api_as(world, "editor")
    create(editor, rival, {"sequence": "MRIV", "name": "ABL9"})
    tab = {"entity": atlas["dna"]["id"]}
    narrowed = {"where": {"field": "name", "op": "eq", "value": "ABL2"}}
    assert hrids(shown(editor, {**tab, "refine": narrowed})) == [atlas["encoded"][1]["hrid"]]
    # Asking for everything, or for what isn't there, still gets only what the tab shows.
    widest = {
        "where": {
            "any": [
                {"field": "hrid", "op": "is_set", "value": True},
                {"not": {"field": "name", "op": "eq", "value": "ABL1"}},
            ]
        }
    }
    assert hrids(shown(editor, {**tab, "refine": widest})) == hrids(atlas["encoded"])
    # The project's page doesn't reach the rival project's ABL9, however it's asked.
    page = {"project": str(world.project.pk), "refine": {"q": "ABL9"}}
    assert shown(editor, page) == []
    # A sort replaces the place's.
    backwards = {**tab, "refine": {"sort": {"field": "name", "descending": True}}}
    assert hrids(shown(editor, backwards)) == hrids(reversed(atlas["encoded"]))


def test_a_refinement_names_only_the_fields_it_reads(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    tab = {"entity": atlas["dna"]["id"]}
    for refine, pointer in (
        # It doesn't read the LIMS ID, so it can't learn it by narrowing on it.
        ({"where": {"field": "lims_id", "op": "eq", "value": "L-1"}}, "#/refine/where/field"),
        (
            {"where": {"all": [{"not": {"field": "summary", "op": "contains", "value": "N"}}]}},
            "#/refine/where/all/0/not/field",
        ),
        ({"where": {"field": "created_by", "op": "is_set", "value": True}}, "#/refine/where/field"),
        ({"sort": {"field": "molecular_weight"}}, "#/refine/sort/field"),
    ):
        refused = problem(editor.post(PATH, {**tab, "refine": refine}), 422, "validation_failed")
        assert [e["pointer"] for e in refused["errors"]] == [pointer], refused
    # What the search itself refuses says where in the refinement.
    wrong = {"where": {"field": "name", "op": "gt", "value": "A"}}
    refused = problem(editor.post(PATH, {**tab, "refine": wrong}), 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/refine/where/op"]


def test_a_refinement_is_bounded(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    tab = {"entity": atlas["dna"]["id"]}
    many = [{"field": "name", "op": "eq", "value": f"P{i}"} for i in range(21)]
    refused = problem(
        editor.post(PATH, {**tab, "refine": {"where": {"any": many}}}), 422, "validation_failed"
    )
    assert "at most 20" in refused["errors"][0]["detail"]
    deep: dict[str, Any] = {"field": "name", "op": "eq", "value": "ABL1"}
    for _ in range(4):
        deep = {"not": deep}
    refused = problem(
        editor.post(PATH, {**tab, "refine": {"where": deep}}), 422, "validation_failed"
    )
    assert "at most 4 levels" in refused["errors"][0]["detail"]
    # Text only where the place's search has none.
    grid = {"search": {"type": PROTEIN, "q": "ABL"}, "refine": {"q": "SRC"}}
    refused = problem(editor.post(PATH, grid), 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/refine/q"]
    # Nothing but what a refinement has.
    extra = {**tab, "refine": {"where": None, "type": "acme/dna.DNASequence"}}
    assert editor.post(PATH, extra).status_code == 422


def test_it_pages_a_page_at_a_time_up_to_its_limit(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    body = {"project": str(world.project.pk), "limit": 2}
    first = editor.post(PATH, body).json()
    assert [e["values"]["name"] for e in first["items"]] == ["ABL1", "ABL2"]
    second = editor.post(PATH, {**body, "cursor": first["next_cursor"]}).json()
    assert [e["values"]["name"] for e in second["items"]] == ["SRC"]
    assert second["next_cursor"] is None
    problem(editor.post(PATH, {**body, "limit": 101}), 422, "validation_failed")


def test_a_component_with_a_smaller_limit_gets_no_more_a_page(
    world: World, atlas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = installed.components()[BOARD]
    assert board.entities is not None
    small = dataclasses.replace(board, entities=dataclasses.replace(board.entities, limit=1))
    monkeypatch.setitem(installed.components(), BOARD, small)
    found = api_as(world, "editor").post(PATH, {"project": str(world.project.pk)}).json()
    assert len(found["items"]) == 1 and found["next_cursor"]


# --- Where it shows ------------------------------------------------------------------------------


def test_one_place_at_a_time(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    problem(editor.post(PATH, {}), 422, "validation_failed")
    both = {"project": str(world.project.pk), "home": True}
    refused = problem(editor.post(PATH, both), 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/project", "#/home"]


def test_only_where_it_shows(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    # A protein's page has no board tab; residues is a tab that shows only its entity.
    protein = atlas["encoded"][0]["id"]
    problem(editor.post(PATH, {"entity": protein}), 422, "not_shown_here")
    residues = "/components/acme/protein.residues/entities"
    problem(editor.post(residues, {"entity": protein}), 422, "not_shown_here")
    problem(editor.post(residues, {"home": True}), 422, "not_shown_here")
    # A search of another type: the board shows proteins.
    refused = problem(editor.post(PATH, {"search": {"type": DNA}}), 422, "not_shown_here")
    assert [e["pointer"] for e in refused["errors"]] == ["#/search/type"]
    problem(editor.post("/components/acme/protein.gone/entities", {"home": True}), 404, "not_found")


def test_a_search_that_doesnt_work_says_where(world: World, atlas: dict[str, Any]) -> None:
    editor = api_as(world, "editor")
    wrong = {"search": {"type": PROTEIN, "where": {"field": "colour", "op": "eq", "value": "red"}}}
    refused = problem(editor.post(PATH, wrong), 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/search/where/field"]


def test_a_declared_search_that_doesnt_run_is_the_plugins_to_put_right(
    world: World, atlas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    board = installed.components()[BOARD]
    broken = ComponentSearch(where={"field": "name", "op": "gt", "value": "A"})
    slots = tuple(
        dataclasses.replace(s, search=broken) if s.slot == "project.page" else s
        for s in board.slots
    )
    monkeypatch.setitem(installed.components(), BOARD, dataclasses.replace(board, slots=slots))
    body = {"project": str(world.project.pk)}
    refused = problem(api_as(world, "editor").post(PATH, body), 422, "component_misdeclared")
    assert "acme/protein plugin" in refused["detail"]


def test_every_installed_components_searches_compile(world: World) -> None:
    user = world.users["editor"]
    for component in installed.components().values():
        for slot in component.slots:
            if slot.slot == "search.results" or slot.search is None:
                continue
            place = (
                places.Place(home=True)
                if slot.slot == "person.home"
                else places.Place(project=world.project)
                if slot.slot == "project.page"
                else None
            )
            if place is None:
                continue  # A tab's needs an entity: test_an_entitys_tab_gets_... runs it.
            search.queryset(user, places.base(user, component, place))
