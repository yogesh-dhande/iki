"""Workflows and their runs (ADR 0079).

``GET /workflows`` lists the workflows installed plugins provide.
``POST /workflows/{name}/runs`` starts one: each step is an ordinary run, launched as you,
in the project, once the steps it takes from have succeeded. Starting one takes the role
each of its steps' tiers needs. Workflow runs are read by anyone who can read their project.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project
from iki_server.api.entities import field_out
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.runs import (
    CancelIn,
    InputOut,
    ParamOut,
    Tier,
    invalid_problem,
    outside_problem,
)
from iki_server.api.schemas import AgentOut, Page, PersonRef, agent_out
from iki_server.entities import schema
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.runs import catalog, services, workflows
from iki_server.runs.models import ActionRun, StepState, WorkflowRun, WorkflowStep
from iki_spec.model import Workflow

router = Router(tags=["workflows"])

#: Tiers from the least a starter needs to the most.
TIER_ORDER = ("client", "backend", "container", "vm")


# --- Workflows ---------------------------------------------------------------------------------


class ConditionOut(Schema):
    value: str = Field(
        description="What's compared: `$params.<name>`, or `$steps.<id>.metrics.<name>`, a "
        "number that step's run reported."
    )
    operator: Literal["is", "is_not", "in", "at_least", "at_most", "above", "below"]
    operand: Any = Field(
        description="What it's compared with: a value, `$params.<name>`, or for `in` a list "
        "of them. A string starting `$$` stands for one starting `$`."
    )


class ApprovalOut(Schema):
    role: Literal["viewer", "editor", "runner", "admin"] = Field(
        description="The project role an approver needs."
    )
    instructions: str = Field(description="What the approver checks; empty if nothing's said.")


class WorkflowStepOut(Schema):
    id: str
    label: str
    action: str = Field(description="The qualified name of the action it runs.")
    inputs: dict[str, str] = Field(
        description="Where each input comes from: `$inputs.<name>`, "
        "`$steps.<id>.created.<Type>` or `$steps.<id>.inputs.<name>`."
    )
    params: dict[str, Any] = Field(
        description="Each param's value, or `$params.<name>` for the workflow's."
    )
    after: list[str] = Field(description="The steps it starts after, in name order.")
    each: str | None = Field(
        description="The input it runs once for each entity of, as a batch, if any."
    )
    when: list[ConditionOut] = Field(
        description="Conditions that must all hold for it to run, checked once the steps it "
        "starts after have succeeded. If one doesn't, it's skipped, and so are the steps "
        "after it."
    )
    approval: ApprovalOut | None = Field(
        description="If a person approves it before it starts: someone with this role in the "
        "project, other than whoever started the workflow, once its conditions hold."
    )


class WorkflowOut(Schema):
    name: str = Field(description="Qualified, such as `protein.translate_and_characterize`.")
    plugin: str
    label: str
    description: str | None
    inputs: list[InputOut]
    params: list[ParamOut]
    params_schema: dict[str, Any] = Field(description="The params as JSON Schema (2020-12).")
    steps: list[WorkflowStepOut] = Field(description="In the order they start.")
    tiers: list[Tier] = Field(
        description="The tiers its steps run on, from the least to the most a starter needs: "
        "starting it takes the role each needs in the project (`can.run` on a project)."
    )
    available: bool = Field(description="Whether every step's action can run on this deployment.")
    unavailable_reason: str | None


def _workflow_out(of: Workflow) -> dict[str, Any]:
    actions = workflows.actions(of)
    reason = None
    for step_id, action in actions.items():
        if action is None:
            reason = f"Step `{step_id}` runs `{of.steps[step_id].action}`, which isn't installed."
            break
        available = catalog.availability(action)
        if not available.available:
            reason = f"Step `{step_id}` can't run here. {available.reason or ''}".strip()
            break
    tiers = {a.tier for a in actions.values() if a is not None}
    return {
        "name": of.qualified_name,
        "plugin": of.plugin,
        "tiers": [t for t in TIER_ORDER if t in tiers],
        "label": of.label,
        "description": of.description,
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
            for i in of.inputs.values()
        ],
        "params": [
            {
                **field_out(name, p, of.plugin),
                "default": p.default,
                "min": p.min,
                "max": p.max,
                "min_length": p.min_length,
                "max_length": p.max_length,
                "pattern": p.pattern,
            }
            for name, p in of.params.items()
        ],
        "params_schema": schema.for_params(of.label, dict(of.params)),
        "steps": [_step_out(of, i) for i in of.order() or sorted(of.steps)],
        "available": reason is None,
        "unavailable_reason": reason,
    }


def _step_out(of: Workflow, step_id: str) -> dict[str, Any]:
    step = of.steps[step_id]
    return {
        "id": step.id,
        "label": step.label,
        "action": f"{of.plugin}.{step.action}",
        "inputs": dict(step.inputs),
        "params": dict(step.params),
        "after": sorted(step.after()),
        "each": step.each,
        "when": [
            {"value": c.value, "operator": c.operator, "operand": c.operand} for c in step.when
        ],
        "approval": None
        if step.approval is None
        else {"role": step.approval.role, "instructions": step.approval.instructions},
    }


def _workflow(name: str) -> Workflow:
    found = workflows.workflow(name)
    if found is None:
        raise Problem(404, f"No workflow `{name}` is installed.")
    return found


@router.get("/workflows", response=list[WorkflowOut], summary="Installed workflows")
def list_workflows(request: HttpRequest) -> list[dict[str, Any]]:
    """Every workflow installed plugins provide, in name order."""
    caller(request)
    return [_workflow_out(w) for w in workflows.workflows().values()]


@router.get("/workflows/{name}", response=WorkflowOut, summary="A workflow")
def get_workflow(request: HttpRequest, name: str) -> dict[str, Any]:
    caller(request)
    return _workflow_out(_workflow(name))


# --- Workflow runs -----------------------------------------------------------------------------


class WorkflowRunIn(Schema):
    project: UUID = Field(description="Where its steps run. Entities they create belong to it.")
    inputs: dict[str, UUID | list[UUID]] = Field(
        default_factory=dict,
        description="Entity IDs for each input: one, or a list for inputs that take many.",
    )
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field("", max_length=2000, description="Why, for the record.")
    add_to_project: bool = Field(
        False,
        description="Its steps read only what's in its project (ADR 0066). Set true to add "
        "inputs from other projects to it first; each is audited as `entity.linked`. Without "
        "it, such inputs are refused with 409 `inputs_outside_project`, which lists them.",
    )


class WorkflowInputOut(Schema):
    name: str
    entity: UUID = Field(alias="entity_id")
    hrid: str = Field(alias="entity.hrid")
    revision: int = Field(description="Its revision when the workflow started.")


class StepErrorOut(Schema):
    code: str = Field(
        description="`run_failed` (its run didn't succeed), `launch_failed` (its run couldn't "
        "start), `condition_false` (skipped: its conditions didn't all hold), `declined` "
        "(its approver declined it), `approval_expired` (nobody approved it in time), "
        "`condition_unknown` (failed: a metric its conditions read wasn't reported) or "
        "`skipped` (a step it starts after didn't succeed or was skipped, or the workflow "
        "was cancelled)."
    )
    message: str


class WorkflowErrorOut(Schema):
    code: str = Field(
        description="`step_failed`, or `step_declined` for a step that wasn't approved."
    )
    message: str
    step: str = Field(description="The step that failed.")


class StepRunOut(Schema):
    id: UUID
    hrid: str
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


class StepBatchOut(Schema):
    id: UUID
    hrid: str = Field(description="Such as BAT-000123.")
    size: int = Field(description="How many runs it launched.")
    statuses: dict[str, int] = Field(
        description="How many of its runs are in each status, for the statuses any are in."
    )


class WorkflowRunRef(Schema):
    id: UUID
    hrid: str


class DecisionOut(Schema):
    approved: bool
    by: PersonRef
    at: datetime
    note: str


class StepCan(Schema):
    decide: bool = Field(description="It waits for approval, and you may approve or decline it.")


class WorkflowRunStepOut(Schema):
    id: str
    label: str
    action: str
    after: list[str]
    each: str | None = Field(description="The input it runs once for each entity of, if any.")
    when: list[ConditionOut] = Field(description="Conditions that must all hold for it to run.")
    approval: ApprovalOut | None = Field(description="Who approves it before it starts, if any.")
    state: Literal[
        "waiting",
        "awaiting_approval",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        "declined",
    ]
    started_at: datetime | None
    finished_at: datetime | None
    error: StepErrorOut | None = Field(description="Why it failed or was skipped.")
    approval_requested_at: datetime | None = Field(description="When it began to wait.")
    approval_expires_at: datetime | None = Field(
        description="When it's declined if nobody has decided: 30 days after it began to wait."
    )
    decision: DecisionOut | None = Field(description="Who approved or declined it, if anyone.")
    carried_from: WorkflowRunRef | None = Field(
        description="In a re-run, the workflow run whose step it is: it succeeded there, and "
        "its run and results are that one's."
    )
    can: StepCan
    run: StepRunOut | None = Field(description="Its run, once launched, when it runs once.")
    batch: StepBatchOut | None = Field(
        description="When it runs once for each entity (`each`): their batch, once launched. "
        "`GET /runs?batch=` lists them."
    )


class WorkflowRunCan(Schema):
    cancel: bool = Field(description="It hasn't finished, and you may cancel it.")
    rerun: bool = Field(description="It failed or was cancelled, and you may start it again.")


class WorkflowRunOut(Schema):
    id: UUID
    hrid: str = Field(description="Such as WF-000123.")
    workflow: str
    label: str
    plugin_version: str
    project: UUID = Field(alias="project_id")
    requested_by: PersonRef
    via: str
    client: str = Field(description="The OAuth app it was started through, if any.")
    agent: AgentOut | None = Field(
        description="The agent that started it, acting for requested_by, if one did (ADR 0072)."
    )
    reason: str
    inputs: list[WorkflowInputOut]
    params: dict[str, Any]
    status: Literal["running", "succeeded", "failed", "cancelled"]
    steps: list[WorkflowRunStepOut] = Field(description="In the order they start.")
    created_at: datetime
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    error: WorkflowErrorOut | None = Field(description="Why it failed.")
    rerun_of: WorkflowRunRef | None = Field(description="The workflow run it re-runs, if any.")
    reruns: list[WorkflowRunRef] = Field(
        description="The workflow runs that re-run it, oldest first."
    )
    can: WorkflowRunCan


def _runs_out(me: User, found: list[WorkflowRun]) -> list[dict[str, Any]]:
    steps: dict[Any, list[WorkflowStep]] = {}
    listed = WorkflowStep.objects.filter(workflow__in=found).select_related(
        "decided_by", "carried_from"
    )
    for step in listed.order_by("workflow", "position"):
        steps.setdefault(step.workflow_id, []).append(step)
    # A step run once has its run; one run for each entity, its batch's counts. A step
    # carried over into a re-run has those of the workflow run it came from.
    sources = {run.pk for run in found} | {
        s.carried_from_id for each in steps.values() for s in each if s.carried_from_id
    }
    theirs = ActionRun.objects.filter(workflow__in=sources)
    launched: dict[tuple[Any, str], ActionRun] = {
        (r.workflow_id, r.workflow_step): r
        for r in theirs.filter(batch__isnull=True).only(
            "id", "hrid", "status", "workflow_id", "workflow_step"
        )
    }
    batches: dict[tuple[Any, str], dict[str, Any]] = {}
    rows = theirs.filter(batch__isnull=False).values(
        "workflow", "workflow_step", "batch", "batch__hrid", "batch__size", "status"
    )
    for row in rows.annotate(count=Count("id")).order_by():
        made = batches.setdefault(
            (row["workflow"], row["workflow_step"]),
            {
                "id": row["batch"],
                "hrid": row["batch__hrid"],
                "size": row["batch__size"],
                "statuses": {},
            },
        )
        made["statuses"][row["status"]] = row["count"]
    inputs: dict[Any, list[Any]] = {}
    for each in workflows.inputs_of(found):
        inputs.setdefault(each.workflow_id, []).append(each)
    reruns: dict[Any, list[dict[str, Any]]] = {}
    later = WorkflowRun.objects.filter(rerun_of__in=found).order_by("id")
    for again in later.values("id", "hrid", "rerun_of"):
        reruns.setdefault(again["rerun_of"], []).append({"id": again["id"], "hrid": again["hrid"]})
    earlier = {
        r.pk: {"id": r.pk, "hrid": r.hrid}
        for r in WorkflowRun.objects.filter(pk__in=[r.rerun_of_id for r in found if r.rerun_of_id])
    }
    out = []
    for run in found:
        spec = workflows.spec_of(run)
        out.append(
            {
                **{f: getattr(run, f) for f in _FIELDS},
                "label": spec.label,
                "project_id": run.project_id,
                "requested_by": run.requested_by,
                "agent": agent_out(run.agent, run.client),
                "inputs": inputs.get(run.pk, []),
                "steps": [
                    {
                        **_step_out(spec, s.step),
                        "state": s.state,
                        "started_at": s.started_at,
                        "finished_at": s.finished_at,
                        "error": s.error,
                        "approval_requested_at": s.approval_requested_at,
                        "approval_expires_at": s.approval_expires_at,
                        "decision": _decision(s),
                        "can": {"decide": workflows.why_not_decide(me, run, s, spec) is None},
                        "run": _ref(launched.get((s.carried_from_id or run.pk, s.step))),
                        "batch": batches.get((s.carried_from_id or run.pk, s.step)),
                        "carried_from": None
                        if s.carried_from is None
                        else {"id": s.carried_from.pk, "hrid": s.carried_from.hrid},
                    }
                    for s in steps.get(run.pk, [])
                ],
                "rerun_of": earlier.get(run.rerun_of_id) if run.rerun_of_id else None,
                "reruns": reruns.get(run.pk, []),
                "can": {
                    "cancel": not run.finished and workflows.may_cancel(me, run),
                    "rerun": workflows.may_rerun(me, run),
                },
            }
        )
    return out


def _decision(step: WorkflowStep) -> dict[str, Any] | None:
    if step.decided_by is None or step.decided_at is None:
        return None
    return {
        "approved": step.state != StepState.DECLINED,
        "by": step.decided_by,
        "at": step.decided_at,
        "note": step.decision_note,
    }


def _ref(run: ActionRun | None) -> dict[str, Any] | None:
    return None if run is None else {"id": run.pk, "hrid": run.hrid, "status": run.status}


_FIELDS = (
    "id",
    "hrid",
    "workflow",
    "plugin_version",
    "via",
    "client",
    "reason",
    "params",
    "status",
    "created_at",
    "finished_at",
    "cancel_requested_at",
    "error",
)


def find(me: User, ref: str) -> WorkflowRun:
    found = workflows.visible(me).select_related("project__team", "requested_by", "agent")
    try:
        run = found.filter(pk=UUID(ref)).first()
    except ValueError:
        run = found.filter(hrid=ref.upper()).first()
    if run is None:
        raise Problem(404, "No such workflow run, or you can't read its project.")
    return run


@router.post("/workflows/{name}/runs", response={201: WorkflowRunOut}, summary="Start a workflow")
def start_workflow(
    request: HttpRequest, name: str, payload: WorkflowRunIn, response: HttpResponse
) -> Any:  # noqa: ANN401
    """Start a run of the workflow. Everything it can be is checked first: your role for
    each step's tier, that each step's action can run here, its inputs and its params. Its
    first steps are launched before it answers; each later one once those it takes from
    have succeeded."""
    me = caller(request)
    of = _workflow(name)
    target = project(me, payload.project, permissions.Action.PROJECT_READ)
    inputs = {k: v if isinstance(v, list) else [v] for k, v in payload.inputs.items()}
    try:
        run = workflows.start(
            me,
            of,
            target,
            inputs,
            payload.params,
            reason=payload.reason,
            add_to_project=payload.add_to_project,
        )
    except Invalid as error:
        raise invalid_problem(error) from None
    except services.OutsideProject as error:
        raise outside_problem(error) from None
    response["Location"] = f"/api/v1/workflow-runs/{run.pk}"
    return Status(201, _runs_out(me, [find(me, str(run.pk))])[0])


class WorkflowRunFilters(PageParams):
    project: UUID | None = Field(None, description="Only workflow runs in this project.")
    workflow: str | None = Field(None, description="Only runs of this workflow.")
    status: Literal["running", "succeeded", "failed", "cancelled"] | None = None


@router.get("/workflow-runs", response=Page[WorkflowRunOut], summary="Workflow runs you can read")
def list_workflow_runs(request: HttpRequest, filters: Query[WorkflowRunFilters]) -> dict[str, Any]:
    """Newest first. `GET /runs?workflow=` lists a workflow run's runs."""
    me = caller(request)
    found = workflows.visible(me).select_related("project__team", "requested_by", "agent")
    if filters.project is not None:
        found = found.filter(project=project(me, filters.project, permissions.Action.PROJECT_READ))
    if filters.workflow is not None:
        found = found.filter(workflow=filters.workflow)
    if filters.status is not None:
        found = found.filter(status=filters.status)
    result = page(found, filters, newest_first=True)
    result["items"] = _runs_out(me, result["items"])
    return result


