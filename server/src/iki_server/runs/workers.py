"""Registering workers, and the tokens they use (ADR 0043).

A compute admin makes a registration token for a tier (``iki_wreg_…``) and
gives it to that tier's workers: a Kubernetes secret for backend pods, the
launch configuration of a VM. A worker registers with it once and gets its
own token (``iki_wkr_…``), which it uses for everything else. Only SHA-256
hashes of both are stored; each is shown once. Revoking a registration
token stops new workers registering with it (and, if asked, revokes those
that did). Revoking a worker interrupts its jobs, which are retried elsewhere.

A self-hosted worker (ADR 0112) is a machine a compute admin registers for one or
more teams: an instrument's PC, a licensed workstation, an on-premises GPU. Its
registration token names the teams, and perhaps only some of their projects, and
the labels its machines get. Each worker that registers with it starts with those,
which an admin can change on it later, and it leases only runs in the projects they
cover (:func:`covered`), checked again on every report about a job.

A Slurm cluster's worker (ADR 0133) runs on the cluster's login node. Its registration
token is for the cluster, as the deployment's Slurm templates name it, and so is each
worker that registers with it: it leases the container-tier jobs those templates take,
many at once.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from packaging.version import InvalidVersion, Version

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import Project, Team
from iki_server.org.services import Refused
from iki_server.runs.models import (
    LABEL_PATTERN,
    MAX_LABELS,
    RegistrationToken,
    Worker,
)

__all__ = [
    "REGISTRATION_PREFIX",
    "WORKER_PREFIX",
    "Outdated",
    "authenticate",
    "change",
    "check_version",
    "clean_labels",
    "covered",
    "covers",
    "create_registration",
    "make_scope",
    "register",
    "revoke_registration",
    "revoke_worker",
    "update_for",
]

REGISTRATION_PREFIX = "iki_wreg_"
WORKER_PREFIX = "iki_wkr_"
#: ``last_seen_at`` and ``last_used_at`` are written at most this often, not on every call.
SEEN_RESOLUTION = timedelta(seconds=15)

SELF_HOSTED = "self_hosted"


class Outdated(Exception):  # noqa: N818 - reads as "the worker is outdated"
    """A worker more than one minor release behind the one the server wants (ADR 0060)."""

    def __init__(self, version: str) -> None:
        wanted = settings.IKI_WORKER_VERSION
        super().__init__(
            f"iki-worker {version} is too old for this server, which takes {wanted} or the "
            "minor release before it. Update iki-worker."
        )
        self.version = version


def _parsed(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


def check_version(version: str) -> None:
    """Refuses (with :class:`Outdated`) a worker too old for this server: it takes the
    version it wants and the minor release before it, and anything newer."""
    have, want = _parsed(version), _parsed(settings.IKI_WORKER_VERSION)
    if have is None or want is None:
        return
    oldest = (want.major, max(want.minor - 1, 0))
    if (have.major, have.minor) < oldest:
        raise Outdated(version)


def update_for(worker: Worker) -> dict[str, str] | None:
    """The release a self-hosted worker should update itself to, if any: the one the server
    wants, when that's a release and not what it runs. Workers in images (the other tiers)
    come with their deployment instead."""
    wanted = settings.IKI_WORKER_VERSION
    want, have = _parsed(wanted), _parsed(worker.version)
    if not settings.IKI_WORKER_UPDATES or worker.tier != SELF_HOSTED or want is None:
        return None
    if want.is_prerelease or want.is_devrelease or want == have:
        return None
    base = f"{settings.IKI_WORKER_DOWNLOADS}/v{wanted}/iki-worker-{wanted}-{{target}}.tar.gz"
    return {"version": wanted, "url": base, "bundle_url": f"{base}.sigstore.json"}


#: The states in which a worker holds a job (queue.HELD, which imports this module).
HELD_STATES = ("leased", "staging", "pending", "running", "uploading")

#: A worker not heard from for this long shows as offline.
OFFLINE_AFTER = timedelta(minutes=2)

#: What a worker reports it can give one job.
CAPACITY_KEYS = ("cpu", "memory", "gpu_count", "gpu_memory", "scratch_disk")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def new_worker_token() -> str:
    """A worker's own token: shown to it once, only its hash kept."""
    return _token(WORKER_PREFIX)


_LABEL = re.compile(LABEL_PATTERN)


