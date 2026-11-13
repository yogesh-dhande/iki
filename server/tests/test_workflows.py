"""Workflows: steps that are ordinary runs, launched as their starter, each once the steps it
takes from have succeeded (ADR 0079)."""

from typing import Any
from uuid import UUID

import pytest
from api_client import Api, problem
from org_world import World
from test_api_entities import api_as, create
from test_runs import protein
from test_workers import WorkerClient, keep_connection, worker, world

from iki_server.audit.models import AuditEvent
from iki_server.runs import services, workflows
from iki_server.runs.models import (
    ActionRun,
    EffectKind,
    Job,
    JobState,
    RunEffect,
    RunStatus,
    WorkflowRun,
    WorkflowStep,
)
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

WEIGH_AND_ALIGN = "protein.weigh_and_align"
RELAX_TWICE = "protein.relax_twice"
IMPORT_AND_WEIGH = "protein.import_and_weigh"


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def start(
    api: Api, world: World, inputs: dict[str, Any], name: str = WEIGH_AND_ALIGN, **kw: Any
) -> Any:
    return api.post(
        f"/workflows/{name}/runs", {"project": str(world.project.pk), "inputs": inputs, **kw}
    )


def started(world: World, **kw: Any) -> dict[str, Any]:
    """A run of weigh_and_align, started by the editor on two proteins."""
    editor = api_as(world, "editor")
    query, target = protein(editor, world.project, "kit"), protein(editor, world.project, "met")
    made = start(editor, world, {"query": query["id"], "target": target["id"]}, **kw)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def steps(body: dict[str, Any]) -> dict[str, str]:
    return {s["id"]: s["state"] for s in body["steps"]}


def get(world: World, body: dict[str, Any], who: str = "editor") -> dict[str, Any]:
    found: dict[str, Any] = api_as(world, who).get(f"/workflow-runs/{body['hrid']}").json()
    return found


def complete(pod: WorkerClient, document: dict[str, Any] | None = None) -> dict[str, Any]:
    job = pod.lease().json()
    answer = pod.report(
        job, "complete", output={"apiVersion": "iki/v1", "kind": "RunOutput", **(document or {})}
    )
    assert answer.status_code == 200, answer.content
    return job  # type: ignore[no-any-return]


def weigh(pod: WorkerClient) -> dict[str, Any]:
    """Completes the leased compute_properties job, weighing its protein."""
    job = pod.lease().json()
    (entity,) = job["inputs"]["protein"]
    document = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"protein": [{"entity": entity["id"], "values": {"molecular_weight": 1.5}}]},
    }
    answer = pod.report(job, "complete", output=document)
    assert answer.status_code == 200, answer.content
    return job  # type: ignore[no-any-return]


# --- The catalog -------------------------------------------------------------------------------


def test_lists_installed_workflows(world: World) -> None:
    viewer = api_as(world, "viewer")
    listed = viewer.get("/workflows").json()
    assert [w["name"] for w in listed] == [IMPORT_AND_WEIGH, RELAX_TWICE, WEIGH_AND_ALIGN]
    found = viewer.get(f"/workflows/{WEIGH_AND_ALIGN}").json()
    assert found == listed[2]
    assert (found["label"], found["plugin"]) == ("Weigh and align", "protein")
    assert [i["name"] for i in found["inputs"]] == ["query", "target"]
    assert found["params"][0]["name"] == "mode"
    assert found["params"][0]["default"] == "local"
    assert "mode" in found["params_schema"]["properties"]
    assert found["steps"] == [
        {
            "id": "weigh",
            "label": "Weigh",
            "action": "protein.compute_properties",
            "inputs": {"protein": "$inputs.query"},
            "params": {},
            "after": [],
            "each": None,
        },
        {
            "id": "align",
            "label": "Align",
            "action": "protein.align_pair",
            "inputs": {"query": "$steps.weigh.inputs.protein", "target": "$inputs.target"},
            "params": {"mode": "$params.mode"},
            "after": ["weigh"],
            "each": None,
        },
    ]
    assert found["available"] is True
    problem(viewer.get("/workflows/protein.nope"), 404, "not_found")


# --- Starting ----------------------------------------------------------------------------------


