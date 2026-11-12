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
from django.db.models import Count, Max
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require
from iki_server.api.entities import FieldOut, RunRefOut, field_out
from iki_server.api.pagination import PageParams, cursor_after, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef
from iki_server.entities import access, registry, schema
from iki_server.entities.values import Invalid
from iki_server.files import storage
from iki_server.files.models import UploadState
from iki_server.org import permissions
from iki_server.runs import (
    approvals,
    catalog,
    costs,
    input_files,
    provisioner,
    quotas,
    secrets,
    services,
    workers,
)
from iki_server.runs.models import (
    FINISHED,
    ActionRun,
    ApprovalState,
    ImportPhase,
    Job,
    JobLog,
    JobState,
    RunApproval,
    RunArtifact,
    RunBatch,
    RunCheckpoint,
    RunEffect,
    RunImport,
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


class ParamOut(FieldOut):
    """A param is a field, as entity types' are, with its default and limits."""

    default: Any = Field(None, description="What it is when not given.")
    min: float | None
    max: float | None
    min_length: int | None
    max_length: int | None
    pattern: str | None


class CreatesOut(Schema):
    type: str
    cardinality: Literal["one", "many"]
    derived_from: list[str]


class UpdatesOut(Schema):
    input: str
    fields: list[str]


class ActionSecretOut(Schema):
    name: str = Field(description="The environment variable its runs get it as.")
    required: bool = Field(description="Whether the action can run without it.")
    description: str = Field(description="What it's for, as the plugin says.")
    key: str = Field(description="Where administrators set it: its key in the run secrets.")
    set: bool = Field(description="Whether this deployment has set it. Its value is never shown.")


class EstimateOut(Schema):
    template: str = Field(description="The compute template a run would start on.")
    price_per_hour: float
    currency: str = Field(description="An ISO 4217 code, such as USD.")
    typical: float | None = Field(
        description="What its recent runs that succeeded cost, the median of them; null "
        "before any has."
    )
    runs_seen: int = Field(description="How many recent runs `typical` is from, up to 50.")
    timeout_cost: float = Field(
        description="What an attempt that runs to its timeout costs. Retries cost again."
    )


class ActionOut(Schema):
    name: str = Field(description="The qualified name, such as `protein.compute_properties`.")
    plugin: str
    label: str
    description: str | None
    tier: Tier
    environment: str
    inputs: list[InputOut]
    params: list[ParamOut]
    params_schema: dict[str, Any] = Field(
        description="The params as JSON Schema (2020-12), as entity types' `values_schema`."
    )
    creates: list[CreatesOut]
    updates: list[UpdatesOut]
    deterministic: bool
    network: list[str] = Field(description="The hosts its runs may reach.")
    secrets: list[ActionSecretOut] = Field(description="The secrets its runs get, by name.")
    timeout: int = Field(description="Seconds each attempt may run.")
    batchable: int = Field(
        description="Up to how many runs of a batch one job runs at once (ADR 0055); 1 for one."
    )
    available: bool = Field(description="Whether it can run on this deployment.")
    unavailable_reason: str | None
    needs: str = Field(
        description="The project role that runs it: Editor (client and backend tiers) or "
        "Runner (container and vm)."
    )
    estimate: EstimateOut | None = Field(
        description="On the container and VM tiers, what a run may cost (ADR 0070). Null "
        "where runs cost nothing, or the templates have no price."
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
                **field_out(name, p, action.plugin),
                "default": p.default,
                "min": p.min,
                "max": p.max,
                "min_length": p.min_length,
                "max_length": p.max_length,
                "pattern": p.pattern,
            }
            for name, p in action.params.items()
        ],
        "params_schema": schema.for_params(action.label or action.name, dict(action.params)),
        "creates": [
            {"type": c.type, "cardinality": c.cardinality, "derived_from": list(c.derived_from)}
            for c in action.creates
        ],
        "updates": [{"input": u.input, "fields": list(u.fields)} for u in action.updates],
        "deterministic": action.deterministic,
        "network": list(action.network),
        "secrets": [
            {
                "name": s.name,
                "required": s.required,
                "description": s.description,
                "key": secrets.key(action.plugin, s.name),
                "set": secrets.is_set(action.plugin, s.name),
            }
            for s in action.secrets
        ],
        "timeout": action.timeout,
        "batchable": action.batchable,
        "available": available.available,
        "unavailable_reason": available.reason,
        "needs": "Editor" if action.tier in ("client", "backend") else "Runner",
        "estimate": costs.estimate(action),
    }


def _action(name: str) -> Action:
    found = catalog.action(name)
    if found is None:
        raise Problem(404, f"No action `{name}` is installed.")
    return found


class ActionFilters(Schema):
    takes: list[str] = Field(
        default_factory=list,
        description="Only actions with an input that takes entities of each of these types, "
        "such as `takes=dna.Primer`. An input takes its own type and every type that extends it.",
    )


@router.get("/actions", response=list[ActionOut], summary="Installed actions")
def list_actions(request: HttpRequest, filters: Query[ActionFilters]) -> list[dict[str, Any]]:
    """Every action installed plugins provide, in name order, with whether it can run here."""
    caller(request)
    return [
        _action_out(a)
        for a in catalog.actions().values()
        if not filters.takes or services.input_taking(a, filters.takes) is not None
    ]


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
    reuse: bool = Field(
        True,
        description="For a deterministic action: reuse the results of an identical run that "
        "succeeded, which you can read, instead of running again (ADR 0057). The run ends at "
        "once, with `reused_from`. Set false to run it anyway.",
    )
    add_to_project: bool = Field(
        False,
        description="A run reads only what's in its project (ADR 0066). Set true to add "
        "inputs from other projects to it first, which its members can then read; each is "
        "audited as `entity.linked`. Without it, such inputs are refused with 409 "
        "`inputs_outside_project`, which lists them.",
    )