def clean_labels(given: list[str] | tuple[str, ...], *, field: str = "labels") -> list[str]:
    """Labels as Iki keeps them: lowercase, each once, in order. Refuses one that isn't a
    label, such as ``Lab 3``, and more than :data:`MAX_LABELS`."""
    found = sorted({label.strip().lower() for label in given})
    for label in found:
        if not _LABEL.match(label):
            raise Refused(
                f"`{label}` isn't a label: use lowercase letters, digits, dots, dashes and "
                "underscores, such as `chromeleon` or `lab-3`, up to 50 characters.",
                field=field,
            )
    if len(found) > MAX_LABELS:
        raise Refused(f"Give at most {MAX_LABELS} labels.", field=field)
    return found


@dataclass(frozen=True)
class Scope:
    """The teams a self-hosted worker takes runs for, and within a team, only these
    projects if any of the team's are listed."""

    teams: tuple[Team, ...]
    projects: tuple[Project, ...] = ()

    def data(self) -> dict[str, list[str]]:
        """For the audit log."""
        return {
            "teams": sorted(str(t.pk) for t in self.teams),
            "projects": sorted(str(p.pk) for p in self.projects),
        }


def make_scope(teams: list[Any], projects: list[Any]) -> Scope:
    """The teams and projects with these IDs, checked: at least one team, none archived,
    and each project in one of them."""
    found = list(Team.objects.filter(pk__in=teams, archived_at__isnull=True).order_by("name"))
    if not teams or len(found) != len(set(teams)):
        raise Refused(
            "Choose the teams whose runs it takes." if not teams else "No such team.",
            field="teams",
        )
    chosen = list(Project.objects.filter(pk__in=projects).order_by("name"))
    if len(chosen) != len(set(projects)):
        raise Refused("No such project.", field="projects")
    outside = [p.name for p in chosen if p.team_id not in {t.pk for t in found}]
    if outside:
        raise Refused(
            f"{', '.join(outside)} isn't in any of its teams: add its team, or leave it out.",
            field="projects",
        )
    return Scope(tuple(found), tuple(chosen))


def create_registration(
    *,
    label: str,
    tier: str,
    by: User | None,
    expires_at: datetime | None = None,
    scope: Scope | None = None,
    labels: list[str] | None = None,
    slurm_cluster: str = "",
) -> tuple[RegistrationToken, str]:
    """A new registration token, and its value (shown once). ``by`` is None when an
    operator makes it with `iki-server create_registration_token`. A self-hosted one is
    for the teams (and projects) `scope` names, and gives its machines `labels`. One for
    a Slurm cluster's login node names the cluster."""
    if slurm_cluster:
        from iki_server.runs import slurm

        if tier != "container":
            raise Refused("A Slurm cluster runs container-tier runs.", field="tier")
        if slurm_cluster not in slurm.clusters():
            known = ", ".join(slurm.clusters()) or "none"
            raise Refused(
                f"This deployment's compute.yaml has no Slurm templates for {slurm_cluster!r} "
                f"(its clusters: {known}).",
                field="slurm_cluster",
            )
    if (tier == SELF_HOSTED) != (scope is not None):
        raise Refused(
            "A self-hosted token is for some teams' machines: choose the teams."
            if tier == SELF_HOSTED
            else "Only self-hosted workers belong to teams.",
            field="teams",
        )
    if labels and tier != SELF_HOSTED:
        raise Refused("Only self-hosted workers have labels.", field="labels")
    value = _token(REGISTRATION_PREFIX)
    with transaction.atomic():
        token = RegistrationToken.objects.create(
            label=label,
            tier=tier,
            token_hash=_hash(value),
            hint=value[-4:],
            created_by=by,
            expires_at=expires_at,
            labels=labels or [],
            slurm_cluster=slurm_cluster,
        )
        data: dict[str, Any] = {"tier": tier}
        if slurm_cluster:
            data["slurm_cluster"] = slurm_cluster
        if scope is not None:
            token.teams.set(scope.teams)
            token.projects.set(scope.projects)
            data |= {**scope.data(), "labels": token.labels}
        audit.record("worker_registration.created", actor=by, target=token, data=data)
    return token, value


def covered(worker: Worker) -> QuerySet[Project]:
    """The projects a self-hosted worker runs runs in: its teams' projects, but for a team
    some of whose projects it lists, only those."""
    teams = worker.teams.values("pk")
    listed = worker.projects.filter(team__in=teams)
    return Project.objects.filter(
        Q(pk__in=listed.values("pk")) | ~Q(team__in=listed.values("team")), team__in=teams
    )


