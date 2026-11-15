"""Apps (core.App, ADR 0090): pages of Iki's own components, each panel bound to those
before it by expressions. The server checks a spec on every save, as the person saving
it, and says which panel and prop is wrong."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as, create, patch

from iki_server.entities import app_specs
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = [pytest.mark.django_db, pytest.mark.wasm]

GRID = {
    "id": "proteins",
    "title": "Proteins",
    "width": 6,
    "component": "iki.entities",
    "props": {
        "type": "protein.Protein",
        "where": {"all": [{"field": "name", "op": "starts_with", "value": "ABL"}]},
        "sort": "name",
        "columns": [
            {"field": "name"},
            {"label": "Length", "value": "{{ size(row.values.sequence) }} aa"},
        ],
    },
}
VIEWER = {
    "id": "shown",
    "width": 6,
    "component": "iki.viewer",
    "props": {"entity": "{{ proteins.selection }}", "viewer": "sequence"},
}
TEXT = {
    "id": "summary",
    "component": "iki.text",
    "props": {"text": "{{ size(proteins.selection) }} of {{ size(proteins.rows) }} picked."},
}


@pytest.fixture
def world() -> World:
    return build_world()


def app(api: Api, project: Project, *panels: dict[str, Any]) -> Any:
    spec = {"version": 1, "panels": list(panels)}
    return create(api, project, {"name": "Kinases", "spec": spec}, "core.App")


def problems(response: Any) -> list[str]:
    """The spec's problems, each a sentence or two: the API joins a field's."""
    body = problem(response, 422, "validation_failed")
    assert [e["pointer"] for e in body["errors"]] == ["#/values/spec"]
    detail: str = body["errors"][0]["detail"]
    return [p if p.startswith(("Panel", "Its")) else f"Panel {p}" for p in detail.split(" Panel ")]


def with_props(panel: dict[str, Any], **props: Any) -> dict[str, Any]:
    return {**panel, "props": {**panel["props"], **props}}


def test_an_app_lays_out_panels_that_read_the_ones_before(world: World) -> None:
    editor = api_as(world)
    made = app(editor, world.project, GRID, VIEWER, TEXT)
    assert made.status_code == 201, made.content
    saved = made.json()
    assert saved["hrid"].startswith("APP-") and saved["title"] == "Kinases"
    read = api_as(world, "viewer").get(f"/entities/{saved['hrid']}").json()
    assert [p["id"] for p in read["values"]["spec"]["panels"]] == ["proteins", "shown", "summary"]


def test_its_spec_fits_its_schema_and_says_where_it_doesnt(world: World) -> None:
    editor = api_as(world)
    wide = app(editor, world.project, {**GRID, "width": 13})
    body = problem(wide, 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/values/spec"
    assert body["errors"][0]["detail"].startswith("doesn't fit its schema at `panels.0.width`")
    unknown = app(editor, world.project, with_props(TEXT, colour="red"))
    detail = problem(unknown, 422, "validation_failed")["errors"][0]["detail"]
    assert "panels.0.props" in detail and "colour" in detail
    script = app(editor, world.project, {**TEXT, "component": "iki.script"})
    assert "panels.0.component" in problem(script, 422, "validation_failed")["errors"][0]["detail"]


def test_a_panel_reads_only_panels_before_it_and_what_they_offer(world: World) -> None:
    editor = api_as(world)
    # The viewer reads the grid, which comes after it.
    assert problems(app(editor, world.project, VIEWER, GRID)) == [
        "Panel `shown`, `entity`: `proteins` isn’t a panel before it."
    ]
    # A panel can't read itself, nor anything but panels.
    looped = with_props(TEXT, text="{{ summary }} and {{ secrets.key }}")
    assert problems(app(editor, world.project, looped)) == [
        "Panel `summary`, `text`: `secrets` isn’t a panel before it.",
        "Panel `summary`, `text`: `summary` isn’t a panel before it.",
    ]
    typo = with_props(TEXT, text="{{ proteins.selected }}")
    assert problems(app(editor, world.project, GRID, typo)) == [
        "Panel `summary`, `text`: `proteins` offers `rows` and `selection`, not `selected`."
    ]
    # A column reads its row, and only what a row has.
    column = {"label": "Bad", "value": "{{ row.secret }} {{ other }}"}
    grid = with_props(GRID, columns=[column])
    assert problems(app(editor, world.project, grid)) == [
        "Panel `proteins`, `columns.0.value`: `other` isn’t its row or a panel before it.",
        "Panel `proteins`, `columns.0.value`: `row` offers `id` and `hrid` and `type` and "
        "`title` and `revision` and `values`, not `secret`.",
    ]
    # Only a column has a row.
    rowed = with_props(TEXT, text="{{ row.title }}")
    assert problems(app(editor, world.project, rowed)) == [
        "Panel `summary`, `text`: `row` isn’t a panel before it."
    ]


def test_each_expression_reads_and_says_why_not(world: World) -> None:
    editor = api_as(world)
    broken = with_props(TEXT, text="Picked {{ size(proteins.selection) + }}")
    assert problems(app(editor, world.project, GRID, broken)) == [
        "Panel `summary`, `text`: In `{{ size(proteins.selection) + }}`: It ends too soon, "
        "at line 1, column 29."
    ]
    unclosed = with_props(TEXT, text="Picked {{ size(proteins.selection)")
    assert problems(app(editor, world.project, GRID, unclosed)) == [
        "Panel `summary`, `text`: The `{{` at character 8 isn’t closed.".replace("’", "'")
    ]


def test_a_viewer_shows_one_entity(world: World) -> None:
    editor = api_as(world)
    made = create(
        editor, world.project, {"sequence": "MKVLAAGIVG", "name": "ABL1"}, "protein.Protein"
    )
    abl = made.json()["hrid"]
    by_hrid = with_props(VIEWER, entity=abl)
    assert app(editor, world.project, by_hrid).status_code == 201
    assert problems(app(editor, world.project, with_props(by_hrid, viewer="structure"))) == [
        f"Panel `shown`, `viewer`: {abl}’s type has no structure viewer."
    ]
    assert problems(app(editor, world.project, with_props(VIEWER, entity="PROT-999999"))) == [
        "Panel `shown`, `entity`: PROT-999999 is no entity you can read."
    ]
    # An entity is one expression, not text with one in it.
    texty = with_props(VIEWER, entity="The {{ proteins.selection }}")
    assert problems(app(editor, world.project, GRID, texty)) == [
        "Panel `shown`, `entity`: an entity is an HRID, or one expression between `{{` and "
        "`}}` and nothing else."
    ]


def test_a_grid_searches_a_type_with_its_fields(world: World) -> None:
    editor = api_as(world)
    assert problems(app(editor, world.project, with_props(GRID, type="protein.Nope"))) == [
        "Panel `proteins`, `type`: there’s no entity type protein.Nope."
    ]
    bad = with_props(
        GRID,
        where={"all": [{"field": "weight", "op": "eq", "value": 1}]},
        columns=[{"field": "colour"}],
    )
    found = problems(app(editor, world.project, bad))
    assert found[0].startswith("Panel `proteins`, `where.all.0.field`:")
    assert found[1] == "Panel `proteins`, `columns.0.field`: protein.Protein has no field colour."


def test_its_ids_are_unique(world: World) -> None:
    editor = api_as(world)
    plain = with_props(TEXT, text="Kinases we're working on.")
    assert problems(app(editor, world.project, plain, plain)) == [
        "Panel `summary`: another panel before it has that ID."
    ]


def test_its_text_is_bounded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_specs, "MAX_TEXT", 100)
    long = with_props(TEXT, text="{{ 1 }}" + "x" * 100)
    assert problems(app(api_as(world), world.project, long)) == [
        "Its text and expressions are 107 characters; an app may have 100."
    ]


def test_it_checks_as_the_person_saving_it(world: World) -> None:
    """Someone who can't read an entity can't show it in an app, nor learn it exists."""
    made = create(
        api_as(world), world.project, {"sequence": "MKV", "name": "ABL1"}, "protein.Protein"
    )
    abl = made.json()["hrid"]
    theirs = Project.objects.create(team=world.team, name="Theirs")
    ProjectMembership.objects.create(
        project=theirs, user=world.users["outsider"], role=ProjectRole.EDITOR
    )
    peek = app(api_as(world, "outsider"), theirs, with_props(VIEWER, entity=abl))
    assert problems(peek) == [f"Panel `shown`, `entity`: {abl} is no entity you can read."]


def test_an_edit_checks_the_new_spec(world: World) -> None:
    editor = api_as(world)
    saved = app(editor, world.project, GRID, TEXT).json()
    current = editor.get(f"/entities/{saved['hrid']}")
    spec = {"version": 1, "panels": [TEXT, GRID]}
    moved = patch(editor, saved["hrid"], {"values": {"spec": spec}}, current["ETag"])
    assert problems(moved) == [
        "Panel `summary`, `text`: `proteins` isn’t a panel before it.",
    ]


def test_how_to_write_one_and_its_example_saves(world: World) -> None:
    api = api_as(world, "viewer")
    guide = api.get("/apps/guide").json()
    assert {c["name"]: set(c["offers"]) for c in guide["components"]} == {
        "iki.entities": {"rows", "selection"},
        "iki.viewer": {"entity"},
        "iki.text": set(),
    }
    assert "{{" in guide["expressions"]
    made = app(api_as(world), world.project, *guide["example"]["panels"])
    assert made.status_code == 201, made.content


def test_checking_one_says_every_problem_and_saves_nothing(world: World) -> None:
    api = api_as(world, "viewer")
    spec = {
        "version": 2,
        "panels": [{**GRID, "width": 0}, {**TEXT, "component": "iki.script"}],
    }
    assert api.post("/apps/check", {"spec": spec}).json()["problems"] == [
        "At `panels.0.width`: 0 is less than the minimum of 1.",
        "At `panels.1.component`: 'iki.script' is not one of "
        "['iki.entities', 'iki.viewer', 'iki.text'].",
        "At `version`: 1 was expected.",
    ]
    spec = {"version": 1, "panels": [TEXT, GRID]}
    assert api.post("/apps/check", {"spec": spec}).json()["problems"] == [
        "Panel `summary`, `text`: `proteins` isn’t a panel before it."
    ]
    spec = {"version": 1, "panels": [GRID, VIEWER, TEXT]}
    assert api.post("/apps/check", {"spec": spec}).json() == {"problems": []}
    assert not api.get("/entities", params={"type": "core.App"}).json().get("items")
