"""Actions and runs: the catalog, launching with every check, reading and cancelling."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_api_entities import api_as, create

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun, Job, JobState, RunInput

pytestmark = pytest.mark.django_db

COMPUTE = "protein.compute_properties"
PREDICT = "structure_prediction.predict_structure"

TEMPLATE: dict[str, Any] = {
    "label": "",
    "description": None,
    "gpu_model": None,
    "kubernetes": {},
    "aws": {},
    "volumes": {},
    "max_concurrent": None,
    "min_warm": 0,
    "idle_shutdown": None,
    "price_per_hour": None,
    "currency": None,
}


def _capacity(cpu: float, memory_gi: int, gpus: int = 0) -> dict[str, Any]:
    return {
        "cpu": cpu,
        "memory": memory_gi * 1024**3,
        "gpu_count": gpus,
        "gpu_memory": 40 * 1024**3 if gpus else None,
        "scratch_disk": 1024**4 if gpus else None,
    }


@pytest.fixture
def world() -> Iterator[World]:
    """The tests' deployment (iki_test_compute.json) runs protein-tools on backend pods."""
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


def protein(api: Api, project: Project, name: str = "abl1") -> Any:
    made = create(
        api, project, {"sequence": "MKV" + name.upper().replace("1", "A")}, "protein.Protein"
    )
    assert made.status_code == 201, made.content
    return made.json()


def launch(
    api: Api, project: Project, inputs: dict[str, Any], action: str = COMPUTE, **kw: Any
) -> Any:
    return api.post(f"/actions/{action}/runs", {"project": str(project.pk), "inputs": inputs, **kw})


# --- The catalog -------------------------------------------------------------------------------


def test_lists_installed_actions_and_where_they_run(world: World) -> None:
    actions = api_as(world, "viewer").get("/actions").json()
    assert [a["name"] for a in actions] == [COMPUTE, PREDICT]
    compute, predict = actions
    assert compute["tier"] == "backend"
    assert compute["available"] is True
    assert compute["needs"] == "Editor"
    assert compute["inputs"] == [
        {
            "name": "protein",
            "label": "Protein",
            "description": None,
            "type": "protein.Protein",
            "cardinality": "one",
            "max": None,
            "optional": False,
        }
    ]
    assert compute["updates"] == [{"input": "protein", "fields": ["molecular_weight"]}]
    assert predict["available"] is False
    assert predict["needs"] == "Runner"
    assert (
        "no vm-tier machines for `structure_prediction.alphafold-gpu`"
        in (predict["unavailable_reason"])
    )
    assert {p["name"]: p["default"] for p in predict["params"]} == {
        "model_preset": "monomer",
        "seed": 0,
    }
    assert api_as(world, "viewer").get(f"/actions/{PREDICT}").json() == predict
    problem(api_as(world, "viewer").get("/actions/protein.nope"), 404, "not_found")
    assert api_as(world, "anonymous").get("/actions").status_code == 401


def test_without_compute_only_client_tier_actions_run(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(tmp_path / "missing.json"))
    catalog.compute.cache_clear()
    assert not any(a["available"] for a in api_as(world).get("/actions").json())


# --- Launching ---------------------------------------------------------------------------------


