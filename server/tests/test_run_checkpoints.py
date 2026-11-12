"""Checkpoints (ADR 0069): a checkpointable run's worker saves its command's progress under
the attempt's lease, a later attempt resumes from the newest one whose bytes were checked
and records that it did, and they're deleted once a newer one is in or the run ends."""

import hashlib
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
import requests
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from org_world import World
from test_api_entities import api_as
from test_files import run_tasks
from test_run_results import done, output
from test_runs import world
from test_workers import WorkerClient, keep_connection, queued, worker

from iki_server.files import storage
from iki_server.files.models import Upload, UploadState
from iki_server.runs import checkpoints
from iki_server.runs.models import ActionRun, Job, JobState, RunCheckpoint

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]

SAVED = b"step 41\n" * 64


@pytest.fixture(autouse=True)
def storage_(s3: Any) -> Any:
    return s3


def checkpointable(run: dict[str, Any], **spec: Any) -> None:
    found = ActionRun.objects.get(pk=run["id"])
    ActionRun.objects.filter(pk=found.pk).update(
        spec={**found.spec, "checkpointable": True, **spec}
    )


def leased(world: World, **spec: Any) -> tuple[WorkerClient, dict[str, Any], dict[str, Any]]:
    """A checkpointable run, leased."""
    pod = worker(world)
    run = queued(world)
    checkpointable(run, **spec)
    return pod, run, pod.lease().json()


def save(pod: WorkerClient, job: dict[str, Any], data: bytes = SAVED) -> str:
    """Saves a checkpoint as iki-worker does: start, PUT the parts, finish until ready."""
    started = pod.report(
        job, "checkpoints", size=len(data), sha256=hashlib.sha256(data).hexdigest()
    )
    assert started.status_code == 201, started.content
    made = started.json()
    for part in made["parts"]:
        requests.put(part["url"], data=data, timeout=30).raise_for_status()
    for _ in range(5):
        finished = pod.report(job, f"uploads/{made['id']}/complete").json()
        if finished["state"] == "ready":
            break
        run_tasks()
    assert finished["state"] == "ready" and finished["file"] is None, finished
    return str(RunCheckpoint.objects.get(upload_id=made["id"]).pk)


def interrupted(pod: WorkerClient, job: dict[str, Any]) -> dict[str, Any]:
    """Ends the attempt as a stopped machine's worker does, and leases the next one."""
    answer = pod.report(job, "fail", reason="interrupted", message="Stopped.")
    assert answer.json()["run_status"] == "queued", answer.content
    Job.objects.filter(state=JobState.QUEUED).update(queued_at=timezone.now())
    leased: dict[str, Any] = pod.lease().json()
    return leased


def test_a_checkpointable_runs_worker_is_told_how_to_save(world: World) -> None:
    _, _, job = leased(world)
    assert job["job"]["checkpoint"] == {
        "save_every": 600,
        "save_on": ["interrupted"],
        "saved": None,
    }


def test_other_runs_have_no_checkpoint(world: World) -> None:
    pod = worker(world)
    queued(world)
    job = pod.lease().json()
    assert "checkpoint" not in job["job"]
    refused = pod.report(job, "checkpoints", size=1, sha256="0" * 64)
    assert refused.status_code == 409, refused.content
    assert "isn't checkpointable" in refused.json()["detail"]