@router.get("/workflow-runs/{ref}", response=WorkflowRunOut, summary="A workflow run")
def get_workflow_run(request: HttpRequest, ref: str) -> dict[str, Any]:
    """By its ID or HRID (WF-000123). Its live updates come from
    `GET /workflow-runs/{ref}/events`: `workflow` when it or a step changes."""
    me = caller(request)
    return _runs_out(me, [find(me, ref)])[0]


@router.post(
    "/workflow-runs/{ref}/cancel", response=WorkflowRunOut, summary="Cancel a workflow run"
)
def cancel_workflow_run(request: HttpRequest, ref: str, payload: CancelIn) -> dict[str, Any]:
    """No further step starts, and its running steps' runs are cancelled, as
    `POST /runs/{ref}/cancel` would. It ends cancelled once they have."""
    me = caller(request)
    workflows.cancel(me, find(me, ref), reason=payload.reason)
    return _runs_out(me, [find(me, ref)])[0]


class ApproveIn(Schema):
    note: str = Field("", max_length=2000, description="What you checked, for the record.")


class DeclineStepIn(Schema):
    note: str = Field(
        min_length=1,
        max_length=2000,
        description="Why, for the record and for whoever started the workflow.",
    )


def _deciding(request: HttpRequest, ref: str) -> tuple[User, WorkflowRun]:
    me = caller(request)
    if permissions.token_limits(me) is not None:
        raise Problem(
            403,
            "Approve or decline workflow steps in Iki’s web app. Tokens can’t, so no agent "
            "can approve a step.",
            code="web_only",
        )
    return me, find(me, ref)


