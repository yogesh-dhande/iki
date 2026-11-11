"""Batches: runs of one action launched together, and packing a ``batchable`` action's runs
into one job (ADR 0055)."""

import asyncio
import uuid
from typing import Any

import pytest
from api_client import problem
from asgiref.sync import sync_to_async
from django.db.models.functions import Now
from django.test import AsyncClient
from org_world import World, build_world
from test_api_entities import api_as
from test_realtime import Events, signed_in
from test_runs import COMPUTE, protein
from test_workers import WorkerClient, expire, keep_connection, worker, world

from iki_server.audit.models import AuditEvent
from iki_server.realtime.hub import hub
from iki_server.runs import queue
from iki_server.runs.models import ActionRun, Job, JobLog, JobState, RunOutput, RunQuota
from iki_spec import schemas

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

# Kinases whose names are valid sequences, so the real worker can weigh them too.
NAMES = ("kit", "met", "ret", "egfr", "alk")


def batch(world: World, count: int = 3, *, first: int = 0, reason: str = "") -> dict[str, Any]:
    """A batch of protein.compute_properties (``batchable: 100``) on `count` proteins."""
    editor = api_as(world, "editor")
    made = [protein(editor, world.project, name) for name in NAMES[first : first + count]]
    launched = editor.post(
        f"/actions/{COMPUTE}/runs/batch",
        {
            "project": str(world.project.pk),
            "each": "protein",
            "entities": [p["id"] for p in made],
            "reason": reason,
        },
    )
    assert launched.status_code == 201, launched.content
    body: dict[str, Any] = launched.json()
    return body


def output(**parts: Any) -> dict[str, Any]:
    return {"apiVersion": "iki/v1", "kind": "RunOutput", **parts}


def weights(job: dict[str, Any], *values: Any) -> dict[str, Any]:
    """An update of each entity the job was given, in order, to each of `values`."""
    entities = job["inputs"]["protein"]
    return {
        "protein": [
            {"entity": e["id"], "values": {"molecular_weight": v}}
            for e, v in zip(entities, values, strict=True)
        ]
    }