def test_starting_a_workflow_launches_its_first_steps(world: World) -> None:
    body = started(world, reason="Kinase panel")
    assert body["hrid"].startswith("WF-")
    assert (body["workflow"], body["label"], body["status"]) == (
        WEIGH_AND_ALIGN,
        "Weigh and align",
        "running",
    )
    assert body["reason"] == "Kinase panel"
    assert body["params"] == {"mode": "local"}
    assert [i["name"] for i in body["inputs"]] == ["query", "target"]
    assert body["requested_by"]["name"] == world.users["editor"].name
    assert steps(body) == {"weigh": "running", "align": "waiting"}
    weigh_run = body["steps"][0]["run"]
    assert weigh_run["status"] == "queued"
    assert body["steps"][1]["run"] is None
    assert body["can"] == {"cancel": True}

    run = ActionRun.objects.get(pk=weigh_run["id"])
    assert (run.workflow_id, run.workflow_step) == (WorkflowRun.objects.get().pk, "weigh")
    assert run.requested_by == world.users["editor"]
    assert run.via == WorkflowRun.objects.get().via == "web"
    assert run.reason == f"Step `weigh` of {body['hrid']}: Kinase panel"
    event = AuditEvent.objects.get(action="workflow.started")
    assert (event.target_label, event.data["steps"]) == (body["hrid"], 2)
    launched = AuditEvent.objects.get(action="run.launched")
    assert (launched.data["workflow"], launched.data["step"]) == (body["hrid"], "weigh")

    viewer = api_as(world, "viewer")
    assert get(world, body, "viewer") == {**body, "can": {"cancel": False}}
    runs = viewer.get("/runs", workflow=body["hrid"]).json()["items"]
    assert [r["id"] for r in runs] == [weigh_run["id"]]
    assert runs[0]["workflow"] == {"id": body["id"], "hrid": body["hrid"], "step": "weigh"}
    listed = viewer.get("/workflow-runs", project=str(world.project.pk)).json()["items"]
    assert [w["hrid"] for w in listed] == [body["hrid"]]
    assert viewer.get("/workflow-runs", status="failed").json()["items"] == []

    outsider = api_as(world, "outsider")
    problem(outsider.get(f"/workflow-runs/{body['hrid']}"), 404, "not_found")
    problem(outsider.get("/runs", workflow=body["hrid"]), 404, "not_found")
    assert outsider.get("/workflow-runs").json()["items"] == []


def test_a_workflow_moves_on_as_its_steps_succeed(world: World) -> None:
    body = started(world, params={"mode": "global"})
    pod = worker(world)
    weighed = weigh(pod)
    # The run's end queued a task that moves the workflow on.
    assert steps(get(world, body)) == {"weigh": "running", "align": "waiting"}
    assert run_tasks() >= 1
    now = get(world, body)
    assert steps(now) == {"weigh": "succeeded", "align": "running"}
    align = ActionRun.objects.get(workflow_step="align")
    assert align.params == {"mode": "global"}
    given = {i.name: i.entity_id for i in align.inputs.all()}
    query = weighed["inputs"]["protein"][0]["id"]
    assert str(given["query"]) == query
    assert {str(given["target"])} == {i["entity"] for i in body["inputs"] if i["name"] == "target"}

    complete(pod)
    run_tasks()
    done = get(world, body)
    assert done["status"] == "succeeded"
    assert done["finished_at"] is not None
    assert steps(done) == {"weigh": "succeeded", "align": "succeeded"}
    assert done["can"] == {"cancel": False}
    assert done["error"] is None