class CancelIn(Schema):
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class RunInputOut(Schema):
    name: str
    entity: UUID = Field(alias="entity_id")
    hrid: str = Field(alias="entity.hrid")
    revision: int = Field(description="The revision the run read.")


class RunFileOut(Schema):
    name: str
    media_type: str
    size: int


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


class EffectCountsOut(Schema):
    created: int
    updated: int
    reused: int


#: The effects a run's answer lists; the rest are paged from ``/runs/{ref}/effects``.
EFFECTS_SHOWN = 100


class MessageOut(Schema):
    level: Literal["info", "warning", "error"]
    text: str
    entity: UUID | None = Field(None, description="The input entity it's about, if any.")


class RunCan(Schema):
    cancel: bool
    reproduce: bool = Field(
        description="You may reproduce it exactly: it ran (or reused a run that did) with "
        "code this deployment still has, and you may run its tier in its project."
    )
    run_again: bool = Field(
        description="It reused another run's results, and you may launch its action in its "
        "project: launching it with `reuse: false` runs it after all."
    )


class DifferenceOut(Schema):
    pointer: str = Field(description="Where in the output, as a JSON pointer.")
    original: Any = Field(description="Its value in the original run's output.")
    reproduced: Any = Field(description="Its value in the reproduction's.")


class BuildsOut(Schema):
    original: str = Field(description="The environment build the original ran in.")
    reproduced: str = Field(description="The one the reproduction ran in.")


class ReproductionOut(Schema):
    matches: bool = Field(
        description="Its results are the same as the original's, in the same environment build."
    )
    differences: list[DifferenceOut] = Field(description="The first 20 places they differ.")
    more_differences: int = Field(description="How many more places they differ.")
    environment_build: BuildsOut


class ReproduceIn(Schema):
    reason: str = Field("", max_length=2000, description="Why, for the record.")


class ApprovalRunOut(Schema):
    id: UUID
    hrid: str


class ApprovalCan(Schema):
    decide: bool = Field(
        description="You may approve or decline it: you asked for it, it's waiting, and you're "
        "in the web app (tokens can't, so no agent can approve its own request)."
    )


class RunApprovalOut(Schema):
    id: UUID
    state: Literal["pending", "approved", "declined", "expired"]
    client: str = Field(description="The app that asked, such as an AI agent's MCP client.")
    requested_by: PersonRef = Field(description="Who the app acted for: who decides.")
    created_at: datetime
    expires_at: datetime
    decided_by: PersonRef | None
    decided_at: datetime | None
    note: str = Field(description="Why it was declined, if they said.")
    runs: list[ApprovalRunOut] = Field(description="The runs it holds, all decided together.")
    can: ApprovalCan


class BatchRefOut(Schema):
    id: UUID
    hrid: str = Field(description="Such as BAT-000123.")


class RunCostOut(Schema):
    amount: float = Field(description="What it has cost so far, at its templates' prices.")
    currency: str | None = Field(description="Null while nothing it ran on has a price.")
    seconds: float = Field(description="Machine time it has used.")
    final: bool = Field(
        description="It's ended and so have its machines: nothing more will be added."
    )


class RunOut(Schema):
    id: UUID
    hrid: str
    action: str
    plugin_version: str
    plugin_sha: str = Field(description="The plugin's commit, when built from a lockfile.")
    project: UUID = Field(alias="project_id")
    requested_by: PersonRef
    via: str
    client: str = Field(description="The OAuth app it was asked for through, if any.")
    approval: RunApprovalOut | None = Field(
        description="When an agent asked for it on a tier that needs approval: the request."
    )
    reason: str
    batch: BatchRefOut | None = Field(description="The batch it was launched in, if any.")
    tier: Tier
    environment: str
    params: dict[str, Any]
    settings: dict[str, Any] = Field(
        alias="plugin_settings",
        description="The plugin's settings, as the deployment set them when it was launched.",
    )
    network: list[str] = Field(
        description="The hosts it may reach, as its action declared them when it was launched."
    )
    inputs: list[RunInputOut]
    files: dict[str, RunFileOut] = Field(
        description="The files its file params name, by ID, that you can read."
    )
    status: Literal[
        "awaiting_approval",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
        "rejected",
        "declined",
    ]
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    error: ErrorOut | None
    effects: list[EffectOut] = Field(
        description=f"The first {EFFECTS_SHOWN} entities it created, updated or reused, that "
        "you can read. `GET /runs/{ref}/effects` pages through all of them."
    )
    effects_next_cursor: str | None = Field(
        description="When it changed more than `effects` lists: the `cursor` for "
        "`GET /runs/{ref}/effects` that goes on from there."
    )
    effect_counts: EffectCountsOut = Field(
        description="How many entities it created, updated and reused."
    )
    metrics: dict[str, float]
    messages: list[MessageOut]
    waiting: str | None = Field(
        description="Why a queued run hasn't started: a quota is holding it, or the machine "
        "being started for it is waiting (for a node with a GPU, say)."
    )
    needs_worker: Literal["yours", "theirs"] | None = Field(
        description="A queued client-tier run whose requester has no worker online on their "
        "own computer to run it (ADR 0058): `yours` when that's you, else `theirs`."
    )
    seed: int | None = Field(
        description="The seed its command was given (`IKI_SEED`). None for runs from before seeds."
    )
    reused: bool = Field(
        description="It reused an identical run's results, and ran nothing (ADR 0057)."
    )
    reused_from: RunRefOut | None = Field(
        description="The run whose results it reused, if you can read it."
    )
    reproduces: RunRefOut | None = Field(
        description="The run it reproduces exactly, if you can read it. It writes nothing."
    )
    reproduction: ReproductionOut | None = Field(
        description="Once a reproduction has its results: whether they match the original's."
    )
    cost: RunCostOut | None = Field(
        description="On the container and VM tiers: what the machines it ran on cost "
        "(ADR 0070). Null on tiers whose machines run anyway."
    )
    can: RunCan


