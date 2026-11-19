"""Views (iki/core.View, ADR 0086, ADR 0110): a saved look at entities in Iki's viewers, each at
the revision it showed."""

from typing import Any

import pytest
from api_client import Api, problem
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
    made = create(api, world.project, {"sequence": sequence, "name": name}, "acme/protein.Protein")
    assert made.status_code == 201, made.content
    return made.json()


def structure(world: World, name: str, digit: str) -> Any:
    file = add_file(world.project, name=f"{name}.pdb", digit=digit)
    made = services.create(
        world.users["editor"],
        "acme/protein.ProteinStructure",
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
    return create(api, world.project, body, "iki/core.View")


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
    # It holds the registry entry (ADR 0115), and pins the project's record of it.
    assert read["values"]["targets"] == [abl["entry"]["id"]]
    assert read["values"]["target_revisions"] == [1]
    assert read["values"]["viewers"] == ["sequence"]
    assert read["values"]["state"] == {"version": 1, "feature": feature}
    assert read["refs"][abl["entry"]["id"]]["hrid"] == abl["hrid"]


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
        api_as(world, "outsider"),
        theirs,
        {"sequence": "MKKK", "name": "Mine"},
        "acme/protein.Protein",
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
        "iki/core.View",
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
    made = create(editor, world.project, superposed(apo, holo), "iki/core.View")
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
    assert create(editor, world.project, beside, "iki/core.View").status_code == 201


def test_several_entities_each_have_a_revision_a_viewer_and_state(world: World) -> None:
    editor = api_as(world)
    apo, holo = two_structures(world)
    values = superposed(apo, holo)
    assert errors(
        create(editor, world.project, {**values, "target_revisions": [1]}, "iki/core.View")
    ) == {
        "target_revisions": "has 1 for 2 entities: give one for each, in the same order as "
        "`targets`."
    }
    revisions = {**values, "target_revisions": [1, 4]}
    assert errors(create(editor, world.project, revisions, "iki/core.View")) == {
        "target_revisions": f"{holo.hrid} has no revision 4."
    }
    twice = {**values, "targets": [str(apo.pk), str(apo.pk)]}
    assert errors(create(editor, world.project, twice, "iki/core.View")) == {
        "viewers": f"It shows {apo.hrid} in the structure viewer twice."
    }
    one = {**values, "state": {"version": 2, "targets": [{}]}}
    assert errors(create(editor, world.project, one, "iki/core.View")) == {
        "state": "`targets` has 1 for 2 entities: give one for each, in the same order."
    }
    legacy = {**values, "state": {"version": 1, "camera": CAMERA}}
    assert errors(create(editor, world.project, legacy, "iki/core.View")) == {
        "state": "A view of several entities keeps their state as version 2, in `targets`."
    }
    onto = superposed(apo, holo)
    onto["state"]["targets"][1]["superposed"] = {"onto": 1}
    assert errors(create(editor, world.project, onto, "iki/core.View")) == {
        "state": f"{holo.hrid} is superposed on entity 2, which isn’t another entity in the "
        "structure viewer."
    }
    abl = protein(editor, world)
    mixed = {
        **values,
        "targets": [str(apo.pk), abl["id"]],
        "viewers": ["structure", "sequence"],
    }
    assert errors(create(editor, world.project, mixed, "iki/core.View")) == {
        "state": "The sequence viewer doesn’t keep `colour`, `selection`, `superposed`, "
        f"`transform` for {abl['hrid']}."
    }
    too_many = {**values, "targets": [str(apo.pk)] * 21}
    assert "targets" in errors(create(editor, world.project, too_many, "iki/core.View"))


def test_a_selection_keeps_residues_insertion_codes(world: World) -> None:
    """35, 35A and 35B are three residues (wwPDB's pdbx_PDB_ins_code), as in Kabat's
    numbering of a long CDR-H1: a view keeps 35A alone, and a range runs in their order."""
    editor = api_as(world)
    apo, holo = two_structures(world)

    def selecting(*spans: dict[str, Any]) -> dict[str, Any]:
        values = superposed(apo, holo)
        values["state"]["targets"][1]["selection"] = list(spans)
        return values

    only = {"chain": "H", "start": 35, "start_insertion": "A", "end": 35, "end_insertion": "A"}
    made = create(editor, world.project, selecting(only), "iki/core.View")
    assert made.status_code == 201, made.content
    read = editor.get(f"/entities/{made.json()['hrid']}").json()
    assert read["values"]["state"]["targets"][1]["selection"] == [only]
    onward = {"chain": "H", "start": 35, "end": 35, "end_insertion": "B"}
    assert create(editor, world.project, selecting(onward), "iki/core.View").status_code == 201
    backward = {"chain": "H", "start": 35, "start_insertion": "B", "end": 35, "end_insertion": "A"}
    assert errors(create(editor, world.project, selecting(backward), "iki/core.View")) == {
        "state": f"{holo.hrid}’s selection has a range that ends before it starts."
    }
    two = {**only, "start_insertion": "AB"}
    assert "state" in errors(create(editor, world.project, selecting(two), "iki/core.View"))


def test_only_someone_who_can_read_every_entity_it_shows_reads_a_view(world: World) -> None:
    """A view of what someone can't read doesn't exist for them: not its page, not in a
    search, not as what refers to an entity they can read (ADR 0110)."""
    editor, viewer = api_as(world), api_as(world, "viewer")
    abl = protein(editor, world)
    elsewhere = Project.objects.create(team=world.team, name="Antigens")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    # Another project's structure: a type without an identity, which a reference may name
    # where the editor reads it (one with an identity names an entry the project has).
    file = add_file(elsewhere, name="her2.pdb", digit="9")
    antigen = {
        "id": str(
            services.create(
                world.users["editor"],
                "acme/protein.ProteinStructure",
                elsewhere,
                {"structure": file.pk},
            ).entity.pk
        )
    }
    both = {
        "name": "ABL1 with HER2",
        "targets": [abl["id"], antigen["id"]],
        "target_revisions": [1, 1],
        "viewers": ["sequence", "structure"],
        "state": {"version": 2, "targets": [{}, {"label": "antigen"}]},
    }
    made = create(editor, world.project, both, "iki/core.View")
    assert made.status_code == 201, made.content
    hrid = made.json()["hrid"]
    assert editor.get(f"/entities/{hrid}").status_code == 200
    # The viewer reads ABL1 and the view's project, but not HER2's structure.
    assert viewer.get(f"/entities/{abl['hrid']}").status_code == 200
    assert problem(viewer.get(f"/entities/{hrid}"), 404, "not_found")
    assert viewer.get(f"/entities/{made.json()['id']}/revisions").status_code == 404
    search = {
        "type": "iki/core.View",
        "where": {"field": "targets", "op": "has", "value": abl["id"]},
    }
    assert viewer.post("/entities/search", search).json()["items"] == []
    assert [e["hrid"] for e in editor.post("/entities/search", search).json()["items"]] == [hrid]
    listed = viewer.get("/entities", type="iki/core.View", project=str(world.project.pk)).json()
    assert listed["items"] == []
    # Once they can read HER2, the view is theirs to read too.
    ProjectLink.objects.create(entity_id=antigen["id"], project=world.project)
    assert viewer.get(f"/entities/{hrid}").status_code == 200
    assert [e["hrid"] for e in viewer.post("/entities/search", search).json()["items"]] == [hrid]
