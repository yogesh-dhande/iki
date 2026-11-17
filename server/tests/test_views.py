"""Views (core.View, ADR 0086, ADR 0110): a saved look at entities in Iki's viewers, each at
the revision it showed."""

from typing import Any

import pytest
from api_client import Api, problem
from conftest import allow_test_flush
from django.db import connection
from org_world import World, build_world
from test_api_entities import api_as, create, patch
from test_file_fields import add_file

from iki_server.entities import services
from iki_server.entities.models import Entity, ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

CAMERA = {"position": [0, 0, 50], "target": [0, 0, 0], "up": [0, 1, 0], "fov": 0.78}
#: Moved 5 Å along x, as Mol* keeps a transform: a 4-by-4 matrix in column-major order.
MOVED = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 0, 0, 1]


@pytest.fixture
def world() -> World:
    return build_world()


def protein(api: Api, world: World, name: str = "ABL1", sequence: str = "MKVLAAGIVG") -> Any:
    made = create(api, world.project, {"sequence": sequence, "name": name}, "protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def structure(world: World, name: str, digit: str) -> Any:
    file = add_file(world.project, name=f"{name}.pdb", digit=digit)
    made = services.create(
        world.users["editor"],
        "protein.ProteinStructure",
        world.project,
        {"structure": file.pk},
    )
    return Entity.objects.get(pk=made.entity.pk)


def view(api: Api, world: World, target: str, **values: Any) -> Any:
    body = {
        "name": "ABL1, start to finish",
        "targets": [target],
        "target_revisions": [1],
        "viewers": ["sequence"],
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
    assert read["values"]["targets"] == [abl["id"]]
    assert read["values"]["target_revisions"] == [1]
    assert read["values"]["viewers"] == ["sequence"]
    assert read["values"]["state"] == {"version": 1, "feature": feature}
    assert read["refs"][abl["id"]]["hrid"] == abl["hrid"]


def test_it_shows_a_revision_its_entity_has(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    assert errors(view(editor, world, abl["id"], target_revisions=[2])) == {
        "target_revisions": f"{abl['hrid']} has no revision 2."
    }
    # Saving it again on a later revision is an edit, checked the same way.
    made = view(editor, world, abl["id"]).json()
    current = editor.get(f"/entities/{made['hrid']}")
    moved = patch(editor, made["hrid"], {"values": {"target_revisions": [5]}}, current["ETag"])
    assert errors(moved) == {"target_revisions": f"{abl['hrid']} has no revision 5."}


def test_it_shows_a_viewer_its_entitys_type_has_with_that_viewers_state(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    assert errors(view(editor, world, abl["id"], viewers=["structure"])) == {
        "viewers": f"{abl['hrid']}’s type has no structure viewer."
    }
    camera = {"version": 1, "camera": CAMERA}
    assert errors(view(editor, world, abl["id"], state=camera)) == {
        "state": "The sequence viewer doesn’t keep `camera`."
    }
    unknown = view(editor, world, abl["id"], state={"version": 3})
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
    mine = create(
        api_as(world, "outsider"), theirs, {"sequence": "MKKK", "name": "Mine"}, "protein.Protein"
    ).json()
    body = {
        "targets": [mine["id"], abl["id"]],
        "target_revisions": [1, 9],
        "viewers": ["sequence", "sequence"],
    }
    made = create(
        api_as(world, "outsider"),
        theirs,
        {"name": "Peek", "state": {"version": 2, "targets": [{}, {}]}, **body},
        "core.View",
    )
    found = errors(made)
    assert set(found) == {"targets"} and "9" not in found["targets"]
    assert abl["hrid"] not in found["targets"]


def test_its_entities_and_viewers_dont_change(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world)
    other = protein(editor, world, "Other", "MKKKKKKKK")
    made = view(editor, world, abl["id"]).json()
    current = editor.get(f"/entities/{made['hrid']}")
    moved = patch(editor, made["hrid"], {"values": {"targets": [other["id"]]}}, current["ETag"])
    assert "targets" in errors(moved)
    moved = patch(editor, made["hrid"], {"values": {"viewers": ["structure"]}}, current["ETag"])
    assert "viewers" in errors(moved)


# --- Several entities ---------------------------------------------------------------------------


def two_structures(world: World) -> tuple[Entity, Entity]:
    return structure(world, "apo", "a"), structure(world, "holo", "b")


def superposed(apo: Entity, holo: Entity, **state: Any) -> dict[str, Any]:
    return {
        "name": "Apo and holo, superposed",
        "targets": [str(apo.pk), str(holo.pk)],
        "target_revisions": [1, 1],
        "viewers": ["structure", "structure"],
        "state": {
            "version": 2,
            "camera": CAMERA,
            "targets": [
                {"label": "apo", "colour": "#1f77b4"},
                {
                    "label": "holo",
                    "colour": "#ff7f0e",
                    "transform": MOVED,
                    "superposed": {"onto": 0, "rmsd": 0.42, "residues": 280},
                    "selection": [{"chain": "A", "start": 10, "end": 20}],
                },
            ],
            **state,
        },
    }


def test_a_view_shows_several_entities_each_at_its_revision(world: World) -> None:
    """Two structures superposed in one Mol* scene, each its own, as the view saved them."""
    editor = api_as(world)
    apo, holo = two_structures(world)
    made = create(editor, world.project, superposed(apo, holo), "core.View")
    assert made.status_code == 201, made.content
    read = api_as(world, "viewer").get(f"/entities/{made.json()['hrid']}").json()
    assert read["values"]["targets"] == [str(apo.pk), str(holo.pk)]
    assert read["values"]["viewers"] == ["structure", "structure"]
    assert read["values"]["state"]["targets"][1]["transform"] == MOVED
    assert {read["refs"][str(e.pk)]["hrid"] for e in (apo, holo)} == {apo.hrid, holo.hrid}
    # A protein's sequence beside its structure: each entity in a viewer of its own.
    abl = protein(editor, world)
    beside = {
        "name": "ABL1 beside its structure",
        "targets": [str(apo.pk), abl["id"]],
        "target_revisions": [1, 1],
        "viewers": ["structure", "sequence"],
        "state": {
            "version": 2,
            "targets": [{}, {"feature": {"type": "domain", "start": 2, "end": 8}}],
        },
    }
    assert create(editor, world.project, beside, "core.View").status_code == 201


def test_several_entities_each_have_a_revision_a_viewer_and_state(world: World) -> None:
    editor = api_as(world)
    apo, holo = two_structures(world)
    values = superposed(apo, holo)
    assert errors(
        create(editor, world.project, {**values, "target_revisions": [1]}, "core.View")
    ) == {
        "target_revisions": "has 1 for 2 entities: give one for each, in the same order as "
        "`targets`."
    }
    revisions = {**values, "target_revisions": [1, 4]}
    assert errors(create(editor, world.project, revisions, "core.View")) == {
        "target_revisions": f"{holo.hrid} has no revision 4."
    }
    twice = {**values, "targets": [str(apo.pk), str(apo.pk)]}
    assert errors(create(editor, world.project, twice, "core.View")) == {
        "viewers": f"It shows {apo.hrid} in the structure viewer twice."
    }
    one = {**values, "state": {"version": 2, "targets": [{}]}}
    assert errors(create(editor, world.project, one, "core.View")) == {
        "state": "`targets` has 1 for 2 entities: give one for each, in the same order."
    }
    legacy = {**values, "state": {"version": 1, "camera": CAMERA}}
    assert errors(create(editor, world.project, legacy, "core.View")) == {
        "state": "A view of several entities keeps their state as version 2, in `targets`."
    }
    onto = superposed(apo, holo)
    onto["state"]["targets"][1]["superposed"] = {"onto": 1}
    assert errors(create(editor, world.project, onto, "core.View")) == {
        "state": f"{holo.hrid} is superposed on entity 2, which isn’t another entity in the "
        "structure viewer."
    }
    abl = protein(editor, world)
    mixed = {
        **values,
        "targets": [str(apo.pk), abl["id"]],
        "viewers": ["structure", "sequence"],
    }
    assert errors(create(editor, world.project, mixed, "core.View")) == {
        "state": "The sequence viewer doesn’t keep `colour`, `selection`, `superposed`, "
        f"`transform` for {abl['hrid']}."
    }
    too_many = {**values, "targets": [str(apo.pk)] * 21}
    assert "targets" in errors(create(editor, world.project, too_many, "core.View"))


def test_only_someone_who_can_read_every_entity_it_shows_reads_a_view(world: World) -> None:
    """A view of what someone can't read doesn't exist for them: not its page, not in a
    search, not as what refers to an entity they can read (ADR 0110)."""
    editor, viewer = api_as(world), api_as(world, "viewer")
    abl = protein(editor, world)
    elsewhere = Project.objects.create(team=world.team, name="Antigens")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    antigen = create(
        editor, elsewhere, {"sequence": "MKTAYIAKQR", "name": "HER2"}, "protein.Protein"
    ).json()
    both = {
        "name": "ABL1 with HER2",
        "targets": [abl["id"], antigen["id"]],
        "target_revisions": [1, 1],
        "viewers": ["sequence", "sequence"],
        "state": {"version": 2, "targets": [{}, {"label": "antigen"}]},
    }
    made = create(editor, world.project, both, "core.View")
    assert made.status_code == 201, made.content
    hrid = made.json()["hrid"]
    assert editor.get(f"/entities/{hrid}").status_code == 200
    # The viewer reads ABL1 and the view's project, but not HER2's.
    assert viewer.get(f"/entities/{abl['hrid']}").status_code == 200
    assert problem(viewer.get(f"/entities/{hrid}"), 404, "not_found")
    assert viewer.get(f"/entities/{made.json()['id']}/revisions").status_code == 404
    search = {"type": "core.View", "where": {"field": "targets", "op": "has", "value": abl["id"]}}
    assert viewer.post("/entities/search", search).json()["items"] == []
    assert [e["hrid"] for e in editor.post("/entities/search", search).json()["items"]] == [hrid]
    listed = viewer.get("/entities", type="core.View", project=str(world.project.pk)).json()
    assert listed["items"] == []
    # Once they can read HER2, the view is theirs to read too.
    ProjectLink.objects.create(entity_id=antigen["id"], project=world.project)
    assert viewer.get(f"/entities/{hrid}").status_code == 200
    assert [e["hrid"] for e in viewer.post("/entities/search", search).json()["items"]] == [hrid]


@pytest.mark.django_db(transaction=True)
def test_a_view_of_one_entity_from_before_is_a_view_of_a_list_of_one(world: World) -> None:
    """The migration that made views of several: each view's entity, revision and viewer
    become the first of its lists, at every revision of its history, and rolling back
    keeps the first (ADR 0110)."""
    from django.db.migrations.executor import MigrationExecutor

    from iki_server.plugins import sync

    abl = Entity.objects.get(pk=protein(api_as(world), world)["id"])
    with connection.cursor() as cursor:
        sync.drop_views(cursor)
    before = [("core", "0005_app_history_app")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        made = old.get_model("core", "View").objects.create(
            type="core.View",
            hrid="VIEW-000123",
            owner_project_id=world.project.pk,
            revision=2,
            name="ABL1, SH3",
            target_id=abl.pk,
            target_revision=1,
            viewer="sequence",
            state={"version": 1, "feature": {"type": "domain", "start": 2, "end": 8}},
        )
        history = old.get_model("core", "View_History")
        for revision in (1, 2):
            history.objects.create(
                entity_id=made.pk,
                revision=revision,
                name="ABL1, SH3",
                target=abl.pk,
                target_revision=1,
                viewer="sequence",
                state={"version": 1},
            )
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        (values,) = services.current_values_of([Entity.objects.get(pk=made.pk)]).values()
        assert values["targets"] == [abl.pk]
        assert values["target_revisions"] == [1] and values["viewers"] == ["sequence"]
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT revision, target_id, targets, target_revisions, viewers "
                "FROM core_view__history WHERE entity_id = %s ORDER BY revision",
                [made.pk],
            )
            rows = cursor.fetchall()
        assert rows == [(n, abl.pk, [abl.pk], [1], ["sequence"]) for n in (1, 2)]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_id, target_revision, viewer FROM core_view WHERE entity_id = %s",
                [made.pk],
            )
            assert cursor.fetchone() == (abl.pk, 1, "sequence")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        sync.sync()
        allow_test_flush()
