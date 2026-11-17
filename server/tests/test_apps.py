"""Apps (iki/core.App, ADR 0090): pages of Iki's own components, each panel bound to those
before it by expressions. The server checks a spec on every save, as the person saving
it, and says which panel and prop is wrong."""

import json
from typing import Any

import pytest
from api_client import Api, problem
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as, create, patch

from iki_server.entities import app_specs
from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = [pytest.mark.django_db, pytest.mark.wasm]

GRID = {
    "id": "proteins",
    "title": "Proteins",
    "width": 6,
    "component": "iki.entities",
    "props": {
        "type": "acme/protein.Protein",
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
    return create(api, project, {"name": "Kinases", "spec": spec}, "iki/core.App")


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
        "Panel `proteins`, `columns.0.value`: `row` offers `id`, `hrid`, `type`, `title`, "
        "`revision` and `values`, not `secret`.",
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
        editor, world.project, {"sequence": "MKVLAAGIVG", "name": "ABL1"}, "acme/protein.Protein"
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
    assert problems(app(editor, world.project, with_props(GRID, type="acme/protein.Nope"))) == [
        "Panel `proteins`, `type`: there’s no entity type acme/protein.Nope."
    ]
    bad = with_props(
        GRID,
        where={"all": [{"field": "weight", "op": "eq", "value": 1}]},
        columns=[{"field": "colour"}],
    )
    found = problems(app(editor, world.project, bad))
    assert found[0].startswith("Panel `proteins`, `where.all.0.field`:")
    assert (
        found[1] == "Panel `proteins`, `columns.0.field`: acme/protein.Protein has no field colour."
    )


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
        api_as(world), world.project, {"sequence": "MKV", "name": "ABL1"}, "acme/protein.Protein"
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
    # Its example searches Iki's iki/protein; here, the fixture plugin acme/protein stands in.
    panels = json.loads(
        json.dumps(guide["example"]["panels"]).replace("iki/protein.", "acme/protein.")
    )
    made = app(api_as(world), world.project, *panels)
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
    assert not api.get("/entities", params={"type": "iki/core.App"}).json().get("items")


ABOUT = {"type": "acme/protein.Protein"}
SAME_ORGANISM = {
    "id": "kin",
    "title": "From the same organism",
    "component": "iki.entities",
    "props": {
        "type": "acme/protein.Protein",
        "where": {
            "all": [
                {"field": "organism", "op": "eq", "value": "{{ entity.values.organism }}"},
                {"not": {"field": "name", "op": "eq", "value": "{{ entity.values.name }}"}},
            ]
        },
        "columns": [{"field": "name"}],
    },
}
ITSELF = {
    "id": "itself",
    "component": "iki.viewer",
    "props": {"entity": "{{ entity }}", "viewer": "sequence"},
}


def about(api: Api, project: Project, *panels: dict[str, Any], name: str = "Kin") -> Any:
    spec = {"version": 1, "entity": ABOUT, "panels": list(panels)}
    return create(api, project, {"name": name, "spec": spec}, "iki/core.App")


def test_an_app_about_an_entity_reads_it_and_its_grids_conditions_can_too(
    world: World,
) -> None:
    editor = api_as(world)
    assert about(editor, world.project, ITSELF, SAME_ORGANISM).status_code == 201
    check = editor.post
    # Without saying what it's about, there's no `entity` to read.
    spec = {"version": 1, "panels": [ITSELF]}
    assert check("/apps/check", {"spec": spec}).json()["problems"] == [
        "Panel `itself`, `entity`: `entity` isn’t a panel before it."
    ]
    # A condition's value is one expression, read as the others are.
    texty = {"field": "name", "op": "starts_with", "value": "x{{ entity.hrid }}"}
    reads = {"field": "nope", "op": "eq", "value": "{{ entity.secret }}"}
    grid = with_props(SAME_ORGANISM, where={"any": [texty, reads]})
    spec = {"version": 1, "entity": {"type": "acme/protein.Nope"}, "panels": [grid]}
    found = check("/apps/check", {"spec": spec}).json()["problems"]
    assert found[0] == "`entity.type`: there’s no entity type acme/protein.Nope."
    assert found[1].startswith("Panel `kin`, `where.any.1.field`:")
    assert found[2:] == [
        "Panel `kin`, `where.any.0.value`: a condition’s value is one expression between "
        "`{{` and `}}` and nothing else, or no expression.",
        "Panel `kin`, `where.any.1.value`: `entity` offers `id`, `hrid`, `type`, `title`, "
        "`revision` and `values`, not `secret`.",
    ]
    # A grid's conditions can read the panels before it, too.
    picked = with_props(
        SAME_ORGANISM,
        where={"field": "name", "op": "eq", "value": "{{ proteins.selection[0].values.name }}"},
    )
    spec = {"version": 1, "panels": [GRID, picked]}
    assert check("/apps/check", {"spec": spec}).json()["problems"] == []
    # `entity` is the entity, not a panel.
    named = {**TEXT, "id": "entity"}
    spec = {"version": 1, "entity": ABOUT, "panels": [named]}
    assert check("/apps/check", {"spec": spec}).json()["problems"] == [
        "Panel `entity`: the app reads the entity it’s about as `entity`, so a panel can’t "
        "have that ID.",
        "Panel `entity`, `text`: `proteins` isn’t the entity the app is about or a panel "
        "before it.",
    ]


def test_an_entitys_page_lists_the_apps_about_it_in_its_projects(world: World) -> None:
    """An app is a tab on the entities of its own projects, for those who can read it."""
    editor = api_as(world)
    abl = create(editor, world.project, {"sequence": "MKV", "name": "ABL1"}, "acme/protein.Protein")
    abl_hrid = abl.json()["hrid"]
    ours = about(editor, world.project, ITSELF, name="Variants").json()["hrid"]
    dna = {"version": 1, "entity": {"type": "acme/dna.DNASequence"}, "panels": [TEXT]}
    create(editor, world.project, {"name": "Plasmid", "spec": dna}, "iki/core.App")
    plain = app(editor, world.project, GRID).json()["hrid"]
    assert plain
    gone = about(editor, world.project, ITSELF, name="Old").json()["hrid"]
    Entity.objects.filter(hrid=gone).update(archived_at=timezone.now())
    # Another team's project that the protein is also in, and one it isn't in.
    theirs = Project.objects.create(team=world.team, name="Theirs")
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    for project in (theirs, elsewhere):
        ProjectMembership.objects.create(
            project=project, user=world.users["outsider"], role=ProjectRole.EDITOR
        )
        ProjectMembership.objects.create(
            project=project, user=world.users["editor"], role=ProjectRole.EDITOR
        )
    linked = editor.put(f"/entities/{abl_hrid}/projects/{theirs.pk}")
    assert linked.status_code == 201, linked.content
    outsider = api_as(world, "outsider")
    their_app = about(outsider, theirs, ITSELF, name="Their notes").json()["hrid"]
    about(outsider, elsewhere, ITSELF, name="Not here")

    def tabs(principal: str) -> list[tuple[str, str]]:
        found = api_as(world, principal).get(f"/entities/{abl_hrid}/apps")
        assert found.status_code == 200, found.content
        return [(a["hrid"], a["name"]) for a in found.json()]

    assert tabs("viewer") == [(ours, "Variants")]
    assert tabs("outsider") == [(their_app, "Their notes")]
    assert tabs("editor") == [(their_app, "Their notes"), (ours, "Variants")]
    # Someone who can't read the entity can't learn its apps.
    assert api_as(world, "auditor").get(f"/entities/{abl_hrid}/apps").status_code == 404


NOTE = {"id": "note", "component": "iki.text", "props": {"text": "Hello."}}


def test_a_projects_admins_choose_the_app_its_page_opens_with(world: World) -> None:
    """Its dashboard: one of its apps that isn't about an entity, for everyone who reads
    it, while it's still there to read. Choosing and clearing it are recorded."""
    from iki_server.audit.models import AuditEvent

    editor, admin, viewer = (api_as(world, p) for p in ("editor", "admin", "viewer"))
    path = f"/projects/{world.project.pk}/dashboard"
    assert viewer.get(path).json() == {"app": None, "set_by": None, "set_at": None}
    first = app(editor, world.project, GRID, VIEWER, TEXT).json()["hrid"]
    second = create(
        editor,
        world.project,
        {"name": "Atlas", "spec": {"version": 1, "panels": [NOTE]}},
        "iki/core.App",
    ).json()["hrid"]
    tab = about(editor, world.project, ITSELF, name="Variants").json()["hrid"]
    old = app(editor, world.project, NOTE).json()["hrid"]
    Entity.objects.filter(hrid=old).update(archived_at=timezone.now())
    protein = create(editor, world.project, {"sequence": "MKV"}, "acme/protein.Protein").json()[
        "hrid"
    ]
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    theirs = app(api_as(world, "admin"), elsewhere, NOTE).json()["hrid"]

    # Its Admins choose among the project's apps that could be one.
    choices = admin.get(f"{path}/choices")
    assert [a["hrid"] for a in choices.json()] == [second, first]
    assert viewer.get(f"{path}/choices").status_code == 403
    assert viewer.put(path, {"app": first}).status_code == 403

    def refused(ref: str) -> str:
        body = problem(admin.put(path, {"app": ref}), 409, "refused")
        detail: str = body["errors"][0]["detail"]
        return detail

    assert refused(tab) == (
        f"{tab} is about one entity at a time, so it’s a tab on their pages, not a dashboard."
    )
    assert refused(old) == f"{old} is archived. Restore it first."
    assert refused(theirs) == f"{theirs} isn’t in {world.project.name}. Add it first."
    assert refused(protein) == f"There’s no app {protein} that you can read."
    assert refused("APP-999999") == "There’s no app APP-999999 that you can read."

    chosen = admin.put(path, {"app": first.lower()})
    assert chosen.status_code == 200, chosen.content
    assert chosen.json()["app"]["hrid"] == first
    assert chosen.json()["set_by"]["id"] == str(world.users["admin"].pk)
    shown = viewer.get(path).json()
    assert shown["app"] == {"id": shown["app"]["id"], "hrid": first, "name": "Kinases"}
    # Choosing it again changes nothing; choosing another says what it replaced.
    admin.put(path, {"app": first})
    admin.put(path, {"app": second})
    events = AuditEvent.objects.filter(action__startswith="project.dashboard").order_by("pk")
    assert [(e.action, e.data) for e in events] == [
        ("project.dashboard_set", {"app": [None, first]}),
        ("project.dashboard_set", {"app": [first, second]}),
    ]

    # Archived, it's no longer shown, though it's still chosen.
    Entity.objects.filter(hrid=second).update(archived_at=timezone.now())
    assert viewer.get(path).json()["app"] is None
    assert admin.get(path).json()["set_by"] is not None

    assert admin.delete(path).status_code == 204
    assert admin.get(path).json()["set_by"] is None
    assert events.last().data == {"app": [second, None]}  # type: ignore[union-attr]
    assert admin.delete(path).status_code == 204
    assert events.all().count() == 3

    # An archived project's dashboard can't change until it's restored.
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    body = problem(admin.put(path, {"app": first}), 409, "refused")
    assert "Restore the project before changing it." in str(body)


def test_a_draft_is_for_its_person_to_look_at_for_a_day(world: World) -> None:
    """An app to look at before saving it: checked as saving it would be, opened only by
    the person it's for, while they can read its project, for a day."""
    from datetime import timedelta

    from iki_server.entities import app_drafts
    from iki_server.entities.models import AppDraft

    editor, viewer = api_as(world), api_as(world, "viewer")
    spec = {"version": 1, "panels": [NOTE]}
    body = {"project": str(world.project.pk), "name": " Greeting ", "spec": spec}
    made = editor.post("/apps/drafts", body)
    assert made.status_code == 201, made.content
    draft = made.json()
    assert draft["name"] == "Greeting" and draft["spec"] == spec
    assert draft["project"]["slug"] == world.project.slug
    assert draft["entity"] is None and draft["agent"] is None
    assert editor.get(f"/apps/drafts/{draft['id']}").json()["id"] == draft["id"]
    # No one else can open it, nor draft without editing the project.
    assert viewer.get(f"/apps/drafts/{draft['id']}").status_code == 404
    assert viewer.post("/apps/drafts", body).status_code == 403
    # A spec with problems isn't drafted.
    wrong = {**body, "spec": {"version": 1, "panels": [TEXT]}}
    found = problem(editor.post("/apps/drafts", wrong), 422, "validation_failed")
    assert [e["pointer"] for e in found["errors"]] == ["#/spec"]
    assert "`proteins` isn’t" in found["errors"][0]["detail"]

    # An app about an entity is drafted on one of its type in the project.
    abl = create(editor, world.project, {"sequence": "MKV", "name": "ABL1"}, "acme/protein.Protein")
    abl_hrid = abl.json()["hrid"]
    tab = {**body, "spec": {"version": 1, "entity": ABOUT, "panels": [ITSELF]}}

    def refused(payload: dict[str, Any]) -> str:
        detail: str = problem(editor.post("/apps/drafts", payload), 409, "refused")["detail"]
        return detail

    assert refused(tab) == "This app is about one acme/protein.Protein: say which to show it on."
    assert refused({**body, "entity": abl_hrid}) == (
        "This app isn’t about an entity, so it takes none."
    )
    plasmid = create(editor, world.project, {"bases": "ACGT"}, "acme/dna.DNASequence").json()[
        "hrid"
    ]
    assert refused({**tab, "entity": plasmid}) == f"{plasmid} isn’t a acme/protein.Protein."
    assert refused({**tab, "entity": "PRT-999999"}) == (
        f"There’s no entity PRT-999999 in {world.project.name} that you can read."
    )
    on = editor.post("/apps/drafts", {**tab, "entity": abl_hrid.lower()})
    assert on.status_code == 201, on.content
    assert on.json()["entity"]["hrid"] == abl_hrid

    # It lasts a day, and only while its person can read its project.
    AppDraft.objects.filter(pk=draft["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert editor.get(f"/apps/drafts/{draft['id']}").status_code == 404
    assert app_drafts.prune() == 1
    ProjectMembership.objects.filter(project=world.project, user=world.users["editor"]).delete()
    assert editor.get(f"/apps/drafts/{on.json()['id']}").status_code == 404


def test_a_person_keeps_their_latest_drafts(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.entities import app_drafts
    from iki_server.entities.models import AppDraft

    monkeypatch.setattr(app_drafts, "MAX_PER_PERSON", 3)
    editor = api_as(world)
    body = {"project": str(world.project.pk), "spec": {"version": 1, "panels": [NOTE]}}
    made = [editor.post("/apps/drafts", {**body, "name": f"D{i}"}).json()["id"] for i in range(5)]
    kept = AppDraft.objects.filter(created_by=world.users["editor"]).order_by("created_at")
    assert [str(d.pk) for d in kept] == made[2:]
    # An archived project takes no drafts, as it takes no other changes.
    Project.objects.filter(pk=world.project.pk).update(archived_at=timezone.now())
    assert editor.post("/apps/drafts", {**body, "name": "Late"}).status_code == 403


# --- What a spec could try (ADR 0090) ------------------------------------------------------


def test_a_spec_cant_get_round_its_checks(world: World) -> None:
    """Someone who can edit a project writes a spec to reach past what its readers may
    see, or to slow Iki down. Each is refused, saying where."""
    import time

    editor = api_as(world)

    def check(*panels: dict[str, Any], **top: Any) -> list[str]:
        spec = {"version": 1, "panels": list(panels), **top}
        found: list[str] = editor.post("/apps/check", {"spec": spec}).json()["problems"]
        return found

    # Keys the schema doesn't have, prototype ones included, go nowhere.
    assert check(with_props(NOTE, __proto__={"admin": True}))
    assert check({**NOTE, "constructor": {}})
    assert check(NOTE, owner="someone")
    # A search's field is a field of the type, never an expression.
    sneaky = with_props(GRID, where={"field": "{{ proteins.rows }}", "op": "eq", "value": "x"})
    assert any("where" in p for p in check(sneaky))
    # A condition's value is a value, not an operator for the database.
    raw = with_props(GRID, where={"field": "name", "op": "eq", "value": {"$gt": ""}})
    assert check(raw)
    # A column is a field of the type, or an expression: not another table's column.
    assert any("columns" in p for p in check(with_props(GRID, columns=[{"field": "password"}])))
    # Expressions are bounded, and checking them stays quick however they're written.
    started = time.monotonic()
    deep = "{{ " + "[" * 200 + "]" * 200 + " }}"
    long = "{{ " + " + ".join(["1"] * 600) + " }}"
    # Each says which, quoting no more than the start of it.
    assert check(with_props(NOTE, text=deep)) == [
        f"Panel `note`, `text`: In `{{{{{deep[2:62]}…}}}}`: It nests more than 32 deep."
    ]
    assert check(with_props(NOTE, text=long)) == [
        f"Panel `note`, `text`: In `{{{{{long[2:62]}…}}}}`: It's over 1000 characters, "
        "the most an expression may be."
    ]
    expression = "{{ " + "1 + " * 200 + "1 }}"
    many = [{**NOTE, "id": f"n{i}", "props": {"text": expression}} for i in range(25)]
    assert check(*many) == ["At `panels`: It has more than 24 items."]
    assert time.monotonic() - started < 10
    # An app reads only what's offered: not the person, the token or the page.
    for name in ("user", "request", "window", "document"):
        assert check(with_props(NOTE, text=f"{{{{ {name} }}}}")) == [
            f"Panel `note`, `text`: `{name}` isn’t a panel before it."
        ]