def _qualified_environment(run: ActionRun) -> str:
    return f"{run.action.partition('.')[0]}.{run.environment}"


def _effect_out(effect: RunEffect) -> dict[str, Any]:
    return {
        "entity": effect.entity_id,
        "hrid": effect.entity.hrid,
        "type": effect.entity.type,
        "kind": effect.kind,
        "revision": effect.revision,
    }


def _effect_counts(runs: list[ActionRun]) -> dict[Any, dict[str, int]]:
    """By run. A bulk import counted what it wrote (ADR 0056), so its millions of effects
    aren't counted again on every read."""
    out: dict[Any, dict[str, int]] = {r.pk: {"created": 0, "updated": 0, "reused": 0} for r in runs}
    imported = {
        i.run_id: i
        for i in RunImport.objects.filter(
            run__in=runs, phase=ImportPhase.ENDED, job__state=JobState.SUCCEEDED
        )
    }
    for run_id, i in imported.items():
        out[run_id] = {"created": i.created, "updated": i.updated, "reused": i.reused}
    rest = [r for r in runs if r.pk not in imported]
    for row in (
        RunEffect.objects.filter(run__in=rest).values("run_id", "kind").annotate(n=Count("id"))
    ):
        out[row["run_id"]][row["kind"]] = row["n"]
    return out


class EffectFilters(PageParams):
    kind: Literal["created", "updated", "reused"] | None = None


@router.get("/runs/{ref}/effects", response=Page[EffectOut], summary="What a run changed")
def list_effects(request: HttpRequest, ref: str, filters: Query[EffectFilters]) -> dict[str, Any]:
    """The entities it created, updated or reused, that you can read, in the order it
    wrote them."""
    me = caller(request)
    run = _find(me, ref)
    found = RunEffect.objects.filter(run=run, entity__in=access.visible(me).values("pk"))
    if filters.kind:
        found = found.filter(kind=filters.kind)
    result = page(found.select_related("entity"), filters)
    result["items"] = [_effect_out(e) for e in result["items"]]
    return result


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
    visible = access.visible(me).values("pk")
    shown = [
        RunEffect.objects.filter(run=run, entity__in=visible)
        .order_by("id")
        .values_list("pk", flat=True)[: EFFECTS_SHOWN + 1]
        for run in runs
    ]
    more: dict[Any, str] = {}
    if shown:
        first, *rest = shown
        ids = list(first.union(*rest, all=True) if rest else first)
        for effect in RunEffect.objects.filter(pk__in=ids).select_related("entity").order_by("id"):
            listed = effects.setdefault(effect.run_id, [])
            if len(listed) == EFFECTS_SHOWN:
                more[effect.run_id] = cursor_after(listed[-1]["id"])
                continue
            listed.append({**_effect_out(effect), "id": effect.pk})
    counts = _effect_counts(runs)
    # A run that reused another's results shows that run's metrics and messages.
    sources = {r.pk: r.reused_from_id or r.pk for r in runs}
    documents = {
        o.run_id: o.document for o in RunOutput.objects.filter(run__in=set(sources.values()))
    }
    outputs = {pk: documents[source] for pk, source in sources.items() if source in documents}
    linked = {pk for r in runs for pk in (r.reused_from_id, r.reproduces_id) if pk is not None}
    refs: dict[Any, ActionRun] = {
        r.pk: r
        for r in services.visible_runs(me).filter(pk__in=linked).select_related("project__team")
    }
    shas = {r.action.split(".")[0] for r in runs}
    here = {
        plugin: (services.plugin_sha(plugin), registry.plugins()[plugin].version)
        for plugin in shas
        if plugin in registry.plugins()
    }
    named = {run.pk: input_files.of_params(run) for run in runs}
    readable = {
        f.pk: f
        for f in access.readable_files(me)
        .filter(pk__in=set().union(*named.values()))
        .select_related("blob")
    }
    projects = {r.project_id: r.project for r in runs}
    holds = {
        tier: quotas.blocked(tier)
        for tier in {r.tier for r in runs if r.status == RunStatus.QUEUED}
    }
    waiting_for_client = {
        r.requested_by_id for r in runs if r.status == RunStatus.QUEUED and r.tier == "client"
    }
    online = workers.online_owners(waiting_for_client) if waiting_for_client else set()
    starting = provisioner.waiting(
        {
            _qualified_environment(r)
            for r in runs
            if r.status == RunStatus.QUEUED and r.tier == "container"
        }
    )
    held = _approvals_out(me, {r.approval_id for r in runs if r.approval_id is not None})
    batches = {
        b["id"]: b
        for b in RunBatch.objects.filter(
            pk__in={r.batch_id for r in runs if r.batch_id is not None}
        ).values("id", "hrid")
    }
    projects.update({r.project_id: r.project for r in refs.values()})
    may_cancel = {
        (pid, tier): permissions.can(me, TIER_PERMISSIONS[tier], projects[pid])
        for pid, tier in {(r.project_id, r.tier) for r in [*runs, *refs.values()]}
    }
    return [
        {
            **{f: getattr(run, f) for f in _RUN_FIELDS},
            "project_id": run.project_id,
            "requested_by": run.requested_by,
            "approval": held.get(run.approval_id) if run.approval_id else None,
            "batch": batches.get(run.batch_id) if run.batch_id else None,
            "network": run.spec.get("network") or [],
            "inputs": inputs.get(run.pk, []),
            "files": {
                str(i): {
                    "name": readable[i].name,
                    "media_type": readable[i].media_type,
                    "size": readable[i].blob.size,
                }
                for i in sorted(named[run.pk] & readable.keys())
            },
            "attempts": attempts.get(run.pk, 0),
            "effects": effects.get(run.pk, []),
            "effects_next_cursor": more.get(run.pk),
            "effect_counts": counts[run.pk],
            "metrics": outputs.get(run.pk, {}).get("metrics") or {},
            "messages": outputs.get(run.pk, {}).get("messages") or [],
            "waiting": (
                holds[run.tier].why(run.project) or starting.get(_qualified_environment(run))
                if run.status == RunStatus.QUEUED
                else None
            ),
            "needs_worker": (
                ("yours" if run.requested_by_id == me.pk else "theirs")
                if run.status == RunStatus.QUEUED
                and run.tier == "client"
                and run.requested_by_id not in online
                else None
            ),
            "seed": run.seed,
            "reused": run.reused_from_id is not None,
            "reused_from": _ref_out(refs.get(run.reused_from_id)),
            "reproduces": _ref_out(refs.get(run.reproduces_id)),
            "reproduction": run.reproduction,
            "cost": costs.of_run(run),
            "can": {
                "cancel": not run.finished
                and run.cancel_requested_at is None
                and may_cancel[(run.project_id, run.tier)],
                "reproduce": _may_reproduce(run, refs, here, may_cancel),
                "run_again": run.reused_from_id is not None
                and catalog.action(run.action) is not None
                and may_cancel[(run.project_id, run.tier)],
            },
        }
        for run in runs
    ]


