"""Workflows: starting them, moving them on as their steps' runs end, and cancelling them
(ADR 0079).

A workflow run's steps are ordinary runs, launched by :func:`services.launch` as the person
who started it, as they started it (through the web app, the API or an agent, whose runs
then wait for approval as theirs would), in its project. A step starts once the steps it
takes from or needs have succeeded, with the entities they say: the workflow's inputs, the
entities a step created, or those a step was given.

:func:`advance` moves a workflow run on. It runs when one of its runs ends (a task queued in
the transaction that ended it), when it starts, when it's cancelled, and from the task
runners' housekeeping, so none is left behind. It locks the workflow run, so two never
launch a step twice.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, hrids, registry
from iki_server.entities.values import Invalid
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog, live, services
from iki_server.runs.models import (
    APPROVAL_EXPIRES_AFTER,
    WORKFLOW_HRID_PREFIX,
    WORKFLOW_HRID_SEQUENCE,
    ActionRun,
    EffectKind,
    RunEffect,
    RunInput,
    RunOutput,
    RunStatus,
    StepState,
    WorkflowInput,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)
from iki_spec.model import Action, Condition, Workflow, plugin_to_dict, workflow_from_dict
from iki_spec.workflows import literal

__all__ = [
    "actions",
    "advance",
    "approve",
    "cancel",
    "decline",
    "inputs_of",
    "may_cancel",
    "may_start",
    "recover",
    "spec_of",
    "start",
    "visible",
    "why_not_decide",
    "workflow",
    "workflows",
]

#: The most runs a step run for each entity launches: as many as a batch takes.
MAX_EACH = 1000

#: The most workflow runs one housekeeping pass moves on.
RECOVER_AT_MOST = 500


def workflows() -> dict[str, Workflow]:
    """Every installed workflow, by qualified name, in name order."""
    found = {w.qualified_name: w for p in registry.plugins().values() for w in p.workflows.values()}
    return dict(sorted(found.items()))


def workflow(name: str) -> Workflow | None:
    return workflows().get(name)


def actions(of: Workflow) -> dict[str, Action | None]:
    """The action each step runs, by step ID, as installed now; None for one that isn't."""
    return {i: catalog.action(f"{of.plugin}.{s.action}") for i, s in of.steps.items()}


def visible(user: Any) -> QuerySet[WorkflowRun]:  # noqa: ANN401 - User or AnonymousUser
    """Workflow runs in projects whose data the user can read."""
    return WorkflowRun.objects.filter(project__in=access.readable_projects(user).values("pk"))


def may_start(user: User, of: Workflow, project: Project) -> None:
    """Raises :class:`PermissionDenied` unless the user may run every step's tier in the
    project, and :class:`Refused` if one of its actions can't run here."""
    for step_id, action in actions(of).items():
        if action is None:
            raise Refused(
                f"Step `{step_id}` runs `{of.steps[step_id].action}`, which isn't installed."
            )
        services.may_launch(user, action, project)


def may_cancel(user: User, run: WorkflowRun) -> bool:
    """Whoever may launch each of its steps' tiers in its project may cancel it."""
    tiers = {s.get("tier") for s in run.spec.get("tiers", {}).values()} - {None}
    return all(
        permissions.can(user, services.TIER_PERMISSIONS[t], run.project) for t in tiers
    ) and permissions.can(user, permissions.Action.PROJECT_READ, run.project)


def _updated_inputs(of: Workflow, steps: Mapping[str, Action | None]) -> set[str]:
    """The workflow's inputs some step changes: they must be ones the starter can edit."""
    out: set[str] = set()
    for step_id, step in of.steps.items():
        action = steps.get(step_id)
        if action is None:
            continue
        for update in action.updates:
            root = _root(of, step.inputs.get(update.input, ""))
            if root is not None:
                out.add(root)
    return out


