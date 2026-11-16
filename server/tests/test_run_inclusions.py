"""Included references (ADR 0105): an action's input can include the entities its references
name, such as a structure's chains. A run reads them as of launch, as it reads its inputs:
pinned at their revisions, checked readable and in its project, kept with the run, and
counted in what makes two runs identical."""

from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World
from test_api_entities import api_as, create, patch
from test_bundle_imports import PUBLIC, applied, bundle_of, upload
from test_run_projects import elsewhere, outside
from test_run_results import RELAX, _stored, done, output, world
from test_run_reuse import pod as built_pod
from test_runs import launch, protein
from test_workers import keep_connection, worker

from iki_server.bundles import reading
from iki_server.entities.models import Entity, ProjectLink
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.runs import services
from iki_server.runs.models import ActionRun, RunInclusion
from iki_spec import schemas

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]

SHA = "a" * 40


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


def structure(world: World, *chains: dict[str, Any]) -> dict[str, Any]:
    values = {
        "structure": _stored(world, "abl.pdb"),
        "method": "xray",
        "chains": [c["id"] for c in chains],
    }
    made = create(api_as(world, "editor"), world.project, values, "protein.ProteinStructure")
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def relax(world: World, of: dict[str, Any], **kw: Any) -> Any:
    return launch(api_as(world, "editor"), world.project, {"structure": of["id"]}, RELAX, **kw)


def test_a_run_reads_what_its_inputs_references_name_as_of_launch(world: World) -> None:
    editor = api_as(world, "editor")
    abl, src = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    pst = structure(world, abl, src)

    made = relax(world, pst)

    assert made.status_code == 201, made.content
    run = made.json()
    assert [(i["field"], i["index"], i["hrid"], i["revision"]) for i in run["included"]] == [
        ("chains", 0, abl["hrid"], 1),
        ("chains", 1, src["hrid"], 1),
    ]
    assert {(i["input"], i["position"]) for i in run["included"]} == {("structure", 0)}
    # A change after launch isn't what the run reads.
    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content
    job = worker(world).lease().json()
    assert not list(schemas.check("run-input", job))
    (given,) = job["inputs"]["structure"]
    chains = given["included"]["chains"]
    assert [(c["hrid"], c["revision"], c["values"]["name"]) for c in chains] == [
        (abl["hrid"], 1, None),
        (src["hrid"], 1, None),
    ]
    assert chains[1]["values"]["sequence"] == src["values"]["sequence"]
    assert chains[0]["type"] == "protein.Protein"


def test_an_input_that_includes_nothing_has_no_included(world: World) -> None:
    editor = api_as(world, "editor")
    made = launch(editor, world.project, {"protein": protein(editor, world.project)["id"]})
    assert made.status_code == 201, made.content
    assert made.json()["included"] == []
    job = worker(world).lease().json()
    assert "included" not in job["inputs"]["protein"][0]


def test_a_reference_from_another_project_is_refused_unless_added(world: World) -> None:
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")
    pst = structure(world, theirs)

    body = outside(relax(world, pst))

    assert body["entities"] == [
        {"id": theirs["id"], "hrid": theirs["hrid"], "type": "protein.Protein"}
    ]
    assert not ActionRun.objects.exists()
    made = relax(world, pst, add_to_project=True)
    assert made.status_code == 201, made.content
    assert ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()
    assert [i["hrid"] for i in made.json()["included"]] == [theirs["hrid"]]