def _ref_out(run: ActionRun | None) -> dict[str, Any] | None:
    return {"id": run.pk, "hrid": run.hrid, "action": run.action} if run is not None else None


def _may_reproduce(
    run: ActionRun,
    refs: dict[Any, ActionRun],
    here: dict[str, tuple[str, str]],
    may_run: dict[tuple[Any, str], bool],
) -> bool:
    """Whether reproducing the run would be taken: what it reproduces (a run that reused or
    reproduced another stands for it) ended with an output, at code this deployment has."""
    linked = run.reused_from_id or run.reproduces_id
    original = refs.get(linked) if linked else run
    if original is None or not _has_output(original):
        return False
    if here.get(original.action.split(".")[0]) != (original.plugin_sha, original.plugin_version):
        return False
    return may_run.get((original.project_id, original.tier), False)


def _has_output(run: ActionRun) -> bool:
    """It ended with results: it succeeded, or they didn't fit."""
    if run.status == RunStatus.SUCCEEDED:
        return run.reused_from_id is None
    code = (run.error or {}).get("code")
    return run.status in (RunStatus.FAILED, RunStatus.REJECTED) and code in (
        "invalid_output",
        "schema_changed",
    )


_RUN_FIELDS = (
    "id",
    "hrid",
    "action",
    "plugin_version",
    "plugin_sha",
    "via",
    "client",
    "reason",
    "tier",
    "environment",
    "params",
    "plugin_settings",
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


def _outside(error: services.OutsideProject) -> Problem:
    return Problem(
        409,
        str(error),
        code="inputs_outside_project",
        extensions={
            "entities": [{"id": str(e.pk), "hrid": e.hrid, "type": e.type} for e in error.entities]
        },
    )


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
    and editable by you where it changes them) and its params. A run reads only what's in its
    project: inputs from other projects need `add_to_project`."""
    me = caller(request)
    action = _action(name)
    target = project(me, payload.project, services.TIER_PERMISSIONS[action.tier])
    inputs = {k: v if isinstance(v, list) else [v] for k, v in payload.inputs.items()}
    try:
        run = services.launch(
            me,
            action,
            target,
            inputs,
            payload.params,
            reason=payload.reason,
            reuse_results=payload.reuse,
            add_to_project=payload.add_to_project,
        )
    except Invalid as error:
        raise _invalid(error) from None
    except services.OutsideProject as error:
        raise _outside(error) from None
    response["Location"] = f"/api/v1/runs/{run.pk}"
    return Status(201, _runs_out(me, [_find(me, str(run.pk))])[0])


#: The most runs one batch launches.
MAX_BATCH = 1000


class RunBatchIn(Schema):
    project: UUID = Field(description="Where they run. Entities they create belong to it.")
    each: str = Field(description="The input each run takes one of `entities` for.")
    entities: list[UUID] = Field(
        min_length=1,
        max_length=MAX_BATCH,
        description=f"One run for each, in this order: at most {MAX_BATCH}.",
    )
    inputs: dict[str, UUID | list[UUID]] = Field(
        default_factory=dict, description="Every run's other inputs, as for one run."
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Every run's params.")
    reason: str = Field("", max_length=2000, description="Why, for the record.")
    reuse: bool = Field(
        True,
        description="For a deterministic action: for each run, reuse the results of an "
        "identical one that succeeded, which you can read, instead of running again (ADR "
        "0057). Those end at once, with `reused_from`. Set false to run them all.",
    )
    add_to_project: bool = Field(
        False,
        description="A run reads only what's in its project (ADR 0066). Set true to add "
        "inputs from other projects to it first, which its members can then read; each is "
        "audited as `entity.linked`. Without it, such inputs are refused with 409 "
        "`inputs_outside_project`, which lists them.",
    )


class BatchCan(Schema):
    cancel: bool = Field(description="Some of its runs haven't finished, and you may cancel them.")


class BatchOut(Schema):
    id: UUID
    hrid: str = Field(description="Such as BAT-000123.")
    action: str
    project: UUID = Field(alias="project_id")
    requested_by: PersonRef
    via: str
    client: str = Field(description="The OAuth app it was asked for through, if any.")
    reason: str
    each: str = Field(description="The input each run took one entity for.")
    size: int = Field(description="How many runs it launched.")
    statuses: dict[str, int] = Field(
        description="How many of its runs are in each status, for the statuses any are in."
    )
    created_at: datetime
    finished_at: datetime | None = Field(description="When its last run finished, once all have.")
    can: BatchCan


class RunBatchOut(Schema):
    batch: BatchOut
    runs: list[RunOut]


def _batches_out(me: User, batches: list[RunBatch]) -> list[dict[str, Any]]:
    runs = ActionRun.objects.filter(batch__in=batches)
    statuses: dict[Any, dict[str, int]] = {}
    for row in runs.values("batch", "status").annotate(count=Count("id")):
        statuses.setdefault(row["batch"], {})[row["status"]] = row["count"]
    # A batch's runs are of one action, so on one tier.
    tiers = dict(runs.values_list("batch", "tier").distinct())
    last = dict(runs.values("batch").annotate(last=Max("finished_at")).values_list("batch", "last"))
    out = []
    for batch in batches:
        counts = statuses.get(batch.pk, {})
        unfinished = sum(n for status, n in counts.items() if status not in _FINISHED)
        out.append(
            {
                **{f: getattr(batch, f) for f in _BATCH_FIELDS},
                "project_id": batch.project_id,
                "requested_by": batch.requested_by,
                "statuses": counts,
                "finished_at": last.get(batch.pk) if not unfinished else None,
                "can": {
                    "cancel": bool(unfinished)
                    and permissions.can(
                        me, TIER_PERMISSIONS[tiers.get(batch.pk, "backend")], batch.project
                    )
                },
            }
        )
    return out


_BATCH_FIELDS = ("id", "hrid", "action", "via", "client", "reason", "each", "size", "created_at")
_FINISHED = frozenset(s.value for s in FINISHED)


@router.post(
    "/actions/{name}/runs/batch",
    response={201: RunBatchOut},
    summary="Run an action on each of several entities",
    tags=["runs"],
)
def launch_runs(request: HttpRequest, name: str, payload: RunBatchIn) -> Any:  # noqa: ANN401
    """Queue one run for each entity, as `POST /actions/{name}/runs` would one at a time,
    but all or none: if any can't run, none is queued, and the problem says which and why."""
    me = caller(request)
    action = _action(name)
    target = project(me, payload.project, services.TIER_PERMISSIONS[action.tier])
    inputs = {k: v if isinstance(v, list) else [v] for k, v in payload.inputs.items()}
    try:
        launched = services.launch_each(
            me,
            action,
            target,
            payload.each,
            payload.entities,
            inputs,
            payload.params,
            reason=payload.reason,
            reuse_results=payload.reuse,
            add_to_project=payload.add_to_project,
        )
    except Invalid as error:
        raise _invalid(error) from None
    except services.OutsideProject as error:
        raise _outside(error) from None
    found = {
        r.pk: r
        for r in services.visible_runs(me)
        .filter(pk__in=[r.pk for r in launched])
        .select_related("project__team", "requested_by")
    }
    runs = [found[r.pk] for r in launched]
    batch = _find_batch(me, str(launched[0].batch_id))
    return Status(201, {"batch": _batches_out(me, [batch])[0], "runs": _runs_out(me, runs)})


