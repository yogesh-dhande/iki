"""The job queue: leasing jobs to workers, what they report, and retries (ADR 0043).

A worker leases the best queued job it can run: on its tier, that fits its
capacity, and (on the client tier) its own person's. Projects with fewer
jobs running go first, so one project's batch can't starve the rest; then
higher priority, then older. Rows are claimed with ``FOR UPDATE SKIP
LOCKED``, so workers never wait on each other.

Every lease has a new token and expires unless the worker's heartbeats
renew it. Every report names the token. A worker whose lease ran out (or
whose job was retried elsewhere) gets ``Stale``, so it can't change what
replaced it: a fencing token.

An attempt that ends badly is retried as a new job when the action's
``retries.on`` lists the reason and attempts remain, after a short backoff.
Otherwise the run ends. A cancelled run ends cancelled, whatever happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Now
from django.utils import timezone

from iki_server.entities import registry, services
from iki_server.entities.values import shape, to_json
from iki_server.runs.models import ActionRun, Job, JobLog, JobState, RunInput, RunStatus, Worker

__all__ = [
    "HEARTBEAT_EVERY",
    "LEASE_TTL",
    "LogGap",
    "Stale",
    "append_log",
    "fail",
    "heartbeat",
    "interrupt_worker",
    "lease",
    "notify",
    "payload",
    "progress",
    "recover_expired",
]

CHANNEL = "iki_jobs"
#: A lease lasts this long unless a heartbeat renews it.
LEASE_TTL = timedelta(seconds=60)
#: How often workers should send heartbeats (they're told when they lease).
HEARTBEAT_EVERY = timedelta(seconds=20)
#: How long past its timeout a job may run before the server ends it, if its worker didn't.
TIMEOUT_GRACE = timedelta(minutes=5)
#: The most log one job keeps; past this, chunks are dropped and the log says so.
MAX_LOG_BYTES = 16 * 1024**2
#: Retries wait this long times their attempt number, so a failing dependency gets a moment.
RETRY_BACKOFF = timedelta(seconds=10)

#: States in which a worker holds the job.
HELD = (JobState.LEASED, JobState.STAGING, JobState.RUNNING, JobState.UPLOADING)
#: The order a worker may report them in.
PHASES = (JobState.LEASED, JobState.STAGING, JobState.RUNNING, JobState.UPLOADING)

#: Why an attempt ended → the run's status, if it isn't retried.
_FINAL = {
    "failed": RunStatus.FAILED,
    "timed_out": RunStatus.TIMED_OUT,
    "interrupted": RunStatus.FAILED,
    "worker_lost": RunStatus.FAILED,
}


class Stale(Exception):  # noqa: N818 - reads as "the lease is stale"
    """The worker doesn't hold this job's current lease."""


class LogGap(Exception):  # noqa: N818 - reads as "there's a gap in the log"
    """A log chunk didn't start where the log ends."""

    def __init__(self, expected: int) -> None:
        super().__init__(f"The log continues at byte {expected}.")
        self.expected = expected


def notify(tier: str) -> None:
    """Wakes workers waiting for a job on `tier`, once the transaction commits."""

    def send() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, tier])

    transaction.on_commit(send)


# --- Leasing -----------------------------------------------------------------------------------


def _fits(worker: Worker) -> Q:
    """Jobs whose resources fit the worker's capacity."""
    fits = Q()
    for key in ("cpu", "memory", "gpu_count", "gpu_memory", "scratch_disk"):
        have = worker.capacity.get(key) or 0
        needs_nothing = (
            ~Q(resources__has_key=key)
            | Q(**{f"resources__{key}": None})
            | Q(**{f"resources__{key}": 0})
        )
        fits &= needs_nothing | Q(**{f"resources__{key}__lte": have})
    return fits