def test_a_reference_the_launcher_cant_read_is_refused(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    # Purged since: it's gone, and only its tombstone is left.
    Entity.objects.filter(pk=abl["id"]).update(purged_at="2026-01-01T00:00:00Z")

    body = problem(relax(world, pst), 422, "validation_failed")

    assert body["errors"] == [
        {
            "pointer": "#/inputs/structure",
            "detail": f"{pst['hrid']}'s chains isn't an entity you can read.",
        }
    ]
    assert not ActionRun.objects.exists()


def test_a_change_to_what_was_included_runs_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    first = relax(world, pst).json()
    pod = built_pod(world)
    job = pod.lease().json()
    values = {**job["inputs"]["structure"][0]["values"]}
    made = {"type": "protein.ProteinStructure", "values": values}
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"

    same = relax(world, pst).json()
    assert same["reused"] is True and same["reused_from"]["hrid"] == first["hrid"]
    assert [i["hrid"] for i in same["included"]] == [abl["hrid"]]

    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content
    again = relax(world, pst).json()
    assert again["reused"] is False and again["status"] == "queued"
    assert [(i["hrid"], i["revision"]) for i in again["included"]] == [(abl["hrid"], 2)]


def test_reproducing_reads_what_the_run_read(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    pst = structure(world, abl)
    first = relax(world, pst).json()
    pod = built_pod(world)
    job = pod.lease().json()
    values = {**job["inputs"]["structure"][0]["values"]}
    made = {"type": "protein.ProteinStructure", "values": values}
    assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
    changed = patch(editor, abl["id"], {"values": {"name": "Renamed"}}, '"1"')
    assert changed.status_code == 200, changed.content

    again = editor.post(f"/runs/{first['hrid']}/reproduce", {"reason": "Audit"})

    assert again.status_code == 201, again.content
    assert [(i["hrid"], i["revision"]) for i in again.json()["included"]] == [(abl["hrid"], 1)]
    job = pod.lease().json()
    (given,) = job["inputs"]["structure"]
    assert given["included"]["chains"][0]["values"]["name"] is None
    # One the person can't read any more isn't read for them.
    ProjectLink.objects.filter(entity_id=abl["id"]).delete()
    assert editor.post(f"/runs/{first['hrid']}/reproduce", {"reason": "x"}).status_code == 403


def test_a_bundle_keeps_what_its_runs_read_through_references(
    world: World, tmp_path: Path, settings: Any
) -> None:
    settings.IKI_BUNDLE_TRUSTED_KEYS = {"test": (PUBLIC,)}
    OrgRoleAssignment.objects.create(user=world.users["admin"], role=OrgRole.ORG_ADMIN)
    editor = api_as(world, "editor")
    kept: dict[str, Any] = {}

    def elsewhere_() -> list[Any]:
        abl = protein(editor, world.project)
        pst = structure(world, abl)
        run = relax(world, pst).json()
        pod = worker(world)
        job = pod.lease().json()
        values = {**job["inputs"]["structure"][0]["values"]}
        made = {"type": "protein.ProteinStructure", "values": values}
        assert done(pod, job, output(creates=[made]))["run_status"] == "succeeded"
        kept.update(run=run, abl=abl)
        relaxed = ActionRun.objects.get(pk=run["id"]).effects.get().entity_id
        return [Entity.objects.get(pk=relaxed)]

    path = bundle_of(world, tmp_path, elsewhere_)
    (line,) = reading.read(path, {}, limit=1024**3).lines("runs.ndjson")
    abl = kept["abl"]
    assert line["included"] == [
        {
            "input": "structure",
            "position": 0,
            "field": "chains",
            "index": 0,
            "entity": abl["id"],
            "revision": 1,
        }
    ]
    api = api_as(world, "admin")
    browser = Api(world.browser("admin"))
    made = upload(browser, world.project, path)
    assert made["state"] == "planned" and made["plan"]["applicable"], made
    assert applied(browser, made)["state"] == "applied"
    inclusion = RunInclusion.objects.get(run_id=line["id"])
    assert (str(inclusion.entity_id), inclusion.revision) == (abl["id"], 1)
    shown = api.get(f"/runs/{line['id']}").json()
    assert [i["hrid"] for i in shown["included"]] == [abl["hrid"]]
    assert shown["imported"]["included_elsewhere"] == []


def test_each_run_of_a_batch_reads_its_own_entitys_references(world: World) -> None:
    editor = api_as(world, "editor")
    abl, src = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    first, second = structure(world, abl), structure(world, src)

    made = editor.post(
        f"/actions/{RELAX}/runs/batch",
        {
            "project": str(world.project.pk),
            "each": "structure",
            "entities": [first["id"], second["id"]],
        },
    )

    assert made.status_code == 201, made.content
    runs = made.json()["runs"]
    read = [[(i["position"], i["hrid"]) for i in r["included"]] for r in runs]
    assert read == [[(0, abl["hrid"])], [(0, src["hrid"])]]


def test_a_workflow_checks_what_its_steps_include_when_it_starts(world: World) -> None:
    editor = api_as(world, "editor")
    theirs = protein(editor, elsewhere(world), "src")
    pst = structure(world, theirs)

    started = editor.post(
        "/workflows/protein.relax_twice/runs",
        {"project": str(world.project.pk), "inputs": {"structure": pst["id"]}},
    )

    body = outside(started)
    assert [e["hrid"] for e in body["entities"]] == [theirs["hrid"]]