def _root(of: Workflow, ref: str) -> str | None:
    """The workflow input `ref` comes from, following what steps were given."""
    seen: set[str] = set()
    while ref.startswith("$steps.") and ref not in seen:
        seen.add(ref)
        parts = ref.split(".", 3)
        step = of.steps.get(parts[1])
        if step is None or parts[2] != "inputs":
            return None
        ref = step.inputs.get(parts[3], "")
    return ref.removeprefix("$inputs.") if ref.startswith("$inputs.") else None


def start(
    user: User,
    of: Workflow,
    project: Project,
    inputs: Mapping[str, Sequence[UUID]],
    params: Mapping[str, Any],
    *,
    reason: str = "",
    add_to_project: bool = False,
) -> WorkflowRun:
    """Start a run of `of` in `project`, or raise why it can't start: as
    :func:`services.launch` does, for the workflow's inputs and params, and for each step's
    tier and action. Its first steps are launched before it returns."""
    may_start(user, of, project)
    steps = actions(of)
    errors: dict[str, list[str]] = {}
    entities = services.check_inputs(user, of, inputs, errors, updated=_updated_inputs(of, steps))
    values = services.check_params(user, of, project, params, errors)
    if errors:
        raise Invalid(errors)
    outside = services.outside_project(user, project, entities, errors)
    if errors:
        raise Invalid(errors)
    if outside and not add_to_project:
        raise services.OutsideProject(project, outside)
    order = of.order()
    assert order is not None  # noqa: S101 - the plugin's build refuses cycles
    with transaction.atomic():
        services.link_into_project(user, project, outside)
        run = WorkflowRun.objects.create(
            hrid=_next_hrid(),
            workflow=of.qualified_name,
            plugin_version=registry.plugins()[of.plugin].version,
            plugin_sha=services.plugin_sha(of.plugin),
            spec={
                "workflow": _as_json(of),
                "tiers": {
                    i: {"action": a.qualified_name, "tier": a.tier}
                    for i, a in steps.items()
                    if a is not None
                },
            },
            project=project,
            requested_by=user,
            via=audit.current_via(),
            client=audit.current_oauth_client(),
            agent=audit.current_agent(),
            reason=reason,
            params=values,
        )
        WorkflowInput.objects.bulk_create(
            WorkflowInput(
                workflow=run, name=name, position=i, entity=entity, revision=entity.revision
            )
            for name, chosen in entities.items()
            for i, entity in enumerate(chosen)
        )
        WorkflowStep.objects.bulk_create(
            WorkflowStep(workflow=run, step=step_id, position=i) for i, step_id in enumerate(order)
        )
        audit.record(
            "workflow.started",
            actor=user,
            target=run,
            project=project,
            data={
                "workflow": of.qualified_name,
                "inputs": sum(len(e) for e in entities.values()),
                "steps": len(order),
            },
        )
        _advance(run)
    return run


def _as_json(of: Workflow) -> dict[str, Any]:
    """The workflow as JSON, as :func:`workflow_from_dict` reads it back."""
    plugin = dataclasses.replace(registry.plugins()[of.plugin], workflows={of.name: of})
    found: dict[str, Any] = plugin_to_dict(plugin)["workflows"][of.name]
    return found


def spec_of(run: WorkflowRun) -> Workflow:
    """The workflow as it was installed when `run` started."""
    return workflow_from_dict(run.spec["workflow"])


def cancel(user: User, run: WorkflowRun, *, reason: str = "") -> WorkflowRun:
    """Cancel a workflow run: no further step starts, and its running steps' runs are
    cancelled as :func:`services.cancel` would. Whoever may launch its steps may."""
    if not may_cancel(user, run):
        raise PermissionDenied
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        if run.finished:
            raise Refused(f"{run.hrid} has already finished ({run.get_status_display().lower()}).")
        if run.cancel_requested_at is not None:
            return run
        run.cancel_requested_at = timezone.now()
        run.cancel_requested_by = user
        run.save(update_fields=["cancel_requested_at", "cancel_requested_by"])
        audit.record(
            "workflow.cancelled",
            actor=user,
            target=run,
            project=run.project,
            data={"reason": reason} if reason else {},
        )
        for each in _unfinished_runs(run):
            try:
                services.cancel(user, each, reason=reason or f"{run.hrid} was cancelled.")
            except (Refused, PermissionDenied):
                continue  # it finished meanwhile, or its own cancel failed: it ends anyway
        _advance(run)
    return run