def _find_batch(me: User, ref: str) -> RunBatch:
    batches = services.visible_batches(me).select_related("project__team", "requested_by")
    try:
        found = batches.filter(pk=UUID(ref)).first()
    except ValueError:
        found = batches.filter(hrid=ref.upper()).first()
    if found is None:
        raise Problem(404, "No such batch, or you can't read its project.")
    return found


class BatchFilters(PageParams):
    project: UUID | None = Field(None, description="Only batches in this project.")
    action: str | None = Field(None, description="Only batches of this action.")


@router.get("/batches", response=Page[BatchOut], summary="Batches you can read")
def list_batches(request: HttpRequest, filters: Query[BatchFilters]) -> dict[str, Any]:
    """Newest first. A batch is the runs of one action launched together, one for each of
    several entities; `GET /runs?batch=` lists them."""
    me = caller(request)
    batches = services.visible_batches(me).select_related("project__team", "requested_by")
    if filters.project is not None:
        batches = batches.filter(
            project=project(me, filters.project, permissions.Action.PROJECT_READ)
        )
    if filters.action is not None:
        batches = batches.filter(action=filters.action)
    result = page(batches, filters, newest_first=True)
    result["items"] = _batches_out(me, result["items"])
    return result


@router.get("/batches/{ref}", response=BatchOut, summary="A batch")
def get_batch(request: HttpRequest, ref: str) -> dict[str, Any]:
    """By its ID or HRID (BAT-000123)."""
    me = caller(request)
    return _batches_out(me, [_find_batch(me, ref)])[0]