def test_starting_again_reuses_steps_that_succeeded(
    world: World, monkeypatch: pytest.MonkeyPatch, s3: Any
) -> None:
    # Built from a lockfile: only then are results reused (ADR 0057).
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: "a" * 40)
    body = started(world)
    built = {"protein-tools": "sha256:" + "b" * 64}
    pod = worker(world, plugins=[{"name": "protein", "sha": "a" * 40, "environments": built}])
    weigh(pod)
    run_tasks()
    complete(pod)
    run_tasks()
    assert get(world, body)["status"] == "succeeded"

    editor = api_as(world, "editor")
    inputs = {i["name"]: i["entity"] for i in body["inputs"]}
    again = start(editor, world, inputs).json()
    # Weighing changed the query since, so it's weighed again: to the same weight, which
    # changes nothing. The alignment then reads what the first one did, and is reused.
    assert steps(again) == {"weigh": "running", "align": "waiting"}
    weigh(pod)
    run_tasks()
    done = get(world, again)
    assert done["status"] == "succeeded"
    align = ActionRun.objects.get(pk=done["steps"][1]["run"]["id"])
    assert align.reused_from == ActionRun.objects.get(pk=get(world, body)["steps"][1]["run"]["id"])


def test_a_failed_step_fails_the_workflow(world: World) -> None:
    body = started(world)
    pod = worker(world)
    job = pod.lease().json()
    pod.report(job, "fail", reason="failed", message="ValueError: bad sequence")
    run_tasks()
    failed = get(world, body)
    assert failed["status"] == "failed"
    assert steps(failed) == {"weigh": "failed", "align": "skipped"}
    weigh_step, align_step = failed["steps"]
    hrid = weigh_step["run"]["hrid"]
    assert weigh_step["error"] == {
        "code": "run_failed",
        "message": f"Its run, {hrid}, failed. ValueError: bad sequence",
    }
    assert align_step["error"]["message"] == "It didn't start: an earlier step didn't succeed."
    assert failed["error"]["step"] == "weigh"
    assert failed["error"]["message"].startswith("Step `weigh` didn't succeed.")


def test_a_step_that_cant_launch_fails_the_workflow(world: World) -> None:
    body = started(world)
    # The editor loses Editor before the second step: it can't launch as them.
    from iki_server.org.models import ProjectMembership

    pod = worker(world)
    weigh(pod)
    ProjectMembership.objects.filter(project=world.project, user=world.users["editor"]).delete()
    run_tasks()
    failed = WorkflowRun.objects.get(pk=body["id"])
    assert failed.status == "failed"
    step = WorkflowStep.objects.get(workflow=failed, step="align")
    assert step.state == "failed"
    assert step.error is not None
    assert step.error["code"] == "launch_failed"
    assert "can no longer run its action" in step.error["message"]


def test_cancelling_a_workflow(world: World) -> None:
    body = started(world)
    viewer = api_as(world, "viewer")
    problem(viewer.post(f"/workflow-runs/{body['hrid']}/cancel", {}), 403, "forbidden")
    editor = api_as(world, "editor")
    cancelled = editor.post(f"/workflow-runs/{body['hrid']}/cancel", {"reason": "Wrong pair"})
    assert cancelled.status_code == 200, cancelled.content
    out = cancelled.json()
    # Its queued run was cancelled at once, so it ended.
    assert out["status"] == "cancelled"
    assert steps(out) == {"weigh": "cancelled", "align": "skipped"}
    assert out["cancel_requested_at"] is not None
    run = ActionRun.objects.get(pk=body["steps"][0]["run"]["id"])
    assert run.status == RunStatus.CANCELLED
    event = AuditEvent.objects.get(action="workflow.cancelled")
    assert event.data == {"reason": "Wrong pair"}
    problem(editor.post(f"/workflow-runs/{body['hrid']}/cancel", {}), 409, "refused")


def test_cancelling_waits_for_running_steps(world: World) -> None:
    body = started(world)
    pod = worker(world)
    job = pod.lease().json()
    out = api_as(world, "editor").post(f"/workflow-runs/{body['hrid']}/cancel", {}).json()
    assert out["status"] == "running"
    assert steps(out) == {"weigh": "running", "align": "skipped"}
    # Its worker stops it.
    pod.report(job, "fail", reason="cancelled", message="Cancelled.")
    run_tasks()
    ended = get(world, body)
    assert ended["status"] == "cancelled"
    assert steps(ended) == {"weigh": "cancelled", "align": "skipped"}


def test_a_lost_task_is_made_up_for(world: World) -> None:
    body = started(world)
    weigh(worker(world))
    TaskRecord.objects.all().delete()  # as if the task was lost
    assert workflows.recover() == 0  # moved on, not ended
    assert steps(get(world, body)) == {"weigh": "succeeded", "align": "running"}