def _unfinished_runs(run: WorkflowRun) -> list[ActionRun]:
    return list(
        ActionRun.objects.filter(workflow=run, finished_at__isnull=True)
        .select_related("project")
        .order_by("id")
    )


def advance(workflow_id: str | UUID, *, wait: bool = True) -> str:
    """Move the workflow run on: record the steps whose runs ended, launch those that can
    start, and end it once nothing's left to do. Returns its status. Without `wait`, a run
    another transaction is moving on is left to it."""
    with transaction.atomic():
        found = WorkflowRun.objects.select_for_update(skip_locked=not wait, of=("self",))
        found = found.filter(pk=workflow_id)
        run = found.select_related("project", "requested_by", "agent").first()
        if run is None:
            return "busy" if not wait else "missing"
        if not run.finished:
            _advance(run)
        return str(run.status)


def recover() -> int:
    """Moves on every running workflow run no other transaction is: in case a task that
    should have was lost. Returns how many ended."""
    ended = 0
    ids = WorkflowRun.objects.filter(status=WorkflowStatus.RUNNING).order_by("id")
    for pk in ids.values_list("pk", flat=True)[:RECOVER_AT_MOST]:
        if advance(pk, wait=False) not in ("running", "busy"):
            ended += 1
    return ended


# --- Moving on ---------------------------------------------------------------------------------


def _advance(run: WorkflowRun) -> None:
    """Call inside a transaction, with `run` locked."""
    spec = spec_of(run)
    steps = {s.step: s for s in WorkflowStep.objects.filter(workflow=run).order_by("position")}
    runs = _step_runs(run)
    now = timezone.now()
    changed = False
    for step in steps.values():
        theirs = runs.get(step.step, [])
        if step.state == StepState.RUNNING and theirs and all(r.finished for r in theirs):
            _ended(run, step, theirs, now)
            changed = True
        elif _expired(step, now):
            _expire(run, spec, step, now)
            changed = True
    while not _stopping(run, steps):
        moved = False
        for step in steps.values():
            if step.state != StepState.WAITING or _stopping(run, steps):
                continue
            after = spec.steps[step.step].after()
            skipped = sorted(o for o in after if steps[o].state == StepState.SKIPPED)
            if skipped:
                _skip(step, "skipped", f"It didn't start: step `{skipped[0]}` was skipped.", now)
            elif all(steps[o].state == StepState.SUCCEEDED for o in after):
                unmet = _unmet(run, spec.steps[step.step].when, runs)
                if unmet is not None:
                    _skip(step, *unmet, now)
                elif spec.steps[step.step].approval is not None and step.decided_by_id is None:
                    _ask(run, spec, step, now)
                else:
                    launched = _launch(run, spec, step, runs)
                    if launched is not None:
                        runs[step.step] = launched
                        if all(r.finished for r in launched):
                            _ended(run, step, launched, now)  # it reused identical runs' results
            else:
                continue
            moved = changed = True
        if not moved:
            break
    if _stopping(run, steps):
        for step in steps.values():
            if step.state in (StepState.WAITING, StepState.AWAITING_APPROVAL):
                step.state = StepState.SKIPPED
                step.finished_at = now
                step.error = {
                    "code": "skipped",
                    "message": "It didn't start: the workflow was cancelled."
                    if run.cancel_requested_at
                    else "It didn't start: an earlier step didn't succeed.",
                }
                step.save(update_fields=["state", "finished_at", "error"])
                changed = True
    if not any(s.state in _UNFINISHED for s in steps.values()):
        _end(run, steps, now)
        changed = True
    if changed:
        transaction.on_commit(partial(live.workflow_changed, run.pk))


