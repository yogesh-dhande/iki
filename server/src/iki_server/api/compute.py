"""Compute admin: the tokens workers register with, and the workers themselves (ADR 0043).

All of it takes the Compute admin role. A registration token's value is
shown once, when it's made; only a hash is kept.
"""

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import caller, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.org.permissions import Action
from iki_server.runs import workers
from iki_server.runs.models import RegistrationToken, Worker
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
    created_by: PersonOut
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
        "created_by": {"id": token.created_by.pk, "name": token.created_by.name},
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
