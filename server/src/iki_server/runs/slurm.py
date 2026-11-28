"""Slurm clusters (ADR 0133): container-tier runs that a cluster's own scheduler runs.

A Compute admin registers ``iki-worker`` on a cluster's login node with a registration
token for the cluster (Compute → Workers). The deployment's Slurm templates in
compute.yaml say where its jobs go: the partition, QOS and default account, the most one
job may ask for, the partition's time limit, and what Slurm's CPUs, GPUs and memory cost.

Its worker leases each queued container-tier job whose template, the first in its
environment's binding that it fits, is one of its cluster's, while that template has room
under its ``max_concurrent``. It holds many at once, unlike other workers: it doesn't run
them itself, Slurm does, each in an allocation of its own. Two workers on one cluster (a
second login node) share the limit exactly, as leases on a cluster take a lock.

The server places each job as it leases it (:func:`place`): its template, its project's
Slurm account there (:class:`SlurmAccount`, or else the template's), and what it asks
Slurm for, sized from its action. The worker submits it with ``sbatch``, says what Slurm
reports as it waits and runs (:func:`report`), and once it ends, what ``sacct`` says it
used: its cost is recorded from that (:mod:`iki_server.runs.costs`).

A Slurm job waits in its cluster's queue, perhaps for hours, and outlives its worker
restarting, so the worker's leases on it last :data:`LEASE_TTL` rather than a minute: the
login node can reboot without losing a job.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Literal, get_args

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import catalog, live
from iki_server.runs.models import HELD, Job, SlurmAccount, SlurmJob, Worker
from iki_spec.compute import Template
from iki_spec.model import Resources

__all__ = [
    "LEASE_TTL",
    "STATES",
    "State",
    "Unplaceable",
    "account_for",
    "clusters",
    "first",
    "for_payload",
    "leasable",
    "lock",
    "place",
    "report",
    "set_account",
    "taken",
    "templates",
    "waiting",
]

#: How long a lease on a Slurm job lasts unless a heartbeat renews it: long enough for its
#: worker's login node to restart, while the job waits or runs on the cluster.
LEASE_TTL = timedelta(minutes=10)

#: States a worker may say Slurm reports (``squeue``/``sacct``'s, without their detail).
State = Literal[
    "PENDING",
    "CONFIGURING",
    "REQUEUED",
    "REQUEUE_HOLD",
    "REQUEUE_FED",
    "RESV_DEL_HOLD",
    "SUSPENDED",
    "STOPPED",
    "RUNNING",
    "COMPLETING",
    "STAGE_OUT",
    "SIGNALING",
    "RESIZING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "DEADLINE",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "BOOT_FAIL",
    "PREEMPTED",
    "SPECIAL_EXIT",
    "REVOKED",
]
STATES: frozenset[str] = frozenset(get_args(State))

_RESOURCES = ("cpu", "memory", "gpu_count", "gpu_memory", "scratch_disk")


def templates(cluster: str) -> list[Template]:
    """The deployment's Slurm templates on `cluster`, by name."""
    return sorted(
        (
            t
            for t in catalog.compute().templates.values()
            if t.provisioner == "slurm" and t.slurm.get("cluster") == cluster
        ),
        key=lambda t: t.name,
    )


def clusters() -> dict[str, list[Template]]:
    """Each cluster the deployment's Slurm templates name, with its templates."""
    found: dict[str, list[Template]] = {}
    for template in catalog.compute().templates.values():
        if template.provisioner == "slurm":
            found.setdefault(str(template.slurm["cluster"]), []).append(template)
    return {name: sorted(ts, key=lambda t: t.name) for name, ts in sorted(found.items())}


def template_of(job: Job) -> Template | None:
    """The Slurm template the job was placed on, as the deployment's compute says now."""
    found = catalog.compute().templates.get(job.template)
    return found if found is not None and found.provisioner == "slurm" else None


# --- Which jobs a cluster's worker leases ------------------------------------------------------