@router.post("/batches/{ref}/cancel", response=BatchOut, summary="Cancel a batch")
def cancel_batch(request: HttpRequest, ref: str, payload: CancelIn) -> dict[str, Any]:
    """Cancels each of its runs that hasn't finished, as `POST /runs/{ref}/cancel` would."""
    me = caller(request)
    batch = _find_batch(me, ref)
    try:
        services.cancel_batch(me, batch, reason=payload.reason)
    except PermissionDenied:
        raise Problem(403) from None
    return _batches_out(me, [_find_batch(me, ref)])[0]


class RunFilters(PageParams):
    project: UUID | None = Field(None, description="Only runs in this project.")
    batch: str | None = Field(
        None, description="Only runs of this batch (its ID or HRID), in the order it launched them."
    )
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
    if filters.batch is not None:
        runs = runs.filter(batch=_find_batch(me, filters.batch))
    result = page(runs, filters, newest_first=filters.batch is None)
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


@router.post("/runs/{ref}/reproduce", response={201: RunOut}, summary="Reproduce a run exactly")
def reproduce_run(
    request: HttpRequest, ref: str, payload: ReproduceIn, response: HttpResponse
) -> Any:  # noqa: ANN401
    """Queue a run that reproduces this one exactly (ADR 0057): the same plugin commit,
    action as launched, params, plugin settings, inputs at the revisions it read, and seed.
    It writes nothing: when it ends, `reproduction` says whether its results are the same.
    A run that reused another's results is reproduced as that one. Whoever may launch its
    tier in its project may reproduce it; it's refused (409) when this deployment runs
    other code for its plugin."""
    me = caller(request)
    run = _find(me, ref)
    try:
        made = services.reproduce(me, run, reason=payload.reason)
    except PermissionDenied:
        raise Problem(403) from None
    response["Location"] = f"/api/v1/runs/{made.pk}"
    return Status(201, _runs_out(me, [_find(me, str(made.pk))])[0])


# --- Approvals -----------------------------------------------------------------------------------


def _approvals_out(me: User, ids: set[Any]) -> dict[Any, dict[str, Any]]:
    if not ids:
        return {}
    found = RunApproval.objects.filter(pk__in=ids).select_related("requested_by", "decided_by")
    held: dict[Any, list[dict[str, Any]]] = {}
    for run_id, hrid, approval_id in (
        ActionRun.objects.filter(approval__in=ids)
        .order_by("id")
        .values_list("pk", "hrid", "approval")
    ):
        held.setdefault(approval_id, []).append({"id": run_id, "hrid": hrid})
    web = permissions.token_limits(me) is None
    return {
        a.pk: {
            "id": a.pk,
            "state": a.state,
            "client": a.client,
            "requested_by": a.requested_by,
            "created_at": a.created_at,
            "expires_at": a.expires_at,
            "decided_by": a.decided_by,
            "decided_at": a.decided_at,
            "note": a.note,
            "runs": held.get(a.pk, []),
            "can": {
                "decide": web and a.requested_by_id == me.pk and a.state == ApprovalState.PENDING
            },
        }
        for a in found
    }


def _approval(me: User, approval_id: UUID) -> RunApproval:
    found = (
        RunApproval.objects.filter(project__in=access.readable_projects(me).values("pk"))
        .filter(pk=approval_id)
        .first()
    )
    if found is None:
        raise Problem(404, "No such request, or you can’t see it.")
    return found


def _decide(me: User, approval: RunApproval) -> None:
    if permissions.token_limits(me) is not None:
        raise Problem(
            403,
            "Approve or decline runs in Iki’s web app. Tokens can’t, so no agent can approve "
            "its own request.",
            code="web_only",
        )
    if approval.requested_by_id != me.pk:
        raise Problem(403, "Only the person the app asked for can decide.")


class DeclineIn(Schema):
    note: str = Field("", max_length=2000, description="Why, for the record and for the agent.")


@router.get(
    "/run-approvals/{approval_id}", response=RunApprovalOut, summary="Runs waiting for approval"
)
def get_run_approval(request: HttpRequest, approval_id: UUID) -> dict[str, Any]:
    """A request an app made, acting for someone, for runs that need their approval."""
    me = caller(request)
    found = _approval(me, approval_id)
    return _approvals_out(me, {found.pk})[found.pk]


@router.post(
    "/run-approvals/{approval_id}/approve", response=RunApprovalOut, summary="Approve runs"
)
def approve_runs(request: HttpRequest, approval_id: UUID) -> dict[str, Any]:
    """Queues every run the request holds. Only its person may, in the web app, and only
    while they may still run its tier in the project."""
    me = caller(request)
    found = _approval(me, approval_id)
    _decide(me, found)
    try:
        approvals.approve(me, found)
    except PermissionDenied:
        raise Problem(403, "You can’t run this tier in its project any more.") from None
    return _approvals_out(me, {found.pk})[found.pk]


@router.post(
    "/run-approvals/{approval_id}/decline", response=RunApprovalOut, summary="Decline runs"
)
def decline_runs(request: HttpRequest, approval_id: UUID, payload: DeclineIn) -> dict[str, Any]:
    """Ends every run the request holds, unstarted. Only its person may, in the web app."""
    me = caller(request)
    found = _approval(me, approval_id)
    _decide(me, found)
    try:
        approvals.decline(me, found, note=payload.note)
    except PermissionDenied:
        raise Problem(403) from None
    return _approvals_out(me, {found.pk})[found.pk]


# --- Attempts and their logs ---------------------------------------------------------------------