def _skip(step: WorkflowStep, code: str, message: str, now: Any) -> None:  # noqa: ANN401 - a datetime
    """Skip a step, or fail it if what decides whether it runs couldn't be read."""
    step.state = StepState.FAILED if code == "condition_unknown" else StepState.SKIPPED
    step.finished_at = now
    step.error = {"code": code, "message": message}
    step.save(update_fields=["state", "finished_at", "error"])


#: The states of a step its workflow waits on, once nothing more will start.
_UNFINISHED = (StepState.RUNNING, StepState.AWAITING_APPROVAL)


def _stopping(run: WorkflowRun, steps: Mapping[str, WorkflowStep]) -> bool:
    return run.cancel_requested_at is not None or any(
        s.state in (StepState.FAILED, StepState.DECLINED) for s in steps.values()
    )


def _step_runs(run: WorkflowRun) -> dict[str, list[ActionRun]]:
    """Each step's runs, in the order they were launched: one, or one for each entity."""
    out: dict[str, list[ActionRun]] = {}
    for each in ActionRun.objects.filter(workflow=run).order_by("id"):
        out.setdefault(each.workflow_step, []).append(each)
    return out


def _ended(
    run: WorkflowRun,
    step: WorkflowStep,
    launched: Sequence[ActionRun],
    now: Any,  # noqa: ANN401 - a datetime
) -> None:
    """Every run of the step has ended: it succeeded if each of them did."""
    unsuccessful = [r for r in launched if r.status != RunStatus.SUCCEEDED]
    if not unsuccessful:
        step.state = StepState.SUCCEEDED
        step.error = None
    elif run.cancel_requested_at is not None and all(
        r.status == RunStatus.CANCELLED for r in unsuccessful
    ):
        step.state = StepState.CANCELLED
        step.error = None
    else:
        step.state = StepState.FAILED
        # The first that failed, rather than one cancelled because of it.
        first = next((r for r in unsuccessful if r.status != RunStatus.CANCELLED), unsuccessful[0])
        message = (first.error or {}).get("message") or ""
        which = (
            f"Its run, {first.hrid},"
            if len(launched) == 1
            else f"{len(unsuccessful)} of its {len(launched)} runs didn't succeed. {first.hrid}"
        )
        step.error = {
            "code": f"run_{first.status}",
            "message": f"{which} {_ended_as(first)}." + (f" {message}" if message else ""),
        }
    step.finished_at = max((r.finished_at for r in launched if r.finished_at), default=now)
    step.save(update_fields=["state", "finished_at", "error"])


def _ended_as(launched: ActionRun) -> str:
    return {
        RunStatus.FAILED: "failed",
        RunStatus.CANCELLED: "was cancelled",
        RunStatus.TIMED_OUT: "timed out",
        RunStatus.REJECTED: "was rejected: its results didn't fit",
        RunStatus.DECLINED: "wasn't approved",
    }.get(RunStatus(launched.status), str(launched.status))


def _end(run: WorkflowRun, steps: Mapping[str, WorkflowStep], now: Any) -> None:  # noqa: ANN401 - a datetime
    failed = next(
        (s for s in steps.values() if s.state in (StepState.FAILED, StepState.DECLINED)), None
    )
    if failed is not None:
        declined = failed.state == StepState.DECLINED
        run.status = WorkflowStatus.FAILED
        run.error = {
            "code": "step_declined" if declined else "step_failed",
            "step": failed.step,
            "message": f"Step `{failed.step}` "
            + ("wasn't approved. " if declined else "didn't succeed. ")
            + ((failed.error or {}).get("message") or ""),
        }
    elif run.cancel_requested_at is not None:
        run.status = WorkflowStatus.CANCELLED
    else:
        run.status = WorkflowStatus.SUCCEEDED
    run.finished_at = now
    run.save(update_fields=["status", "finished_at", "error"])


