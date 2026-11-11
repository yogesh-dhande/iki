"""Compute admin: the tokens workers register with, the workers themselves (ADR 0043), and
quotas on runs (ADR 0045).

All of it takes the Compute admin role. A registration token's value is
shown once, when it's made; only a hash is kept.
"""

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from django.db import transaction
from django.db.models import Count, F, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import caller, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.audit import log as audit
from iki_server.org.models import Project, Team
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs import queue, workers
from iki_server.runs.models import (
    TIERS,
    ActionRun,
    Job,
    RegistrationToken,
    RunQuota,
    RunStatus,
    Worker,
)
from iki_server.runs.queue import HELD

router = Router(tags=["compute"])

#: A worker not heard from for this long is shown as offline.
OFFLINE_AFTER = timedelta(minutes=2)


class PersonOut(Schema):
    id: UUID
    name: str


class RegistrationIn(Schema):
    label: str = Field(
        min_length=1, max_length=100, description="What it's for, such as `GPU pool (us-east-1)`."
    )
    tier: Literal["backend", "container", "vm"]
    expires_at: datetime | None = Field(
        None, description="When it stops working. Workers already registered keep working."
    )


class RegistrationOut(Schema):
    id: UUID
    label: str
    tier: str
    hint: str = Field(description="The token's last four characters, to tell it by.")
    created_by: PersonOut | None = Field(
        description="Null when an operator made it with `iki-server create_registration_token`."
    )
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    workers: int = Field(description="Workers registered with it and not revoked.")


class RegistrationCreatedOut(RegistrationOut):
    token: str = Field(description="Shown once. Give it to the tier's workers as a secret.")


def _registration_out(token: RegistrationToken) -> dict[str, Any]:
    return {
        "id": token.pk,
        "label": token.label,
        "tier": token.tier,
        "hint": token.hint,
        "created_by": (
            {"id": token.created_by.pk, "name": token.created_by.name} if token.created_by else None
        ),
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
        "workers": getattr(token, "live_workers", 0),
    }


def _registrations() -> QuerySet[RegistrationToken]:
    return RegistrationToken.objects.select_related("created_by").annotate(
        live_workers=Count("workers", filter=Q(workers__revoked_at__isnull=True))
    )