class EgressOut(Schema):
    host: str
    port: int
    refused: str | None = Field(
        description="Why the egress proxy refused; null when it let the connections through."
    )
    connections: int
    bytes_sent: int
    bytes_received: int
    first_at: datetime
    last_at: datetime


class AttemptPackOut(Schema):
    runs: int = Field(description="How many runs of its batch its job ran at once, this one too.")
    reported_as: str = Field(
        description="The run its worker held the job as (its HRID). The job's log, progress "
        "and network use are every one of its runs'."
    )


class AttemptImportOut(Schema):
    phase: Literal["checking", "writing", "ended"] = Field(
        description="Checking each line, then writing them all at once."
    )
    size: int = Field(description="The creates file's size, in bytes.")
    read: int = Field(description="Bytes of it checked so far.")
    lines: int = Field(description="Lines checked so far.")
    staged: int = Field(description="Creates checked and ready to write.")
    created: int
    reused: int


class CheckpointOut(Schema):
    attempt: int = Field(description="The attempt that saved it.")
    saved_at: datetime
    size: int = Field(description="Bytes.")
    sha256: str
    kept: bool = Field(
        description="Whether its bytes are still kept: they're deleted once a newer one is "
        "saved, or the run ends (ADR 0069)."
    )


class RunAttemptOut(Schema):
    attempt: int = Field(description="1 for the first, then 2, 3, ...")
    state: JobState
    worker: str | None = Field(description="The name of the worker that ran it.")
    environment_build: str = Field(
        description="The digest of the environment it ran in; empty for the host's own software."
    )
    template: str = Field(
        description="The compute template of the machine Iki started for it, if it did (ADR 0061)."
    )
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: float | None = Field(description="From 0 to 1, as its command reports it.")
    progress_message: str
    log_size: int = Field(description="Bytes its command has written so far.")
    error: ErrorOut | None
    network: list[EgressOut] = Field(
        description="The hosts it reached through the egress proxy, or tried to (ADR 0053)."
    )
    secrets: list[str] = Field(description="The secrets its worker was given, by name (ADR 0054).")
    pack: AttemptPackOut | None = Field(
        description="When it ran in one job with other runs of its batch (ADR 0055)."
    )
    bulk_import: AttemptImportOut | None = Field(
        description="When its command gave its creates in a file: how far importing them has "
        "got (ADR 0056)."
    )
    checkpoint: CheckpointOut | None = Field(
        description="For a checkpointable action: the newest checkpoint it saved (ADR 0069)."
    )
    resumed_from: CheckpointOut | None = Field(
        description="The checkpoint an earlier attempt saved that this one resumed from."
    )


@router.get("/runs/{ref}/attempts", response=list[RunAttemptOut], summary="A run's attempts")
def list_attempts(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Oldest first. A run is attempted again when its machine goes away."""
    run = _find(caller(request), ref)
    jobs = list(
        Job.objects.filter(run=run)
        .select_related("worker", "packed_into__worker", "packed_into__run")
        .prefetch_related("egress", "packed_into__egress")
        .order_by("attempt")
    )
    bulk = {i.job_id: i for i in RunImport.objects.filter(job__in=jobs)}
    saved = _checkpoints(run)
    carriers = {job.packed_into_id or job.pk for job in jobs}
    packed = dict(
        Job.objects.filter(packed_into__in=carriers)
        .values("packed_into")
        .annotate(n=Count("id"))
        .values_list("packed_into", "n")
    )
    out = []
    for job in jobs:
        held = job.packed_into or job
        size = packed.get(held.pk, 0)
        out.append(
            {
                "attempt": job.attempt,
                "state": job.state,
                "worker": held.worker.name if held.worker else None,
                "environment_build": job.environment_build,
                "template": job.template,
                "queued_at": job.queued_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "progress": job.progress,
                "progress_message": job.progress_message,
                "log_size": held.log_size,
                "error": job.error,
                "secrets": held.secrets,
                "network": [
                    {
                        "host": e.host,
                        "port": e.port,
                        "refused": e.refused or None,
                        "connections": e.connections,
                        "bytes_sent": e.bytes_sent,
                        "bytes_received": e.bytes_received,
                        "first_at": e.first_at,
                        "last_at": e.last_at,
                    }
                    for e in sorted(held.egress.all(), key=lambda e: (bool(e.refused), e.first_at))
                ],
                "pack": {
                    "runs": size + 1,
                    "reported_as": held.run.hrid if held is not job else run.hrid,
                }
                if size
                else None,
                "bulk_import": _import_out(bulk[job.pk]) if job.pk in bulk else None,
                "checkpoint": _checkpoint_out(saved["by_job"].get(job.pk)),
                "resumed_from": _checkpoint_out(saved["by_id"].get(job.resumed_from_id)),
            }
        )
    return out


def _checkpoints(run: ActionRun) -> dict[str, dict[Any, RunCheckpoint]]:
    """The run's checked checkpoints: each attempt's newest, and each by its ID."""
    found = list(
        RunCheckpoint.objects.filter(run=run, upload__state=UploadState.READY)
        .select_related("job", "upload")
        .order_by("created_at", "id")
    )
    return {"by_job": {c.job_id: c for c in found}, "by_id": {c.pk: c for c in found}}


def _checkpoint_out(checkpoint: RunCheckpoint | None) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    return {
        "attempt": checkpoint.job.attempt,
        "saved_at": checkpoint.created_at,
        "size": checkpoint.upload.size,
        "sha256": checkpoint.upload.sha256,
        "kept": checkpoint.deleted_at is None,
    }


def _import_out(found: RunImport) -> dict[str, Any]:
    return {
        "phase": found.phase,
        "size": found.size,
        "read": found.offset,
        "lines": found.lines,
        "staged": found.staged,
        "created": found.created,
        "reused": found.reused,
    }


def _attempt(run: ActionRun, attempt: int) -> Job:
    job = Job.objects.filter(run=run, attempt=attempt).select_related("packed_into").first()
    if job is None:
        raise Problem(404, f"{run.hrid} has no attempt {attempt}.")
    return job


#: The most of a log one answer holds; ask again from ``next_offset`` for more.
LOG_PAGE_BYTES = 1024**2


class AttemptLogOut(Schema):
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


@router.get(
    "/runs/{ref}/attempts/{attempt}/log", response=AttemptLogOut, summary="An attempt's log"
)
def attempt_log(
    request: HttpRequest, ref: str, attempt: int, params: Query[LogParams]
) -> dict[str, Any]:
    """What the attempt's command printed, from `offset` on. Follow a running attempt with
    the run's live updates (`GET /runs/{ref}/events`): each `log` event says it grew."""
    run = _find(caller(request), ref)
    job = _attempt(run, attempt)
    # An attempt packed into another's job has that job's log.
    held = job.packed_into or job
    chunks = JobLog.objects.filter(job=held).order_by("offset")
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
        "size": held.log_size,
        "ended": job.finished_at is not None,
    }