def test_it_saves_after_every_ending_the_run_is_retried_for(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    checkpointable(run)
    ActionRun.objects.filter(pk=run["id"]).update(
        retry_on=["failed", "timed_out", "interrupted", "worker_lost"]
    )
    job = pod.lease().json()
    assert job["job"]["checkpoint"]["save_on"] == ["failed", "timed_out", "interrupted"]


def test_an_interrupted_attempt_resumes_from_its_checkpoint(world: World) -> None:
    pod, run, job = leased(world)
    saved = save(pod, job)

    second = interrupted(pod, job)

    given = second["job"]["checkpoint"]
    found = RunCheckpoint.objects.select_related("upload").get(pk=saved)
    assert given["saved"] == {
        "id": saved,
        "attempt": 1,
        "saved_at": found.created_at.isoformat(),
        "size": len(SAVED),
        "sha256": hashlib.sha256(SAVED).hexdigest(),
    }
    url = pod.report(second, f"checkpoints/{saved}/download")
    assert url.status_code == 200, url.content
    assert url.json()["sha256"] == given["saved"]["sha256"]
    assert requests.get(url.json()["url"], timeout=30).content == SAVED
    restored = pod.report(second, f"checkpoints/{saved}/restored")
    assert restored.status_code == 204, restored.content
    assert Job.objects.get(pk=second["job"]["id"]).resumed_from_id == found.pk

    attempts = api_as(world, "viewer").get(f"/runs/{run['hrid']}/attempts").json()
    assert attempts[0]["checkpoint"]["attempt"] == 1
    assert attempts[0]["checkpoint"]["kept"] is True
    assert attempts[1]["checkpoint"] is None
    assert attempts[1]["resumed_from"] == attempts[0]["checkpoint"]

    # Once the run ends, its checkpoints go; their records stay.
    assert done(pod, second, output(metrics={"score": 1.0}))["run_status"] == "succeeded"
    run_tasks()
    found.refresh_from_db()
    assert found.deleted_at is not None
    assert not storage.exists(storage.checkpoint_key(found.upload_id))
    attempts = api_as(world).get(f"/runs/{run['hrid']}/attempts").json()
    assert attempts[1]["resumed_from"]["kept"] is False


def test_only_a_later_attempt_at_the_same_run_gets_a_checkpoint(world: World) -> None:
    pod, _, job = leased(world)
    saved = save(pod, job)
    # The attempt that saved it doesn't restore it.
    for what in ("download", "restored"):
        assert pod.report(job, f"checkpoints/{saved}/{what}").status_code == 404
    # Nor does another run's.
    other = worker(world, name="pod-2")
    run = queued(world)
    checkpointable(run)
    theirs = other.lease().json()
    for what in ("download", "restored"):
        assert other.report(theirs, f"checkpoints/{saved}/{what}").status_code == 404
    # Nor anyone without the lease.
    second = interrupted(pod, job)
    stale = {**second, "job": {**second["job"], "lease_token": job["job"]["lease_token"]}}
    assert pod.report(stale, f"checkpoints/{saved}/download").status_code == 409


def test_a_newer_checkpoint_replaces_the_older(world: World) -> None:
    pod, _, job = leased(world)
    first = save(pod, job)
    newer = save(pod, job, b"step 42\n")

    older = RunCheckpoint.objects.get(pk=first)
    assert older.deleted_at is not None
    assert not storage.exists(storage.checkpoint_key(older.upload_id))
    second = interrupted(pod, job)
    assert second["job"]["checkpoint"]["saved"]["id"] == newer


def test_a_later_attempt_waits_while_its_checkpoint_is_checked(world: World) -> None:
    pod, _, job = leased(world)
    started = pod.report(
        job, "checkpoints", size=len(SAVED), sha256=hashlib.sha256(SAVED).hexdigest()
    ).json()
    requests.put(started["parts"][0]["url"], data=SAVED, timeout=30).raise_for_status()
    assert pod.report(job, f"uploads/{started['id']}/complete").json()["state"] == "checking"
    pod.report(job, "fail", reason="interrupted", message="Stopped.")
    Job.objects.filter(state=JobState.QUEUED).update(queued_at=timezone.now())

    assert pod.lease().status_code == 204
    run_tasks()
    second = pod.lease().json()
    assert second["job"]["checkpoint"]["saved"]["attempt"] == 1


def test_the_last_attempt_saves_nothing(world: World) -> None:
    pod, run, job = leased(world)
    ActionRun.objects.filter(pk=run["id"]).update(max_attempts=2)
    save(pod, job)
    last = interrupted(pod, job)
    assert last["job"]["checkpoint"]["save_every"] is None
    assert last["job"]["checkpoint"]["save_on"] == []
    assert last["job"]["checkpoint"]["saved"]["attempt"] == 1


def test_a_cancelled_run_waiting_to_resume_deletes_its_checkpoint(world: World) -> None:
    pod, run, job = leased(world)
    saved = save(pod, job)
    pod.report(job, "fail", reason="interrupted", message="Stopped.")
    assert api_as(world, "editor").post(f"/runs/{run['id']}/cancel", {}).status_code == 200
    run_tasks()
    assert RunCheckpoint.objects.get(pk=saved).deleted_at is not None


def test_a_new_save_cancels_one_left_unfinished(world: World) -> None:
    pod, _, job = leased(world)
    unfinished = pod.report(job, "checkpoints", size=3, sha256="0" * 64).json()
    save(pod, job)
    assert Upload.objects.get(pk=unfinished["id"]).state == UploadState.CANCELLED


def test_maintenance_deletes_what_no_attempt_will_resume_from(world: World) -> None:
    pod, run, job = leased(world)
    saved = save(pod, job)
    # Ended without the task that deletes its checkpoints having run.
    ActionRun.objects.filter(pk=run["id"]).update(status="failed", finished_at=timezone.now())
    out = StringIO()
    call_command("maintain", stdout=out)
    assert "deleted 1 run checkpoint(s) nothing will resume from." in out.getvalue()
    assert RunCheckpoint.objects.get(pk=saved).deleted_at is not None
    assert checkpoints.delete_stale() == 0


def test_people_cant_send_parts_for_a_runs_checkpoint(world: World) -> None:
    """The launcher is who a run's uploads are made for, but only its worker sends them."""
    pod, _, job = leased(world)
    started = pod.report(job, "checkpoints", size=3, sha256="0" * 64).json()
    editor = api_as(world, "editor")
    assert editor.post(f"/uploads/{started['id']}/parts", {}).status_code == 404
    assert editor.post(f"/uploads/{started['id']}/complete", {}).status_code == 404
    assert editor.get(f"/uploads/{started['id']}").status_code == 404
    assert editor.get("/uploads").json()["items"] == []


@override_settings(IKI_FILES_MAX_UPLOAD_BYTES=10)
def test_a_checkpoint_is_no_bigger_than_an_upload_may_be(world: World) -> None:
    pod, _, job = leased(world)
    refused = pod.report(job, "checkpoints", size=11, sha256="0" * 64)
    assert refused.status_code == 422, refused.content


def test_ready_checkpoints_stay_while_the_run_may_resume(world: World) -> None:
    pod, _, job = leased(world)
    saved = save(pod, job)
    interrupted(pod, job)
    assert checkpoints.delete_stale() == 0
    assert RunCheckpoint.objects.get(pk=saved).deleted_at is None
    assert timezone.now() - RunCheckpoint.objects.get(pk=saved).created_at < timedelta(minutes=1)