def done(pod: WorkerClient, job: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    answer = pod.report(job, "complete", output=document)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def statuses(launched: dict[str, Any]) -> list[str]:
    return [ActionRun.objects.get(pk=r["id"]).status for r in launched["runs"]]


# --- The batch ---------------------------------------------------------------------------------


def test_runs_launched_together_share_a_batch(world: World) -> None:
    launched = batch(world, reason="Weights for the kinase panel")
    record = launched["batch"]
    assert record["hrid"].startswith("BAT-")
    assert (record["action"], record["each"], record["size"]) == (COMPUTE, "protein", 3)
    assert record["reason"] == "Weights for the kinase panel"
    assert record["requested_by"]["name"] == world.users["editor"].name
    assert record["statuses"] == {"queued": 3}
    assert record["finished_at"] is None
    assert record["can"] == {"cancel": True}
    assert {r["batch"]["hrid"] for r in launched["runs"]} == {record["hrid"]}
    event = AuditEvent.objects.get(action="run.batch_launched")
    assert (event.target_label, event.data["runs"]) == (record["hrid"], 3)
    assert AuditEvent.objects.filter(action="run.launched").count() == 3

    viewer = api_as(world, "viewer")
    seen = viewer.get(f"/batches/{record['hrid'].lower()}").json()
    assert seen == {**record, "can": {"cancel": False}}
    assert viewer.get(f"/batches/{record['id']}").json()["hrid"] == record["hrid"]
    # Its runs, in the order it launched them.
    runs = viewer.get("/runs", batch=record["hrid"]).json()["items"]
    assert [r["id"] for r in runs] == [r["id"] for r in launched["runs"]]
    assert viewer.get(f"/runs/{runs[0]['id']}").json()["batch"] == {
        "id": record["id"],
        "hrid": record["hrid"],
    }
    listed = viewer.get("/batches", project=str(world.project.pk)).json()["items"]
    assert [b["hrid"] for b in listed] == [record["hrid"]]
    assert viewer.get("/batches", action="protein.nope").json()["items"] == []

    outsider = api_as(world, "outsider")
    problem(outsider.get(f"/batches/{record['hrid']}"), 404, "not_found")
    problem(outsider.get("/runs", batch=record["hrid"]), 404, "not_found")
    assert outsider.get("/batches").json()["items"] == []


def test_a_batch_launches_up_to_a_thousand_runs(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    too_many = editor.post(
        f"/actions/{COMPUTE}/runs/batch",
        {"project": str(world.project.pk), "each": "protein", "entities": [abl["id"]] * 1001},
    )
    problem(too_many, 422, "validation_failed")


def test_cancelling_a_batch_cancels_each_run_that_hasnt_finished(world: World) -> None:
    launched = batch(world)
    record = launched["batch"]
    problem(api_as(world, "viewer").post(f"/batches/{record['hrid']}/cancel", {}), 403, "forbidden")
    editor = api_as(world, "editor")
    cancelled = editor.post(f"/batches/{record['hrid']}/cancel", {"reason": "Wrong panel"})
    assert cancelled.status_code == 200, cancelled.content
    body = cancelled.json()
    assert body["statuses"] == {"cancelled": 3}
    assert body["finished_at"] is not None
    assert body["can"] == {"cancel": False}
    assert set(statuses(launched)) == {"cancelled"}
    event = AuditEvent.objects.get(action="run.batch_cancelled")
    assert event.data == {"runs": 3, "reason": "Wrong panel"}
    # Again: nothing left to cancel.
    assert editor.post(f"/batches/{record['hrid']}/cancel", {}).json()["statuses"] == {
        "cancelled": 3
    }


# --- Packing -----------------------------------------------------------------------------------


def test_a_batchable_actions_runs_are_packed_into_one_job(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    job = leased.json()
    assert not list(schemas.check("run-input", job))
    first, *rest = launched["runs"]
    assert job["run"]["hrid"] == first["hrid"]
    hrids = [e["hrid"] for e in job["inputs"]["protein"]]
    assert hrids == [r["inputs"][0]["hrid"] for r in launched["runs"]]
    assert job["pack"] == [
        {"run": r["id"], "hrid": r["hrid"], "entity": r["inputs"][0]["entity"]}
        for r in launched["runs"]
    ]
    assert set(statuses(launched)) == {"running"}
    members = Job.objects.filter(run_id__in=[r["id"] for r in rest])
    assert {(m.state, m.packed_into_id, m.lease_token) for m in members} == {
        ("leased", uuid.UUID(job["job"]["id"]), None)
    }
    # Nothing's left for another worker.
    assert pod.lease().status_code == 204


def test_a_pack_holds_at_most_batchable_runs(world: World) -> None:
    launched = batch(world, 5)
    for run in ActionRun.objects.filter(batch__hrid=launched["batch"]["hrid"]):
        run.spec = {**run.spec, "batchable": 2}
        run.save(update_fields=["spec"])
    pod = worker(world)
    sizes = []
    while (leased := pod.lease()).status_code == 200:
        sizes.append(len(leased.json()["inputs"]["protein"]))
    assert sizes == [2, 2, 1]


def test_runs_launched_on_their_own_arent_packed(world: World) -> None:
    from test_workers import queued

    queued(world)
    queued(world)
    pod = worker(world)
    assert len(pod.lease().json()["inputs"]["protein"]) == 1
    assert len(pod.lease().json()["inputs"]["protein"]) == 1


def test_a_pack_counts_once_against_running_limits(world: World) -> None:
    RunQuota.objects.create(project=world.project, max_running=1, created_by=world.users["admin"])
    batch(world)
    pod = worker(world)
    assert len(pod.lease().json()["inputs"]["protein"]) == 3
    from test_workers import queued

    queued(world)
    assert pod.lease().status_code == 204  # the pack is the one job running


def test_a_packed_runs_attempt_follows_its_job(world: World) -> None:
    launched = batch(world)
    pod = worker(world, name="pod-9")
    job = pod.lease().json()
    pod.report(job, "heartbeat", state="running")
    pod.report(job, "progress", fraction=0.5, message="Weighing")
    pod.report(job, "logs", offset=0, text="abl1: 12345.6\n")
    viewer = api_as(world, "viewer")
    last = launched["runs"][-1]
    (attempt,) = viewer.get(f"/runs/{last['hrid']}/attempts").json()
    assert (attempt["state"], attempt["worker"]) == ("running", "pod-9")
    assert (attempt["progress"], attempt["progress_message"]) == (0.5, "Weighing")
    assert attempt["log_size"] == 14
    assert attempt["pack"] == {"runs": 3, "reported_as": launched["runs"][0]["hrid"]}
    (held,) = viewer.get(f"/runs/{launched['runs'][0]['hrid']}/attempts").json()
    assert held["pack"] == {"runs": 3, "reported_as": launched["runs"][0]["hrid"]}
    log = viewer.get(f"/runs/{last['hrid']}/attempts/1/log").json()
    assert (log["text"], log["size"], log["ended"]) == ("abl1: 12345.6\n", 14, False)
    text = viewer.get(f"/runs/{last['hrid']}/attempts/1/log.txt").content
    assert text == b"abl1: 12345.6\n"
    assert JobLog.objects.count() == 1  # kept once, with the job its worker held


# --- Results -----------------------------------------------------------------------------------


def test_a_packs_results_are_split_into_each_runs_own(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    entities = [e["id"] for e in job["inputs"]["protein"]]
    document = output(
        updates=weights(job, 1.5, 2.5, 3.5),
        metrics={"proteins": 3},
        messages=[
            {"level": "info", "text": "Used average isotopic masses."},
            {"level": "warning", "text": "Has an X.", "entity": entities[1]},
        ],
    )
    assert not list(schemas.check("run-output", document))
    assert done(pod, job, document)["run_status"] == "succeeded"
    viewer = api_as(world, "viewer")
    runs = [viewer.get(f"/runs/{r['id']}").json() for r in launched["runs"]]
    assert [r["status"] for r in runs] == ["succeeded"] * 3
    assert [[(e["entity"], e["kind"]) for e in r["effects"]] for r in runs] == [
        [(entity, "updated")] for entity in entities
    ]
    assert [r["metrics"] for r in runs] == [{"proteins": 3}] * 3
    assert [len(r["messages"]) for r in runs] == [1, 2, 1]
    assert runs[1]["messages"][1]["text"] == "Has an X."
    # Each run keeps its own part.
    kept = RunOutput.objects.get(run_id=launched["runs"][2]["id"]).document
    assert kept["updates"] == {"protein": [weights(job, 1.5, 2.5, 3.5)["protein"][2]]}
    record = viewer.get(f"/batches/{launched['batch']['hrid']}").json()
    assert record["statuses"] == {"succeeded": 3}
    assert record["finished_at"] is not None


def test_one_runs_results_that_dont_fit_hold_back_only_that_run(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    done(pod, job, output(updates=weights(job, 1.5, "heavy", 3.5)))
    assert statuses(launched) == ["succeeded", "failed", "succeeded"]
    error = ActionRun.objects.get(pk=launched["runs"][1]["id"]).error
    assert error is not None
    assert error["code"] == "invalid_output"
    assert [e["pointer"] for e in error["errors"]] == [
        "#/updates/protein/0/values/molecular_weight"
    ]


def test_results_that_dont_say_which_run_theyre_for_fail_the_pack(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    stranger = "0195f2a8-0000-7000-8000-000000000000"
    document = output(
        updates={"protein": [{"entity": stranger, "values": {"molecular_weight": 1.0}}]},
        messages=[{"level": "info", "text": "?", "entity": stranger}],
    )
    body = done(pod, job, document)
    assert (body["run_status"], body["code"]) == ("failed", "invalid_output")
    assert {e["pointer"]: e["detail"] for e in body["errors"]} == {
        "#/updates/protein/0/entity": "It isn't one of the entities the job was given.",
        "#/messages/0/entity": "It isn't one of the entities the job was given.",
    }
    assert set(statuses(launched)) == {"failed"}
    assert RunOutput.objects.filter(document=document).count() == 3


def test_a_message_about_an_entity_a_run_wasnt_given_doesnt_fit(world: World) -> None:
    from test_workers import queued

    run = queued(world)
    pod = worker(world)
    job = pod.lease().json()
    stranger = "0195f2a8-0000-7000-8000-000000000000"
    body = done(pod, job, output(messages=[{"level": "info", "text": "?", "entity": stranger}]))
    assert body["run_status"] == "failed"
    assert body["errors"] == [
        {"pointer": "#/messages/0/entity", "detail": "It isn't one of the run's inputs."}
    ]
    assert ActionRun.objects.get(pk=run["id"]).status == "failed"


# --- Ending badly, and cancelling --------------------------------------------------------------


def test_a_lost_pack_retries_each_run_on_its_own(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    expire(job)
    assert queue.recover_expired() == 1
    assert set(statuses(launched)) == {"queued"}
    retries = Job.objects.filter(attempt=2)
    assert retries.count() == 3
    assert set(retries.values_list("state", flat=True)) == {JobState.QUEUED}
    assert set(Job.objects.filter(attempt=1).values_list("state", flat=True)) == {
        JobState.INTERRUPTED
    }
    # Retries are packed again.
    retries.update(queued_at=Now())
    again = pod.lease().json()
    assert again["run"]["attempt"] == 2
    assert len(again["inputs"]["protein"]) == 3


def test_a_failed_pack_fails_each_run(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    assert pod.report(job, "fail", reason="failed", message="KeyError").json() == {
        "run_status": "failed"
    }
    assert set(statuses(launched)) == {"failed"}
    errors = {(r.error or {}).get("message") for r in ActionRun.objects.all()}
    assert errors == {"KeyError"}


def test_a_pack_stops_only_once_every_run_in_it_is_cancelled(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    editor = api_as(world, "editor")
    second = launched["runs"][1]
    editor.post(f"/runs/{second['id']}/cancel", {})
    assert pod.report(job, "heartbeat").json()["cancel_requested"] is False
    # The pack goes on; the cancelled run writes nothing.
    done(pod, job, output(updates=weights(job, 1.5, 2.5, 3.5)))
    assert statuses(launched) == ["succeeded", "cancelled", "succeeded"]
    assert api_as(world).get(f"/runs/{second['id']}").json()["effects"] == []

    again = batch(world, 2, first=3)
    job = pod.lease().json()
    editor.post(f"/batches/{again['batch']['hrid']}/cancel", {})
    assert pod.report(job, "heartbeat").json()["cancel_requested"] is True
    assert pod.report(job, "fail", reason="cancelled").json() == {"run_status": "cancelled"}
    assert set(statuses(again)) == {"cancelled"}


def test_a_revoked_worker_ends_its_whole_pack(world: World) -> None:
    from iki_server.runs.models import Worker

    launched = batch(world)
    pod = worker(world)
    pod.lease()
    held = Worker.objects.get()
    assert queue.interrupt_worker(held, code="worker_revoked") == 1
    assert set(statuses(launched)) == {"queued"}  # each retried


# --- Following a batch -------------------------------------------------------------------------


async def follow(client: AsyncClient, ref: str) -> Events:
    response = await client.get(f"/api/v1/batches/{ref}/events", secure=True)
    assert response.status_code == 200, response.content
    events = Events(response)
    ready = await events.next()
    assert ready is not None
    assert ready[0] == "ready"
    await asyncio.wait_for(hub().listening.wait(), 5)
    return events


@pytest.mark.django_db(transaction=True)
async def test_a_batchs_page_hears_as_its_runs_start_and_end() -> None:
    from iki_server.runs.models import Worker

    world: World = await sync_to_async(build_world)()
    launched: dict[str, Any] = await sync_to_async(batch)(world, 2)
    await sync_to_async(worker)(world)
    pod = await Worker.objects.aget(name="pod-1")
    events = await follow(await signed_in(world.users["viewer"]), launched["batch"]["hrid"])
    ids = [r["id"] for r in launched["runs"]]
    try:
        leased = await sync_to_async(queue.lease)(pod)
        assert leased is not None
        job, _ = leased
        assert job.lease_token is not None
        heard = {(await events.next() or ("", {}))[1]["id"] for _ in ids}
        assert heard == set(ids)
        await sync_to_async(queue.fail)(pod, job.pk, job.lease_token, "failed", "ValueError")
        heard = {(await events.next() or ("", {}))[1]["id"] for _ in ids}
        assert heard == set(ids)
    finally:
        await events.close()


@pytest.mark.django_db(transaction=True)
async def test_only_people_who_can_read_a_batch_follow_it() -> None:
    world: World = await sync_to_async(build_world)()
    launched: dict[str, Any] = await sync_to_async(batch)(world, 1)
    outsider = await signed_in(world.users["outsider"])
    path = f"/api/v1/batches/{launched['batch']['id']}/events"
    assert (await outsider.get(path, secure=True)).status_code == 404
    assert (await AsyncClient().get(path, secure=True)).status_code == 401


def test_an_error_about_an_entity_fails_only_its_run(world: World) -> None:
    launched = batch(world)
    pod = worker(world)
    job = pod.lease().json()
    entities = [e["id"] for e in job["inputs"]["protein"]]
    updates = weights(job, 1.5, 2.5, 3.5)
    updates["protein"].pop(1)
    document = output(
        updates=updates,
        messages=[{"level": "error", "text": "Residue 4 is unknown (B).", "entity": entities[1]}],
    )
    done(pod, job, document)
    assert statuses(launched) == ["succeeded", "failed", "succeeded"]
    failed = api_as(world).get(f"/runs/{launched['runs'][1]['id']}").json()
    assert failed["error"] == {
        "code": "failed",
        "message": "Residue 4 is unknown (B).",
        "errors": [],
    }
    assert failed["effects"] == []
    assert failed["messages"] == document["messages"]
