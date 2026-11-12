"""Runs' artifacts (ADR 0068): files a run's command wrote that its action keeps, uploaded
by the worker whether the run succeeded or not, listed on the run, downloadable by anyone
who can read it, and deleted, every version, once their retention is over. Their records
stay."""

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pytest
import requests
from django.test import override_settings
from django.utils import timezone
from org_world import World
from test_api_entities import api_as
from test_files import run_tasks
from test_run_results import done, output
from test_runs import launch, protein, world
from test_workers import WorkerClient, keep_connection, worker

from iki_server.audit.models import AuditEvent
from iki_server.files import storage
from iki_server.runs import artifacts
from iki_server.runs.models import RunArtifact

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]

ALIGN = "protein.align_pair"
ALIGNMENT = b"# Aligned\nX-1  1 MKV  3\n     |||\nX-2  1 MKV  3\n"


@pytest.fixture(autouse=True)
def storage_(s3: Any) -> Any:
    return s3


def aligning(world: World) -> tuple[WorkerClient, dict[str, Any], dict[str, Any]]:
    """An align_pair run, leased."""
    editor = api_as(world)
    a, b = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    run = launch(editor, world.project, {"query": a["id"], "target": b["id"]}, ALIGN)
    assert run.status_code == 201, run.content
    pod = worker(world)
    job = pod.lease().json()
    return pod, run.json(), job


def keep(pod: WorkerClient, job: dict[str, Any], path: str, data: bytes = ALIGNMENT) -> Any:
    return pod.report(
        job,
        "artifacts",
        path=path,
        media_type="text/plain",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def kept(pod: WorkerClient, job: dict[str, Any], path: str, data: bytes = ALIGNMENT) -> str:
    """Uploads an artifact as iki-worker does: start, PUT the parts, finish until ready."""
    started = keep(pod, job, path, data)
    assert started.status_code == 201, started.content
    made = started.json()
    assert made["file"] is None
    for part in made["parts"]:
        requests.put(part["url"], data=data, timeout=30).raise_for_status()
    for _ in range(5):
        finished = pod.report(job, f"uploads/{made['id']}/complete").json()
        if finished["state"] == "ready":
            break
        run_tasks()
    assert finished["state"] == "ready" and finished["file"] is None, finished
    return str(made["id"])


def refusal(response: Any) -> str:
    """Why the worker's artifact was refused: invalid, for the path it gave."""
    assert response.status_code == 422, response.content
    (error,) = response.json()["errors"]
    detail: str = error["detail"]
    return detail


def listed(world: World, run: dict[str, Any], principal: str = "editor") -> Any:
    return api_as(world, principal).get(f"/runs/{run['hrid']}/artifacts")


def test_a_run_keeps_its_artifacts_for_its_readers(world: World) -> None:
    pod, run, job = aligning(world)
    assert job["job"]["artifacts"] == ["alignment.txt", "alignment.fasta"]

    kept(pod, job, "alignment.txt")
    kept(pod, job, "alignment.fasta", b">X-1\nMKV\n>X-2\nMKV\n")
    answer = done(pod, job, output(metrics={"score": 15.0, "identity_percent": 100}))

    assert answer["run_status"] == "succeeded"
    found = listed(world, run, "viewer")
    assert found.status_code == 200, found.content
    items = found.json()
    assert [(a["attempt"], a["path"], a["state"]) for a in items] == [
        (1, "alignment.fasta", "kept"),
        (1, "alignment.txt", "kept"),
    ]
    text = items[1]
    assert text["name"] == "alignment.txt" and text["media_type"] == "text/plain"
    assert text["size"] == len(ALIGNMENT)
    assert text["sha256"] == hashlib.sha256(ALIGNMENT).hexdigest()
    retention = datetime.fromisoformat(text["expires_at"]) - timezone.now()
    assert timedelta(days=89) < retention <= timedelta(days=90)
    got = api_as(world, "viewer").get(f"/runs/{run['hrid']}/artifacts/{text['id']}/content")
    assert got.status_code == 302 and got["Cache-Control"] == "no-store"
    downloaded = requests.get(got["Location"], timeout=30)
    assert downloaded.content == ALIGNMENT
    assert "attachment" in downloaded.headers["Content-Disposition"]
    # An artifact isn't a file: it's nowhere in the project's files.
    files = api_as(world).get("/files", project=str(world.project.pk)).json()["items"]
    assert files == []
    assert listed(world, run, "outsider").status_code == 404


def test_a_failed_run_keeps_them_too(world: World) -> None:
    pod, run, job = aligning(world)
    kept(pod, job, "alignment.txt")

    failed = pod.report(job, "fail", reason="failed", message="It fell over.")

    assert failed.json()["run_status"] == "failed"
    assert [a["state"] for a in listed(world, run).json()] == ["kept"]


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("output.json", "doesn't keep output.json: it keeps alignment.txt, alignment.fasta"),
        ("../alignment.txt", "relative to the output directory"),
        ("/alignment.txt", "relative to the output directory"),
        ("a//alignment.txt", "relative to the output directory"),
    ],
)
def test_only_what_its_action_keeps_is_kept(world: World, path: str, why: str) -> None:
    pod, _, job = aligning(world)

    refused = keep(pod, job, path)

    assert why in refusal(refused)
    assert not RunArtifact.objects.exists()