@router.get(
    "/compute/registration-tokens",
    response=Page[RegistrationOut],
    summary="Worker registration tokens",
)
def list_registrations(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first, revoked ones included."""
    require(caller(request), Action.COMPUTE_MANAGE)
    result = page(_registrations(), params, newest_first=True)
    result["items"] = [_registration_out(t) for t in result["items"]]
    return result


@router.post(
    "/compute/registration-tokens",
    response={201: RegistrationCreatedOut},
    summary="Make a worker registration token",
)
def create_registration(
    request: HttpRequest, payload: RegistrationIn, response: HttpResponse
) -> Any:  # noqa: ANN401
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    if payload.expires_at is not None and payload.expires_at <= timezone.now():
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/expires_at", "detail": "Choose a time in the future."}],
        )
    token, value = workers.create_registration(
        label=payload.label, tier=payload.tier, by=me, expires_at=payload.expires_at
    )
    response["Location"] = f"/api/v1/compute/registration-tokens/{token.pk}"
    response["Cache-Control"] = "no-store"
    created = _registrations().get(pk=token.pk)
    return Status(201, {**_registration_out(created), "token": value})


class RevokeIn(Schema):
    workers: bool = Field(False, description="Also revoke the workers that registered with it.")


def _registration(token_id: UUID) -> RegistrationToken:
    found = _registrations().filter(pk=token_id).first()
    if found is None:
        raise Problem(404, "No such registration token.")
    return found


@router.post(
    "/compute/registration-tokens/{token_id}/revoke",
    response=RegistrationOut,
    summary="Revoke a worker registration token",
)
def revoke_registration(request: HttpRequest, token_id: UUID, payload: RevokeIn) -> dict[str, Any]:
    """New workers can't register with it. Workers that did keep working, unless you also
    revoke them (`workers: true`, for a token that leaked); their jobs are then interrupted,
    and retried on other workers if their action allows."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    workers.revoke_registration(_registration(token_id), by=me, workers=payload.workers)
    return _registration_out(_registration(token_id))


class WorkerOut(Schema):
    id: UUID
    name: str
    tier: str
    hint: str
    version: str
    capacity: dict[str, Any] = Field(
        description="What it can give one job: cpu, memory, gpu_count, gpu_memory and "
        "scratch_disk (bytes)."
    )
    plugins: list[dict[str, Any]] = Field(
        description="The plugin code it has, which is all it leases: each one's `name`, "
        "`sha` (null for a local directory) and `environments` it has built, with each "
        "build's digest (null when it runs them with its host's own software)."
    )
    registration: UUID | None = Field(alias="registration_id")
    owner: PersonOut | None = Field(description="For the client tier: whose machine it is.")
    registered_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    status: Literal["online", "offline", "revoked"]
    jobs: int = Field(description="Jobs it holds now.")


def _worker_out(worker: Worker) -> dict[str, Any]:
    if worker.revoked_at is not None:
        status = "revoked"
    elif timezone.now() - worker.last_seen_at > OFFLINE_AFTER:
        status = "offline"
    else:
        status = "online"
    return {
        **{
            f: getattr(worker, f)
            for f in (
                "id",
                "name",
                "tier",
                "hint",
                "version",
                "capacity",
                "plugins",
                "registration_id",
                "registered_at",
                "last_seen_at",
                "revoked_at",
            )
        },
        "owner": (
            {"id": worker.owner.pk, "name": worker.owner.name} if worker.owner is not None else None
        ),
        "status": status,
        "jobs": getattr(worker, "held_jobs", 0),
    }


def _workers() -> QuerySet[Worker]:
    return Worker.objects.select_related("owner").annotate(
        held_jobs=Count("jobs", filter=Q(jobs__state__in=HELD))
    )


class WorkerFilters(PageParams):
    tier: Literal["client", "backend", "container", "vm"] | None = None
    revoked: bool = Field(False, description="Include revoked workers.")


@router.get("/compute/workers", response=Page[WorkerOut], summary="Workers")
def list_workers(request: HttpRequest, filters: Query[WorkerFilters]) -> dict[str, Any]:
    """Newest first."""
    require(caller(request), Action.COMPUTE_MANAGE)
    found = _workers()
    if filters.tier is not None:
        found = found.filter(tier=filters.tier)
    if not filters.revoked:
        found = found.filter(revoked_at__isnull=True)
    result = page(found, filters, newest_first=True)
    result["items"] = [_worker_out(w) for w in result["items"]]
    return result


def _worker(worker_id: UUID) -> Worker:
    found = _workers().filter(pk=worker_id).first()
    if found is None:
        raise Problem(404, "No such worker.")
    return found


@router.post("/compute/workers/{worker_id}/revoke", response=WorkerOut, summary="Revoke a worker")
def revoke_worker(request: HttpRequest, worker_id: UUID) -> dict[str, Any]:
    """Its token stops working at once. Jobs it holds are interrupted, and retried on other
    workers if their action allows."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    workers.revoke_worker(_worker(worker_id), by=me)
    return _worker_out(_worker(worker_id))


# --- Quotas --------------------------------------------------------------------------------------

Tier = Literal["client", "backend", "container", "vm"]


class ScopeOut(Schema):
    id: UUID
    name: str


class QuotaOut(Schema):
    id: UUID
    team: ScopeOut | None
    project: ScopeOut | None
    tier: Tier | None = Field(description="None: every tier together.")
    max_running: int | None = Field(description="Jobs running at once. 0 pauses the scope.")
    max_queued: int | None = Field(description="Runs waiting to start.")
    running: int = Field(description="Jobs running in its scope now.")
    queued: int = Field(description="Runs waiting in its scope now.")
    created_by: PersonOut
    created_at: datetime
    updated_at: datetime


class QuotaIn(Schema):
    team: UUID | None = Field(
        None, description="A team's runs. Leave out team and project for the organization's."
    )
    project: UUID | None = Field(None, description="A project's runs.")
    tier: Tier | None = Field(None, description="Only this tier's runs; all tiers if left out.")
    max_running: int | None = Field(None, ge=0, le=100_000)
    max_queued: int | None = Field(None, ge=0, le=1_000_000)


class QuotaPatch(Schema):
    max_running: int | None = Field(None, ge=0, le=100_000)
    max_queued: int | None = Field(None, ge=0, le=1_000_000)


def _quota_out(quota: RunQuota) -> dict[str, Any]:
    held = Job.objects.filter(state__in=HELD)
    waiting = ActionRun.objects.filter(status=RunStatus.QUEUED)
    if quota.tier:
        held, waiting = held.filter(tier=quota.tier), waiting.filter(tier=quota.tier)
    if quota.project_id is not None:
        held, waiting = (
            held.filter(project_id=quota.project_id),
            waiting.filter(project_id=quota.project_id),
        )
    elif quota.team_id is not None:
        held, waiting = (
            held.filter(project__team_id=quota.team_id),
            waiting.filter(project__team_id=quota.team_id),
        )
    return {
        "id": quota.pk,
        "team": {"id": quota.team.pk, "name": quota.team.name} if quota.team else None,
        "project": (
            {"id": quota.project.pk, "name": quota.project.name} if quota.project else None
        ),
        "tier": quota.tier,
        "max_running": quota.max_running,
        "max_queued": quota.max_queued,
        "running": held.count(),
        "queued": waiting.count(),
        "created_by": {"id": quota.created_by.pk, "name": quota.created_by.name},
        "created_at": quota.created_at,
        "updated_at": quota.updated_at,
    }


def _quotas() -> QuerySet[RunQuota]:
    return RunQuota.objects.select_related("team", "project", "created_by")


def _quota(quota_id: UUID) -> RunQuota:
    found = _quotas().filter(pk=quota_id).first()
    if found is None:
        raise Problem(404, "No such quota.")
    return found


def _limits_given(max_running: int | None, max_queued: int | None) -> None:
    if max_running is None and max_queued is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/max_running", "detail": "Set max_running, max_queued or both."}],
        )


@router.get("/compute/quotas", response=list[QuotaOut], summary="Run quotas")
def list_quotas(request: HttpRequest) -> list[dict[str, Any]]:
    """Every quota, the organization's first, with how much of each is in use."""
    require(caller(request), Action.COMPUTE_MANAGE)
    ordered = _quotas().order_by(
        F("project").asc(nulls_first=True), F("team").asc(nulls_first=True), "tier", "id"
    )
    return [_quota_out(q) for q in ordered]


@router.post("/compute/quotas", response={201: QuotaOut}, summary="Set a run quota")
def create_quota(request: HttpRequest, payload: QuotaIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """One per scope and tier. Lowering a limit doesn't stop what's running or cancel what's
    waiting; it holds back what's next."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    _limits_given(payload.max_running, payload.max_queued)
    if payload.team is not None and payload.project is not None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/project", "detail": "Choose a team or a project, not both."}],
        )
    team = Team.objects.filter(pk=payload.team).first() if payload.team else None
    project = Project.objects.filter(pk=payload.project).first() if payload.project else None
    if payload.team is not None and team is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/team", "detail": "No such team."}],
        )
    if payload.project is not None and project is None:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": "#/project", "detail": "No such project."}],
        )
    if RunQuota.objects.filter(team=team, project=project, tier=payload.tier).exists():
        raise Refused("There's a quota for this scope and tier already: change that one.")
    with transaction.atomic():
        quota = RunQuota.objects.create(
            team=team,
            project=project,
            tier=payload.tier,
            max_running=payload.max_running,
            max_queued=payload.max_queued,
            created_by=me,
        )
        audit.record(
            "quota.created",
            actor=me,
            target=quota,
            project=project,
            team=team,
            data=_limits(quota),
        )
    response["Location"] = f"/api/v1/compute/quotas/{quota.pk}"
    return Status(201, _quota_out(_quota(quota.pk)))


def _limits(quota: RunQuota) -> dict[str, Any]:
    return {"tier": quota.tier, "max_running": quota.max_running, "max_queued": quota.max_queued}


@router.patch("/compute/quotas/{quota_id}", response=QuotaOut, summary="Change a run quota")
def update_quota(request: HttpRequest, quota_id: UUID, payload: QuotaPatch) -> dict[str, Any]:
    """Send both limits: a limit left out is removed."""
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    _limits_given(payload.max_running, payload.max_queued)
    with transaction.atomic():
        quota = RunQuota.objects.select_for_update().get(pk=_quota(quota_id).pk)
        before = _limits(quota)
        quota.max_running, quota.max_queued = payload.max_running, payload.max_queued
        quota.updated_at = timezone.now()
        quota.save(update_fields=["max_running", "max_queued", "updated_at"])
        audit.record(
            "quota.updated",
            actor=me,
            target=quota,
            project=quota.project,
            team=quota.team,
            data={"before": before, "after": _limits(quota)},
        )
    for tier in [quota.tier] if quota.tier else list(TIERS):
        queue.notify(tier)  # a raised limit may let jobs start
    return _quota_out(_quota(quota.pk))


@router.delete("/compute/quotas/{quota_id}", response={204: None}, summary="Remove a run quota")
def delete_quota(request: HttpRequest, quota_id: UUID) -> Any:  # noqa: ANN401
    me = caller(request)
    require(me, Action.COMPUTE_MANAGE)
    quota = _quota(quota_id)
    with transaction.atomic():
        audit.record(
            "quota.removed",
            actor=me,
            target=quota,
            project=quota.project,
            team=quota.team,
            data=_limits(quota),
        )
        quota.delete()
    for tier in [quota.tier] if quota.tier else list(TIERS):
        queue.notify(tier)
    return Status(204, None)