def test_launches_a_run_on_an_entity(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    made = launch(editor, world.project, {"protein": abl["id"]}, reason="Fill in weights")
    assert made.status_code == 201, made.content
    run = made.json()
    assert made["Location"] == f"/api/v1/runs/{run['id']}"
    assert run["hrid"].startswith("RUN-")
    assert run["action"] == COMPUTE
    assert run["plugin_version"] == "3.0.0"
    assert run["status"] == "queued"
    assert run["tier"] == "backend"
    assert run["environment"] == "protein-tools"
    assert run["params"] == {}
    assert run["reason"] == "Fill in weights"
    assert run["requested_by"]["name"] == "Editor"
    assert run["inputs"] == [
        {"name": "protein", "entity": abl["id"], "hrid": abl["hrid"], "revision": 1}
    ]
    assert run["attempts"] == 1
    assert run["can"] == {"cancel": True}

    stored = ActionRun.objects.get(pk=run["id"])
    assert stored.spec["command"] == ["python", "./scripts/properties.py"]
    assert stored.timeout == 300
    assert (stored.max_attempts, stored.retry_on) == (3, ["worker_lost", "interrupted"])
    job = Job.objects.get(run=stored)
    assert (job.state, job.tier, job.environment) == (
        JobState.QUEUED,
        "backend",
        "protein.protein-tools",
    )
    assert job.resources["memory"] == 512 * 1024**2
    assert job.owner is None
    event = AuditEvent.objects.get(action="run.launched")
    assert event.data == {"action": COMPUTE, "tier": "backend", "inputs": 1}
    assert event.project == world.project

    # Its HRID finds it too, and runs are numbered in order.
    assert editor.get(f"/runs/{run['hrid'].lower()}").json()["id"] == run["id"]
    second = launch(editor, world.project, {"protein": abl["id"]}).json()
    assert int(second["hrid"][4:]) == int(run["hrid"][4:]) + 1


@pytest.mark.parametrize(
    ("principal", "status"),
    [("viewer", 403), ("editor", 201), ("runner", 201), ("via_team", 201), ("outsider", 404)],
)
def test_the_backend_tier_takes_editor(world: World, principal: str, status: int) -> None:
    abl = protein(api_as(world, "admin"), world.project)
    made = launch(api_as(world, principal), world.project, {"protein": abl["id"]})
    assert made.status_code == status, made.content


def test_actions_this_deployment_cant_run_are_refused(world: World) -> None:
    runner = api_as(world, "runner")
    abl = protein(runner, world.project)
    body = problem(launch(runner, world.project, {"protein": abl["id"]}, PREDICT), 409, "refused")
    assert "no vm-tier machines" in body["detail"]
    # Editors can't run the vm tier at all, which is said first.
    problem(launch(api_as(world), world.project, {"protein": abl["id"]}, PREDICT), 403, "forbidden")


def test_archived_projects_run_nothing(world: World) -> None:
    """Archived projects are read-only, as for every other change."""
    abl = protein(api_as(world), world.project)
    problem(launch(api_as(world), world.archived, {"protein": abl["id"]}), 403, "forbidden")


def _errors(response: Any) -> dict[str, str]:
    body = problem(response, 422, "validation_failed")
    return {e["pointer"]: e["detail"] for e in body["errors"]}


def test_inputs_are_checked(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world.project)
    src = protein(editor, world.project, "src")
    src_etag = editor.get(f"/entities/{src['id']}")["ETag"]
    dna = create(editor, world.project, {"bases": "ACGT", "topology": "linear"}).json()
    assert _errors(launch(editor, world.project, {})) == {
        "#/inputs/protein": "Choose an entity for this input."
    }
    assert _errors(launch(editor, world.project, {"protein": [abl["id"], src["id"]]})) == {
        "#/inputs/protein": "This input takes one entity."
    }
    assert _errors(launch(editor, world.project, {"protein": dna["id"], "dna": dna["id"]})) == {
        "#/inputs/dna": f"`{COMPUTE}` has no input `dna`.",
        "#/inputs/protein": f"{dna['hrid']} is a dna.DNASequence, not a protein.Protein.",
    }
    missing = "0195f2a8-0000-7000-8000-000000000000"
    assert _errors(launch(editor, world.project, {"protein": missing})) == {
        "#/inputs/protein": f"No such entity, or you can't read it: {missing}."
    }
    archived = editor.patch(
        f"/entities/{src['id']}", {"archived": True}, headers={"If-Match": src_etag}
    )
    assert archived.status_code == 200, archived.content
    assert _errors(launch(editor, world.project, {"protein": src["id"]})) == {
        "#/inputs/protein": f"{src['hrid']} is archived."
    }
    assert not ActionRun.objects.exists()


def test_inputs_must_be_readable(world: World) -> None:
    secret = Project.objects.create(team=world.team, name="Secret")
    ProjectMembership.objects.create(
        project=secret, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    hidden = protein(api_as(world, "admin"), secret)
    errors = _errors(launch(api_as(world), world.project, {"protein": hidden["id"]}))
    assert errors == {"#/inputs/protein": f"No such entity, or you can't read it: {hidden['id']}."}


def test_entities_an_action_updates_must_be_editable(world: World) -> None:
    """Running in your project doesn't let an action change entities you can only read."""
    shared = Project.objects.create(team=world.team, name="Shared")
    ProjectMembership.objects.create(
        project=shared, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    ProjectMembership.objects.create(
        project=shared, user=world.users["editor"], role=ProjectRole.VIEWER
    )
    theirs = protein(api_as(world, "admin"), shared)
    errors = _errors(launch(api_as(world), world.project, {"protein": theirs["id"]}))
    assert errors == {
        "#/inputs/protein": f"This action changes {theirs['hrid']}, and you can't edit it in "
        "its project."
    }


def test_params_are_checked_and_defaulted(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Bind the vm tier, so predict_structure runs.
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["templates"]["a100"] = {
        **TEMPLATE,
        "name": "a100",
        "provisioner": "aws_ec2",
        "lifecycle": "ephemeral",
        "capacity": _capacity(96, 1152, gpus=8),
    }
    compute["bindings"]["structure_prediction.alphafold-gpu"] = {
        "environment": "structure_prediction.alphafold-gpu",
        "templates": [["a100", 0]],
    }
    file = tmp_path / "with-vm.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()

    runner = api_as(world, "runner")
    abl = protein(runner, world.project)
    bad = launch(
        runner,
        world.project,
        {"protein": abl["id"]},
        PREDICT,
        params={"model_preset": "dimer", "seed": -1, "temperature": 1},
    )
    assert _errors(bad) == {
        "#/params/model_preset": "The value must be one of: `monomer`, `multimer`.",
        "#/params/seed": "The value must be at least 0.",
        "#/params/temperature": f"`{PREDICT}` has no param `temperature`.",
    }
    made = launch(runner, world.project, {"protein": abl["id"]}, PREDICT, params={"seed": 7})
    assert made.status_code == 201, made.content
    assert made.json()["params"] == {"model_preset": "monomer", "seed": 7}
    job = Job.objects.get(run_id=made.json()["id"])
    # The action's own 96Gi raises the environment's 64Gi minimum.
    assert job.resources["memory"] == 96 * 1024**3
    assert job.resources["gpu_count"] == 1


# --- Reading and cancelling --------------------------------------------------------------------


def test_runs_are_read_by_whoever_reads_their_project(world: World) -> None:
    abl = protein(api_as(world), world.project)
    run = launch(api_as(world), world.project, {"protein": abl["id"]}).json()
    viewer = api_as(world, "viewer")
    seen = viewer.get(f"/runs/{run['id']}").json()
    assert seen["can"] == {"cancel": False}
    listed = viewer.get("/runs", project=str(world.project.pk)).json()
    assert [r["id"] for r in listed["items"]] == [run["id"]]
    assert viewer.get("/runs", status="succeeded").json()["items"] == []
    assert viewer.get("/runs", action=COMPUTE).json()["items"][0]["id"] == run["id"]
    problem(api_as(world, "outsider").get(f"/runs/{run['id']}"), 404, "not_found")
    assert api_as(world, "outsider").get("/runs").json()["items"] == []


def test_runs_list_newest_first(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world.project)
    ids = [launch(editor, world.project, {"protein": abl["id"]}).json()["id"] for _ in range(3)]
    page = editor.get("/runs", limit=2).json()
    assert [r["id"] for r in page["items"]] == ids[::-1][:2]
    rest = editor.get("/runs", limit=2, cursor=page["next_cursor"]).json()
    assert [r["id"] for r in rest["items"]] == ids[:1]


def test_cancelling_a_queued_run(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world.project)
    run = launch(editor, world.project, {"protein": abl["id"]}).json()
    problem(api_as(world, "viewer").post(f"/runs/{run['id']}/cancel", {}), 403, "forbidden")
    # Anyone who may run it may cancel it, not only whoever launched it.
    cancelled = api_as(world, "runner").post(f"/runs/{run['hrid']}/cancel", {"reason": "Wrong one"})
    assert cancelled.status_code == 200, cancelled.content
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert body["can"] == {"cancel": False}
    assert Job.objects.get(run_id=run["id"]).state == JobState.CANCELLED
    event = AuditEvent.objects.get(action="run.cancelled")
    assert event.data == {"reason": "Wrong one"}
    assert event.actor == world.users["runner"]
    again = problem(editor.post(f"/runs/{run['id']}/cancel", {}), 409, "refused")
    assert "already finished (cancelled)" in again["detail"]


def test_cancelling_a_running_run_asks_its_worker(world: World) -> None:
    editor = api_as(world)
    abl = protein(editor, world.project)
    run = launch(editor, world.project, {"protein": abl["id"]}).json()
    Job.objects.filter(run_id=run["id"]).update(state=JobState.RUNNING)
    ActionRun.objects.filter(pk=run["id"]).update(status="running")
    body = editor.post(f"/runs/{run['id']}/cancel", {}).json()
    assert body["status"] == "running"
    assert body["cancel_requested_at"] is not None
    assert body["can"] == {"cancel": False}
    assert AuditEvent.objects.filter(action="run.cancel_requested").count() == 1
    # Asking again changes nothing.
    assert editor.post(f"/runs/{run['id']}/cancel", {}).status_code == 200
    assert AuditEvent.objects.filter(action="run.cancel_requested").count() == 1


def test_run_inputs_are_append_only(world: World) -> None:
    assert getattr(RunInput, "APPEND_ONLY", False)
