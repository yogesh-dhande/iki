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

An action that's ``batchable`` runs several runs of a batch in one job
(ADR 0055). The worker leases one of them; up to ``batchable - 1`` more
queued runs of the same batch are *packed into* it, and its command gets
all their entities. Those attempts follow the one the worker holds: its
state, progress and log are theirs, and it counts once against quotas.
Its results are split back into each run's own, and each run ends, or is
retried, on its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Now
from django.utils import timezone

from iki_server.entities import registry, services
from iki_server.entities.values import shape, to_json
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.runs import checkpoints, costs, endings, launches, live, quotas, secrets, workers
from iki_server.runs.models import (
    ActionRun,
    Job,
    JobLog,
    JobState,
    RunInput,
    RunOutput,
    RunStatus,
    Worker,
)

__all__ = [
    "HEARTBEAT_EVERY",
    "LEASE_TTL",
    "LogGap",
    "Stale",
    "append_log",
    "complete",
    "end_cancelled",
    "fail",
    "finish",
    "heartbeat",
    "held",
    "interrupt_worker",
    "lease",
    "notify",
    "outcome",
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
    "machine_unavailable": RunStatus.FAILED,
    "not_allowed": RunStatus.REJECTED,
}

#: The permission running on each tier needs, as launching it did.
TIER_ACTIONS = {
    "client": Action.RUN_CLIENT,
    "backend": Action.RUN_BACKEND,
    "container": Action.RUN_CONTAINER,
    "vm": Action.RUN_VM,
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


def watch_key(job_id: uuid.UUID) -> str:
    """What a worker watching a job waits on (``POST …/jobs/{id}/watch``). Tiers are plain
    names, so the two never meet."""
    return f"job:{job_id}"


def notify_job(job_id: uuid.UUID) -> None:
    """Wakes the worker watching `job_id`, once the transaction commits: its run was
    cancelled."""
    notify(watch_key(job_id))


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


def _runnable(worker: Worker) -> Q:
    """Jobs launched with plugin code the worker has, in an environment it has built."""
    runnable = Q(pk__in=[])
    for plugin in worker.plugins:
        name, sha, built = plugin["name"], plugin.get("sha") or "", plugin.get("environments")
        mine = Q(environment__startswith=f"{name}.", run__plugin_sha=sha)
        if built is not None:
            mine &= Q(environment__in=[f"{name}.{env}" for env in built])
        runnable |= mine
    return runnable


def _build(worker: Worker, environment: str) -> str:
    """The digest of the worker's build of a ``plugin.environment``, if it has one."""
    name, _, env = environment.partition(".")
    for plugin in worker.plugins:
        if plugin["name"] == name:
            return str((plugin.get("environments") or {}).get(env, ""))
    return ""


def lease(worker: Worker) -> tuple[Job, dict[str, Any]] | None:
    """Leases the best job this worker can run, with what it needs to run it; None if none."""
    now = timezone.now()
    active = (
        Job.objects.filter(project=OuterRef("project"), state__in=HELD, packed_into__isnull=True)
        .values("project")
        .annotate(n=Count("pk"))
        .values("n")
    )
    candidates = Job.objects.filter(
        _fits(worker),
        _runnable(worker),
        # A later attempt waits for its run's checkpoint to be checked (ADR 0069).
        checkpoints.ready_to_resume(),
        state=JobState.QUEUED,
        tier=worker.tier,
        queued_at__lte=Now(),
    )
    if worker.launch is not None:
        # A machine Iki started runs its own environment's jobs (ADR 0061), and only those
        # whose actions take its kind of machine (ADR 0062).
        candidates = candidates.filter(
            Q(lifecycle="") | Q(lifecycle=launches.lifecycle(worker.launch)),
            environment=worker.launch.environment,
        )
    if worker.tier == "client":
        candidates = candidates.filter(owner=worker.owner_id)
        allowed = workers.allowed_projects(worker)
        if allowed is not None:
            if not allowed:
                return None
            candidates = candidates.filter(project__in=allowed)
    with transaction.atomic():
        # It's still running, and an ephemeral machine's first job.
        if not launches.may_lease(worker):
            return None
        stop = quotas.blocked(worker.tier, lock=True)
        if stop.organization:
            return None
        if stop.projects:
            candidates = candidates.exclude(project__in=list(stop.projects))
        if stop.teams:
            candidates = candidates.exclude(project__team__in=list(stop.teams))
        job = (
            candidates.annotate(active=Coalesce(Subquery(active), 0))
            .order_by("active", "-priority", "queued_at", "id")
            .select_for_update(skip_locked=True, of=("self",))
            .first()
        )
        if job is None:
            return None
        run = (
            ActionRun.objects.select_for_update(of=("self",))
            .select_related("requested_by", "project__team")
            .get(pk=job.run_id)
        )
        if not permissions.can(run.requested_by, TIER_ACTIONS[run.tier], run.project):
            # Its launcher was removed, or lost the role it needs there, since: it doesn't
            # start in their name. Nor does what's packed with it, until it's leased itself.
            fail_queued(
                job,
                "not_allowed",
                f"{run.requested_by.name} can no longer run actions on the {run.tier} tier in "
                f"{run.project.name}, so it didn't start.",
            )
            return None
        members = _pack(job, run)
        build = _build(worker, job.environment)
        template = worker.launch.template if worker.launch is not None else ""
        # The versions of the environment's volumes its machine has (ADR 0075).
        volumes = dict(worker.launch.volumes) if worker.launch is not None else {}
        job.state = JobState.LEASED
        job.template = template
        job.worker = worker
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + LEASE_TTL
        job.started_at = now
        job.environment_build = build
        job.volumes = volumes
        job.save(
            update_fields=[
                "state",
                "worker",
                "lease_token",
                "lease_expires_at",
                "started_at",
                "environment_build",
                "template",
                "volumes",
            ]
        )
        Job.objects.filter(pk__in=[m.pk for m in members]).update(
            state=JobState.LEASED,
            worker=worker,
            started_at=now,
            environment_build=build,
            template=template,
            volumes=volumes,
            packed_into=job,
        )
        runs = [
            run,
            *ActionRun.objects.select_for_update()
            .filter(pk__in=[m.run_id for m in members])
            .order_by("id"),
        ]
        for each in runs:
            each.status = RunStatus.RUNNING
            each.started_at = each.started_at or now
            # A packed command gets one seed, its first run's: each run records the seed
            # it was run with, so a reproduction gets it again (ADR 0057).
            each.seed = run.seed
            each.save(update_fields=["status", "started_at", "seed"])
            live.changed(each.pk, each.batch_id)
        return job, payload(job, run)


def _pack(job: Job, run: ActionRun) -> list[Job]:
    """For a ``batchable`` action: more queued jobs of the run's batch, locked, that the
    same command can run at once with `job` (ADR 0055)."""
    size = int(run.spec.get("batchable") or 1)
    if size <= 1 or run.batch_id is None:
        return []
    return list(
        Job.objects.filter(
            state=JobState.QUEUED,
            run__batch_id=run.batch_id,
            run__plugin_version=run.plugin_version,
            run__plugin_sha=run.plugin_sha,
            tier=job.tier,
            environment=job.environment,
            owner=job.owner_id,
            queued_at__lte=Now(),
        )
        .exclude(pk=job.pk)
        .order_by("queued_at", "id")
        .select_for_update(skip_locked=True, of=("self",))[: size - 1]
    )


def packed(job: Job) -> list[Job]:
    """The attempts packed into `job`, with their runs, in the order their entities are
    given to the command."""
    return list(Job.objects.filter(packed_into=job).select_related("run").order_by("run_id"))


def payload(job: Job, run: ActionRun) -> dict[str, Any]:
    """The run's input, as ``spec/…/run-input/v1`` describes it: what the command reads."""
    from iki_server.runs import input_files

    inputs: dict[str, list[dict[str, Any]]] = {name: [] for name in run.spec["inputs"]}
    files: set[uuid.UUID] = input_files.of_params(run)
    members = packed(job)
    runs = [run, *(m.run for m in members)]
    order = {r.pk: n for n, r in enumerate(runs)}
    rows = sorted(
        RunInput.objects.filter(run__in=[r.pk for r in runs]).select_related("entity"),
        key=lambda row: (row.name, order[row.run_id], row.position),
    )
    hrids = {r.pk: r.hrid for r in runs}
    pack = [
        {"run": str(row.run_id), "hrid": hrids[row.run_id], "entity": str(row.entity_id)}
        for row in rows
    ]
    for row in rows:
        entity = row.entity
        values = services.values_at(entity, row.revision)
        fields = shape(entity.type).fields()
        for name, value in values.items():
            if name in fields:
                files |= input_files.file_ids(fields[name], value)
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
    given = secrets.for_run(run.action.split(".")[0], run.spec.get("secrets") or [])
    if sorted(given) != job.secrets:
        job.secrets = sorted(given)
        job.save(update_fields=["secrets"])
        Job.objects.filter(packed_into=job).update(secrets=job.secrets)
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "run": {
            "id": str(run.pk),
            "hrid": run.hrid,
            "attempt": job.attempt,
            **({"seed": run.seed} if run.seed is not None else {}),
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
            **_proxy(job, run),
            **({"secrets": given} if given else {}),
            **({"artifacts": run.spec["artifacts"]} if run.spec.get("artifacts") else {}),
            **({"checkpoint": saving} if (saving := checkpoints.for_payload(job, run)) else {}),
        },
        "command": run.spec["command"],
        "environment": f"{run.action.split('.')[0]}.{run.environment}",
        "params": run.params,
        "settings": run.plugin_settings,
        "inputs": inputs,
        "files": input_files.describe(files),
        **({"volumes": job.volumes} if job.volumes else {}),
        **({"pack": pack} if members else {}),
    }


