"""The job queue: leasing jobs to workers, what they report, and retries (ADR 0043).

A worker leases the best queued job it can run: on its tier, that fits its
capacity, and (on the self-hosted tier) in a project its teams cover (ADR
0112). Projects with fewer jobs running go first, so one project's batch
can't starve the rest; then higher priority, then older. Rows are claimed with ``FOR UPDATE SKIP
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
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from django.conf import settings
from django.core.exceptions import EmptyResultSet
from django.db import connection, transaction
from django.db.models import (
    Case,
    Count,
    F,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce, Now
from django.utils import timezone
from opentelemetry import metrics

from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.runs import (
    budgets,
    catalog,
    checkpoints,
    costs,
    egress,
    endings,
    included,
    launches,
    leases,
    live,
    outside,
    quotas,
    secrets,
    slurm,
    workers,
)
from iki_server.runs.leases import HEARTBEAT_EVERY, LEASE_TTL, LogGap, Stale
from iki_server.runs.models import (
    FINISHED,
    HELD,
    ActionRun,
    Job,
    JobLog,
    JobState,
    RunApproval,
    RunOutput,
    RunStatus,
    Worker,
    not_waiting_for_scan,
)
from iki_spec.compute import KUBERNETES

__all__ = [
    "HEARTBEAT_EVERY",
    "LEASE_TTL",
    "LogGap",
    "OutOfScope",
    "Stale",
    "append_log",
    "complete",
    "end_cancelled",
    "fail",
    "finish",
    "heartbeat",
    "held",
    "interrupt_outside",
    "interrupt_worker",
    "lease",
    "notify",
    "outcome",
    "payload",
    "progress",
    "recover_expired",
]

#: How long each attempt waited before a worker leased it, recorded as the lease commits:
#: it sums across the pods that lease, so dashboards add them up (Compute → Overview shows
#: the queue as it is now).
queue_wait = metrics.get_meter("iki.compute").create_histogram(
    "iki.runs.queue_wait",
    unit="s",
    description="How long attempts waited in the queue before a worker leased them, by tier "
    "and, for a machine Iki started, its template.",
)

CHANNEL = "iki_jobs"
#: Retries wait this long times their attempt number, so a failing dependency gets a moment.
RETRY_BACKOFF = timedelta(seconds=10)

#: Why an attempt ended → the run's status, if it isn't retried.
_FINAL = {
    "failed": RunStatus.FAILED,
    "timed_out": RunStatus.TIMED_OUT,
    "interrupted": RunStatus.FAILED,
    "worker_lost": RunStatus.FAILED,
    "machine_unavailable": RunStatus.FAILED,
    "not_allowed": RunStatus.REJECTED,
    # A file it reads is held by its scan (ADR 0216, 0234).
    "file_quarantined": RunStatus.FAILED,
    "file_not_scanned": RunStatus.FAILED,
    # It can't go where its template says, such as a project with no Slurm account there.
    "unplaceable": RunStatus.FAILED,
    "plugin_detached": RunStatus.CANCELLED,
}

#: The permission running on each tier needs, as launching it did.
TIER_ACTIONS = {
    "self_hosted": Action.RUN_SELF_HOSTED,
    "backend": Action.RUN_BACKEND,
    "container": Action.RUN_CONTAINER,
    "vm": Action.RUN_VM,
    # A core action Iki runs itself (ADR 0178): what it does, as an Editor may.
    "server": Action.PROJECT_EDIT,
}


class OutOfScope(Stale):
    """A self-hosted worker holds a job in a project it no longer covers (ADR 0112)."""


def lease_ttl(worker: Worker) -> timedelta:
    """How long the worker's leases last without a heartbeat: a Slurm job outlives its
    login node's worker restarting (ADR 0133)."""
    return slurm.LEASE_TTL if worker.slurm_cluster else LEASE_TTL


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
    """Jobs launched with plugin code the worker has, in an environment it has built. A
    local directory's code has no commit, so a worker that fetched it says which bundle it
    got, and leases none of that plugin's jobs once another is installed."""
    runnable = Q(pk__in=[])
    installed = {e["name"]: e for e in (registry.installed() or {}).get("plugins", ())}
    for plugin in worker.plugins:
        name, sha, built = plugin["name"], plugin.get("sha") or "", plugin.get("environments")
        fetched = plugin.get("bundle")
        if fetched and not sha:
            now = (installed.get(name) or {}).get("bundle") or {}
            if now.get("sha256") != fetched:
                continue
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