def _launch(
    run: WorkflowRun, spec: Workflow, step: WorkflowStep, runs: Mapping[str, list[ActionRun]]
) -> list[ActionRun] | None:
    """Launch the step's run, or a run for each entity, as the person who started the
    workflow, as they started it. On failure the step fails, and None is returned."""
    declared = spec.steps[step.step]
    action = catalog.action(f"{spec.plugin}.{declared.action}")
    now = timezone.now()
    step.started_at = now
    try:
        if action is None:
            raise Refused(f"`{spec.plugin}.{declared.action}` is no longer installed.")
        inputs = _inputs(run, spec, declared.inputs, action, runs)
        params = _params(run, declared.params)
        with audit.using(_context(run)):
            if declared.each is None:
                launched = [
                    services.launch(
                        run.requested_by,
                        action,
                        run.project,
                        inputs,
                        params,
                        reason=_reason(run, step),
                        workflow=run,
                        workflow_step=step.step,
                    )
                ]
            else:
                chosen = inputs.pop(declared.each)
                if len(chosen) > MAX_EACH:
                    raise Refused(
                        f"it would run once for each of {len(chosen):,} entities, and a step "
                        f"runs for at most {MAX_EACH:,}."
                    )
                launched = services.launch_each(
                    run.requested_by,
                    action,
                    run.project,
                    declared.each,
                    chosen,
                    inputs,
                    params,
                    reason=_reason(run, step),
                    workflow=run,
                    workflow_step=step.step,
                )
    except (Refused, PermissionDenied, Invalid, services.OutsideProject, _NothingToTake) as error:
        step.state = StepState.FAILED
        step.finished_at = now
        step.error = {"code": "launch_failed", "message": _why(error)}
        step.save(update_fields=["state", "started_at", "finished_at", "error"])
        return None
    step.state = StepState.RUNNING
    step.save(update_fields=["state", "started_at"])
    return launched


# --- Approval ----------------------------------------------------------------------------------


def _ask(run: WorkflowRun, spec: Workflow, step: WorkflowStep, now: Any) -> None:  # noqa: ANN401 - a datetime
    """The step's turn has come: it waits for someone with its approval's role in the
    project, other than whoever started the workflow, who are told."""
    approval = spec.steps[step.step].approval
    assert approval is not None  # noqa: S101 - asked only for steps that need it
    step.state = StepState.AWAITING_APPROVAL
    step.approval_requested_at = now
    step.approval_expires_at = now + APPROVAL_EXPIRES_AFTER
    step.save(update_fields=["state", "approval_requested_at", "approval_expires_at"])
    audit.record(
        "workflow.approval_requested",
        actor=None,
        target=run,
        project=run.project,
        data={"step": step.step, "role": approval.role},
    )
    approvers = permissions.members_with(run.project, _role(approval.role))
    label = spec.steps[step.step].label
    notify(
        approvers.exclude(pk=run.requested_by_id),
        kinds.WORKFLOW_APPROVAL_REQUESTED,
        title=f"{run.hrid} ({spec.label}) waits for approval to {label.lower()}",
        body=(approval.instructions + "\n\n" if approval.instructions else "")
        + f"Started by {run.requested_by.name}. Approve or decline it in Iki; it waits "
        f"{APPROVAL_EXPIRES_AFTER.days} days.",
        path=f"/workflow-runs/{run.hrid}/",
        project=run.project,
    )


def _role(name: str) -> ProjectRole:
    return ProjectRole[name.upper()]


def _expired(step: WorkflowStep, now: Any) -> bool:  # noqa: ANN401 - a datetime
    return (
        step.state == StepState.AWAITING_APPROVAL
        and step.approval_expires_at is not None
        and step.approval_expires_at <= now
    )


