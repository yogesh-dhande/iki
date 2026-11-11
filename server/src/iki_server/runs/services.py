"""Launching and cancelling runs (ADR 0042).

:func:`launch` checks everything a run needs before it's queued, so a worker
never starts work that can't be committed: the caller may run the action's
tier in the project, the action can run on this deployment, every input is
an entity they can read, of the right type and not archived, the entities it
updates are ones they can edit, and every param fits. It records the
revision of each input, so the run's provenance says what it read.

A run an agent asks for through MCP, on a tier that needs approval, waits for
the person it acts for to approve it (:mod:`iki_server.runs.approvals`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, hrids, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action as Permission
from iki_server.org.services import Refused
from iki_server.runs import approvals, catalog, live, queue, quotas
from iki_server.runs.models import (
    HRID_PREFIX,
    HRID_SEQUENCE,
    ActionRun,
    Job,
    JobState,
    RunApproval,
    RunInput,
    RunStatus,
)
from iki_spec.model import Action
from iki_spec.validate import value_problems

__all__ = ["TIER_PERMISSIONS", "cancel", "launch", "visible_runs"]

#: The project permission each tier needs (Editor for client and backend, Runner above).
TIER_PERMISSIONS = {
    "client": Permission.RUN_CLIENT,
    "backend": Permission.RUN_BACKEND,
    "container": Permission.RUN_CONTAINER,
    "vm": Permission.RUN_VM,
}


def visible_runs(user: Any) -> QuerySet[ActionRun]:  # noqa: ANN401 - User or AnonymousUser
    """Runs in projects whose data the user can read."""
    return ActionRun.objects.filter(project__in=access.readable_projects(user).values("pk"))


def launch(
    user: User,
    action: Action,
    project: Project,
    inputs: Mapping[str, Sequence[UUID]],
    params: Mapping[str, Any],
    *,
    reason: str = "",
) -> ActionRun:
    """Queue a run of `action` in `project`, or raise why it can't run.

    Raises :class:`PermissionDenied` if the caller can't run the action's tier in the project,
    :class:`Refused` if it can't run here now, and :class:`Invalid` for inputs and params.
    """
    _may_launch(user, action, project)
    errors: dict[str, list[str]] = {}
    entities = _inputs(user, action, inputs, errors)
    values = _params(action, params, errors)
    if errors:
        raise Invalid(errors)
    with transaction.atomic():
        approval = approvals.ask(user, action, project)
        run = _queue(user, action, project, entities, values, reason, approval)
        if approval is not None:
            approvals.announce(approval, run, 1, action.label or action.qualified_name)
        return run


def launch_each(
    user: User,
    action: Action,
    project: Project,
    each: str,
    entity_ids: Sequence[UUID],
    inputs: Mapping[str, Sequence[UUID]],
    params: Mapping[str, Any],
    *,
    reason: str = "",
) -> list[ActionRun]:
    """Queue one run of `action` for each of `entity_ids` as its `each` input, the others the
    same for all, or none of them: raises as :func:`launch` does, with problems with the
    entities under ``entities``."""
    _may_launch(user, action, project)
    errors: dict[str, list[str]] = {}
    put = action.inputs.get(each)
    if put is None:
        errors["each"] = [f"`{action.qualified_name}` has no input `{each}`."]
    elif put.cardinality != "one":
        errors["each"] = ["This input takes several entities: give them to one run instead."]
    elif each in inputs:
        errors["each"] = [f"`{each}` is given in `inputs` too."]
    elif len(set(entity_ids)) != len(entity_ids):
        errors["entities"] = ["Each entity can be given once."]
    if errors:
        raise Invalid(errors)
    entities = _inputs(user, action, {**inputs, each: entity_ids}, errors, each=each)
    values = _params(action, params, errors)
    if f"inputs/{each}" in errors:
        errors["entities"] = errors.pop(f"inputs/{each}")
    if errors:
        raise Invalid(errors)
    chosen = entities.pop(each)
    with transaction.atomic():
        # One request, one approval: a batch is approved or declined together.
        approval = approvals.ask(user, action, project, runs=len(chosen))
        runs = [
            _queue(user, action, project, {**entities, each: [entity]}, values, reason, approval)
            for entity in chosen
        ]
        if approval is not None and runs:
            approvals.announce(approval, runs[0], len(runs), action.label or action.qualified_name)
        return runs


def input_taking(action: Action, types: Sequence[str]) -> str | None:
    """The first of `action`'s inputs that takes entities of every one of `types`."""
    for name, put in action.inputs.items():
        if all(is_a(t, put.type) for t in types):
            return name
    return None