def _bound(environment: str) -> list[Template]:
    """The container-tier templates `environment` is bound to, in the order jobs try them:
    by priority, then the smallest (as :func:`catalog.availability` orders them)."""
    deployment = catalog.compute()
    binding = deployment.bindings.get(environment)
    found = [
        (priority, deployment.templates[name])
        for name, priority in (binding.templates if binding else ())
        if name in deployment.templates and deployment.templates[name].tier == "container"
    ]
    return [t for _, t in sorted(found, key=lambda pair: (pair[0], catalog._size(pair[1])))]


def _takes(template: Template) -> Q:
    """The jobs `template` takes: they fit it, and it takes their action's lifecycle, its
    timeout and its network (:meth:`Template.serves`, :meth:`Template.why_not`)."""
    have = template.capacity
    takes = Q()
    for key in _RESOURCES:
        if key == "gpu_memory":
            limit: float | int = have.gpu_memory or 0
        elif key == "gpu_count":
            limit = have.gpu_count
        else:
            limit = getattr(have, key) or 0
        needs_nothing = (
            ~Q(resources__has_key=key)
            | Q(**{f"resources__{key}": None})
            | Q(**{f"resources__{key}": 0})
        )
        takes &= needs_nothing | Q(**{f"resources__{key}__lte": limit})
    if template.provisioner != "slurm":
        return takes & (Q(lifecycle="") | Q(lifecycle=template.lifecycle))
    takes &= Q(run__environment_image_pull_secret="")
    walltime = template.slurm.get("max_walltime")
    if walltime is not None:
        takes &= Q(run__timeout__lte=walltime)
    if not template.slurm.get("egress_proxy_url"):
        # Hosts it declares, or outside APIs it calls, are reached through the egress proxy
        # (ADR 0053, 0130): the cluster's nodes must reach it.
        for key in ("network", "apis"):
            takes &= ~Q(run__spec__has_key=key) | Q(**{f"run__spec__{key}": []})
    return takes


def _resources(job: Job) -> Resources:
    """What the job needs, from what it was queued with."""
    known = {k: v for k, v in (job.resources or {}).items() if k in Resources.__dataclass_fields__}
    return Resources(**known)


def _takes_job(template: Template, job: Job, wanted: Resources) -> bool:
    """:func:`_takes`, for one job."""
    if not template.fits(wanted):
        return False
    if template.provisioner != "slurm":
        return not job.lifecycle or job.lifecycle == template.lifecycle
    if job.run.environment_image_pull_secret:
        return False
    walltime = template.slurm.get("max_walltime")
    if walltime is not None and job.run.timeout > walltime:
        return False
    spec = job.run.spec
    return bool(template.slurm.get("egress_proxy_url")) or not (
        spec.get("network") or spec.get("apis")
    )


def first(job: Job) -> Template | None:
    """The template a queued job goes to: the first of its environment's that takes it.
    When that's a Slurm template, its cluster's workers lease it, and Iki starts no machine
    for it (ADR 0133)."""
    if job.tier != "container":
        return None
    wanted = _resources(job)
    return next((t for t in _bound(job.environment) if _takes_job(t, job, wanted)), None)


def leasable(worker: Worker) -> Q | None:
    """The queued jobs `worker`, on a cluster's login node, may lease: those whose first
    template is one of its cluster's with room under its ``max_concurrent``. None if there
    are none. Call holding :func:`lock`."""
    cluster = worker.slurm_cluster
    mine = {t.name: t for t in templates(cluster)}
    if not mine:
        return None
    running = dict(
        Job.objects.filter(state__in=HELD, template__in=list(mine), packed_into__isnull=True)
        .values("template")
        .annotate(n=Count("pk"))
        .values_list("template", "n")
    )
    room = {
        name
        for name, t in mine.items()
        if t.max_concurrent is None or running.get(name, 0) < t.max_concurrent
    }
    return _first_is(room) if room else None


def taken() -> Q | None:
    """The jobs whose first template is a Slurm cluster's, which only its workers lease;
    None if the deployment has no Slurm templates."""
    names = {t.name for ts in clusters().values() for t in ts}
    return _first_is(names) if names else None