# --- What's checked ----------------------------------------------------------------------------


def _pointers(response: Any) -> list[str]:
    return [e["pointer"] for e in problem(response, 422, "validation_failed")["errors"]]


def test_inputs_and_params_are_checked(world: World) -> None:
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    assert _pointers(start(editor, world, {"query": kit["id"]})) == ["#/inputs/target"]
    bad = start(editor, world, {"query": kit["id"], "target": kit["id"]}, params={"mode": "x"})
    assert _pointers(bad) == ["#/params/mode"]
    assert WorkflowRun.objects.count() == 0


def test_starting_takes_the_role_each_step_needs(world: World) -> None:
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    inputs = {"query": kit["id"], "target": kit["id"]}
    problem(start(api_as(world, "viewer"), world, inputs), 403, "forbidden")
    problem(start(api_as(world, "outsider"), world, inputs), 404, "not_found")
    assert WorkflowRun.objects.count() == 0


# --- Created entities --------------------------------------------------------------------------


def test_a_step_takes_what_an_earlier_step_created(world: World, s3: Any) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    upload = _empty_file(world)
    values = {"structure": upload, "method": "xray", "chains": [abl["id"]]}
    structure = create(editor, world.project, values, "protein.ProteinStructure").json()
    relaxed = create(editor, world.project, values, "protein.ProteinStructure").json()
    body = start(editor, world, {"structure": structure["id"]}, RELAX_TWICE).json()
    assert steps(body) == {"first": "running", "second": "waiting"}

    # The first relax "creates" `relaxed`: as its worker's output would have.
    first = ActionRun.objects.get(pk=body["steps"][0]["run"]["id"])
    RunEffect.objects.create(run=first, entity_id=relaxed["id"], kind=EffectKind.CREATED)
    ActionRun.objects.filter(pk=first.pk).update(status=RunStatus.SUCCEEDED)
    workflows.advance(body["id"])
    second = ActionRun.objects.get(workflow_step="second")
    assert [str(i.entity_id) for i in second.inputs.all()] == [relaxed["id"]]


def test_a_step_with_nothing_created_to_take_fails(world: World, s3: Any) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    values = {"structure": _empty_file(world), "method": "xray", "chains": [abl["id"]]}
    structure = create(editor, world.project, values, "protein.ProteinStructure").json()
    body = start(editor, world, {"structure": structure["id"]}, RELAX_TWICE).json()
    first = ActionRun.objects.get(pk=body["steps"][0]["run"]["id"])
    ActionRun.objects.filter(pk=first.pk).update(status=RunStatus.SUCCEEDED)
    workflows.advance(body["id"])
    step = WorkflowStep.objects.get(step="second")
    assert step.state == "failed"
    assert step.error is not None
    assert step.error["message"] == (
        "Its run couldn't start: step `first` created no `protein.ProteinStructure` for it to take."
    )


# --- Once for each entity ----------------------------------------------------------------------


def imported(world: World, *names: str) -> dict[str, Any]:
    """A run of import_and_weigh whose import "created" a protein for each of `names`."""
    editor = api_as(world, "editor")
    made = [protein(editor, world.project, name) for name in names]
    fasta = _empty_file(world, "panel.fasta", "chemical/seq-aa-fasta")
    body: dict[str, Any] = start(editor, world, {}, IMPORT_AND_WEIGH, params={"file": fasta}).json()
    assert steps(body) == {"import": "running", "weigh": "waiting"}
    first = ActionRun.objects.get(pk=body["steps"][0]["run"]["id"])
    for each in made:
        RunEffect.objects.create(run=first, entity_id=each["id"], kind=EffectKind.CREATED)
    ActionRun.objects.filter(pk=first.pk).update(status=RunStatus.SUCCEEDED)
    Job.objects.filter(run=first).update(state=JobState.SUCCEEDED)
    workflows.advance(body["id"])
    return body