def _may_launch(user: User, action: Action, project: Project) -> None:
    if not permissions.can(user, TIER_PERMISSIONS[action.tier], project):
        raise PermissionDenied
    if project.archived_at is not None:
        raise Refused(f"{project.name} is archived, so nothing new can run in it.")
    available = catalog.availability(action)
    if not available.available:
        raise Refused(available.reason or "This action can't run on this deployment.")


def _queue(
    user: User,
    action: Action,
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    values: dict[str, Any],
    reason: str,
    approval: RunApproval | None = None,
) -> ActionRun:
    """Queues the run, or holds it for `approval`. Call inside a transaction, with its
    inputs and params checked."""
    environment = catalog.environment(action)
    if approval is None:
        quotas.check_queued(project, action.tier)
    run = ActionRun.objects.create(
        status=RunStatus.AWAITING_APPROVAL if approval else RunStatus.QUEUED,
        approval=approval,
        client=audit.current_oauth_client(),
        hrid=_next_hrid(),
        action=action.qualified_name,
        plugin_version=registry.plugins()[action.plugin].version,
        plugin_sha=plugin_sha(action.plugin),
        spec=asdict(action),
        project=project,
        requested_by=user,
        via=audit.current_via(),
        reason=reason,
        tier=action.tier,
        environment=action.environment,
        params=values,
        timeout=action.timeout,
        max_attempts=1 + action.retry_max,
        retry_on=list(action.retry_on),
    )
    RunInput.objects.bulk_create(
        RunInput(run=run, name=name, position=index, entity=entity, revision=entity.revision)
        for name, chosen in entities.items()
        for index, entity in enumerate(chosen)
    )
    Job.objects.create(
        run=run,
        project=project,
        tier=action.tier,
        environment=f"{action.plugin}.{action.environment}",
        resources=asdict(environment.resources.at_least(action.resources)),
        owner=user if action.tier == "client" else None,
        state=JobState.AWAITING_APPROVAL if approval else JobState.QUEUED,
    )
    if approval is None:
        queue.notify(action.tier)
    audit.record(
        "run.launched",
        actor=user,
        target=run,
        project=project,
        data={
            "action": action.qualified_name,
            "tier": action.tier,
            "inputs": sum(len(e) for e in entities.values()),
            **({"approval": str(approval.pk)} if approval else {}),
        },
    )
    return run


def _inputs(
    user: User,
    action: Action,
    given: Mapping[str, Sequence[UUID]],
    errors: dict[str, list[str]],
    *,
    each: str | None = None,
) -> dict[str, list[Entity]]:
    """The entities for each input, checked; problems go in `errors`. The `each` input is
    one entity per run: it may be given several."""
    for name in sorted(given.keys() - action.inputs.keys()):
        errors[f"inputs/{name}"] = [f"`{action.qualified_name}` has no input `{name}`."]
    wanted = {i for name in action.inputs for i in given.get(name, ())}
    found = {e.pk: e for e in access.visible(user).filter(pk__in=wanted)}
    updated = {u.input for u in action.updates}
    out: dict[str, list[Entity]] = {}
    for name, put in action.inputs.items():
        key = f"inputs/{name}"
        ids = list(given.get(name, ()))
        if not ids:
            if not put.optional:
                errors[key] = ["Choose an entity for this input."]
            continue
        if put.cardinality == "one" and len(ids) > 1 and name != each:
            errors[key] = ["This input takes one entity."]
            continue
        if put.max is not None and len(ids) > put.max:
            errors[key] = [f"This input takes at most {put.max} entities."]
            continue
        if len(set(ids)) != len(ids):
            errors[key] = ["Each entity can be given once."]
            continue
        chosen: list[Entity] = []
        problems: list[str] = []
        for entity_id in ids:
            entity = found.get(entity_id)
            if entity is None:
                problems.append(f"No such entity, or you can't read it: {entity_id}.")
                continue
            if entity.archived_at is not None:
                problems.append(f"{entity.hrid} is archived.")
            elif not is_a(entity.type, put.type):
                problems.append(f"{entity.hrid} is a {entity.type}, not a {put.type}.")
            elif name in updated and not access.can_edit(user, entity):
                problems.append(
                    f"This action changes {entity.hrid}, and you can't edit it in its project."
                )
            chosen.append(entity)
        if problems:
            errors[key] = problems
        else:
            out[name] = chosen
    return out


