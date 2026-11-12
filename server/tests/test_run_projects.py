"""A run reads only what's in its project (ADR 0066): its inputs are linked into it, and its
file params are its files. Inputs from other projects are refused, naming them, unless the
launch adds them to the project first, which is audited as a link."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as
from test_run_imports import IMPORT, stored
from test_run_results import done, output, weight
from test_runs import launch, launch_each, protein, world
from test_workers import keep_connection, worker

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import ProjectLink
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs.models import ActionRun

__all__ = ["keep_connection", "world"]

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


def elsewhere(world: World, name: str = "Lab B") -> Project:
    """Another project the editor edits too."""
    other = Project.objects.create(team=world.team, name=name)
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    return other


def outside(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = problem(response, 409, "inputs_outside_project")
    return body


def test_an_input_from_another_project_is_refused_naming_it(world: World) -> None:
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")

    body = outside(launch(editor, world.project, {"protein": theirs["id"]}))

    name = world.project.name
    assert body["detail"] == (
        f"{theirs['hrid']} isn't in {name}. A run reads only what's in its project: add it to "
        f"{name} first, and its members will be able to read it."
    )
    assert body["entities"] == [
        {"id": theirs["id"], "hrid": theirs["hrid"], "type": "protein.Protein"}
    ]
    assert not ActionRun.objects.exists()
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()


def test_adding_them_to_the_project_links_them_audited_and_runs(world: World) -> None:
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")

    made = launch(editor, world.project, {"protein": theirs["id"]}, add_to_project=True)

    assert made.status_code == 201, made.content
    assert made.json()["status"] == "queued"
    assert ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()
    linked = AuditEvent.objects.get(action="entity.linked", project=world.project)
    assert str(linked.target_id) == theirs["id"]
    # Now in the project, it runs there without asking again.
    again = launch(editor, world.project, {"protein": theirs["id"]}, reuse=False)
    assert again.status_code == 201, again.content


def test_a_batch_lists_every_input_outside_and_queues_none(world: World) -> None:
    editor = api_as(world, "editor")
    other = elsewhere(world)
    ours = protein(editor, world.project, "abl1")
    theirs = [protein(editor, other, name) for name in ("src", "lck")]

    ids = [ours["id"], *(p["id"] for p in theirs)]
    body = outside(launch_each(editor, world.project, ids))

    assert [e["hrid"] for e in body["entities"]] == [p["hrid"] for p in theirs]
    assert body["detail"].startswith(f"{theirs[0]['hrid']} and {theirs[1]['hrid']} aren't in ")
    assert not ActionRun.objects.exists()
    made = launch_each(editor, world.project, ids, add_to_project=True)
    assert made.status_code == 201, made.content
    assert len(made.json()["runs"]) == 3
    assert AuditEvent.objects.filter(action="entity.linked", project=world.project).count() == 2


def test_other_problems_are_said_first(world: World) -> None:
    """Adding inputs to a project is offered only for a launch that's otherwise fine."""
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")

    bad = launch(editor, world.project, {"protein": theirs["id"], "nope": theirs["id"]})

    problem(bad, 422, "validation_failed")


def test_a_file_from_another_project_is_refused(world: World) -> None:
    editor = api_as(world, "editor")
    file_id = stored(world, project=elsewhere(world))

    bad = launch(editor, world.project, {}, IMPORT, params={"file": file_id})

    body = problem(bad, 422, "validation_failed")
    assert body["errors"] == [
        {
            "pointer": "#/params/file",
            "detail": f"That file isn't in {world.project.name}: a run reads only what's in its "
            "project. Upload it there, or choose one of its files.",
        }
    ]
    ok = launch(editor, world.project, {}, IMPORT, params={"file": stored(world)})
    assert ok.status_code == 201, ok.content


def test_a_run_whose_input_left_the_project_cant_be_reproduced(world: World) -> None:
    editor = api_as(world, "editor")
    other = elsewhere(world)
    theirs = protein(editor, other, "src")
    first = launch(editor, world.project, {"protein": theirs["id"]}, add_to_project=True).json()
    pod = worker(world)
    job = pod.lease().json()
    assert done(pod, job, output(updates=weight(first)))["run_status"] == "succeeded"
    unlinked = editor.delete(f"/entities/{theirs['id']}/projects/{world.project.pk}")
    assert unlinked.status_code == 204, unlinked.content

    refused = editor.post(f"/runs/{first['hrid']}/reproduce", {"reason": "Audit"})

    body = problem(refused, 409, "refused")
    assert body["detail"] == (
        f"{theirs['hrid']} isn't in {world.project.name} any more, so this run can't be "
        "reproduced there: a run reads only what's in its project."
    )