def _first_is(names: set[str]) -> Q:
    """The jobs whose first template, of those their environment is bound to, is one of
    `names`: :func:`first`, as a query."""
    found = Q(pk__in=[])
    for environment in sorted(e for e, b in catalog.compute().bindings.items() if b.templates):
        earlier = Q()
        for template in _bound(environment):
            takes = _takes(template)
            if template.name in names:
                found |= Q(environment=environment) & takes & earlier
            earlier &= ~takes
    return found


def lock(cluster: str) -> None:
    """Serializes leases on a cluster for the rest of the transaction, so its templates'
    ``max_concurrent`` holds exactly when two of its workers lease at once."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"iki_slurm:{cluster}"])


# --- Placing a job -----------------------------------------------------------------------------


def account_for(template: Template, project: Project) -> str | None:
    """The Slurm account a project's jobs on `template` are charged to: the project's on its
    cluster, or the template's."""
    given = SlurmAccount.objects.filter(cluster=template.slurm["cluster"], project=project).first()
    if given is not None:
        return given.account
    default = template.slurm.get("account")
    return str(default) if default else None


class Unplaceable(Exception):  # noqa: N818 - reads as "the job is unplaceable"
    """A job a cluster's worker leased can't go to the cluster: why, for people."""


def place(job: Job, worker: Worker) -> SlurmJob:
    """Records where on its cluster a job the worker just leased goes, from its template
    and its project. Raises :class:`Unplaceable` if it can't go there. Call inside the
    lease's transaction, holding the job and its run."""
    run = job.run
    wanted = _resources(job)
    template = first(job)
    if (
        template is None
        or template.provisioner != "slurm"
        or template.slurm.get("cluster") != worker.slurm_cluster
    ):
        raise Unplaceable(
            "The deployment's compute changed since it was queued, so no Slurm template "
            "takes it now."
        )
    account = account_for(template, run.project)
    if account is None:
        cluster = template.slurm["cluster"]
        raise Unplaceable(
            f"{run.project.name} has no Slurm account on {cluster}, and the `{template.name}` "
            "template names none, so it didn't start. A Compute admin can set the project's "
            "under Compute → Slurm."
        )
    job.template = template.name
    requested = {
        "cpus": max(1, math.ceil(wanted.cpu or 1)),
        "memory": wanted.memory,
        "gpus": wanted.gpu_count,
        "scratch_disk": wanted.scratch_disk,
        "walltime": run.timeout,
    }
    found, _ = SlurmJob.objects.update_or_create(
        job=job,
        defaults={
            "cluster": template.slurm["cluster"],
            "partition": template.slurm["partition"],
            "account": account,
            "qos": template.slurm.get("qos") or "",
            "requested": requested,
        },
    )
    return found


def image_for(job: Job) -> str | None:
    """The image this attempt will run, pinned at enqueue for a customer binding."""
    if job.run.environment_image:
        return job.run.environment_image
    action = catalog.action(job.run.action)
    return catalog.environment(action).image if action is not None else None


def for_payload(job: Job) -> dict[str, Any]:
    """What a cluster's worker needs to submit the job: ``job.slurm`` in its run input."""
    placed = SlurmJob.objects.filter(job=job).first()
    template = template_of(job)
    if placed is None or template is None:
        return {}
    found: dict[str, Any] = {
        "cluster": placed.cluster,
        "template": template.name,
        "partition": placed.partition,
        "account": placed.account,
        **placed.requested,
        "image": image_for(job),
    }
    for key in ("constraint", "gpu_type"):
        if template.slurm.get(key):
            found[key] = template.slurm[key]
    if placed.qos:
        found["qos"] = placed.qos
    if settings.IKI_IMAGE_POLICY:
        found["image_policy"] = settings.IKI_IMAGE_POLICY
    return {"slurm": {k: v for k, v in found.items() if v is not None}}


def proxy_url(job: Job) -> str:
    """Where the job's compute node reaches the egress proxy, if its template says."""
    template = template_of(job)
    return str(template.slurm.get("egress_proxy_url") or "") if template is not None else ""


# --- What the worker says Slurm reports --------------------------------------------------------

_TIMES = ("submitted_at", "started_at", "ended_at")
_TEXT = {"slurm_id": 32, "state": 32, "reason": 200, "nodes": 500, "exit_code": 16}