def is_a(entity_type: str, wanted: str) -> bool:
    if wanted in (entity_type, "core.Entity"):
        return True
    own = registry.types().get(entity_type)
    return own is not None and any(t.qualified_name == wanted for t in registry.ancestors(own))


def _params(
    action: Action, given: Mapping[str, Any], errors: dict[str, list[str]]
) -> dict[str, Any]:
    for name in sorted(given.keys() - action.params.keys()):
        errors[f"params/{name}"] = [f"`{action.qualified_name}` has no param `{name}`."]
    values: dict[str, Any] = {}
    for name, f in action.params.items():
        key = f"params/{name}"
        if name in given and given[name] is not None:
            problems = value_problems(f, given[name])
            if problems:
                errors[key] = [f"The value {p}" for p in problems]
                continue
            values[name] = given[name]
        elif f.has_default:
            values[name] = f.default
        elif f.required:
            errors[key] = ["Choose a value for this param."]
    return values


def _next_hrid() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s::regclass)", [HRID_SEQUENCE])
        (number,) = cursor.fetchone()
    return hrids.format_hrid(HRID_PREFIX, 6, number)


def plugin_sha(plugin: str) -> str:
    record = registry.installed() or {}
    for entry in record.get("plugins", ()):
        if entry.get("name") == plugin:
            return str(entry.get("sha") or "")
    return ""


def cancel(user: User, run: ActionRun, *, reason: str = "") -> ActionRun:
    """Cancel a run: at once if no worker has it yet, or by asking its worker to stop.

    Whoever may launch the run's tier in its project may cancel it.
    """
    if not permissions.can(user, TIER_PERMISSIONS[run.tier], run.project):
        raise PermissionDenied
    with transaction.atomic():
        # Jobs before their run, as the queue locks them, so the two never deadlock.
        job = Job.objects.select_for_update().filter(run=run).order_by("-attempt").first()
        run = ActionRun.objects.select_for_update().get(pk=run.pk)
        if run.finished:
            raise Refused(f"{run.hrid} has already finished ({run.get_status_display().lower()}).")
        if run.cancel_requested_at is not None:
            return run
        now = timezone.now()
        data = {"reason": reason} if reason else {}
        if job is None or job.state in (JobState.QUEUED, JobState.AWAITING_APPROVAL):
            if job is not None:
                job.state = JobState.CANCELLED
                job.finished_at = now
                job.save(update_fields=["state", "finished_at"])
            run.status = RunStatus.CANCELLED
            run.finished_at = now
            run.cancel_requested_at = now
            run.cancel_requested_by = user
            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "cancel_requested_at",
                    "cancel_requested_by",
                ]
            )
            audit.record("run.cancelled", actor=user, target=run, project=run.project, data=data)
            if run.approval_id is not None:
                approvals.settle(
                    RunApproval.objects.select_for_update().get(pk=run.approval_id), user
                )
        else:
            run.cancel_requested_at = now
            run.cancel_requested_by = user
            run.save(update_fields=["cancel_requested_at", "cancel_requested_by"])
            audit.record(
                "run.cancel_requested", actor=user, target=run, project=run.project, data=data
            )
            # Its worker is watching for this: the command stops within a second.
            queue.notify_job(job.pk)
        live.changed(run.pk)
    return run