def _expire(run: WorkflowRun, spec: Workflow, step: WorkflowStep, now: Any) -> None:  # noqa: ANN401 - a datetime
    step.state = StepState.DECLINED
    step.finished_at = now
    step.error = {
        "code": "approval_expired",
        "message": f"Nobody approved it within {APPROVAL_EXPIRES_AFTER.days} days.",
    }
    step.save(update_fields=["state", "finished_at", "error"])
    audit.record(
        "workflow.approval_expired",
        actor=None,
        target=run,
        project=run.project,
        data={"step": step.step},
    )
    _tell_declined(run, spec, step, None)


def _tell_declined(run: WorkflowRun, spec: Workflow, step: WorkflowStep, by: User | None) -> None:
    label = spec.steps[step.step].label
    notify(
        [run.requested_by],
        kinds.WORKFLOW_STEP_DECLINED,
        title=f"{run.hrid} ({spec.label}) stopped: “{label}” wasn't approved",
        body=(step.error or {}).get("message") or "",
        path=f"/workflow-runs/{run.hrid}/",
        project=run.project,
        actor=by,
    )


def why_not_decide(
    user: User, run: WorkflowRun, step: WorkflowStep, spec: Workflow | None = None
) -> str | None:
    """Why `user` may not approve or decline the step, or None if they may: someone with its
    approval's role in the project, other than whoever started the workflow, in the web app
    (no token, so no agent approves a step)."""
    approval = (spec or spec_of(run)).steps[step.step].approval
    if approval is None or step.state != StepState.AWAITING_APPROVAL:
        return "It isn't waiting for approval."
    if permissions.token_limits(user) is not None:
        return "Steps are approved in the web app, not with a token."
    if user.pk == run.requested_by_id:
        return "Someone else approves it: whoever started a workflow can't approve its steps."
    role = permissions.project_role(user, run.project)
    if role is None or role < _role(approval.role):
        return f"It needs someone who is {_role(approval.role).label} or above in the project."
    return None


def approve(user: User, run: WorkflowRun, step_id: str, *, note: str = "") -> WorkflowRun:
    """Approve a step waiting for it: it starts, as whoever started the workflow."""
    return _decide(user, run, step_id, note=note, approved=True)


def decline(user: User, run: WorkflowRun, step_id: str, *, note: str) -> WorkflowRun:
    """Decline a step waiting for approval: it doesn't run, and the workflow fails."""
    return _decide(user, run, step_id, note=note, approved=False)


def _decide(
    user: User, run: WorkflowRun, step_id: str, *, note: str, approved: bool
) -> WorkflowRun:
    advance(run.pk)  # first, so one that's due expires rather than be decided
    with transaction.atomic():
        found = WorkflowRun.objects.select_for_update(of=("self",)).select_related(
            "project", "requested_by", "agent"
        )
        run = found.get(pk=run.pk)
        step = WorkflowStep.objects.filter(workflow=run, step=step_id).first()
        if step is None:
            raise LookupError(step_id)
        if step.state != StepState.AWAITING_APPROVAL:
            raise Refused(f"Step `{step_id}` of {run.hrid} isn't waiting for approval.")
        why = why_not_decide(user, run, step)
        if why is not None:
            if user.pk == run.requested_by_id:
                raise Refused(why)  # they may otherwise: say why not
            raise PermissionDenied(why)
        now = timezone.now()
        step.decided_by = user
        step.decided_at = now
        step.decision_note = note
        if approved:
            step.state = StepState.WAITING  # its turn has come: it starts now
            step.save(update_fields=["state", "decided_by", "decided_at", "decision_note"])
        else:
            step.state = StepState.DECLINED
            step.finished_at = now
            step.error = {
                "code": "declined",
                "message": f"{user.name} declined it" + (f": {note}" if note else "."),
            }
            step.save(
                update_fields=[
                    "state",
                    "finished_at",
                    "error",
                    "decided_by",
                    "decided_at",
                    "decision_note",
                ]
            )
        audit.record(
            "workflow.step_approved" if approved else "workflow.step_declined",
            actor=user,
            target=run,
            project=run.project,
            data={"step": step_id, **({"note": note} if note else {})},
        )
        if not approved:
            _tell_declined(run, spec_of(run), step, user)
        _advance(run)
    return run