def test_an_attempt_keeps_each_path_once_and_a_stale_lease_none(world: World) -> None:
    pod, _, job = aligning(world)
    kept(pod, job, "alignment.txt")

    again = keep(pod, job, "alignment.txt")

    assert "already kept" in refusal(again)
    stale = {**job, "job": {**job["job"], "lease_token": "00000000-0000-7000-8000-000000000000"}}
    assert keep(pod, stale, "alignment.fasta").status_code == 409


def test_an_action_that_keeps_none_has_none_kept(world: World) -> None:
    editor = api_as(world)
    p = protein(editor, world.project)
    assert launch(editor, world.project, {"protein": p["id"]}).status_code == 201
    pod = worker(world)
    job = pod.lease().json()
    assert "artifacts" not in job["job"]

    refused = keep(pod, job, "alignment.txt")

    assert "keeps no artifacts" in refusal(refused)


@override_settings(IKI_FILES_ARTIFACT_RETENTION=7 * 86400)
def test_the_deployment_sets_how_long_unless_the_action_says(world: World) -> None:
    pod, _, job = aligning(world)
    kept(pod, job, "alignment.txt")
    stored = RunArtifact.objects.get()
    assert stored.expires_at - stored.created_at < timedelta(days=7, minutes=1)
    with override_settings(IKI_FILES_ARTIFACT_MIN_RETENTION=3650 * 86400):
        assert artifacts.retention(stored.run) == timedelta(days=3650)
    stored.run.spec["artifact_retention"] = 365 * 86400
    assert artifacts.retention(stored.run) == timedelta(days=365)


def test_retention_over_deletes_their_bytes_every_version_and_records_it(world: World) -> None:
    pod, run, job = aligning(world)
    upload_id = kept(pod, job, "alignment.txt")
    kept(pod, job, "alignment.fasta", b">X\nM\n")
    done(pod, job, output(metrics={"score": 1.0}))
    key = storage.artifact_key(upload_id)
    assert storage.exists(key)
    RunArtifact.objects.filter(path="alignment.txt").update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    assert artifacts.delete_expired() == 1
    assert artifacts.delete_expired() == 0

    assert (
        not storage.client()
        .list_object_versions(Bucket=storage.bucket(), Prefix=key)
        .get("Versions")
    )
    items = {a["path"]: a for a in listed(world, run).json()}
    assert items["alignment.txt"]["state"] == "deleted"
    assert items["alignment.txt"]["deleted_at"] is not None
    assert items["alignment.txt"]["sha256"] == hashlib.sha256(ALIGNMENT).hexdigest()
    assert items["alignment.fasta"]["state"] == "kept"
    gone = api_as(world).get(
        f"/runs/{run['hrid']}/artifacts/{items['alignment.txt']['id']}/content"
    )
    assert gone.status_code == 410 and gone.json()["code"] == "artifact_deleted"
    event = AuditEvent.objects.get(action="run.artifacts_deleted")
    assert str(event.target_id) == run["id"] and event.project_id == world.project.pk
    assert event.actor is None
    assert event.data == {
        "count": 1,
        "bytes": len(ALIGNMENT),
        "retention_days": 90,
        "paths": ["alignment.txt"],
    }