#: How many of each project's best jobs a lease weighs, and how many of those, across the
#: projects, it may try to lock: workers leasing at once each skip the others' choices.
EACH = 8
TRIES = 64


def _shortlist(
    candidates: QuerySet[Job],
    tier: str,
    projects: list[uuid.UUID] | None = None,
    labels: list[str] | None = None,
) -> RawSQL | None:
    """The best of what ``candidates`` holds, as a subquery of job IDs, or None if it holds
    nothing by definition (a worker with no plugin it may run): each project's best
    few, the projects with fewer jobs running first, then by priority, oldest first
    (ADR 0108). With `projects` (a self-hosted worker's), only theirs.

    With `labels` (a self-hosted worker's), only jobs needing none but those (ADR 0112).

    It costs as many index lookups as there are projects with jobs queued on the tier
    (times the sets of labels they need, on the self-hosted tier), however deep the queue
    or long its history. Ranking every queued job by its project's running count instead
    counted each project's jobs once per job queued. A project `projects` leaves out, and
    labels the worker hasn't got, are skipped before their jobs are read.
    """
    heads = (
        candidates.filter(
            project_id=RawSQL("queued.project_id", ()), labels=RawSQL("queued.labels", ())
        )
        .order_by("-priority", "queued_at", "id")
        .values_list("id", "priority", "queued_at")[:EACH]
    )
    try:
        sql, params = heads.query.sql_with_params()
    except EmptyResultSet:
        return None
    held = ", ".join(["%s"] * len(HELD))
    scoped = " AND queued.project_id = ANY(%s::uuid[])" if projects is not None else ""
    labelled = " AND queued.labels <@ %s::varchar[]" if labels is not None else ""
    # Every value is a parameter; what's interpolated is Django's own compiled SQL, the
    # placeholders for HELD, and constants.
    return RawSQL(  # noqa: S611
        # The projects with a job queued on the tier, and the labels those jobs need: from
        # the lowest, each step the next one up, as a skip scan of iki_job_queued_by_labels.
        "WITH RECURSIVE queued(project_id, labels) AS ("  # noqa: S608 - see above
        " (SELECT project_id, labels FROM iki_job WHERE state = %s AND tier = %s"
        "  ORDER BY project_id, labels LIMIT 1)"
        " UNION ALL"
        " SELECT next.project_id, next.labels FROM queued CROSS JOIN LATERAL ("
        "  SELECT j.project_id, j.labels FROM iki_job j WHERE j.state = %s AND j.tier = %s"
        "  AND (j.project_id, j.labels) > (queued.project_id, queued.labels)"
        "  ORDER BY j.project_id, j.labels LIMIT 1) next"
        "), running AS ("
        f" SELECT project_id, count(*) AS n FROM iki_job WHERE state IN ({held})"
        "  AND packed_into_id IS NULL GROUP BY project_id"
        ") "
        f"SELECT head.id FROM queued CROSS JOIN LATERAL ({sql}) head(id, priority, queued_at)"
        " LEFT JOIN running ON running.project_id = queued.project_id"
        f" WHERE true{scoped}{labelled}"
        " ORDER BY coalesce(running.n, 0), head.priority DESC, head.queued_at, head.id"
        f" LIMIT {TRIES}",
        (
            JobState.QUEUED,
            tier,
            JobState.QUEUED,
            tier,
            *HELD,
            *params,
            *([projects] if projects is not None else []),
            *([labels] if labels is not None else []),
        ),
    )