def covers(worker: Worker, project_id: Any) -> bool:  # noqa: ANN401 - a project's ID
    """Whether `worker` may run runs in the project. Workers on the other tiers run any
    project's runs on their tier."""
    return worker.tier != SELF_HOSTED or covered(worker).filter(pk=project_id).exists()


def change(
    worker: Worker, *, by: User, labels: list[str] | None = None, scope: Scope | None = None
) -> Worker:
    """Change a self-hosted worker's labels or scope (None: as they are). Audited. Jobs it
    holds in projects it no longer covers are interrupted, and retried elsewhere if their
    action allows. A job keeps running if only labels changed, as a GitHub runner's does."""
    from iki_server.runs import queue

    if worker.tier != SELF_HOSTED:
        raise Refused("Only self-hosted workers have labels and teams.")
    with transaction.atomic():
        locked = Worker.objects.select_for_update().get(pk=worker.pk)
        if locked.revoked_at is not None:
            raise Refused(f"{locked.name} was revoked: register it again instead.")
        before = {"labels": locked.labels, **_scope_of(locked).data()}
        if labels is not None:
            locked.labels = labels
            locked.save(update_fields=["labels"])
        if scope is not None:
            locked.teams.set(scope.teams)
            locked.projects.set(scope.projects)
        after = {"labels": locked.labels, **_scope_of(locked).data()}
        if after != before:
            audit.record(
                "worker.changed", actor=by, target=locked, data={"before": before, "after": after}
            )
            if scope is not None:
                queue.interrupt_outside(locked, covered(locked))
            queue.notify(SELF_HOSTED)  # it may take runs it couldn't before
    return locked


#: Why a queued self-hosted run waits (ADR 0112): no worker covers its project with the
#: labels it needs, those that do are offline, or they're all running other jobs.
Waiting = Literal["unregistered", "offline", "busy"]
#: What a run needs of a self-hosted worker: its project's ID, its team's, and labels.
Need = tuple[Any, Any, tuple[str, ...]]


def waiting(needs: set[Need]) -> dict[Need, Waiting | None]:
    """For each (project ID, its team's ID, labels) that queued self-hosted runs need, why
    none of the workers could take one now, or None if one could. Self-hosted workers are
    a deployment's few machines, so it weighs them all, once."""
    if not needs:
        return {}
    now = timezone.now()
    live = list(
        Worker.objects.filter(tier=SELF_HOSTED, revoked_at__isnull=True)
        .prefetch_related("teams", "projects")
        .annotate(held=Count("jobs", filter=Q(jobs__state__in=HELD_STATES)))
    )
    found: dict[Need, Waiting | None] = {}
    for need in needs:
        project, team, wanted = need
        able = [w for w in live if _would_cover(w, project, team) and set(wanted) <= set(w.labels)]
        online = [w for w in able if now - w.last_seen_at <= OFFLINE_AFTER]
        found[need] = (
            "unregistered"
            if not able
            else "offline"
            if not online
            else "busy"
            if all(w.held for w in online)
            else None
        )
    return found


def _would_cover(worker: Worker, project: Any, team: Any) -> bool:  # noqa: ANN401 - IDs
    """:func:`covers`, for a worker whose teams and projects are prefetched."""
    if team not in {t.pk for t in worker.teams.all()}:
        return False
    listed = {p.pk for p in worker.projects.all() if p.team_id == team}
    return not listed or project in listed


def _scope_of(worker: Worker) -> Scope:
    return Scope(tuple(worker.teams.order_by("name")), tuple(worker.projects.order_by("name")))


def revoke_registration(token: RegistrationToken, *, by: User, workers: bool = False) -> int:
    """Stops new workers registering with it. Workers that did keep working (as GitHub's
    runners do) unless `workers`, for a token that leaked: then they're revoked too, and
    their jobs interrupted. Returns how many workers were revoked."""
    with transaction.atomic():
        locked = RegistrationToken.objects.select_for_update().get(pk=token.pk)
        if locked.revoked_at is None:
            locked.revoked_at = timezone.now()
            locked.save(update_fields=["revoked_at"])
            audit.record("worker_registration.revoked", actor=by, target=locked)
        revoked = 0
        if workers:
            for worker in Worker.objects.filter(registration=locked, revoked_at__isnull=True):
                revoked += revoke_worker(worker, by=by)
    return revoked