def _proxy(job: Job, run: ActionRun) -> dict[str, str]:
    """For an action that declares hosts: the egress proxy, with this attempt's job ID and
    lease token as its credentials (ADR 0053). They work while the attempt holds its lease."""
    # VMs reach the proxy from their VPC, not the cluster's network (ADR 0074).
    vm = job.tier == "vm"
    url = settings.IKI_RUNS_VM_EGRESS_PROXY_URL if vm else settings.IKI_EGRESS_PROXY_URL
    if not run.spec.get("network") or not url:
        return {}
    parts = urlsplit(url)
    return {"proxy": f"http://{job.pk}:{job.lease_token}@{parts.netloc}"}


# --- Reports from the worker -------------------------------------------------------------------


def _holds(job: Job | None, token: uuid.UUID) -> Job:
    if job is None or job.state not in HELD or job.lease_token != token:
        raise Stale
    if job.lease_expires_at is None or job.lease_expires_at <= timezone.now():
        raise Stale
    return job


def held(worker: Worker, job_id: uuid.UUID, token: uuid.UUID) -> Job:
    """The job, locked, if this worker holds its current lease."""
    return _holds(Job.objects.select_for_update().filter(pk=job_id, worker=worker).first(), token)


def cancel_requested(worker: Worker, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Whether the job's run was cancelled, if this worker holds its lease. Reads only:
    it doesn't renew the lease, as heartbeats do."""
    return _cancelled(_holds(Job.objects.filter(pk=job_id, worker=worker).first(), token))


def _cancelled(job: Job) -> bool:
    """Whether the command should stop: its run was cancelled, or, for a packed job, every
    one of its runs was. Runs cancelled in a pack that goes on end cancelled when it ends,
    with nothing written."""
    runs = ActionRun.objects.filter(Q(pk=job.run_id) | Q(jobs__packed_into=job))
    return not runs.filter(cancel_requested_at__isnull=True).exists()


def heartbeat(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, state: str | None = None
) -> tuple[datetime, bool]:
    """Renews the lease, and moves the job on to `state`. Returns the new expiry, and whether
    the run's been cancelled (the worker should stop its command and report ``cancelled``)."""
    with transaction.atomic():
        job = held(worker, job_id, token)
        fields = ["lease_expires_at"]
        if state is not None and state != job.state:
            if state not in PHASES or PHASES.index(JobState(state)) < PHASES.index(
                JobState(job.state)
            ):
                raise ValueError(f"A job can't go from {job.state} back to {state}.")
            job.state = state
            fields.append("state")
            live.changed(job.run_id)
            for member in packed(job):
                member.state = state
                member.save(update_fields=["state"])
                live.changed(member.run_id)
        job.lease_expires_at = timezone.now() + LEASE_TTL
        job.save(update_fields=fields)
        return job.lease_expires_at, _cancelled(job)


def progress(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, fraction: float, message: str
) -> None:
    with transaction.atomic():
        job = held(worker, job_id, token)
        job.progress = max(0.0, min(1.0, fraction))
        job.progress_message = message[:500]
        job.save(update_fields=["progress", "progress_message"])
        live.changed(job.run_id)
        for member in packed(job):
            member.progress, member.progress_message = job.progress, job.progress_message
            member.save(update_fields=["progress", "progress_message"])
            live.changed(member.run_id)


def append_log(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, offset: int, text: str) -> int:
    """Adds a chunk at `offset` (in bytes of UTF-8) and returns where the next one goes.

    A chunk already received (the worker retried) is accepted again, unchanged; a gap or an
    overlap raises ``LogGap``, so the log stays exactly what the command wrote.
    """
    size = len(text.encode())
    with transaction.atomic():
        job = held(worker, job_id, token)
        end = job.log_size
        if offset < end and offset + size <= end:
            return end  # already received: the worker retried
        if offset != end:
            raise LogGap(end)
        job.log_size = end + size
        job.save(update_fields=["log_size"])
        live.logged(job.run_id, job.attempt, job.log_size)
        for member in Job.objects.filter(packed_into=job).only("run_id", "attempt"):
            live.logged(member.run_id, member.attempt, job.log_size)
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
        job = held(worker, job_id, token)
        state = {
            "failed": JobState.FAILED,
            "timed_out": JobState.TIMED_OUT,
            "interrupted": JobState.INTERRUPTED,
            "cancelled": JobState.CANCELLED,
        }[reason]
        return _end_pack(job, state, reason, message[:2000])


def complete(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, document: dict[str, Any]
) -> ActionRun:
    """The command finished: write its results (ADR 0044), all or none, and end the run.

    The output is kept whatever happens. Results that don't fit what the action declared
    fail the run (``invalid_output``), unless the plugin changed since it was launched: then
    it's ``rejected`` (``schema_changed``), as is a run whose results can't be written now
    (an entity archived meanwhile, or the launcher's access gone). A run cancelled meanwhile
    writes nothing. None of these is retried: another attempt would end the same way.
    """
    from iki_server.runs import packs

    with transaction.atomic():
        job = held(worker, job_id, token)
        costs.released(job)
        members = list(Job.objects.select_for_update().filter(packed_into=job).order_by("run_id"))
        runs = {
            r.pk: r
            for r in ActionRun.objects.select_for_update()
            .select_related("project__team", "requested_by")
            .filter(pk__in=[job.run_id, *(m.run_id for m in members)])
        }
        if not members:
            return _commit(job, runs[job.run_id], document)
        attempts = [(job, runs[job.run_id]), *((m, runs[m.run_id]) for m in members)]
        parts = packs.split(document, [r for _, r in attempts])
        if isinstance(parts, packs.Unsplittable):
            error = {
                "code": "invalid_output",
                "message": "The results don't say which of the job's runs each is for.",
                "errors": parts.errors,
            }
            for attempt, run in attempts:
                RunOutput.objects.create(run=run, job=attempt, document=document)
                if run.cancel_requested_at is not None:
                    _end_attempt(
                        attempt, JobState.CANCELLED, "cancelled", "Cancelled before it ended."
                    )
                else:
                    finish(attempt, run, JobState.FAILED, RunStatus.FAILED, error)
            return runs[job.run_id]
        for (attempt, run), part in zip(attempts, parts, strict=True):
            _commit(attempt, run, part)
        return runs[job.run_id]


def _commit(job: Job, run: ActionRun, document: dict[str, Any]) -> ActionRun:
    """Keeps one run's output, writes its results or not, and ends it. Creates in a file
    are checked and written by a bulk import (ADR 0056): the job stays ``committing``."""
    from iki_server.runs import imports, results

    RunOutput.objects.create(run=run, job=job, document=document)
    if run.cancel_requested_at is not None:
        return _end_attempt(job, JobState.CANCELLED, "cancelled", "Cancelled before it ended.")
    if run.reproduces_id is not None:
        # A reproduction writes nothing: it says whether its results are the same (ADR 0057).
        from iki_server.runs import reuse

        run.reproduction = reuse.compare(job, run, document)
        run.save(update_fields=["reproduction"])
        return finish(job, run, JobState.SUCCEEDED, RunStatus.SUCCEEDED, None)
    started: list[bool] = []

    def commit() -> None:
        done = results.checked(run, document)
        if done.creates_file is None:
            results.write(done)
        else:
            imports.start(job, run, done.creates_file)
            started.append(True)

    state, status, error = outcome(run, commit)
    if started:
        return run
    return finish(job, run, state, status, error)


def outcome(run: ActionRun, write: Callable[[], None]) -> tuple[str, str, dict[str, Any] | None]:
    """How the run ends once ``write`` writes its results: succeeded, or why not."""
    from iki_server.runs import results

    try:
        write()
    except results.InvalidOutput as invalid:
        changed = _plugin_changed(run)
        error = {
            "code": "schema_changed" if changed else "invalid_output",
            "message": (
                f"The {run.action.split('.')[0]} plugin changed while the run ran, and its "
                "results no longer fit. They're kept with the run."
                if changed
                else "The results don't fit what the action declares."
            ),
            "errors": invalid.errors,
        }
        if changed:
            return JobState.REJECTED, RunStatus.REJECTED, error
        return JobState.FAILED, RunStatus.FAILED, error
    except results.Rejected as rejected:
        return (
            JobState.REJECTED,
            RunStatus.REJECTED,
            {"code": rejected.code, "message": rejected.message},
        )
    except results.Failed as failed:
        return JobState.FAILED, RunStatus.FAILED, {"code": "failed", "message": failed.message}
    return JobState.SUCCEEDED, RunStatus.SUCCEEDED, None


def finish(
    job: Job, run: ActionRun, state: str, status: str, error: dict[str, Any] | None
) -> ActionRun:
    """Ends the attempt and its run, as ``state`` and ``status``. Call inside a transaction
    holding both."""
    now = timezone.now()
    job.state, job.finished_at, job.error = state, now, error
    job.lease_token = job.lease_expires_at = None
    job.save(update_fields=["state", "finished_at", "error", "lease_token", "lease_expires_at"])
    run.status, run.finished_at, run.error = status, now, error
    run.save(update_fields=["status", "finished_at", "error"])
    live.changed(run.pk, run.batch_id)
    endings.ended(run)
    return run


def end_cancelled(job: Job) -> ActionRun:
    """Ends an attempt whose run was cancelled. Call inside a transaction holding it."""
    return _end_attempt(job, JobState.CANCELLED, "cancelled", "Cancelled before it ended.")


def _plugin_changed(run: ActionRun) -> bool:
    from iki_server.runs.services import plugin_sha

    plugin = run.action.split(".")[0]
    installed = registry.plugins().get(plugin)
    if installed is None:
        return True
    return installed.version != run.plugin_version or plugin_sha(plugin) != run.plugin_sha


def fail_queued(job: Job, reason: str, message: str) -> ActionRun:
    """Ends a queued attempt that can't run here (no machine for it can start), and its
    run with it. Call inside a transaction, with the job locked."""
    return _end_attempt(job, JobState.FAILED, reason, message)


def interrupt_worker(worker: Worker, *, code: str) -> int:
    """Ends every attempt the worker holds as interrupted (it was revoked or went away)."""
    count = 0
    for job in Job.objects.select_for_update().filter(
        worker=worker, state__in=HELD, packed_into__isnull=True
    ):
        _end_pack(
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
            _end_pack(
                job,
                JobState.INTERRUPTED,
                "worker_lost",
                "The worker stopped sending heartbeats.",
            )
            ended += 1
        overdue = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(state__in=HELD, started_at__lt=now - TIMEOUT_GRACE, packed_into__isnull=True)
            .select_related("run")[:500]
        )
        for job in overdue:
            assert job.started_at is not None  # noqa: S101 - held jobs have started
            if job.started_at + timedelta(seconds=job.run.timeout) + TIMEOUT_GRACE < now:
                _end_pack(
                    job,
                    JobState.TIMED_OUT,
                    "timed_out",
                    f"It ran past its {job.run.timeout}s timeout, and its worker didn't stop it.",
                )
                ended += 1
    return ended


def _end_pack(job: Job, state: str, reason: str, message: str) -> ActionRun:
    """Ends the attempt a worker held and those packed into it, each run retried or ended
    under its own policy. Returns the held attempt's run. Call inside a transaction."""
    # Its machine is let go of: on a persistent one, the time it held it is its runs'.
    costs.released(job)
    members = list(Job.objects.select_for_update().filter(packed_into=job).order_by("run_id"))
    run = _end_attempt(job, state, reason, message)
    for member in members:
        _end_attempt(member, state, reason, message)
    return run


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
    live.changed(run.pk, run.batch_id)
    if run.cancel_requested_at is not None or reason == "cancelled":
        run.status = RunStatus.CANCELLED
        run.finished_at = now
        run.save(update_fields=["status", "finished_at"])
        endings.ended(run)
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
            lifecycle=job.lifecycle,
            owner_id=job.owner_id,
            # The database's clock, as leasing compares with: app servers' clocks may drift.
            queued_at=Now() + RETRY_BACKOFF * job.attempt,
        )
        run.status = RunStatus.QUEUED
        run.save(update_fields=["status"])
        notify(job.tier)
        return run
    run.status = _FINAL[reason]
    run.finished_at = now
    run.error = {"code": reason, "message": message}
    run.save(update_fields=["status", "finished_at", "error"])
    endings.ended(run)
    return run
