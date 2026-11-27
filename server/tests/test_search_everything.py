"""Searching everything (ADR 0176): records, requests, runs, projects, saved views and people,
across every project the caller can read, each kind by its own reading rules. Another
project's things never appear, nor count."""

import itertools
import json
from typing import Any

import pytest
from api_client import Api
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.accounts.models import User
from iki_server.api import everything
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectMembership, ProjectRole, TeamMembership
from iki_server.runs import catalog
from iki_server.runs import services as runs
from iki_server.searches import services as saved_searches

pytestmark = pytest.mark.django_db

PROTEIN = "acme/protein.Protein"
ASK = "acme/protein.AssayRequest"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def second(world: World) -> Project:
    """Another project the editor reads, in the same team: a record can be in both."""
    project = Project.objects.create(team=world.team, name="Enzymes")
    ProjectMembership.objects.create(
        project=project, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    permissions.forget(world.users["editor"])
    return project


@pytest.fixture
def foreign() -> World:
    """An organization's worth of projects and people nobody in the first world can reach."""
    return build_world(prefix="foreign-")


#: Each protein its own sequence: one sequence is one registry entry (ADR 0115), one HRID.
_SEQUENCES = ("MK" + "".join(r) for r in itertools.product("ACDEFGHIKLMNPQRSTVWY", repeat=4))


def protein(user: User, project: Project, name: str) -> Entity:
    values = {"sequence": next(_SEQUENCES), "name": name}
    return Entity.objects.get(pk=entities.create(user, PROTEIN, project, values).entity.pk)


def run_on(user: User, project: Project, target: Entity) -> Any:
    action = catalog.action("acme/protein.compute_properties")
    assert action is not None
    return runs.launch(user, action, project, {"protein": [target.pk]}, {})


def lysozyme_everywhere(w: World) -> dict[str, Any]:
    """One of each kind, all named for lysozyme, in the world's project."""
    editor = w.users["editor"]
    record = protein(editor, w.project, "Lysozyme C")
    request = entities.create(
        editor, ASK, w.project, {"title": "Lysozyme purity", "samples": []}
    ).entity
    run = run_on(editor, w.project, record)
    saved = saved_searches.create(
        editor, w.project, name="Lysozyme work", query={"q": "lysozyme"}, shared=True
    )
    w.project.name = f"{w.project.name} Lysozyme"
    w.project.save()
    return {"record": record, "request": request, "run": run, "saved": saved}


def searched(api: Api, **query: Any) -> dict[str, Any]:
    answer = api.get("/search", **query)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def group(body: dict[str, Any], kind: str) -> dict[str, Any]:
    found: dict[str, Any] = next(g for g in body["groups"] if g["kind"] == kind)
    return found


def titles(body: dict[str, Any], kind: str) -> list[str]:
    return [r["title"] for r in group(body, kind)["items"]]


def test_each_kind_is_found_across_the_projects_its_caller_reads(
    world: World, second: Project
) -> None:
    made = lysozyme_everywhere(world)
    elsewhere = protein(world.users["editor"], second, "Lysozyme B")
    body = searched(Api(world.browser("editor")), q="lysozyme")

    assert [g["kind"] for g in body["groups"]] == list(everything.KINDS)
    assert body["searched_records"] is True
    assert body["projects_readable"] == 3  # Kinase Atlas, Old Screens and Enzymes
    records = group(body, "entities")
    # Newest first, each with its project and where it opens.
    assert [(r["hrid"], r["projects"][0]["name"]) for r in records["items"]] == [
        (elsewhere.hrid, "Enzymes"),
        (made["record"].hrid, world.project.name),
    ]
    first = records["items"][0]
    assert first["kind"] == "entity"
    assert first["detail"] == "Protein"
    assert first["type"] == PROTEIN
    # A protein is a record of its registry entry, whose HRID each project's record shares.
    assert first["href"] == f"/entities/{elsewhere.hrid}/?project={second.slug}"
    assert records["total"] == 2
    assert records["more_than_total"] is False
    assert records["href"] == "/entities/?q=lysozyme"
    # A request is a request, not a record.
    asks = group(body, "requests")
    assert [r["hrid"] for r in asks["items"]] == [made["request"].hrid]
    assert asks["items"][0]["kind"] == "request"
    assert asks["href"] == "/requests/?q=lysozyme"
    assert made["request"].hrid not in [r["hrid"] for r in records["items"]]
    # Runs by their action's name or label; projects, saved views by theirs.
    assert titles(searched(Api(world.browser("editor")), q="compute"), "runs") == [
        catalog.action("acme/protein.compute_properties").label  # type: ignore[union-attr]
    ]
    assert titles(body, "projects") == [world.project.name]
    assert group(body, "projects")["items"][0]["href"] == f"/projects/{world.project.slug}/"
    view = group(body, "saved_views")["items"][0]
    assert (view["title"], view["detail"], view["query"]) == (
        "Lysozyme work",
        "Shared with the project",
        {"q": "lysozyme"},
    )
    # It opens from its own address, as the sidebar's saved views do.
    assert view["href"] == f"/saved-searches/{view['id']}/"


def test_a_record_in_two_projects_lists_both_its_owner_first(world: World, second: Project) -> None:
    editor = world.users["editor"]
    record = protein(editor, second, "Lysozyme C")
    entities.link(editor, record, world.project)
    found = group(searched(Api(world.browser("editor")), q="lysozyme"), "entities")["items"]
    assert [p["name"] for p in found[0]["projects"]] == ["Enzymes", world.project.name]
    # The viewer reads only one of them: only it is listed, and the record is still found.
    found = group(searched(Api(world.browser("viewer")), q="lysozyme"), "entities")["items"]
    assert [p["name"] for p in found[0]["projects"]] == [world.project.name]


def test_another_projects_things_never_appear_nor_count(world: World, foreign: World) -> None:
    """The same names in a project the caller can't read: none of it is found, counted, or
    named in the answer, whoever asks, and however they ask."""
    ours = lysozyme_everywhere(world)
    theirs = lysozyme_everywhere(foreign)
    secret = {
        str(foreign.project.pk),
        foreign.project.name,
        *(str(t.pk) for t in theirs.values()),
        *(t.hrid for t in (theirs["record"], theirs["request"], theirs["run"])),
    }
    for principal in ("viewer", "editor", "admin", "via_team", "team_member", "org_admin"):
        api = Api(world.browser(principal))
        for query in (
            {"q": "lysozyme"},
            {"q": theirs["record"].hrid},
            {"q": theirs["run"].hrid},
            {"q": theirs["request"].hrid.lower()},
            {"q": theirs["record"].hrid.split("-")[0] + "-"},
            {"q": "compute"},
            {"q": "lysozyme", "kind": "entities", "limit": 50},
            {"q": "lysozyme", "kind": "requests"},
            {"q": "compute", "kind": "runs"},
            {"q": "lysozyme", "kind": "saved_views"},
            {"q": ""},
        ):
            answer = api.get("/search", **query)
            assert answer.status_code == 200
            text = answer.content.decode()
            asked = str(query["q"]).casefold()
            hidden = secret
            if principal == "org_admin":
                # Org Admins see every project, as their list of projects does; nothing in it.
                hidden = secret - {str(foreign.project.pk), foreign.project.name}
            leaked = {s for s in hidden if s in text and s.casefold() not in asked}
            assert not leaked, (principal, query)
            body = answer.json()
            readable = principal not in ("team_member", "org_admin")
            for kind in ("entities", "requests", "runs", "saved_views"):
                g = next((g for g in body["groups"] if g["kind"] == kind), None)
                if g is None:
                    continue
                # Only ours count: one of each, for those who read the world's project.
                assert g["total"] <= (1 if readable else 0), (principal, query, kind)
    # The world's own are found by those who read its project, by HRID too.
    body = searched(Api(world.browser("viewer")), q=ours["run"].hrid)
    assert [r["hrid"] for r in group(body, "runs")["items"]] == [ours["run"].hrid]
    assert group(body, "runs")["items"][0]["href"] == f"/runs/{ours['run'].hrid}/"


def test_the_foreign_worlds_people_see_none_of_it_either(world: World, foreign: World) -> None:
    lysozyme_everywhere(world)
    body = searched(Api(foreign.browser("editor")), q="lysozyme")
    assert all(g["total"] == 0 and g["items"] == [] for g in body["groups"]), body


def test_an_org_admin_sees_projects_but_not_records_they_dont_read(world: World) -> None:
    """Org Admins see every project, as their list does, but read only their own (ADR 0005)."""
    lysozyme_everywhere(world)
    body = searched(Api(world.browser("org_admin")), q="lysozyme")
    assert titles(body, "projects") == [world.project.name]
    for kind in ("entities", "requests", "saved_views"):
        assert group(body, kind)["total"] == 0, kind
    assert body["projects_readable"] == 0


def test_short_text_searches_only_the_small_lists(world: World) -> None:
    lysozyme_everywhere(world)
    body = searched(Api(world.browser("editor")), q="ly")
    assert body["searched_records"] is False
    for kind in ("entities", "requests", "runs"):
        assert group(body, kind) == {
            "kind": kind,
            "label": everything._LABELS[kind],
            "total": 0,
            "more_than_total": False,
            "href": None,
            "items": [],
        }
    assert titles(body, "projects") == [world.project.name]
    assert titles(body, "saved_views") == ["Lysozyme work"]


def test_the_start_of_an_hrid_finds_records_and_a_whole_one_comes_first(world: World) -> None:
    editor = world.users["editor"]
    made = [protein(editor, world.project, f"Kinase {i}") for i in range(3)]
    api = Api(world.browser("editor"))
    prefix = made[0].hrid.split("-")[0]
    found = group(searched(api, q=f"{prefix.lower()}-"), "entities")
    assert found["total"] == 3
    assert [r["hrid"] for r in found["items"]] == [m.hrid for m in reversed(made)]
    found = group(searched(api, q=made[0].hrid.lower()), "entities")
    assert found["items"][0]["hrid"] == made[0].hrid


def test_one_kind_comes_with_more_and_totals_stop_at_the_cap(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor = world.users["editor"]
    for i in range(7):
        protein(editor, world.project, f"Lysozyme {i}")
    api = Api(world.browser("editor"))
    assert len(group(searched(api, q="lysozyme"), "entities")["items"]) == 5
    body = searched(api, q="lysozyme", kind="entities")
    assert [g["kind"] for g in body["groups"]] == ["entities"]
    assert len(body["groups"][0]["items"]) == 7
    assert len(group(searched(api, q="lysozyme", kind="entities", limit=2), "entities")["items"])
    monkeypatch.setattr(everything, "TOTAL_CAP", 4)
    capped = group(searched(api, q="lysozyme"), "entities")
    assert (capped["total"], capped["more_than_total"]) == (4, True)


def test_archived_records_arent_searched(world: World) -> None:
    editor = world.users["editor"]
    record = protein(editor, world.project, "Lysozyme C")
    entities.set_archived(editor, record, True)
    assert group(searched(Api(world.browser("editor")), q="lysozyme"), "entities")["total"] == 0


def test_a_record_of_no_registry_entry_opens_by_its_hrid_alone(world: World) -> None:
    made = entities.create(
        world.users["editor"], "acme/protein.Sample", world.project, {"label": "S1"}
    )
    sample = Entity.objects.get(pk=made.entity.pk)
    found = group(searched(Api(world.browser("editor")), q=sample.hrid), "entities")["items"]
    assert [(r["title"], r["detail"], r["href"]) for r in found] == [
        (sample.hrid, "Sample", f"/entities/{sample.hrid}/")
    ]


def test_people_are_those_whose_members_pages_the_caller_sees(world: World, foreign: World) -> None:
    api = Api(world.browser("editor"))
    body = searched(api, q="example.com", kind="people", limit=50)
    named = {r["title"] for r in body["groups"][0]["items"]}
    # Colleagues in the editor's projects, by grant or by team; not the foreign world's, not
    # the deactivated, and not people in no project of theirs.
    assert {"Viewer", "Admin", "Via Team", "Mixed", "Editor"} <= named
    assert not {n for n in named if n.startswith("foreign-")}
    assert "Inactive" not in named
    assert "Outsider" not in named
    assert body["groups"][0]["href"] is None
    viewer = next(r for r in body["groups"][0]["items"] if r["title"] == "Viewer")
    assert viewer["detail"] == "viewer@example.com"
    assert viewer["href"] == f"/projects/{world.project.slug}/members/"
    assert [p["name"] for p in viewer["projects"]] == [world.project.name, world.archived.name]
    # A teammate's link is their team's page.
    TeamMembership.objects.create(team=world.team, user=world.users["editor"])
    permissions.forget(world.users["editor"])
    body = searched(api, q="team member", kind="people")
    assert [(r["title"], r["href"]) for r in body["groups"][0]["items"]] == [
        ("Team Member", f"/teams/{world.team.slug}/")
    ]


def test_those_who_manage_people_find_everyone(world: World, foreign: World) -> None:
    body = searched(Api(world.browser("org_admin")), q="outsider", kind="people")
    found = body["groups"][0]
    assert found["href"] == "/admin/people/"
    assert {(r["title"], r["href"]) for r in found["items"]} == {
        ("foreign-Outsider", f"/admin/people/{foreign.users['outsider'].pk}/"),
        ("Outsider", f"/admin/people/{world.users['outsider'].pk}/"),
    }
    body = searched(Api(world.browser("org_admin")), q="inactive", kind="people")
    assert "Deactivated" in body["groups"][0]["items"][-1]["detail"]


def test_saved_views_are_your_own_and_those_shared(world: World) -> None:
    editor, viewer = world.users["editor"], world.users["viewer"]
    saved_searches.create(editor, world.project, name="Kinases mine", query={"q": "kinase"})
    saved_searches.create(
        editor, world.project, name="Kinases shared", query={"q": "kinase"}, shared=True
    )
    saved_searches.create(viewer, world.project, name="Kinases viewer's", query={"q": "kinase"})
    body = searched(Api(world.browser("editor")), q="kinases", kind="saved_views")
    assert titles(body, "saved_views") == ["Kinases mine", "Kinases shared"]
    body = searched(Api(world.browser("viewer")), q="kinases", kind="saved_views")
    assert titles(body, "saved_views") == ["Kinases shared", "Kinases viewer's"]


def test_a_view_the_grid_cant_show_opens_from_its_own_address(world: World) -> None:
    """Saved with the API, an exact type alone or either of two conditions can't open in the
    grid. Search opens each where the sidebar does, the page that says so, never the
    project's grid without its conditions (ADR 0176)."""
    editor = world.users["editor"]
    exact = saved_searches.create(
        editor,
        world.project,
        name="Kinases, this type only",
        query={"type": "acme/protein.Protein", "exact_type": True},
    )
    either = saved_searches.create(
        editor,
        world.project,
        name="Kinases, either name",
        query={
            "type": "acme/protein.Protein",
            "where": {
                "any": [
                    {"field": "name", "op": "eq", "value": "Aurora kinase A"},
                    {"field": "name", "op": "eq", "value": "Aurora kinase B"},
                ]
            },
        },
    )
    body = searched(Api(world.browser("editor")), q="kinases", kind="saved_views")
    assert [(v["title"], v["href"]) for v in group(body, "saved_views")["items"]] == [
        ("Kinases, either name", f"/saved-searches/{either.pk}/"),
        ("Kinases, this type only", f"/saved-searches/{exact.pk}/"),
    ]


def test_a_view_of_every_project_is_its_owners_and_opens_in_all_entities(world: World) -> None:
    """A saved search with no project (ADR 0176) searches every project its owner reads, and
    is theirs alone: search finds it for them, in All entities, and for no one else."""
    editor = world.users["editor"]
    saved_searches.create(editor, None, name="Kinases everywhere", query={"q": "kinase"})
    body = searched(Api(world.browser("editor")), q="kinases", kind="saved_views")
    [view] = group(body, "saved_views")["items"]
    assert (view["title"], view["detail"], view["href"], view["projects"]) == (
        "Kinases everywhere",
        "Yours, across your projects",
        f"/saved-searches/{view['id']}/",
        [],
    )
    body = searched(Api(world.browser("viewer")), q="kinases", kind="saved_views")
    assert titles(body, "saved_views") == []
    assert group(body, "saved_views")["total"] == 0


def test_limited_tokens_find_the_same_saved_views_in_search_and_the_list(world: World) -> None:
    editor = world.users["editor"]
    saved_searches.create(editor, None, name="Kinases everywhere", query={"q": "kinase"})
    saved_searches.create(editor, world.project, name="Kinases here", query={"q": "kinase"})
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    token = tokens["access_token"]
    listed = bearer(token, "GET", "/saved-searches").json()
    found = bearer(token, "GET", "/search?q=kinases&kind=saved_views").json()
    assert [s["name"] for s in listed] == titles(found, "saved_views") == ["Kinases here"]
    assert group(found, "saved_views")["total"] == 1


def test_with_no_text_recent_is_what_you_changed_or_ran(world: World, second: Project) -> None:
    editor = world.users["editor"]
    older = protein(editor, world.project, "Older")
    theirs = protein(world.users["admin"], world.project, "The admin's")
    run = run_on(editor, world.project, older)
    newer = protein(editor, second, "Newer")
    api = Api(world.browser("editor"))
    body = searched(api, q="")
    assert body["groups"] == []
    assert body["searched_records"] is False
    assert [(r["kind"], r["hrid"]) for r in body["recent"]] == [
        ("entity", newer.hrid),
        ("run", run.hrid),
        ("entity", older.hrid),
    ]
    assert theirs.hrid not in json.dumps(body)
    # What they can't read any more isn't recent for them.
    ProjectMembership.objects.filter(project=second, user=editor).delete()
    permissions.forget(editor)
    assert newer.hrid not in [r["hrid"] for r in searched(api, q="   ")["recent"]]


def test_searching_needs_signing_in_and_answers_tokens_for_their_projects(world: World) -> None:
    assert Api().get("/search", q="lysozyme").status_code == 401
    assert Api(world.browser("inactive")).get("/search", q="x").status_code == 401
    too_long = Api(world.browser("editor")).get("/search", q="x" * 201)
    assert too_long.status_code == 422
    unknown = Api(world.browser("editor")).get("/search", q="x", kind="plasmids")
    assert unknown.status_code == 422


def test_a_kind_too_slow_to_search_says_so_and_the_rest_still_answer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count that would be slow is left out, not estimated (ADR 0176)."""
    from iki_server.entities import search

    lysozyme_everywhere(world)

    def slow(*_: Any) -> Any:
        raise search.TooSlow

    monkeypatch.setattr(everything, "_entities", slow)
    body = searched(Api(world.browser("editor")), q="lysozyme")
    assert group(body, "entities")["total"] is None
    assert group(body, "entities")["items"] == []
    assert titles(body, "saved_views") == ["Lysozyme work"]
