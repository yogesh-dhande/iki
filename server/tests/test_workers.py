"""Workers: registering, leasing jobs, reporting on them, retries and recovery (ADR 0043)."""

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from api_client import problem
from asgiref.sync import sync_to_async
from django.db import connection
from django.test import Client
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_runs import launch, protein

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import catalog, queue, workers
from iki_server.runs.models import ActionRun, Job, JobLog, JobState, Worker
from iki_server.runs.wakeup import waker
from iki_spec import schemas

pytestmark = pytest.mark.django_db

GI = 1024**3
#: protein-tools needs half a CPU and 512Mi.
ROOMY = {"cpu": 4, "memory": 16 * GI}


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """The lease view returns its connection to the pool between queries; inside a test's
    transaction that would close it, as Django's test client avoids for its own signals."""
    from iki_server.realtime import views

    if "transactional_db" not in request.fixturenames:
        monkeypatch.setattr(views, "close_old_connections", lambda: None)


class WorkerClient:
    """Calls ``/worker/v1`` as iki-worker does: JSON, with its own token."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.client = Client(raise_request_exception=False)

    def call(self, method: str, path: str, body: Any = None, **extra: Any) -> Any:
        return self.client.generic(
            method,
            f"/worker/v1{path}",
            "" if body is None else json.dumps(body),
            content_type="application/json",
            secure=True,
            headers={"Authorization": f"Bearer {self.token}"},
            **extra,
        )

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body)

    def lease(self) -> Any:
        return self.call("POST", "/lease?wait=0")

    def report(self, job: dict[str, Any], what: str, **body: Any) -> Any:
        return self.post(
            f"/jobs/{job['job']['id']}/{what}", {"lease_token": job["job"]["lease_token"], **body}
        )


def registration(world: World, tier: str = "backend") -> str:
    _, value = workers.create_registration(label="Pods", tier=tier, by=world.users["compute_admin"])
    return value


def worker(world: World, capacity: dict[str, Any] | None = None, name: str = "pod-1") -> Any:
    registering = WorkerClient(registration(world))
    made = registering.post(
        "/register", {"name": name, "version": "0.1.0", "capacity": capacity or ROOMY}
    )
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def queued(world: World, project: Project | None = None) -> dict[str, Any]:
    editor = api_as(world, "editor")
    target = project or world.project
    # A distinct, valid sequence each time.
    abl = protein(editor, target, name="ACDEFGHIKLMNPQRSTVWY"[: Job.objects.count() + 1])
    made = launch(editor, target, {"protein": abl["id"]})
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def expire(job: dict[str, Any]) -> None:
    Job.objects.filter(pk=job["job"]["id"]).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )


# --- Registering -------------------------------------------------------------------------------


def test_compute_admins_make_registration_tokens(world: World) -> None:
    admin = api_as(world, "compute_admin")
    made = admin.post("/compute/registration-tokens", {"label": "GPU pool", "tier": "vm"})
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["token"].startswith("iki_wreg_")
    assert body["hint"] == body["token"][-4:]
    assert made["Cache-Control"] == "no-store"
    listed = admin.get("/compute/registration-tokens").json()["items"]
    assert [t["label"] for t in listed] == ["GPU pool"]
    assert "token" not in listed[0]
    assert AuditEvent.objects.filter(action="worker_registration.created").count() == 1
    # Client-tier workers sign in as their person (commit 30), not with a shared token.
    errors = problem(
        admin.post("/compute/registration-tokens", {"label": "Laptops", "tier": "client"}),
        422,
        "validation_failed",
    )["errors"]
    assert errors[0]["pointer"] == "#/tier"
    past = timezone.now() - timedelta(minutes=1)
    expired = admin.post(
        "/compute/registration-tokens",
        {"label": "Old", "tier": "vm", "expires_at": past.isoformat()},
    )
    assert problem(expired, 422, "validation_failed")["errors"][0]["pointer"] == "#/expires_at"


def test_workers_register_with_a_registration_token(world: World) -> None:
    value = registration(world)
    made = WorkerClient(value).post(
        "/register", {"name": "pod-1", "version": "0.1.0", "capacity": ROOMY}
    )
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["tier"] == "backend"
    assert body["token"].startswith("iki_wkr_")
    assert body["heartbeat_every"] == 20
    me = WorkerClient(body["token"]).call("GET", "/me").json()
    assert me["name"] == "pod-1"
    assert me["capacity"] == {
        "cpu": 4.0,
        "memory": 16 * GI,
        "gpu_count": 0,
        "gpu_memory": 0,
        "scratch_disk": 0,
    }
    stored = Worker.objects.get()
    assert body["token"] not in (stored.token_hash, stored.hint)
    assert AuditEvent.objects.get(action="worker.registered").via == "system"
    # A registration token isn't a worker token, and the other way round.
    problem(WorkerClient(value).call("GET", "/me"), 401, "not_authenticated")
    problem(
        WorkerClient(body["token"]).post(
            "/register", {"name": "x", "version": "0.1.0", "capacity": ROOMY}
        ),
        401,
        "not_authenticated",
    )


def test_bad_tokens_are_refused_with_a_worker_challenge(world: World) -> None:
    for token in ("", "iki_wkr_nope", "iki_wreg_nope"):
        answer = WorkerClient(token).call("GET", "/me")
        assert answer.status_code == 401
        assert answer["WWW-Authenticate"] == 'Bearer realm="iki-worker"'
    assert WorkerClient("iki_wkr_nope").lease().status_code == 401


def test_revoked_and_expired_registration_tokens_register_no_one(world: World) -> None:
    admin = api_as(world, "compute_admin")
    value = registration(world)
    pod = worker(world)  # registers with a token of its own
    target = admin.get("/compute/registration-tokens").json()["items"]
    reg_id = next(t["id"] for t in target if t["hint"] == value[-4:])
    revoked = admin.post(f"/compute/registration-tokens/{reg_id}/revoke", {})
    assert revoked.status_code == 200, revoked.content
    assert revoked.json()["revoked_at"] is not None
    attempt = WorkerClient(value).post(
        "/register", {"name": "pod-2", "version": "0.1.0", "capacity": ROOMY}
    )
    problem(attempt, 401, "not_authenticated")
    # Workers that registered with another token are untouched.
    assert pod.call("GET", "/me").status_code == 200

    soon = workers.create_registration(
        label="Soon",
        tier="backend",
        by=world.users["compute_admin"],
        expires_at=timezone.now() - timedelta(seconds=1),
    )[1]
    late = WorkerClient(soon).post(
        "/register", {"name": "pod-3", "version": "0.1.0", "capacity": ROOMY}
    )
    problem(late, 401, "not_authenticated")


def test_revoking_a_leaked_registration_token_can_revoke_its_workers(world: World) -> None:
    admin = api_as(world, "compute_admin")
    value = registration(world)
    made = WorkerClient(value).post(
        "/register", {"name": "pod-1", "version": "0.1.0", "capacity": ROOMY}
    )
    pod = WorkerClient(made.json()["token"])
    reg = admin.get("/compute/registration-tokens").json()["items"][0]
    assert reg["workers"] == 1
    # By default its workers keep working, as GitHub's runners do.
    admin.post(f"/compute/registration-tokens/{reg['id']}/revoke", {})
    assert pod.call("GET", "/me").status_code == 200
    # Asked to, it revokes them too (again is fine: the token stays revoked).
    again = admin.post(f"/compute/registration-tokens/{reg['id']}/revoke", {"workers": True})
    assert again.json()["workers"] == 0
    problem(pod.call("GET", "/me"), 401, "not_authenticated")
    assert AuditEvent.objects.filter(action="worker_registration.revoked").count() == 1
    assert AuditEvent.objects.filter(action="worker.revoked").count() == 1


def test_compute_admins_see_and_revoke_workers(world: World) -> None:
    admin = api_as(world, "compute_admin")
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    listed = admin.get("/compute/workers").json()["items"]
    assert [(w["name"], w["status"], w["jobs"]) for w in listed] == [("pod-1", "online", 1)]
    Worker.objects.update(last_seen_at=timezone.now() - timedelta(minutes=5))
    assert admin.get("/compute/workers").json()["items"][0]["status"] == "offline"
    revoked = admin.post(f"/compute/workers/{listed[0]['id']}/revoke")
    assert revoked.status_code == 200, revoked.content
    assert revoked.json()["status"] == "revoked"
    assert admin.get("/compute/workers").json()["items"] == []
    assert len(admin.get("/compute/workers", revoked="true").json()["items"]) == 1
    problem(pod.call("GET", "/me"), 401, "not_authenticated")
    # Its job was interrupted, and (worker_lost is retried by default) queued again.
    first = Job.objects.get(pk=job["job"]["id"])
    assert first.state == JobState.INTERRUPTED
    assert first.error == {"code": "worker_lost", "message": "The worker was revoked."}
    assert ActionRun.objects.get(pk=run["id"]).status == "queued"
    assert Job.objects.filter(run_id=run["id"], attempt=2, state=JobState.QUEUED).exists()


def test_workers_can_deregister(world: World) -> None:
    pod = worker(world)
    assert pod.post("/deregister").status_code == 204
    problem(pod.call("GET", "/me"), 401, "not_authenticated")


# --- Leasing -----------------------------------------------------------------------------------


def test_a_worker_leases_a_queued_job_with_what_it_needs(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    assert leased["Cache-Control"] == "no-store"
    job = leased.json()
    assert not list(schemas.check("run-input", job))
    assert job["run"]["hrid"] == run["hrid"]
    assert job["run"]["attempt"] == 1
    assert job["command"][0] == "python"
    assert job["environment"] == "protein.protein-tools"
    assert job["params"] == run["params"]
    (entity,) = job["inputs"]["protein"]
    assert entity["hrid"] == run["inputs"][0]["hrid"]
    assert entity["revision"] == 1
    assert entity["values"]["sequence"].startswith("MKV")
    assert job["job"]["resources"]["cpu"] == 0.5  # protein-tools asks for half a CPU
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "running"
    assert body["started_at"] is not None
    # Nothing else is queued.
    assert pod.lease().status_code == 204


def test_the_command_reads_the_revision_the_run_was_launched_on(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    editor = api_as(world, "editor")
    entity = editor.get(f"/entities/{run['inputs'][0]['entity']}")
    changed = editor.patch(
        f"/entities/{run['inputs'][0]['entity']}",
        {"values": {"organism": "Homo sapiens"}},
        headers={"If-Match": entity["ETag"]},
    )
    assert changed.status_code == 200, changed.content
    (entity_in,) = pod.lease().json()["inputs"]["protein"]
    assert entity_in["revision"] == 1
    assert entity_in["values"]["organism"] is None


def test_workers_lease_only_what_fits_on_their_tier(world: World) -> None:
    small = worker(world, {"cpu": 0.25, "memory": 16 * GI}, name="small")
    tight = worker(world, {"cpu": 4, "memory": 256 * 1024**2}, name="tight")
    vm_value = registration(world, tier="vm")
    vm = WorkerClient(
        WorkerClient(vm_value)
        .post("/register", {"name": "vm", "version": "0.1.0", "capacity": ROOMY})
        .json()["token"]
    )
    queued(world)
    assert small.lease().status_code == 204
    assert tight.lease().status_code == 204
    assert vm.lease().status_code == 204
    assert worker(world, name="roomy").lease().status_code == 200


def test_projects_with_fewer_jobs_running_go_first(world: World) -> None:
    pod = worker(world)
    other = Project.objects.create(team=world.project.team, name="Other")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    busy = [queued(world), queued(world)]
    late = queued(world, other)
    first = pod.lease().json()
    assert first["run"]["id"] == busy[0]["id"]
    # world.project now has one running: the other project's (newer) run goes next.
    assert pod.lease().json()["run"]["id"] == late["id"]
    assert pod.lease().json()["run"]["id"] == busy[1]["id"]


# --- Reporting ---------------------------------------------------------------------------------


def test_heartbeats_keep_the_lease_and_move_the_job_along(world: World) -> None:
    pod = worker(world)
    queued(world)
    job = pod.lease().json()
    before = Job.objects.get(pk=job["job"]["id"]).lease_expires_at
    beat = pod.report(job, "heartbeat", state="staging")
    assert beat.status_code == 200, beat.content
    assert beat.json()["cancel_requested"] is False
    stored = Job.objects.get(pk=job["job"]["id"])
    assert stored.state == JobState.STAGING
    assert stored.lease_expires_at is not None and before is not None
    assert stored.lease_expires_at >= before
    assert pod.report(job, "heartbeat", state="running").status_code == 200
    assert pod.report(job, "heartbeat").status_code == 200
    back = problem(pod.report(job, "heartbeat", state="staging"), 409, "wrong_state")
    assert "running back to staging" in back["detail"]
    progress = pod.report(job, "progress", fraction=0.5, message="Halfway")
    assert progress.status_code == 204
    stored.refresh_from_db()
    assert (stored.progress, stored.progress_message) == (0.5, "Halfway")


def test_only_the_current_lease_may_report(world: World) -> None:
    pod = worker(world)
    queued(world)
    job = pod.lease().json()
    wrong = {**job, "job": {**job["job"], "lease_token": "00000000-0000-0000-0000-000000000000"}}
    problem(pod.report(wrong, "heartbeat"), 409, "stale_lease")
    # Another worker can't report on it either.
    problem(worker(world, name="pod-2").report(job, "heartbeat"), 409, "stale_lease")
    expire(job)
    problem(pod.report(job, "heartbeat"), 409, "stale_lease")
    problem(pod.report(job, "fail", reason="failed"), 409, "stale_lease")


def test_logs_arrive_in_order(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    pod = worker(world)
    queued(world)
    job = pod.lease().json()
    first = pod.report(job, "logs", offset=0, text="héllo\n")
    assert first.json() == {"next_offset": 7}
    # Sent again after a timeout: accepted, not duplicated.
    assert pod.report(job, "logs", offset=0, text="héllo\n").json() == {"next_offset": 7}
    gap = problem(pod.report(job, "logs", offset=10, text="x"), 409, "log_gap")
    assert gap["errors"] == [{"pointer": "#/offset", "detail": "Send from byte 7."}]
    monkeypatch.setattr(queue, "MAX_LOG_BYTES", 10)
    assert pod.report(job, "logs", offset=7, text="world\n").json() == {"next_offset": 13}
    assert pod.report(job, "logs", offset=13, text="more\x00").json() == {"next_offset": 18}
    texts = list(JobLog.objects.filter(job_id=job["job"]["id"]).values_list("text", flat=True))
    assert texts[0] == "héllo\n"
    assert texts[1].startswith("wor\n[iki: the log is over")
    assert len(texts) == 2


def test_a_failed_attempt_ends_the_run(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    failed = pod.report(job, "fail", reason="failed", message="ValueError: bad sequence")
    assert failed.json() == {"run_status": "failed"}
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "failed"
    assert body["error"] == {"code": "failed", "message": "ValueError: bad sequence"}
    assert body["attempts"] == 1
    problem(pod.report(job, "heartbeat"), 409, "stale_lease")


def test_interrupted_attempts_are_retried_then_give_up(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    # compute_properties keeps the default: two retries, on worker_lost and interrupted.
    for attempt in (1, 2, 3):
        Job.objects.filter(run_id=run["id"]).update(queued_at=timezone.now())
        job = pod.lease().json()
        assert job["run"]["attempt"] == attempt
        status = pod.report(job, "fail", reason="interrupted").json()["run_status"]
        assert status == ("queued" if attempt < 3 else "failed")
    assert api_as(world).get(f"/runs/{run['id']}").json()["attempts"] == 3


def test_a_retry_waits_a_little(world: World) -> None:
    pod = worker(world)
    queued(world)
    pod.report(pod.lease().json(), "fail", reason="interrupted")
    assert pod.lease().status_code == 204


def test_a_cancelled_run_ends_cancelled(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    api_as(world).post(f"/runs/{run['id']}/cancel", {})
    assert pod.report(job, "heartbeat").json()["cancel_requested"] is True
    # Whatever the worker says next, the run was cancelled, and isn't retried.
    assert pod.report(job, "fail", reason="interrupted").json() == {"run_status": "cancelled"}
    assert Job.objects.filter(run_id=run["id"]).count() == 1


# --- Recovery ----------------------------------------------------------------------------------


def test_jobs_whose_worker_went_away_are_retried(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    expire(job)
    assert queue.recover_expired() == 1
    ended = Job.objects.get(pk=job["job"]["id"])
    assert ended.state == JobState.INTERRUPTED
    assert ended.error == {
        "code": "worker_lost",
        "message": "The worker stopped sending heartbeats.",
    }
    assert ActionRun.objects.get(pk=run["id"]).status == "queued"
    assert queue.recover_expired() == 0


def test_jobs_running_far_past_their_timeout_are_ended(world: World) -> None:
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    Job.objects.filter(pk=job["job"]["id"]).update(
        started_at=timezone.now() - timedelta(seconds=300 + 6 * 60)
    )
    assert queue.recover_expired() == 1
    assert Job.objects.get(pk=job["job"]["id"]).state == JobState.TIMED_OUT
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "timed_out"
    assert "300s timeout" in body["error"]["message"]


# --- Waiting for work --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_waiting_workers_are_woken_when_a_job_is_queued_on_their_tier() -> None:
    wakes = waker()
    backend = asyncio.ensure_future(wakes.wait("backend", 5))
    vm = asyncio.ensure_future(wakes.wait("vm", 0.5))
    await asyncio.wait_for(wakes.listening.wait(), 5)

    def send() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [queue.CHANNEL, "backend"])

    await sync_to_async(send)()
    assert await asyncio.wait_for(backend, 5) is True
    assert await vm is False
    await wakes.close()


def test_lease_takes_post_and_a_wait_of_up_to_30_seconds(world: World) -> None:
    pod = worker(world)
    assert pod.call("GET", "/lease").status_code == 405
    problem(pod.call("POST", "/lease?wait=31"), 422, "validation_failed")
    problem(pod.call("POST", "/lease?wait=soon"), 422, "validation_failed")


def test_queued_jobs_are_announced(world: World, django_capture_on_commit_callbacks: Any) -> None:
    with django_capture_on_commit_callbacks() as callbacks:
        queued(world)
    assert callbacks, "launching should NOTIFY waiting workers"
