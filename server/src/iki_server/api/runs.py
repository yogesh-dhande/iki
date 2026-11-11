"""Actions and their runs (ADR 0042).

``GET /actions`` lists what installed plugins can do and whether each can run
on this deployment. ``POST /actions/{name}/runs`` queues a run on entities
you can read. Running on the client and backend tiers takes Editor in the
project; the container and vm tiers take Runner. Runs are read by anyone who
can read their project.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.entities import access
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.runs import catalog, quotas, services
from iki_server.runs.models import (
    ActionRun,
    Job,
    JobLog,
    JobState,
    RunEffect,
    RunInput,
    RunOutput,
    RunStatus,
)
from iki_server.runs.services import TIER_PERMISSIONS
from iki_spec.model import Action

router = Router(tags=["runs"])

Tier = Literal["client", "backend", "container", "vm"]


# --- Actions -----------------------------------------------------------------------------------


class InputOut(Schema):
    name: str
    label: str
    description: str | None
    type: str
    cardinality: Literal["one", "many"]
    max: int | None
    optional: bool


class ParamOut(Schema):
    name: str
    label: str
    description: str | None
    type: str
    required: bool
    default: Any = None
    values: list[str] = Field(description="For enum params: the choices.")
    min: float | None
    max: float | None
    unit: str | None


class CreatesOut(Schema):
    type: str
    cardinality: Literal["one", "many"]
    derived_from: list[str]


class UpdatesOut(Schema):
    input: str
    fields: list[str]


class ActionOut(Schema):
    name: str = Field(description="The qualified name, such as `protein.compute_properties`.")
    plugin: str
    label: str
    description: str | None
    tier: Tier
    environment: str
    inputs: list[InputOut]
    params: list[ParamOut]
    creates: list[CreatesOut]
    updates: list[UpdatesOut]
    deterministic: bool
    network: list[str] = Field(description="The hosts its runs may reach.")
    secrets: list[str] = Field(description="The secrets its runs get, by name.")
    timeout: int = Field(description="Seconds each attempt may run.")
    available: bool = Field(description="Whether it can run on this deployment.")
    unavailable_reason: str | None
    needs: str = Field(
        description="The project role that runs it: Editor (client and backend tiers) or "
        "Runner (container and vm)."
    )


def _action_out(action: Action) -> dict[str, Any]:
    available = catalog.availability(action)
    return {
        "name": action.qualified_name,
        "plugin": action.plugin,
        "label": action.label,
        "description": action.description,
        "tier": action.tier,
        "environment": action.environment,
        "inputs": [
            {
                "name": i.name,
                "label": i.label,
                "description": i.description,
                "type": i.type,
                "cardinality": i.cardinality,
                "max": i.max,
                "optional": i.optional,
            }
            for i in action.inputs.values()
        ],
        "params": [
            {
                "name": p.name,
                "label": p.label,
                "description": p.description,
                "type": p.type,
                "required": p.required,
                "default": p.default,
                "values": list(p.values),
                "min": p.min,
                "max": p.max,
                "unit": p.unit,
            }
            for p in action.params.values()
        ],
        "creates": [
            {"type": c.type, "cardinality": c.cardinality, "derived_from": list(c.derived_from)}
            for c in action.creates
        ],
        "updates": [{"input": u.input, "fields": list(u.fields)} for u in action.updates],
        "deterministic": action.deterministic,
        "network": list(action.network),
        "secrets": list(action.secrets),
        "timeout": action.timeout,
        "available": available.available,
        "unavailable_reason": available.reason,
        "needs": "Editor" if action.tier in ("client", "backend") else "Runner",
    }


def _action(name: str) -> Action:
    found = catalog.action(name)
    if found is None:
        raise Problem(404, f"No action `{name}` is installed.")
    return found


@router.get("/actions", response=list[ActionOut], summary="Installed actions")
def list_actions(request: HttpRequest) -> list[dict[str, Any]]:
    """Every action installed plugins provide, in name order, with whether it can run here."""
    caller(request)
    return [_action_out(a) for a in catalog.actions().values()]


@router.get("/actions/{name}", response=ActionOut, summary="An action")
def get_action(request: HttpRequest, name: str) -> dict[str, Any]:
    caller(request)
    return _action_out(_action(name))


# --- Runs --------------------------------------------------------------------------------------


class RunIn(Schema):
    project: UUID = Field(description="Where it runs. Entities it creates belong to it.")
    inputs: dict[str, UUID | list[UUID]] = Field(
        default_factory=dict,
        description="Entity IDs for each input: one, or a list for inputs that take many.",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class CancelIn(Schema):
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class PersonOut(Schema):
    id: UUID
    name: str


class RunInputOut(Schema):
    name: str
    entity: UUID = Field(alias="entity_id")
    hrid: str = Field(alias="entity.hrid")
    revision: int = Field(description="The revision the run read.")


class OutputErrorOut(Schema):
    pointer: str = Field(description="Where in the run's output, as a JSON pointer.")
    detail: str


class ErrorOut(Schema):
    code: str
    message: str
    errors: list[OutputErrorOut] = Field(
        default_factory=list, description="For `invalid_output` and `schema_changed`."
    )


class EffectOut(Schema):
    entity: UUID
    hrid: str
    type: str
    kind: Literal["created", "updated", "reused"]
    revision: int | None = Field(description="The revision it made; none if it changed nothing.")


class MessageOut(Schema):
    level: Literal["info", "warning", "error"]
    text: str


class RunCan(Schema):
    cancel: bool


class RunOut(Schema):
    id: UUID
    hrid: str
    action: str
    plugin_version: str
    plugin_sha: str = Field(description="The plugin's commit, when built from a lockfile.")
    project: UUID = Field(alias="project_id")
    requested_by: PersonOut
    via: str
    reason: str
    tier: Tier
    environment: str
    params: dict[str, Any]
    inputs: list[RunInputOut]
    status: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "timed_out", "rejected"
    ]
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    error: ErrorOut | None
    effects: list[EffectOut] = Field(
        description="The entities it created or updated, that you can read."
    )
    metrics: dict[str, float]
    messages: list[MessageOut]
    waiting: str | None = Field(
        description="Why a queued run hasn't started, when a quota is holding it."
    )
    can: RunCan


def _runs_out(me: User, runs: list[ActionRun]) -> list[dict[str, Any]]:
    inputs: dict[Any, list[RunInput]] = {}
    for row in (
        RunInput.objects.filter(run__in=runs)
        .select_related("entity")
        .order_by("run_id", "name", "position")
    ):
        inputs.setdefault(row.run_id, []).append(row)
    attempts = {
        row["run"]: row["count"]
        for row in Job.objects.filter(run__in=runs).values("run").annotate(count=Count("id"))
    }
    effects: dict[Any, list[dict[str, Any]]] = {}
    for effect in (
        RunEffect.objects.filter(run__in=runs, entity__in=access.visible(me).values("pk"))
        .select_related("entity")
        .order_by("id")
    ):
        effects.setdefault(effect.run_id, []).append(
            {
                "entity": effect.entity_id,
                "hrid": effect.entity.hrid,
                "type": effect.entity.type,
                "kind": effect.kind,
                "revision": effect.revision,
            }
        )
    outputs = {o.run_id: o.document for o in RunOutput.objects.filter(run__in=runs)}
    projects = {r.project_id: r.project for r in runs}
    holds = {
        tier: quotas.blocked(tier)
        for tier in {r.tier for r in runs if r.status == RunStatus.QUEUED}
    }
    may_cancel = {
        (pid, tier): permissions.can(me, TIER_PERMISSIONS[tier], projects[pid])
        for pid, tier in {(r.project_id, r.tier) for r in runs}
    }
    return [
        {
            **{f: getattr(run, f) for f in _RUN_FIELDS},
            "project_id": run.project_id,
            "requested_by": {"id": run.requested_by.pk, "name": run.requested_by.name},
            "inputs": inputs.get(run.pk, []),
            "attempts": attempts.get(run.pk, 0),
            "effects": effects.get(run.pk, []),
            "metrics": outputs.get(run.pk, {}).get("metrics") or {},
            "messages": outputs.get(run.pk, {}).get("messages") or [],
            "waiting": (
                holds[run.tier].why(run.project) if run.status == RunStatus.QUEUED else None
            ),
            "can": {
                "cancel": not run.finished
                and run.cancel_requested_at is None
                and may_cancel[(run.project_id, run.tier)]
            },
        }
        for run in runs
    ]


_RUN_FIELDS = (
    "id",
    "hrid",
    "action",
    "plugin_version",
    "plugin_sha",
    "via",
    "reason",
    "tier",
    "environment",
    "params",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "cancel_requested_at",
    "error",
)


def _find(me: User, ref: str) -> ActionRun:
    runs = services.visible_runs(me).select_related("project__team", "requested_by")
    try:
        found = runs.filter(pk=UUID(ref)).first()
    except ValueError:
        found = runs.filter(hrid=ref.upper()).first()
    if found is None:
        raise Problem(404, "No such run, or you can't read its project.")
    return found


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "Some inputs or params are invalid.",
        code="validation_failed",
        errors=[
            {"pointer": f"#/{name}", "detail": " ".join(messages)}
            for name, messages in error.errors.items()
        ],
    )


@router.post("/actions/{name}/runs", response={201: RunOut}, summary="Run an action", tags=["runs"])
def launch_run(request: HttpRequest, name: str, payload: RunIn, response: HttpResponse) -> Any:  # noqa: ANN401
    """Queue a run. Everything is checked first: your role for the action's tier, that it can
    run on this deployment, its inputs (entities you can read, of the right types, not archived,
    and editable by you where it changes them) and its params."""
    me = caller(request)
    action = _action(name)
    target = project(me, payload.project, services.TIER_PERMISSIONS[action.tier])
    inputs = {k: v if isinstance(v, list) else [v] for k, v in payload.inputs.items()}
    try:
        run = services.launch(me, action, target, inputs, payload.params, reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    response["Location"] = f"/api/v1/runs/{run.pk}"
    return Status(201, _runs_out(me, [_find(me, str(run.pk))])[0])


class RunFilters(PageParams):
    project: UUID | None = Field(None, description="Only runs in this project.")
    action: str | None = Field(None, description="Only runs of this action.")
    status: RunStatus | None = None


@router.get("/runs", response=Page[RunOut], summary="Runs you can read")
def list_runs(request: HttpRequest, filters: Query[RunFilters]) -> dict[str, Any]:
    """Newest first."""
    me = caller(request)
    runs = services.visible_runs(me).select_related("project__team", "requested_by")
    if filters.project is not None:
        runs = runs.filter(project=project(me, filters.project, permissions.Action.PROJECT_READ))
    if filters.action is not None:
        runs = runs.filter(action=filters.action)
    if filters.status is not None:
        runs = runs.filter(status=filters.status)
    result = page(runs, filters, newest_first=True)
    result["items"] = _runs_out(me, result["items"])
    return result


@router.get("/runs/{ref}", response=RunOut, summary="A run")
def get_run(request: HttpRequest, ref: str) -> dict[str, Any]:
    """By its ID or HRID (RUN-000123)."""
    me = caller(request)
    return _runs_out(me, [_find(me, ref)])[0]


@router.post("/runs/{ref}/cancel", response=RunOut, summary="Cancel a run")
def cancel_run(request: HttpRequest, ref: str, payload: CancelIn) -> dict[str, Any]:
    """A queued run is cancelled at once; a running one is asked to stop, and its worker ends
    it. Whoever may launch the run's tier in its project may cancel it."""
    me = caller(request)
    run = _find(me, ref)
    require(me, TIER_PERMISSIONS[run.tier], run.project)
    try:
        services.cancel(me, run, reason=payload.reason)
    except PermissionDenied:
        raise Problem(403) from None
    return _runs_out(me, [_find(me, ref)])[0]