def _bearer(header: str, prefix: str) -> str | None:
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(prefix) or len(value) > 200:
        return None
    return value


def registration_for(header: str) -> RegistrationToken | None:
    """The live registration token an ``Authorization: Bearer`` header names, or None."""
    value = _bearer(header, REGISTRATION_PREFIX)
    if value is None:
        return None
    token = RegistrationToken.objects.filter(
        token_hash=_hash(value), revoked_at__isnull=True
    ).first()
    if token is None or (token.expires_at is not None and token.expires_at <= timezone.now()):
        return None
    return token


def register(
    registration: RegistrationToken,
    *,
    name: str,
    version: str,
    capacity: dict[str, Any],
    plugins: list[dict[str, Any]],
) -> tuple[Worker, str]:
    """A new worker for the registration's tier, and its token (shown once). A self-hosted
    one starts with the registration's teams, projects and labels."""
    value = _token(WORKER_PREFIX)
    now = timezone.now()
    with transaction.atomic():
        worker = Worker.objects.create(
            name=name,
            tier=registration.tier,
            token_hash=_hash(value),
            hint=value[-4:],
            registration=registration,
            version=version,
            capacity={k: capacity.get(k) for k in CAPACITY_KEYS},
            plugins=plugins,
            labels=registration.labels,
            slurm_cluster=registration.slurm_cluster,
            last_seen_at=now,
        )
        data: dict[str, Any] = {
            "tier": worker.tier,
            "registration": str(registration.pk),
            "version": version,
            "plugins": plugins,
        }
        if worker.slurm_cluster:
            data["slurm_cluster"] = worker.slurm_cluster
        if worker.tier == SELF_HOSTED:
            worker.teams.set(registration.teams.all())
            worker.projects.set(registration.projects.all())
            data |= {**_scope_of(worker).data(), "labels": worker.labels}
        RegistrationToken.objects.filter(pk=registration.pk).update(last_used_at=now)
        audit.record("worker.registered", target=worker, data=data)
    return worker, value


def update(
    worker: Worker, *, version: str, capacity: dict[str, Any], plugins: list[dict[str, Any]]
) -> Worker:
    """What a worker that kept its token says it is now, each time it starts: a new image
    can bring a new version, plugins or environments. Audited when it changes."""
    capacity = {k: capacity.get(k) for k in CAPACITY_KEYS}
    changed = {
        key: value
        for key, value in (("version", version), ("capacity", capacity), ("plugins", plugins))
        if getattr(worker, key) != value
    }
    with transaction.atomic():
        Worker.objects.filter(pk=worker.pk).update(**changed, last_seen_at=timezone.now())
        if changed:
            audit.record("worker.updated", target=worker, data=changed)
    for key, value in changed.items():
        setattr(worker, key, value)
    return worker


def authenticate(header: str) -> Worker | None:
    """The live worker an ``Authorization: Bearer`` header names, or None.

    A worker stops working when it's revoked. Revoking its registration token doesn't stop
    it.
    """
    value = _bearer(header, WORKER_PREFIX)
    if value is None:
        return None
    worker = (
        Worker.objects.select_related("launch")
        .filter(token_hash=_hash(value), revoked_at__isnull=True)
        .first()
    )
    if worker is None:
        return None
    now = timezone.now()
    if now - worker.last_seen_at >= SEEN_RESOLUTION:
        Worker.objects.filter(pk=worker.pk).update(last_seen_at=now)
        worker.last_seen_at = now
    return worker


def revoke_worker(worker: Worker, *, by: User | None) -> bool:
    """Revoke a worker's token. Its jobs are interrupted, and retried under their policy."""
    from iki_server.runs import queue

    with transaction.atomic():
        locked = Worker.objects.select_for_update().get(pk=worker.pk)
        if locked.revoked_at is not None:
            return False
        locked.revoked_at = timezone.now()
        locked.save(update_fields=["revoked_at"])
        audit.record("worker.revoked", actor=by, target=locked)
        queue.interrupt_worker(locked, code="worker_revoked")
        if locked.launch_id is not None:
            # Its machine is done with: the provisioner removes what's left of it.
            from iki_server.runs import launches

            assert locked.launch is not None  # noqa: S101 - launch_id is set
            launches.end(locked.launch, "finished")
    return True
