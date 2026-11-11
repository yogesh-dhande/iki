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
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.runs import catalog, services
from iki_server.runs.models import ActionRun, Job, RunInput, RunStatus
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


class ErrorOut(Schema):
    code: str
    message: str


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
    projects = {r.project_id: r.project for r in runs}
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