@router.get(
    "/runs/{ref}/attempts/{attempt}/log.txt",
    summary="Download an attempt's log",
    openapi_extra={"responses": {200: {"description": "The log", "content": {"text/plain": {}}}}},
)
def download_attempt_log(request: HttpRequest, ref: str, attempt: int) -> HttpResponse:
    """All of it that's kept, as a text file, such as for a record."""
    run = _find(caller(request), ref)
    job = _attempt(run, attempt)
    held = job.packed_into or job
    text = "".join(
        JobLog.objects.filter(job=held).order_by("offset").values_list("text", flat=True)
    )
    response = HttpResponse(text.encode(), content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{run.hrid}-attempt-{attempt}.log"'
    response["Cache-Control"] = "no-store"
    return response


# --- Artifacts ---------------------------------------------------------------------------------


class RunArtifactOut(Schema):
    id: UUID
    attempt: int = Field(description="The attempt whose command wrote it.")
    path: str = Field(description="Where the command wrote it, relative to its output directory.")
    name: str = Field(description="Its file name, the last part of its path.")
    media_type: str
    size: int = Field(description="Bytes.")
    sha256: str = Field(description="Of its bytes, as hex.")
    state: Literal["uploading", "kept", "failed", "deleted"] = Field(
        description="`kept` while it can be downloaded; `deleted` once its retention is over "
        "(its record stays); `failed` if its upload didn't finish."
    )
    created_at: datetime
    expires_at: datetime = Field(description="When its retention ends, and it's deleted.")
    deleted_at: datetime | None


def _artifact_state(artifact: RunArtifact) -> str:
    if artifact.deleted_at is not None:
        return "deleted"
    upload = artifact.upload.state
    if upload == UploadState.READY:
        return "kept"
    return "uploading" if upload in (UploadState.PENDING, UploadState.CHECKING) else "failed"


def _artifact_out(artifact: RunArtifact) -> dict[str, Any]:
    upload = artifact.upload
    return {
        "id": artifact.pk,
        "attempt": artifact.job.attempt,
        "path": artifact.path,
        "name": upload.name,
        "media_type": upload.media_type,
        "size": upload.size,
        "sha256": upload.sha256,
        "state": _artifact_state(artifact),
        "created_at": artifact.created_at,
        "expires_at": artifact.expires_at,
        "deleted_at": artifact.deleted_at,
    }


@router.get("/runs/{ref}/artifacts", response=list[RunArtifactOut], summary="A run's artifacts")
def list_artifacts(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Files its command wrote besides its results, that its action keeps (ADR 0068): each
    attempt's, by attempt then path. At most 1,000 an attempt."""
    run = _find(caller(request), ref)
    found = (
        RunArtifact.objects.filter(run=run)
        .select_related("upload", "job")
        .order_by("job__attempt", "path")
    )
    return [_artifact_out(a) for a in found]


@router.get(
    "/runs/{ref}/artifacts/{artifact_id}/content",
    response={302: None},
    summary="Download an artifact",
)
def download_artifact(request: HttpRequest, ref: str, artifact_id: UUID) -> HttpResponse:
    """Redirects to a signed URL that works for five minutes, which downloads it under its
    name. Once its retention is over, it's gone (410)."""
    run = _find(caller(request), ref)
    artifact = RunArtifact.objects.select_related("upload").filter(run=run, pk=artifact_id).first()
    if artifact is None:
        raise Problem(404, "The run has no such artifact.")
    if artifact.deleted_at is not None:
        raise Problem(
            410,
            f"{artifact.path} was deleted on {artifact.deleted_at:%Y-%m-%d}, once its "
            "retention was over.",
            code="artifact_deleted",
        )
    if _artifact_state(artifact) != "kept":
        raise Problem(409, f"{artifact.path} didn't finish uploading.", code="artifact_not_kept")
    if not storage.configured():
        raise Problem(503, str(storage.NotConfigured()), code="storage_not_configured")
    upload = artifact.upload
    url = storage.download_url(
        storage.uri(storage.artifact_key(upload.pk)), "", upload.name, upload.media_type
    )
    response = HttpResponse(status=302)
    response["Location"] = url
    response["Cache-Control"] = "no-store"
    return response