@router.post(
    "/workflow-runs/{ref}/steps/{step}/approve",
    response=WorkflowRunOut,
    summary="Approve a workflow step",
)
def approve_workflow_step(
    request: HttpRequest, ref: str, step: str, payload: ApproveIn
) -> dict[str, Any]:
    """The step starts, as whoever started the workflow. Someone with its approval's role in
    the project may, other than whoever started it, and only in the web app."""
    me, run = _deciding(request, ref)
    try:
        workflows.approve(me, run, step, note=payload.note.strip())
    except LookupError:
        raise Problem(404, f"{run.hrid} has no step `{step}`.") from None
    except PermissionDenied as error:
        raise Problem(403, str(error) or None) from None
    return _runs_out(me, [find(me, ref)])[0]


@router.post(
    "/workflow-runs/{ref}/steps/{step}/decline",
    response=WorkflowRunOut,
    summary="Decline a workflow step",
)
def decline_workflow_step(
    request: HttpRequest, ref: str, step: str, payload: DeclineStepIn
) -> dict[str, Any]:
    """The step doesn't run, and the workflow fails; whoever started it is told why. As
    approving, someone with the role, other than whoever started it, in the web app."""
    me, run = _deciding(request, ref)
    note = payload.note.strip()
    if not note:
        raise Problem(
            422,
            "Say why it's declined.",
            code="validation_failed",
            errors=[{"pointer": "#/note", "detail": "Say why it's declined."}],
        )
    try:
        workflows.decline(me, run, step, note=note)
    except LookupError:
        raise Problem(404, f"{run.hrid} has no step `{step}`.") from None
    except PermissionDenied as error:
        raise Problem(403, str(error) or None) from None
    return _runs_out(me, [find(me, ref)])[0]


class RerunIn(Schema):
    reason: str = Field("", max_length=2000, description="Why, for the record.")


@router.post(
    "/workflow-runs/{ref}/rerun",
    response={201: WorkflowRunOut},
    summary="Re-run a workflow run from its failed steps",
)
def rerun_workflow_run(
    request: HttpRequest, ref: str, payload: RerunIn, response: HttpResponse
) -> Any:  # noqa: ANN401 - a Status
    """A new workflow run, started by you, of the workflow as it was, on the same inputs (as
    they are now) and params, as GitHub re-runs failed jobs. Steps that succeeded are carried
    over with their runs and results; the rest run again, and approvals are asked again. Only
    a workflow run that failed or was cancelled is re-run, by whoever may start it."""
    me = caller(request)
    original = find(me, ref)
    try:
        run = workflows.rerun(me, original, reason=payload.reason.strip())
    except Invalid as error:
        raise invalid_problem(error) from None
    except services.OutsideProject as error:
        raise outside_problem(error) from None
    response["Location"] = f"/api/v1/workflow-runs/{run.pk}"
    return Status(201, _runs_out(me, [find(me, str(run.pk))])[0])