def report(job: Job, given: dict[str, Any]) -> SlurmJob:
    """Keeps what the job's worker says Slurm reports of it. Call holding the job, once
    the worker's lease on it is checked. Times only move forward, and an ID once given
    stays."""
    placed = SlurmJob.objects.select_for_update().filter(job=job).first()
    if placed is None:
        raise Refused("This job wasn't placed on a Slurm cluster.")
    state = given.get("state")
    if state is not None and state not in STATES:
        raise Refused(f"Slurm has no job state {state!r}.", field="state")
    slurm_id = given.get("slurm_id")
    if slurm_id and placed.slurm_id and slurm_id != placed.slurm_id:
        raise Refused(f"The job was submitted as Slurm job {placed.slurm_id}, not {slurm_id}.")
    changed: list[str] = []
    for key, size in _TEXT.items():
        value = given.get(key)
        if value is not None and value != getattr(placed, key):
            setattr(placed, key, str(value)[:size])
            changed.append(key)
    for key in _TIMES:
        when: datetime | None = given.get(key)
        if when is not None and getattr(placed, key) is None:
            setattr(placed, key, when)
            changed.append(key)
    for key in ("usage", "image_signer"):
        if given.get(key) is not None and given[key] != getattr(placed, key):
            setattr(placed, key, given[key])
            changed.append(key)
    if changed:
        placed.updated_at = timezone.now()
        placed.save(update_fields=[*changed, "updated_at"])
        live.changed(job.run_id)
    return placed


# --- Projects' accounts ------------------------------------------------------------------------


def set_account(cluster: str, project: Project, account: str | None, *, by: User) -> None:
    """Sets (or with None, clears) the Slurm account a project's jobs on `cluster` are
    charged to. Audited with the account before and after. Jobs already placed keep
    theirs."""
    if cluster not in clusters():
        raise Refused(f"This deployment has no Slurm cluster {cluster!r}.", field="cluster")
    with transaction.atomic():
        found = SlurmAccount.objects.select_for_update().filter(cluster=cluster, project=project)
        before = found.first()
        was = before.account if before is not None else None
        if account == was:
            return
        if account is None:
            found.delete()
        elif before is None:
            SlurmAccount.objects.create(
                cluster=cluster, project=project, account=account, set_by=by
            )
        else:
            before.account, before.set_by, before.set_at = account, by, timezone.now()
            before.save(update_fields=["account", "set_by", "set_at"])
        audit.record(
            "slurm_account.changed",
            actor=by,
            target=project,
            project=project,
            data={"cluster": cluster, "before": was, "after": account},
        )


# --- Why a queued run waits --------------------------------------------------------------------


def waiting(environments: set[str]) -> dict[str, str]:
    """For environments whose container-tier runs go to a Slurm cluster: why one waits,
    when the cluster has no worker online, or its template no room."""
    from iki_server.runs.workers import OFFLINE_AFTER

    found: dict[str, str] = {}
    now = timezone.now()
    online: dict[str, bool] = {}
    for environment in environments:
        bound = _bound(environment)
        if not bound or any(t.provisioner != "slurm" for t in bound):
            continue  # Iki starts machines for it; the provisioner says why it waits
        for cluster in sorted({str(t.slurm["cluster"]) for t in bound}):
            if cluster not in online:
                online[cluster] = Worker.objects.filter(
                    slurm_cluster=cluster,
                    revoked_at__isnull=True,
                    last_seen_at__gte=now - OFFLINE_AFTER,
                ).exists()
            if not online[cluster]:
                found[environment] = (
                    f"Waiting for the {cluster} cluster's worker: none is online. A Compute "
                    "admin can check iki-worker on the cluster's login node."
                )
                break
        else:
            full = [
                t
                for t in bound
                if t.max_concurrent is not None
                and Job.objects.filter(state__in=HELD, template=t.name).count() >= t.max_concurrent
            ]
            if full and len(full) == len(bound):
                limit = full[0].max_concurrent
                found[environment] = (
                    f"Waiting for room on the {full[0].slurm['cluster']} cluster: "
                    f"{limit} of its jobs are running, as many as `{full[0].name}` allows."
                )
    return found
