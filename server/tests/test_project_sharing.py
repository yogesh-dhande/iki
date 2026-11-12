"""A project can keep its entities to itself (ADR 0067): no one adds them to another project,
refers to them or its files from one, or runs on them there. To anyone working elsewhere,
they're as if unreadable: registering the same thing makes a separate entity, and nothing
says one exists."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as, create
from test_runs import launch, protein, world

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import ProjectLink
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole

__all__ = ["world"]

pytestmark = pytest.mark.django_db


@pytest.fixture
def cro(world: World) -> Project:
    """A partner's project, kept to itself, that the editor edits too."""
    project = Project.objects.create(team=world.team, name="Oncology CRO", allow_sharing=False)
    ProjectMembership.objects.create(
        project=project, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    return project


def not_shared(entity: dict[str, Any]) -> str:
    return (
        f"{entity['hrid']} can't be shared outside Oncology CRO: its project doesn't allow "
        "sharing its entities with other projects."
    )


def test_admins_allow_or_restrict_sharing_audited(world: World) -> None:
    admin = api_as(world, "admin")
    path = f"/projects/{world.project.pk}"
    assert admin.get(path).json()["allow_sharing"] is True

    restricted = admin.patch(path, {"allow_sharing": False})

    assert restricted.status_code == 200, restricted.content
    assert restricted.json()["allow_sharing"] is False
    assert admin.patch(path, {"allow_sharing": False}).status_code == 200  # no change
    assert admin.patch(path, {"allow_sharing": True}).json()["allow_sharing"] is True
    events = AuditEvent.objects.filter(project=world.project, action__startswith="project.sharing")
    assert list(events.order_by("pk").values_list("action", flat=True)) == [
        "project.sharing_restricted",
        "project.sharing_allowed",
    ]
    problem(api_as(world, "editor").patch(path, {"allow_sharing": False}), 403, "forbidden")


def test_an_archived_project_can_still_be_restricted(world: World) -> None:
    changed = api_as(world, "admin").patch(
        f"/projects/{world.archived.pk}", {"allow_sharing": False}
    )
    assert changed.status_code == 200, changed.content
    assert changed.json()["allow_sharing"] is False and changed.json()["archived"] is True


def test_its_entities_cant_be_added_to_another_project(world: World, cro: Project) -> None:
    editor = api_as(world)
    theirs = protein(editor, cro, "src")

    body = problem(
        editor.put(f"/entities/{theirs['id']}/projects/{world.project.pk}"), 409, "refused"
    )

    assert body["detail"] == not_shared(theirs)
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()


def test_the_same_thing_registered_elsewhere_is_a_separate_entity(
    world: World, cro: Project
) -> None:
    editor = api_as(world)
    theirs = protein(editor, cro, "src")

    ours = protein(editor, world.project, "src")

    assert ours["id"] != theirs["id"] and not ours["reused"]
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()
    # In the CRO's own project, it's still the one it has.
    again = create(editor, cro, {"sequence": "MKVSRC"}, "protein.Protein")
    assert again.status_code == 200 and again.json()["id"] == theirs["id"]


def test_entities_shared_before_stay_shared(world: World, cro: Project) -> None:
    editor = api_as(world)
    cro.allow_sharing = True
    cro.save()
    theirs = protein(editor, cro, "src")
    linked = editor.put(f"/entities/{theirs['id']}/projects/{world.project.pk}")
    assert linked.status_code == 201, linked.content
    cro.allow_sharing = False
    cro.save()

    assert editor.get(f"/entities/{theirs['id']}").status_code == 200
    again = create(editor, world.project, {"sequence": "MKVSRC"}, "protein.Protein")
    assert again.status_code == 200 and again.json()["id"] == theirs["id"]
    ran = launch(editor, world.project, {"protein": theirs["id"]})
    assert ran.status_code == 201, ran.content
    # Taken out, it can't come back.
    assert editor.delete(f"/entities/{theirs['id']}/projects/{world.project.pk}").status_code == 204
    problem(editor.put(f"/entities/{theirs['id']}/projects/{world.project.pk}"), 409, "refused")


def test_values_elsewhere_cant_refer_to_its_entities_or_files(world: World, cro: Project) -> None:
    editor = api_as(world)
    theirs = protein(editor, cro, "src")
    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=cro)

    made = create(
        editor,
        world.project,
        {"structure": str(pdb.pk), "chains": [theirs["id"]]},
        "protein.ProteinStructure",
    )

    body = problem(made, 422, "validation_failed")
    details = {e["pointer"]: e["detail"] for e in body["errors"]}
    assert details["#/values/structure"] == (
        "refers to a file in Oncology CRO, which doesn't allow sharing with other projects."
    )
    assert details["#/values/chains"] == (
        "item 1 refers to an entity in Oncology CRO, which doesn't allow sharing its entities with "
        "other projects."
    )
    # In its own project, they may.
    own = create(
        editor,
        cro,
        {"structure": str(pdb.pk), "chains": [theirs["id"]]},
        "protein.ProteinStructure",
    )
    assert own.status_code == 201, own.content


def test_runs_elsewhere_cant_read_its_entities(world: World, cro: Project) -> None:
    editor = api_as(world)
    theirs = protein(editor, cro, "src")

    refused = launch(editor, world.project, {"protein": theirs["id"]}, add_to_project=True)

    body = problem(refused, 422, "validation_failed")
    assert body["errors"] == [{"pointer": "#/inputs/protein", "detail": not_shared(theirs)}]
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()
    assert launch(editor, cro, {"protein": theirs["id"]}).status_code == 201