def lease(worker: Worker) -> tuple[Job, dict[str, Any]] | None:
    """Leases the best job this worker can run, with what it needs to run it; None if none."""
    now = timezone.now()
    active = (
        Job.objects.filter(project=OuterRef("project"), state__in=HELD, packed_into__isnull=True)
        .values("project")
        .annotate(n=Count("pk"))
        .values("n")
    )
    # A cluster's worker leases what its Slurm templates take, rather than what fits it.
    cluster = worker.slurm_cluster
    # Only a machine with the run's pinned reference data can execute it. A run
    # waiting for its first build has SQL NULL here and matches no machine.
    volumes = dict(worker.launch.volumes) if worker.launch is not None else {}
    candidates = Job.objects.filter(
        Q() if cluster else _fits(worker),
        _runnable(worker),
        # A later attempt waits for its run's checkpoint to be checked (ADR 0069).
        checkpoints.ready_to_resume(),
        # And one for the files it reads to be scanned (ADR 0234).
        not_waiting_for_scan(),
        run__volumes=volumes,
        state=JobState.QUEUED,
        tier=worker.tier,
        queued_at__lte=Now(),
    )
    if worker.launch is not None:
        # A machine Iki started runs its own environment's jobs (ADR 0061), and only those
        # whose actions take its kind of machine (ADR 0062).
        candidates = candidates.filter(
            Q(lifecycle="") | Q(lifecycle=launches.lifecycle(worker.launch)),
            Q(run__environment_image="")
            | Q(
                run__environment_image=worker.launch.image,
                run__environment_image_pull_secret=worker.launch.image_pull_secret,
            ),
            environment=worker.launch.environment,
        )
    elif not cluster:
        # Customer images run on their launch or as a provisioned Slurm job, not on a
        # worker that registered its own environment under the same name.
        candidates = candidates.filter(run__environment_image="")
    scope: list[uuid.UUID] | None = None
    labels: list[str] | None = None
    if worker.tier == workers.SELF_HOSTED:
        # Only runs in projects its teams cover, needing no label it hasn't got (ADR 0112).
        scope = list(workers.covered(worker).values_list("pk", flat=True))
        if not scope:
            return None
        labels = list(worker.labels)
        candidates = candidates.filter(project__in=scope, labels__contained_by=labels)
    with transaction.atomic():
        # It's still running, and an ephemeral machine's first job.
        if not launches.may_lease(worker):
            return None
        if cluster:
            # Its templates' `max_concurrent` holds across the cluster's workers (ADR 0133).
            slurm.lock(cluster)
            leasable = slurm.leasable(worker)
            if leasable is None:
                return None
            candidates = candidates.filter(leasable)
        elif worker.tier == "container" and (theirs := slurm.taken()) is not None:
            # Nor does any other worker lease a cluster's jobs.
            candidates = candidates.exclude(theirs)
        stop = quotas.blocked(worker.tier, lock=True)
        if stop.organization:
            return None
        if stop.projects:
            candidates = candidates.exclude(project__in=list(stop.projects))
        if stop.teams:
            candidates = candidates.exclude(project__team__in=list(stop.teams))
        if worker.tier not in costs.PRICED_TIERS:
            # Runs that call priced outside APIs cost on any tier (ADR 0130).
            spent = quotas.Blocked()
            budgets.hold(spent, worker.tier, outside=True)
            if spent.organization:
                candidates = candidates.exclude(outside=True)
            if spent.projects:
                candidates = candidates.exclude(outside=True, project__in=list(spent.projects))
            if spent.teams:
                candidates = candidates.exclude(outside=True, project__team__in=list(spent.teams))
        shortlist = _shortlist(candidates, worker.tier, scope, labels)
        if shortlist is None:
            return None
        # The plan's few milliseconds would wait on JIT compiling it: Postgres's estimate
        # of what a lateral join with limits costs sits above where it compiles plans.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('jit', 'off', true)")
        job = (
            candidates.filter(pk__in=shortlist)
            .annotate(active=Coalesce(Subquery(active), 0))
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
        plugin = run.action.partition(".")[0]
        if plugin in registry.detached():
            # Queued before a deploy detached it, which the sweep hasn't reached yet.
            _end_detached(job, plugin)
            return None
        try:
            manifests = {run.pk: included.manifest(run)}
        except included.Unavailable as why:
            _unavailable(job, why)
            return None
        template = worker.launch.template if worker.launch is not None else ""
        build = _build(worker, job.environment)
        if cluster:
            try:
                slurm.place(job, worker)
            except slurm.Unplaceable as why:
                fail_queued(job, "unplaceable", str(why))
                return None
            template = job.template
            # The image is the environment: its digest is the build it ran (ADR 0061).
            image = slurm.image_for(job)
            build = launches.image_digest(image) if image else ""
        members = []
        for member in _pack(job, run):
            try:
                manifests[member.run_id] = included.manifest(member.run)
            except included.Unavailable as why:
                _unavailable(member, why)
            else:
                members.append(member)
        job.state = JobState.LEASED
        job.template = template
        job.worker = worker
        job.worker_labels = list(worker.labels)
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = now + lease_ttl(worker)
        job.started_at = now
        job.environment_build = build
        job.volumes = volumes
        job.save(
            update_fields=[
                "state",
                "worker",
                "worker_labels",
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
            worker_labels=list(worker.labels),
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
        waits = [max((now - j.queued_at).total_seconds(), 0.0) for j in [job, *members]]
        transaction.on_commit(lambda: _record_waits(job.tier, template, waits))
        for each in runs:
            each.status = RunStatus.RUNNING
            each.started_at = each.started_at or now
            # A packed command gets one seed, its first run's: each run records the seed
            # it was run with, so a reproduction gets it again (ADR 0057).
            each.seed = run.seed
            each.save(update_fields=["status", "started_at", "seed"])
            live.changed(each.pk, each.batch_id)
        return job, payload(job, run, manifests)


def _record_waits(tier: str, template: str, waits: list[float]) -> None:
    labels = {"tier": tier, "template": template} if template else {"tier": tier}
    for wait in waits:
        queue_wait.record(wait, labels)


def _unavailable(job: Job, why: included.Unavailable) -> None:
    if isinstance(why, included.WaitingForScan):
        # Its files were pinned before they were scanned, as before an upgrade that scans
        # them: it waits for their scans, as a run launched with them does (ADR 0234).
        from iki_server.runs import scan_waits

        scan_waits.begin(job, job.run, why.pending)
    else:
        fail_queued(job, why.ends_with, str(why))


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
            run__volumes=run.volumes,
            run__environment_image=run.environment_image,
            run__environment_image_pull_secret=run.environment_image_pull_secret,
            tier=job.tier,
            environment=job.environment,
            labels=job.labels,
            queued_at__lte=Now(),
        )
        .filter(not_waiting_for_scan())
        .exclude(pk=job.pk)
        .order_by("queued_at", "id")
        .select_related("run__project")
        .select_for_update(skip_locked=True, of=("self",))[: size - 1]
    )


def packed(job: Job) -> list[Job]:
    """The attempts packed into `job`, with their runs, in the order their entities are
    given to the command."""
    return list(Job.objects.filter(packed_into=job).select_related("run").order_by("run_id"))


def payload(
    job: Job, run: ActionRun, manifests: dict[uuid.UUID, included.Manifest] | None = None
) -> dict[str, Any]:
    """The command's input document assembled from the authorized pins of its runs."""
    from iki_server.runs import input_files

    members = packed(job)
    runs = [run, *(m.run for m in members)]
    if manifests is None:
        manifests = {r.pk: included.manifest(r) for r in runs}
    inputs: dict[str, list[dict[str, Any]]] = {name: [] for name in run.spec["inputs"]}
    files: set[uuid.UUID] = set()
    pack: list[dict[str, str]] = []
    for each in runs:
        manifest = manifests[each.pk]
        files.update(manifest.input_files | manifest.param_files)
        for name, items in manifest.inputs.items():
            inputs.setdefault(name, []).extend(items)
            if members:
                pack.extend(
                    {"run": str(each.pk), "hrid": each.hrid, "entity": item["id"]} for item in items
                )
    assert job.lease_token is not None and job.lease_expires_at is not None  # noqa: S101
    given = secrets.for_run(run.action.split(".")[0], run.spec.get("secrets") or [])
    if sorted(given) != job.secrets:
        job.secrets = sorted(given)
        job.save(update_fields=["secrets"])
        Job.objects.filter(packed_into=job).update(secrets=job.secrets)
    # What the run input lists is what its worker may download: one set, for both.
    job.input_files = sorted(files)
    job.save(update_fields=["input_files"])
    return {
        "apiVersion": "iki/v1",
        "kind": "RunInput",
        "run": {
            "id": str(run.pk),
            "hrid": run.hrid,
            "attempt": job.attempt,
            **({"requested_by": str(run.requested_by_id)} if not members else {}),
            **({"seed": run.seed} if run.seed is not None else {}),
            "action": run.action,
            "plugin": {
                "name": run.action.split(".")[0],
                "version": run.plugin_version,
                "sha": run.plugin_sha or None,
            },
            "project": str(run.project_id),
            **_trigger(run),
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
            **slurm.for_payload(job),
            **({"artifacts": run.spec["artifacts"]} if run.spec.get("artifacts") else {}),
            **({"checkpoint": saving} if (saving := checkpoints.for_payload(job, run)) else {}),
            **outside.for_payload(job, run),
        },
        "command": run.spec["command"],
        "environment": f"{run.action.split('.')[0]}.{run.environment}",
        "params": run.params,
        "settings": run.plugin_settings,
        "inputs": inputs,
        "files": input_files.describe(set(job.input_files)),
        **({"volumes": job.volumes} if job.volumes else {}),
        **({"pack": pack} if members else {}),
        **outside.for_resume(job),
    }


def _trigger(run: ActionRun) -> dict[str, Any]:
    """For a run a scheduled trigger launched: which, when for, and since when (ADR 0080)."""
    if run.via != "trigger":
        return {}
    from iki_server.runs.models import TriggerFiring

    firing = (
        TriggerFiring.objects.filter(run=run, scheduled_for__isnull=False)
        .select_related("trigger")
        .first()
    )
    if firing is None or firing.scheduled_for is None:
        return {}
    return {
        "trigger": {
            "name": firing.trigger.trigger,
            "scheduled_for": firing.scheduled_for.isoformat(),
            "since": firing.since.isoformat() if firing.since else None,
        }
    }


def _proxy(job: Job, run: ActionRun) -> dict[str, str]:
    """For an action that declares hosts, or calls outside APIs: the egress proxy, with this
    attempt's proxy-only credentials (ADR 0053, 0130). They work while
    the attempt holds its lease."""
    # VMs reach the proxy from their VPC, not the cluster's network (ADR 0074), and a Slurm
    # cluster's compute nodes from theirs (ADR 0133).
    vm = job.tier == "vm"
    url = settings.IKI_RUNS_VM_EGRESS_PROXY_URL if vm else settings.IKI_EGRESS_PROXY_URL
    if slurm.template_of(job) is not None:
        url = slurm.proxy_url(job)
    if not (run.spec.get("network") or run.spec.get("apis")) or not url:
        return {}
    assert job.lease_token is not None  # noqa: S101 - a leased attempt
    return {"proxy": egress.proxy_url(url, job.pk, job.lease_token)}


# --- Reports from the worker -------------------------------------------------------------------


def _in_scope(worker: Worker, job: Job) -> Job:
    """A self-hosted worker reports only on jobs in projects it still covers: an admin who
    narrows its teams takes those away at once (ADR 0112)."""
    if not workers.covers(worker, job.project_id):
        raise OutOfScope
    return job


def held(worker: Worker, job_id: uuid.UUID, token: uuid.UUID) -> Job:
    """The job, locked, if this worker holds its current lease."""
    found = Job.objects.select_for_update().filter(pk=job_id, worker=worker).first()
    job = leases.fenced(found, token, HELD)
    return _in_scope(worker, job)


def cancel_requested(worker: Worker, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Whether the job's run was cancelled, if this worker holds its lease. Reads only:
    it doesn't renew the lease, as heartbeats do."""
    job = leases.fenced(Job.objects.filter(pk=job_id, worker=worker).first(), token, HELD)
    return _cancelled(_in_scope(worker, job))


#: The most jobs one watch covers (``POST /worker/v1/watch``): a cluster's worker holds at
#: most this many (``IKI_SLURM_MAX_JOBS``).
MAX_WATCHED = 1000


class Watched(NamedTuple):
    """What a watch of several jobs answers."""

    #: Jobs whose runs were cancelled: the worker should stop them and report ``cancelled``.
    cancel_requested: list[uuid.UUID]
    #: Jobs this worker doesn't hold any more, as ``stale_lease`` says of one.
    stale: list[uuid.UUID]


def watched(worker: Worker, tokens: Mapping[uuid.UUID, uuid.UUID]) -> Watched:
    """Of the jobs in `tokens` (each with the lease token the worker holds it by), those
    whose runs were cancelled and those it doesn't hold, as :func:`cancel_requested` says of
    one, in as many queries however many there are. Reads only."""
    found = {j.pk: j for j in Job.objects.filter(pk__in=tokens, worker=worker)}
    held: list[Job] = []
    stale: list[uuid.UUID] = []
    for job_id, token in tokens.items():
        try:
            held.append(leases.fenced(found.get(job_id), token, HELD))
        except Stale:
            stale.append(job_id)
    if worker.tier == workers.SELF_HOSTED and held:
        covered = set(
            workers.covered(worker)
            .filter(pk__in={j.project_id for j in held})
            .values_list("pk", flat=True)
        )
        stale += [j.pk for j in held if j.project_id not in covered]
        held = [j for j in held if j.project_id in covered]
    # A job goes on while any of its runs does: its own, or one packed into it.
    going = set(
        ActionRun.objects.filter(
            pk__in=[j.run_id for j in held], cancel_requested_at__isnull=True
        ).values_list("pk", flat=True)
    )
    packing = set(
        Job.objects.filter(
            packed_into__in=[j.pk for j in held], run__cancel_requested_at__isnull=True
        ).values_list("packed_into_id", flat=True)
    )
    cancelled = [j.pk for j in held if j.run_id not in going and j.pk not in packing]
    return Watched(sorted(cancelled), sorted(stale))


def _cancelled(job: Job) -> bool:
    """Whether the command should stop: its run was cancelled, or, for a packed job, every
    one of its runs was. Runs cancelled in a pack that goes on end cancelled when it ends,
    with nothing written."""
    runs = ActionRun.objects.filter(Q(pk=job.run_id) | Q(jobs__packed_into=job))
    return not runs.filter(cancel_requested_at__isnull=True).exists()


class Beat(NamedTuple):
    """What a heartbeat answers."""

    lease_expires_at: datetime
    #: The run's been cancelled: the worker should stop its command and report ``cancelled``.
    cancel_requested: bool
    #: How much of the log the server has: where a restarted worker sends from.
    log_offset: int


def heartbeat(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, state: str | None = None
) -> Beat:
    """Renews the lease, and moves the job on to `state`."""
    with transaction.atomic():
        job = held(worker, job_id, token)
        fields = ["lease_expires_at"]
        if state is not None and state != job.state:
            # HELD is in the order a worker may report its states.
            if state not in HELD or HELD.index(JobState(state)) < HELD.index(JobState(job.state)):
                raise ValueError(f"A job can't go from {job.state} back to {state}.")
            if state == JobState.PENDING and not worker.slurm_cluster:
                raise ValueError("Only a Slurm cluster's jobs wait in its queue.")
            job.state = state
            fields.append("state")
            live.changed(job.run_id)
            for member in packed(job):
                member.state = state
                member.save(update_fields=["state"])
                live.changed(member.run_id)
        job.lease_expires_at = timezone.now() + lease_ttl(worker)
        job.save(update_fields=fields)
        return Beat(job.lease_expires_at, _cancelled(job), job.log_size)


def progress(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, fraction: float, message: str
) -> None:
    with transaction.atomic():
        job = held(worker, job_id, token)
        leases.set_progress(job, fraction, message)
        live.changed(job.run_id)
        for member in packed(job):
            member.progress, member.progress_message = job.progress, job.progress_message
            member.save(update_fields=["progress", "progress_message"])
            live.changed(member.run_id)


def append_log(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, offset: int, text: str) -> int:
    """Adds a chunk at `offset` (in bytes of UTF-8) and returns where the next one goes, by
    the rules of :func:`leases.append_log`. Its run's log, and the log of each run packed
    into it, shows it live."""
    with transaction.atomic():
        job = held(worker, job_id, token)

        def keep(text: str) -> None:
            JobLog.objects.create(job=job, offset=offset, text=text)

        if leases.append_log(job, offset, text, keep):
            live.logged(job.run_id, job.attempt, job.log_size)
            for member in Job.objects.filter(packed_into=job).only("run_id", "attempt"):
                live.logged(member.run_id, member.attempt, job.log_size)
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
        # Only the runs: the projects, teams and launchers they're read with are shared by
        # every run of theirs, and locking those would end them one at a time.
        runs = {
            r.pk: r
            for r in ActionRun.objects.select_for_update(of=("self",))
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
    if "park" in document:
        # It submitted an outside API's job: it waits for it, holding nothing (ADR 0130).
        return outside.park(job, run, document["park"])
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


def end_detached() -> int:
    """Ends the runs of detached plugins' actions (ADR 0091): those not started at once,
    and running ones by asking their workers to stop. Each says why, and is audited.
    The maintenance job runs it. Returns how many."""
    detached = registry.detached()
    if not detached:
        return 0
    of = Q()
    for plugin in detached:
        of |= Q(action__startswith=f"{plugin}.")
    count = 0
    with transaction.atomic():
        waiting = (
            Job.objects.select_for_update(skip_locked=True)
            .filter(
                run__in=ActionRun.objects.filter(of),
                state__in=(JobState.QUEUED, JobState.AWAITING_APPROVAL),
                packed_into__isnull=True,
            )
            .order_by("run_id")
        )
        for job in waiting:
            _end_detached(job, job.environment.partition(".")[0])
            count += 1
        now = timezone.now()
        running = (
            ActionRun.objects.select_for_update(skip_locked=True)
            .filter(of, cancel_requested_at__isnull=True)
            .exclude(status__in=sorted(s.value for s in FINISHED))
            .order_by("id")
        )
        for run in running:
            run.cancel_requested_at = now
            run.save(update_fields=["cancel_requested_at"])
            audit.record(
                "run.cancel_requested",
                target=run,
                project=run.project,
                data={
                    "reason": f"The {run.action.partition('.')[0]} plugin was detached, so it "
                    "was stopped."
                },
            )
            held = run.jobs.filter(state__in=HELD).order_by("-attempt").first()
            if held is not None:
                notify_job(held.packed_into_id or held.pk)
            live.changed(run.pk, run.batch_id)
            count += 1
    return count


def _end_detached(job: Job, plugin: str) -> None:
    """Ends an attempt that hasn't started, its plugin detached. Call holding it."""
    message = f"The {plugin} plugin was detached before it started, so it didn't run."
    run = _end_attempt(job, JobState.CANCELLED, "plugin_detached", message)
    audit.record("run.cancelled", target=run, project=run.project, data={"reason": message})
    if run.approval_id is not None:
        from iki_server.runs import approvals

        approvals.settle(RunApproval.objects.select_for_update().get(pk=run.approval_id), None)


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


def interrupt_outside(worker: Worker, covered: QuerySet[Any]) -> int:
    """Ends the attempts a self-hosted worker holds in projects it no longer covers, as
    interrupted: an admin narrowed its teams or projects (ADR 0112)."""
    count = 0
    for job in (
        Job.objects.select_for_update()
        .filter(worker=worker, state__in=HELD, packed_into__isnull=True)
        .exclude(project__in=covered.values("pk"))
    ):
        _end_pack(
            job,
            JobState.INTERRUPTED,
            "worker_lost",
            "The worker was taken off this project.",
        )
        count += 1
    return count


def recover_expired() -> int:
    """Ends attempts whose lease ran out (the worker went away), and those running well past
    their timeout, as well as queued runs whose pinned pull Secret no template can use.
    The task runner calls it every few seconds. Returns how many it ended.

    Each sweep selects only what's due, soonest first, so however many long jobs are held
    within their timeouts, the ones past theirs are found. Each attempt ends in its own
    transaction (``sweeps.each``): one that can't is logged and left for the next pass."""
    now = timezone.now()
    holding = Job.objects.filter(state__in=HELD)
    ended = _each(leases.lost(holding, now), _worker_lost)
    ended += _each(_overdue(holding, now), _timed_out)
    ended += _each(_incompatible_pull_secrets(), _unplaceable)
    return ended


def _overdue(holding: QuerySet[Job], now: datetime) -> QuerySet[Job]:
    """Of the `holding` attempts, those well past their run's timeout, longest first. Packed
    attempts end with the one they're packed into. A Slurm job's time counts from when the
    cluster started it, not from waiting in its queue (ADR 0133), so one still waiting there
    isn't overdue."""
    began = Case(When(slurm__isnull=True, then=F("started_at")), default=F("slurm__started_at"))
    return leases.overdue(holding.filter(packed_into__isnull=True), began, "run__timeout", now)


def _incompatible_pull_secrets() -> QuerySet[Job]:
    """Queued attempts pinned to a Kubernetes pull Secret that no Kubernetes template their
    environment is bound to can use, oldest first."""
    compute = catalog.compute()
    kubernetes = {name for name, t in compute.templates.items() if t.provisioner in KUBERNETES}
    eligible = {
        name
        for name, binding in compute.bindings.items()
        if any(template in kubernetes for template, _ in binding.templates)
    }
    return (
        Job.objects.filter(state=JobState.QUEUED, packed_into__isnull=True)
        .exclude(run__environment_image_pull_secret="")
        .exclude(environment__in=eligible)
        .order_by("queued_at")
    )


def _each(found: QuerySet[Job], end: Callable[[Job], object]) -> int:
    """Ends each of `found`, with its run, as :func:`leases.end_each` does."""
    with_runs = found.select_related("run")
    return leases.end_each(with_runs, end, failed="job_recovery_failed", key="job")


def _worker_lost(job: Job) -> None:
    _end_pack(job, JobState.INTERRUPTED, "worker_lost", "The worker stopped sending heartbeats.")


def _timed_out(job: Job) -> None:
    _end_pack(
        job,
        JobState.TIMED_OUT,
        "timed_out",
        f"It ran past its {job.run.timeout}s timeout, and its worker didn't stop it.",
    )


def _unplaceable(job: Job) -> None:
    fail_queued(
        job,
        "unplaceable",
        "This run's pinned image uses a Kubernetes pull Secret, but the deployment "
        f"no longer binds `{job.environment}` to a Kubernetes template. Ask a compute "
        "admin to restore a Kubernetes template, or queue a new run under the current binding.",
    )


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
            labels=job.labels,
            resources=job.resources,
            lifecycle=job.lifecycle,
            outside=job.outside,
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