# --- Attempts and their logs ---------------------------------------------------------------------


class AttemptOut(Schema):
    attempt: int = Field(description="1 for the first, then 2, 3, ...")
    state: JobState
    worker: str | None = Field(description="The name of the worker that ran it.")
    environment_build: str = Field(
        description="The digest of the environment it ran in; empty for the host's own software."
    )
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: float | None = Field(description="From 0 to 1, as its command reports it.")
    progress_message: str
    log_size: int = Field(description="Bytes its command has written so far.")
    error: ErrorOut | None


@router.get("/runs/{ref}/attempts", response=list[AttemptOut], summary="A run's attempts")
def list_attempts(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Oldest first. A run is attempted again when its machine goes away."""
    run = _find(caller(request), ref)
    return [
        {
            "attempt": job.attempt,
            "state": job.state,
            "worker": job.worker.name if job.worker else None,
            "environment_build": job.environment_build,
            "queued_at": job.queued_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "progress": job.progress,
            "progress_message": job.progress_message,
            "log_size": job.log_size,
            "error": job.error,
        }
        for job in Job.objects.filter(run=run).select_related("worker").order_by("attempt")
    ]


#: The most of a log one answer holds; ask again from ``next_offset`` for more.
LOG_PAGE_BYTES = 1024**2


class LogOut(Schema):
    offset: int = Field(description="Where `text` starts, in bytes of UTF-8.")
    text: str
    next_offset: int = Field(description="Where to ask from next.")
    more: bool = Field(description="More is kept already: ask again from `next_offset` now.")
    size: int = Field(
        description="Bytes the command has written. Past 16 MiB they're counted, not kept."
    )
    ended: bool = Field(description="The attempt has ended, so nothing more will come.")


class LogParams(Schema):
    offset: int = Field(0, ge=0, description="Bytes of UTF-8 to skip, such as a `next_offset`.")


@router.get("/runs/{ref}/attempts/{attempt}/log", response=LogOut, summary="An attempt's log")
def attempt_log(
    request: HttpRequest, ref: str, attempt: int, params: Query[LogParams]
) -> dict[str, Any]:
    """What the attempt's command printed, from `offset` on. Follow a running attempt with
    the run's live updates (`GET /runs/{ref}/events`): each `log` event says it grew."""
    run = _find(caller(request), ref)
    job = Job.objects.filter(run=run, attempt=attempt).first()
    if job is None:
        raise Problem(404, f"{run.hrid} has no attempt {attempt}.")
    chunks = JobLog.objects.filter(job=job).order_by("offset")
    first = chunks.filter(offset__lte=params.offset).order_by("-offset").first()
    start = first.offset if first is not None else 0
    parts: list[bytes] = []
    taken, more, end = 0, False, params.offset
    for chunk in chunks.filter(offset__gte=start).iterator(chunk_size=64):
        data = chunk.text.encode()
        if chunk.offset < params.offset:
            data = data[params.offset - chunk.offset :]
        if taken and taken + len(data) > LOG_PAGE_BYTES:
            more = True
            break
        parts.append(data)
        taken += len(data)
        end = max(chunk.offset, params.offset) + len(data)
    return {
        "offset": params.offset,
        "text": b"".join(parts).decode(errors="replace"),
        "next_offset": end,
        "more": more,
        "size": job.log_size,
        "ended": job.finished_at is not None,
    }
