"""Views (core.View, ADR 0086): a saved look at an entity in one of Iki's viewers, at the
revision it showed."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as, create, patch

from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

CAMERA = {"position": [0, 0, 50], "target": [0, 0, 0], "up": [0, 1, 0], "fov": 0.78}


@pytest.fixture
def world() -> World:
    return build_world()


def protein(api: Api, world: World) -> Any:
    made = create(api, world.project, {"sequence": "MKVLAAGIVG", "name": "ABL1"}, "protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def view(api: Api, world: World, target: str, **values: Any) -> Any:
    body = {
        "name": "ABL1, start to finish",
        "target": target,
        "target_revision": 1,
        "viewer": "sequence",
        "state": {"version": 1},
        **values,
    }
    return create(api, world.project, body, "core.View")


def errors(response: Any) -> dict[str, str]:
    body = problem(response, 422, "validation_failed")
    return {e["pointer"].removeprefix("#/values/"): e["detail"] for e in body["errors"]}


def test_a_view_pins_an_entity_at_a_revision_in_a_viewer(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    feature = {"type": "domain", "name": "SH3", "start": 2, "end": 8}
    made = view(editor, world, abl["id"], state={"version": 1, "feature": feature})
    assert made.status_code == 201, made.content
    saved = made.json()
    assert saved["hrid"].startswith("VIEW-") and saved["title"] == "ABL1, start to finish"
    read = api_as(world, "viewer").get(f"/entities/{saved['hrid']}").json()
    assert read["values"]["target"] == abl["id"]
    assert read["values"]["state"] == {"version": 1, "feature": feature}
    assert read["refs"][abl["id"]]["hrid"] == abl["hrid"]


def test_it_shows_a_revision_its_entity_has(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    assert errors(view(editor, world, abl["id"], target_revision=2)) == {
        "target_revision": f"{abl['hrid']} has no revision 2."
    }
    # Saving it again on a later revision is an edit, checked the same way.
    made = view(editor, world, abl["id"]).json()
    current = editor.get(f"/entities/{made['hrid']}")
    moved = patch(editor, made["hrid"], {"values": {"target_revision": 5}}, current["ETag"])
    assert errors(moved) == {"target_revision": f"{abl['hrid']} has no revision 5."}


def test_it_shows_a_viewer_its_entitys_type_has_with_that_viewers_state(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    assert errors(view(editor, world, abl["id"], viewer="structure")) == {
        "viewer": f"{abl['hrid']}’s type has no structure viewer."
    }
    camera = {"version": 1, "camera": CAMERA}
    assert errors(view(editor, world, abl["id"], state=camera)) == {
        "state": "The sequence viewer doesn’t keep `camera`."
    }
    unknown = view(editor, world, abl["id"], state={"version": 2})
    assert problem(unknown, 422, "validation_failed")["errors"][0]["pointer"].startswith(
        "#/values/state"
    )


def test_it_shows_only_what_its_maker_can_read(world: World) -> None:
    """Someone who can't read an entity can't learn its revisions by saving a view of it."""
    abl = protein(api_as(world), world)
    theirs = Project.objects.create(team=world.team, name="Theirs")
    ProjectMembership.objects.create(
        project=theirs, user=world.users["outsider"], role=ProjectRole.EDITOR
    )
    body = {"target": abl["id"], "target_revision": 9, "viewer": "sequence"}
    made = create(
        api_as(world, "outsider"),
        theirs,
        {"name": "Peek", "state": {"version": 1}, **body},
        "core.View",
    )
    found = errors(made)
    assert set(found) == {"target"} and "9" not in found["target"]


def test_its_entity_and_viewer_dont_change(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    other = create(
        editor, world.project, {"sequence": "MKKKKKKKK", "name": "Other"}, "protein.Protein"
    ).json()
    made = view(editor, world, abl["id"]).json()
    current = editor.get(f"/entities/{made['hrid']}")
    moved = patch(editor, made["hrid"], {"values": {"target": other["id"]}}, current["ETag"])
    assert "target" in errors(moved)