def lease(worker: Worker) -> tuple[Job, dict[str, Any]] | None:
    """Leases the best job this worker can run, with what it needs to run it; None if none."""
    now = timezone.now()
    active = (
        Job.objects.filter(project=OuterRef("project"), state__in=HELD)
        .values("project")
        .annotate(n=Count("pk"))
        .values("n")
    )
    candidates = Job.objects.filter(
        _fits(worker), state=JobState.QUEUED, tier=worker.tier, queued_at__lte=Now()
    )
    if worker.tier == "client":
        candidates = candidates.filter(owner=worker.owner_id)
    with transaction.atomic():
        job = (
            candidates.annotate(active=Coalesce(Subquery(active), 0))
            .order_by("active", "-priority", "queued_at", "id")
            .select_for_update(skip_locked=True, of=("self",))
            .first()
        )
        if job is None:
            return None
        job.state = JobState.LEASED
        job.worker = worker
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + LEASE_TTL
        job.started_at = now
        job.save(update_fields=["state", "worker", "lease_token", "lease_expires_at", "started_at"])
        run = ActionRun.objects.select_for_update().get(pk=job.run_id)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or now
        run.save(update_fields=["status", "started_at"])
        return job, payload(job, run)


def payload(job: Job, run: ActionRun) -> dict[str, Any]:
    """The run's input, as ``spec/…/run-input/v1`` describes it: what the command reads."""
    inputs: dict[str, list[dict[str, Any]]] = {name: [] for name in run.spec["inputs"]}
    rows = RunInput.objects.filter(run=run).select_related("entity").order_by("name", "position")
    for row in rows:
        entity = row.entity
        values = services.values_at(entity, row.revision)
        fields = shape(entity.type).fields()
        inputs.setdefault(row.name, []).append(
            {
                "id": str(entity.pk),
                "hrid": entity.hrid,
                "type": entity.type,
                "schema_version": registry.types()[entity.type].schema_version,
                "revision": row.revision,
                "values": {n: to_json(fields[n], v) for n, v in values.items() if n in fields},
            }
        )
    assert job.lease_token is not None and job.lease_expires_at is not None  # noqa: S101
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "run": {
            "id": str(run.pk),
            "hrid": run.hrid,
            "attempt": job.attempt,
            "action": run.action,
            "plugin": {
                "name": run.action.split(".")[0],
                "version": run.plugin_version,
                "sha": run.plugin_sha or None,
            },
            "project": str(run.project_id),
        },
        "job": {
            "id": str(job.pk),
            "lease_token": str(job.lease_token),
            "lease_expires_at": job.lease_expires_at.isoformat(),
            "heartbeat_every": int(HEARTBEAT_EVERY.total_seconds()),
            "timeout": run.timeout,
            "resources": job.resources,
        },
        "command": run.spec["command"],
        "environment": f"{run.action.split('.')[0]}.{run.environment}",
        "params": run.params,
        "inputs": inputs,
    }


# --- Reports from the worker -------------------------------------------------------------------


def _held(worker: Worker, job_id: uuid.UUID, token: uuid.UUID) -> Job:
    """The job, locked, if this worker holds its current lease."""
    job = Job.objects.select_for_update().filter(pk=job_id, worker=worker).first()
    if job is None or job.state not in HELD or job.lease_token != token:
        raise Stale
    if job.lease_expires_at is None or job.lease_expires_at <= timezone.now():
        raise Stale
    return job


def heartbeat(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, state: str | None = None
) -> tuple[datetime, bool]:
    """Renews the lease, and moves the job on to `state`. Returns the new expiry, and whether
    the run's been cancelled (the worker should stop its command and report ``cancelled``)."""
    with transaction.atomic():
        job = _held(worker, job_id, token)
        fields = ["lease_expires_at"]
        if state is not None and state != job.state:
            if state not in PHASES or PHASES.index(JobState(state)) < PHASES.index(
                JobState(job.state)
            ):
                raise ValueError(f"A job can't go from {job.state} back to {state}.")
            job.state = state
            fields.append("state")
        job.lease_expires_at = timezone.now() + LEASE_TTL
        job.save(update_fields=fields)
        cancelled = ActionRun.objects.filter(
            pk=job.run_id, cancel_requested_at__isnull=False
        ).exists()
        return job.lease_expires_at, cancelled


def progress(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, fraction: float, message: str
) -> None:
    with transaction.atomic():
        job = _held(worker, job_id, token)
        job.progress = max(0.0, min(1.0, fraction))
        job.progress_message = message[:500]
        job.save(update_fields=["progress", "progress_message"])