_PHRASES = {
    "is": "be {}",
    "is_not": "not be {}",
    "in": "be one of {}",
    "at_least": "be at least {}",
    "at_most": "be at most {}",
    "above": "be above {}",
    "below": "be below {}",
}


def _unmet(
    run: WorkflowRun, conditions: Sequence[Condition], runs: Mapping[str, list[ActionRun]]
) -> tuple[str, str] | None:
    """None if every one of `conditions` holds. Otherwise a code and why: `condition_false`
    for the first that doesn't, or `condition_unknown` for a metric its run didn't report,
    which is never taken as false silently. A param left unset holds nothing."""
    for condition in conditions:
        value = _value(run, condition.value, runs)
        if value is _MISSING:
            step = condition.value.split(".")[1]
            return (
                "condition_unknown",
                f"Its condition couldn't be decided: step `{step}`'s run didn't report "
                f"`{condition.value.split('.')[-1]}`.",
            )
        operand = (
            [_operand(run, o) for o in condition.operand]
            if condition.operator == "in"
            else _operand(run, condition.operand)
        )
        if not _holds(condition.operator, value, operand):
            shown = "not set" if value is None else _show(value)
            return (
                "condition_false",
                f"Its condition didn't hold: `{condition.value}` is {shown}, and it must "
                + _PHRASES[condition.operator].format(
                    "unset" if operand is None else _show(operand)
                )
                + ".",
            )
    return None


class _Missing:
    pass


_MISSING = _Missing()


def _value(run: WorkflowRun, ref: str, runs: Mapping[str, list[ActionRun]]) -> Any:  # noqa: ANN401 - a param's or a metric's value
    """A param's value (None if unset), or a metric's from the step's run, or from the run
    whose results it reused; :data:`_MISSING` if it didn't report it."""
    parts = ref.split(".")
    if parts[0] == "$params":
        return run.params.get(parts[1])
    sources = runs.get(parts[1], [])
    if len(sources) != 1:  # a step whose metrics are read runs once
        return _MISSING
    origin = sources[0].reused_from_id or sources[0].pk
    document = RunOutput.objects.filter(run_id=origin).values_list("document", flat=True).first()
    metrics = (document or {}).get("metrics") or {}
    return metrics.get(parts[3], _MISSING)


def _operand(run: WorkflowRun, given: Any) -> Any:  # noqa: ANN401 - JSON-shaped
    if isinstance(given, str) and given.startswith("$params."):
        return run.params.get(given.removeprefix("$params."))
    return literal(given)


def _holds(operator: str, value: Any, operand: Any) -> bool:  # noqa: ANN401 - JSON-shaped
    if value is None or operand is None:
        return False  # an unset param holds nothing, `is_not` included
    if isinstance(value, bool) != isinstance(operand, bool) and operator != "in":
        return operator == "is_not"
    if operator == "is":
        return bool(value == operand)
    if operator == "is_not":
        return bool(value != operand)
    if operator == "in":
        return value in operand
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if isinstance(operand, bool) or not isinstance(operand, int | float):
        return False
    return {
        "at_least": value >= operand,
        "at_most": value <= operand,
        "above": value > operand,
        "below": value < operand,
    }[operator]


def _show(value: Any) -> str:  # noqa: ANN401 - JSON-shaped
    if isinstance(value, list):
        return ", ".join(_show(v) for v in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value) if isinstance(value, int | bool) else f"“{value}”"


def _reason(run: WorkflowRun, step: WorkflowStep) -> str:
    return f"Step `{step.step}` of {run.hrid}" + (f": {run.reason}" if run.reason else "")