def test_a_step_runs_once_for_each_entity_an_earlier_one_created(world: World, s3: Any) -> None:
    body = imported(world, "kit", "met")
    now = get(world, body)
    assert steps(now) == {"import": "succeeded", "weigh": "running"}
    weigh_step = now["steps"][1]
    assert weigh_step["each"] == "protein"
    assert weigh_step["run"] is None
    batch = weigh_step["batch"]
    assert (batch["size"], batch["statuses"]) == (2, {"queued": 2})
    runs = api_as(world, "viewer").get("/runs", batch=batch["hrid"]).json()["items"]
    assert [r["workflow"]["step"] for r in runs] == ["weigh", "weigh"]

    # The batch's runs go in one job (compute_properties is batchable).
    pod = worker(world)
    job = pod.lease().json()
    document = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {
            "protein": [
                {"entity": e["id"], "values": {"molecular_weight": 1.5}}
                for e in job["inputs"]["protein"]
            ]
        },
    }
    assert pod.report(job, "complete", output=document).status_code == 200
    run_tasks()
    done = get(world, body)
    assert done["status"] == "succeeded"
    assert done["steps"][1]["batch"]["statuses"] == {"succeeded": 2}


def test_a_step_run_for_each_fails_if_any_of_its_runs_does(world: World, s3: Any) -> None:
    body = imported(world, "kit", "met")
    first, second = ActionRun.objects.filter(workflow_step="weigh").order_by("id")
    ActionRun.objects.filter(pk=first.pk).update(status=RunStatus.SUCCEEDED)
    ActionRun.objects.filter(pk=second.pk).update(
        status=RunStatus.FAILED, error={"code": "failed", "message": "Bad residue X"}
    )
    workflows.advance(body["id"])
    failed = get(world, body)
    assert failed["status"] == "failed"
    assert failed["steps"][1]["error"] == {
        "code": "run_failed",
        "message": f"1 of its 2 runs didn't succeed. {second.hrid} failed. Bad residue X",
    }


def test_a_step_run_for_each_waits_for_all_its_runs(world: World, s3: Any) -> None:
    body = imported(world, "kit", "met")
    first = ActionRun.objects.filter(workflow_step="weigh").order_by("id").first()
    assert first is not None
    ActionRun.objects.filter(pk=first.pk).update(status=RunStatus.SUCCEEDED)
    workflows.advance(body["id"])
    assert steps(get(world, body)) == {"import": "succeeded", "weigh": "running"}


def test_a_step_run_for_each_with_none_to_run_on_fails(world: World, s3: Any) -> None:
    body = imported(world)
    failed = get(world, body)
    assert failed["status"] == "failed"
    assert failed["steps"][1]["error"]["message"] == (
        "Its run couldn't start: step `import` created no `protein.Protein` for it to take."
    )


def _empty_file(world: World, name: str = "abl.pdb", media_type: str = "chemical/x-pdb") -> str:
    from iki_server.files import services as files

    upload = files.start(
        world.users["editor"],
        world.project,
        name=name,
        media_type=media_type,
        size=0,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    upload.refresh_from_db()
    return str(upload.file_id)


# --- Agents ------------------------------------------------------------------------------------


def test_later_steps_are_launched_as_the_workflow_was_started(world: World) -> None:
    """By the agent that started it, through MCP: so a step on a tier that needs approval
    waits for it, as a run the agent launched would (ADR 0013)."""
    from iki_server.audit import log as audit
    from iki_server.audit.models import Via

    editor = api_as(world, "editor")
    kit, met = protein(editor, world.project, "kit"), protein(editor, world.project, "met")
    agent = audit.AgentInfo(client_id="notebook", client_name="Notebook", model="claude")
    with audit.using(audit.Context(via=Via.MCP, oauth_client="Notebook", agent=agent)):
        run = workflows.start(
            world.users["editor"],
            workflows.workflow(WEIGH_AND_ALIGN),  # type: ignore[arg-type]
            world.project,
            {"query": [UUID(kit["id"])], "target": [UUID(met["id"])]},
            {},
        )
    assert run.via == Via.MCP
    assert run.agent is not None
    weigh(worker(world))
    run_tasks()  # as the system: not in the agent's request
    align = ActionRun.objects.get(workflow_step="align")
    assert (align.via, align.client, align.agent_id) == (Via.MCP, "Notebook", run.agent.pk)