def append_log(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, offset: int, text: str) -> int:
    """Adds a chunk at `offset` (in bytes of UTF-8) and returns where the next one goes.

    A chunk already received (the worker retried) is accepted again, unchanged; a gap or an
    overlap raises ``LogGap``, so the log stays exactly what the command wrote.
    """
    size = len(text.encode())
    with transaction.atomic():
        job = _held(worker, job_id, token)
        end = job.log_size
        if offset < end and offset + size <= end:
            return end  # already received: the worker retried
        if offset != end:
            raise LogGap(end)
        job.log_size = end + size
        job.save(update_fields=["log_size"])
        if end >= MAX_LOG_BYTES:
            return job.log_size  # past the limit: counted, not kept
        if end + size > MAX_LOG_BYTES:
            text = text.encode()[: MAX_LOG_BYTES - end].decode(errors="ignore")
            text += (
                f"\n[iki: the log is over {MAX_LOG_BYTES // 1024**2} MiB; the rest isn't kept]\n"
            )
        # Postgres text can't hold NUL.
        JobLog.objects.create(job=job, offset=offset, text=text.replace("\x00", "\ufffd"))
        return job.log_size


def fail(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, reason: str, message: str
) -> ActionRun:
    """The attempt ended without results: ``failed`` (the command's own error),
    ``timed_out``, ``interrupted`` (the machine was stopped) or ``cancelled``."""
    with transaction.atomic():
        job = _held(worker, job_id, token)
        state = {
            "failed": JobState.FAILED,
            "timed_out": JobState.TIMED_OUT,
            "interrupted": JobState.INTERRUPTED,
            "cancelled": JobState.CANCELLED,
        }[reason]
        return _end_attempt(job, state, reason, message[:2000])


def interrupt_worker(worker: Worker, *, code: str) -> int:
    """Ends every attempt the worker holds as interrupted (it was revoked or went away)."""
    count = 0
    for job in Job.objects.select_for_update().filter(worker=worker, state__in=HELD):
        _end_attempt(
            job,
            JobState.INTERRUPTED,
            "worker_lost",
            f"The worker was {code.removeprefix('worker_')}.",
        )
        count += 1
    return count


def recover_expired() -> int:
    """Ends attempts whose lease ran out (the worker went away), and those running well past
    their timeout. The task runner calls it every few seconds. Returns how many it ended."""
    now = timezone.now()
    ended = 0
    with transaction.atomic():
        for job in (
            Job.objects.select_for_update(skip_locked=True)
            .filter(state__in=HELD, lease_expires_at__lt=now)
            .order_by("lease_expires_at")[:500]
        ):
            _end_attempt(
                job,
                JobState.INTERRUPTED,
                "worker_lost",
                "The worker stopped sending heartbeats.",
            )
            ended += 1
        overdue = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(state__in=HELD, started_at__lt=now - TIMEOUT_GRACE)
            .select_related("run")[:500]
        )
        for job in overdue:
            assert job.started_at is not None  # noqa: S101 - held jobs have started
            if job.started_at + timedelta(seconds=job.run.timeout) + TIMEOUT_GRACE < now:
                _end_attempt(
                    job,
                    JobState.TIMED_OUT,
                    "timed_out",
                    f"It ran past its {job.run.timeout}s timeout, and its worker didn't stop it.",
                )
                ended += 1
    return ended


def _end_attempt(job: Job, state: str, reason: str, message: str) -> ActionRun:
    """Ends this attempt, then retries the run or ends it. Call inside a transaction."""
    now = timezone.now()
    job.state = state
    job.finished_at = now
    job.error = {"code": reason, "message": message}
    job.lease_token = None
    job.lease_expires_at = None
    job.save(update_fields=["state", "finished_at", "error", "lease_token", "lease_expires_at"])
    run = ActionRun.objects.select_for_update().get(pk=job.run_id)
    if run.cancel_requested_at is not None or reason == "cancelled":
        run.status = RunStatus.CANCELLED
        run.finished_at = now
        run.save(update_fields=["status", "finished_at"])
        return run
    if reason in run.retry_on and job.attempt < run.max_attempts:
        Job.objects.create(
            run=run,
            attempt=job.attempt + 1,
            priority=job.priority,
            project_id=job.project_id,
            tier=job.tier,
            environment=job.environment,
            resources=job.resources,
            owner_id=job.owner_id,
            queued_at=now + RETRY_BACKOFF * job.attempt,
        )
        run.status = RunStatus.QUEUED
        run.save(update_fields=["status"])
        notify(job.tier)
        return run
    run.status = _FINAL[reason]
    run.finished_at = now
    run.error = {"code": reason, "message": message}
    run.save(update_fields=["status", "finished_at", "error"])
    return run