class _NothingToTake(Exception):  # noqa: N818 - reads as "there's nothing to take"
    pass


def _why(error: Exception) -> str:
    if isinstance(error, PermissionDenied):
        return (
            "Its run couldn't start: whoever started the workflow can no longer run its "
            "action in the project."
        )
    if isinstance(error, Invalid):
        problems = "; ".join(
            f"{key.split('/', 1)[-1]}: {' '.join(said)}" for key, said in error.errors.items()
        )
        return f"Its run couldn't start: {problems}"
    return f"Its run couldn't start: {error}"


def _inputs(
    run: WorkflowRun,
    spec: Workflow,
    refs: Mapping[str, str],
    action: Action,
    runs: Mapping[str, list[ActionRun]],
) -> dict[str, list[UUID]]:
    out: dict[str, list[UUID]] = {}
    for name, ref in refs.items():
        ids = _resolve(run, ref, runs)
        put = action.inputs.get(name)
        if not ids:
            if put is not None and put.optional:
                continue
            raise _NothingToTake(_nothing(spec, ref))
        out[name] = ids
    return out


def _resolve(run: WorkflowRun, ref: str, runs: Mapping[str, list[ActionRun]]) -> list[UUID]:
    """The entities `ref` names, each once, in order: those a step's runs made or were
    given, in the order the runs were launched."""
    parts = ref.split(".", 3)
    if parts[0] == "$inputs":
        return list(
            WorkflowInput.objects.filter(workflow=run, name=parts[1])
            .order_by("position")
            .values_list("entity_id", flat=True)
        )
    sources = runs.get(parts[1], [])
    if not sources:
        return []
    if parts[2] == "created":
        made = (
            RunEffect.objects.filter(
                run__in=sources, kind__in=[EffectKind.CREATED, EffectKind.REUSED]
            )
            .select_related("entity")
            .order_by("run_id", "id")
        )
        found = [e.entity_id for e in made if services.is_a(e.entity.type, parts[3])]
    else:
        found = list(
            RunInput.objects.filter(run__in=sources, name=parts[3])
            .order_by("run_id", "position")
            .values_list("entity_id", flat=True)
        )
    return list(dict.fromkeys(found))


def _nothing(spec: Workflow, ref: str) -> str:
    parts = ref.split(".", 3)
    if parts[0] == "$inputs":
        return f"the workflow wasn't given `{parts[1]}`."
    if parts[2] == "created":
        return f"step `{parts[1]}` created no `{parts[3]}` for it to take."
    return f"step `{parts[1]}` wasn't given `{parts[3]}`."


def _params(run: WorkflowRun, given: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in given.items():
        if isinstance(value, str) and value.startswith("$params."):
            chosen = run.params.get(value.removeprefix("$params."))
            if chosen is not None:
                out[name] = chosen
        else:
            out[name] = literal(value)
    return out


def _context(run: WorkflowRun) -> audit.Context:
    """The audit context its steps are launched in: as the workflow was started."""
    agent = run.agent
    if agent is None:
        return audit.Context(via=run.via, oauth_client=run.client)
    info = audit.AgentInfo(
        client_id=agent.client_id,
        client_name=agent.client_name,
        grant=agent.grant,
        session=agent.session,
        software=agent.software,
        model=agent.model,
    )
    return audit.Context(
        via=run.via, oauth_client=run.client, agent=info, stored={agent.key: agent}
    )


def _next_hrid() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s::regclass)", [WORKFLOW_HRID_SEQUENCE])
        (number,) = cursor.fetchone()
    return hrids.format_hrid(WORKFLOW_HRID_PREFIX, 6, number)


def inputs_of(runs: Sequence[WorkflowRun]) -> list[WorkflowInput]:
    """The entities `runs` were given, by run, input and position."""
    return list(
        WorkflowInput.objects.filter(workflow__in=runs)
        .select_related("entity")
        .order_by("workflow", "name", "position")
    )
