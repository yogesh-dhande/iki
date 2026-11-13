"""Launching and cancelling runs (ADR 0042).

:func:`launch` checks everything a run needs before it's queued, so a worker
never starts work that can't be committed: the caller may run the action's
tier in the project, the action can run on this deployment, every input is
an entity they can read, of the right type and not archived, the entities it
updates are ones they can edit, and every param fits. It records the
revision of each input, so the run's provenance says what it read.

A run reads only what belongs to its project (ADR 0066): its inputs must be
linked into it, and its file params be its files. Otherwise the launch raises
:class:`OutsideProject`, unless the caller asked to add the inputs to the
project first, which links them, audited, as the link API does.

A run an agent asks for through MCP, on a tier that needs approval, waits for
the person it acts for to approve it (:mod:`iki_server.runs.approvals`).
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, hrids, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid, Refs, to_db
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import (
    approvals,
    budgets,
    catalog,
    endings,
    live,
    queue,
    quotas,
    reuse,
)
from iki_server.runs.models import (
    BATCH_HRID_PREFIX,
    BATCH_HRID_SEQUENCE,
    FINISHED,
    HRID_PREFIX,
    HRID_SEQUENCE,
    ActionRun,
    Job,
    JobState,
    RunApproval,
    RunBatch,
    RunInput,
    RunStatus,
    WorkflowRun,
)
from iki_spec.model import Action, Workflow
from iki_spec.validate import value_problems

__all__ = [
    "TIER_PERMISSIONS",
    "OutsideProject",
    "cancel",
    "cancel_batch",
    "check_inputs",
    "check_params",
    "launch",
    "launch_each",
    "link_into_project",
    "may_launch",
    "outside_project",
    "reproduce",
    "visible_batches",
    "visible_runs",
]

#: The project permission each tier needs (Editor for client and backend, Runner above).
TIER_PERMISSIONS = queue.TIER_ACTIONS


class OutsideProject(Exception):  # noqa: N818 - reads as "they're outside the project"
    """Inputs the caller can read that aren't in the run's project. Launching with
    `add_to_project` adds them first."""

    def __init__(self, project: Project, entities: Sequence[Entity]) -> None:
        self.project = project
        self.entities = list(entities)
        hrids_ = [e.hrid for e in self.entities]
        listed = ", ".join(hrids_[:-1]) + f" and {hrids_[-1]}" if len(hrids_) > 1 else hrids_[0]
        verb = "aren't" if len(hrids_) > 1 else "isn't"
        them = "them" if len(hrids_) > 1 else "it"
        super().__init__(
            f"{listed} {verb} in {project.name}. A run reads only what's in its project: "
            f"add {them} to {project.name} first, and its members will be able to read {them}."
        )


def outside_project(
    user: User,
    project: Project,
    chosen: Mapping[str, Sequence[Entity]],
    errors: dict[str, list[str]],
    *,
    each: str | None = None,
) -> list[Entity]:
    """The inputs that aren't in `project` but could be added to it, in the order given.
    Ones whose project doesn't allow sharing them can't be (ADR 0067): they go in
    `errors`, as `each`'s go under ``entities``."""
    from iki_server.entities.services import not_shared

    wanted = {e.pk for entities in chosen.values() for e in entities}
    inside = set(access.in_project(project).filter(pk__in=wanted).values_list("pk", flat=True))
    allowed = set(
        access.shareable_into(user, project).filter(pk__in=wanted).values_list("pk", flat=True)
    )
    owners = {
        p.pk: p
        for p in Project.objects.filter(
            pk__in={e.owner_project_id for entities in chosen.values() for e in entities}
        )
    }
    seen: set[UUID] = set()
    out = []
    for name, entities in chosen.items():
        for e in entities:
            if e.pk in inside or e.pk in seen:
                continue
            seen.add(e.pk)
            if e.pk in allowed:
                out.append(e)
            else:
                key = "entities" if name == each else f"inputs/{name}"
                errors.setdefault(key, []).append(not_shared(e, owners[e.owner_project_id]))
    return out


def link_into_project(user: User, project: Project, outside: Sequence[Entity]) -> None:
    from iki_server.entities import services as entities

    for entity in outside:
        entities.link(user, entity, project)


def visible_runs(user: Any) -> QuerySet[ActionRun]:  # noqa: ANN401 - User or AnonymousUser
    """Runs in projects whose data the user can read."""
    return ActionRun.objects.filter(project__in=access.readable_projects(user).values("pk"))


def visible_batches(user: Any) -> QuerySet[RunBatch]:  # noqa: ANN401 - User or AnonymousUser
    """Batches in projects whose data the user can read."""
    return RunBatch.objects.filter(project__in=access.readable_projects(user).values("pk"))


def launch(
    user: User,
    action: Action,
    project: Project,
    inputs: Mapping[str, Sequence[UUID]],
    params: Mapping[str, Any],
    *,
    reason: str = "",
    reuse_results: bool = True,
    add_to_project: bool = False,
    workflow: WorkflowRun | None = None,
    workflow_step: str = "",
) -> ActionRun:
    """Queue a run of `action` in `project`, or raise why it can't run. For a deterministic
    action, an identical earlier run's results are reused instead, unless `reuse_results`
    is off (ADR 0057). `add_to_project` links inputs from other projects into it first.
    A workflow's step names its `workflow` run and `workflow_step` (ADR 0079).

    Raises :class:`PermissionDenied` if the caller can't run the action's tier in the project,
    :class:`Refused` if it can't run here now, :class:`Invalid` for inputs and params, and
    :class:`OutsideProject` for inputs that aren't in the project.
    """
    may_launch(user, action, project)
    errors: dict[str, list[str]] = {}
    entities = check_inputs(user, action, inputs, errors)
    values = check_params(user, action, project, params, errors)
    if errors:
        raise Invalid(errors)
    outside = outside_project(user, project, entities, errors)
    if errors:
        raise Invalid(errors)
    if outside and not add_to_project:
        raise OutsideProject(project, outside)
    with transaction.atomic():
        link_into_project(user, project, outside)
        made = _made(user, action, project, entities, values, reuse_results=reuse_results)
        step = (workflow, workflow_step) if workflow is not None else None
        if made.original is not None:
            return _reuse(user, action, project, entities, values, reason, made, step=step)
        budgets.check_launch(project, action)
        approval = approvals.ask(user, action, project)
        run = _queue(
            user, action, project, entities, values, reason, approval, made=made, step=step
        )
        if approval is not None:
            approvals.announce(
                approval, 1, action.label or action.qualified_name, path=f"/runs/{run.hrid}/"
            )
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
    reuse_results: bool = True,
    add_to_project: bool = False,
) -> list[ActionRun]:
    """Queue one run of `action` for each of `entity_ids` as its `each` input, the others the
    same for all, or none of them: raises as :func:`launch` does, with problems with the
    entities under ``entities``."""
    may_launch(user, action, project)
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
    entities = check_inputs(user, action, {**inputs, each: entity_ids}, errors, each=each)
    values = check_params(user, action, project, params, errors)
    if f"inputs/{each}" in errors:
        errors["entities"] = errors.pop(f"inputs/{each}")
    if errors:
        raise Invalid(errors)
    outside = outside_project(user, project, entities, errors, each=each)
    if errors:
        raise Invalid(errors)
    if outside and not add_to_project:
        raise OutsideProject(project, outside)
    chosen = entities.pop(each)
    with transaction.atomic():
        link_into_project(user, project, outside)
        made = [
            _made(
                user,
                action,
                project,
                {**entities, each: [e]},
                values,
                reuse_results=reuse_results,
            )
            for e in chosen
        ]
        to_run = sum(1 for m in made if m.original is None)
        if to_run:
            budgets.check_launch(project, action, to_run)
        # One request, one approval: a batch is approved or declined together. Runs that
        # reuse results run nothing, so they need none.
        approval = approvals.ask(user, action, project, runs=to_run) if to_run else None
        batch = RunBatch.objects.create(
            hrid=_next_hrid(BATCH_HRID_PREFIX, BATCH_HRID_SEQUENCE),
            action=action.qualified_name,
            project=project,
            requested_by=user,
            via=audit.current_via(),
            client=audit.current_oauth_client(),
            agent=audit.current_agent(),
            reason=reason,
            each=each,
            size=len(chosen),
        )
        runs = [
            _reuse(user, action, project, {**entities, each: [entity]}, values, reason, m, batch)
            if m.original is not None
            else _queue(
                user,
                action,
                project,
                {**entities, each: [entity]},
                values,
                reason,
                approval,
                batch=batch,
                made=m,
            )
            for entity, m in zip(chosen, made, strict=True)
        ]
        audit.record(
            "run.batch_launched",
            actor=user,
            target=batch,
            project=project,
            data={"action": action.qualified_name, "tier": action.tier, "runs": len(runs)},
        )
        if approval is not None and to_run:
            approvals.announce(
                approval,
                to_run,
                action.label or action.qualified_name,
                path=f"/batches/{batch.hrid}/",
            )
        return runs


def input_taking(action: Action, types: Sequence[str]) -> str | None:
    """The first of `action`'s inputs that takes entities of every one of `types`."""
    for name, put in action.inputs.items():
        if all(is_a(t, put.type) for t in types):
            return name
    return None


def may_launch(user: User, action: Action, project: Project) -> None:
    if not permissions.can(user, TIER_PERMISSIONS[action.tier], project):
        raise PermissionDenied
    if project.archived_at is not None:
        raise Refused(f"{project.name} is archived, so nothing new can run in it.")
    available = catalog.availability(action)
    if not available.available:
        raise Refused(available.reason or "This action can't run on this deployment.")


def plugin_settings(action: Action) -> dict[str, Any]:
    """The deployment's values for the settings of `action`'s plugin, which its runs get.
    Raises :class:`Refused` if the plugins were built without them."""
    if not registry.plugins()[action.plugin].settings:
        return {}
    try:
        values = registry.settings(action.plugin)
    except ImproperlyConfigured as error:
        raise Refused(f"{error} Until then, its actions can't run.") from None
    return {name: value for name, value in values.items() if value is not None}


def _queue(
    user: User,
    action: Action,
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    values: dict[str, Any],
    reason: str,
    approval: RunApproval | None = None,
    *,
    batch: RunBatch | None = None,
    made: _Made | None = None,
    copy_of: ActionRun | None = None,
    step: tuple[WorkflowRun, str] | None = None,
) -> ActionRun:
    """Queues the run, or holds it for `approval`. Call inside a transaction, with its
    inputs and params checked. A reproduction is a `copy_of` the run it reproduces: its
    action as launched, plugin settings, inputs at the revisions it read, and seed."""
    environment = catalog.environment(action)
    if approval is None:
        quotas.check_queued(project, action.tier)
    made = made or _made(None, action, project, entities, values, reuse_results=False)
    run = ActionRun.objects.create(
        status=RunStatus.AWAITING_APPROVAL if approval else RunStatus.QUEUED,
        approval=approval,
        client=audit.current_oauth_client(),
        agent=audit.current_agent(),
        hrid=_next_hrid(),
        batch=batch,
        workflow=step[0] if step else None,
        workflow_step=step[1] if step else "",
        action=action.qualified_name,
        plugin_version=registry.plugins()[action.plugin].version,
        plugin_sha=made.sha,
        spec=copy_of.spec if copy_of else asdict(action),
        project=project,
        requested_by=user,
        via=audit.current_via(),
        reason=reason,
        tier=copy_of.tier if copy_of else action.tier,
        environment=copy_of.environment if copy_of else action.environment,
        params=values,
        plugin_settings=made.settings,
        seed=copy_of.seed if copy_of and copy_of.seed is not None else _seed(),
        reuse_key="" if copy_of else made.key,
        reproduces=copy_of,
        timeout=copy_of.timeout if copy_of else action.timeout,
        max_attempts=copy_of.max_attempts if copy_of else 1 + action.retry_max,
        retry_on=copy_of.retry_on if copy_of else list(action.retry_on),
    )
    if copy_of:
        RunInput.objects.bulk_create(
            RunInput(
                run=run,
                name=i.name,
                position=i.position,
                entity_id=i.entity_id,
                revision=i.revision,
            )
            for i in RunInput.objects.filter(run=copy_of).order_by("name", "position")
        )
    else:
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
        lifecycle=action.lifecycle or "",
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
            **({"batch": batch.hrid} if batch else {}),
            **({"workflow": step[0].hrid, "step": step[1]} if step else {}),
            **({"reproduces": copy_of.hrid} if copy_of else {}),
        },
    )
    return run


@dataclass(frozen=True)
class _Made:
    """What a run's results depend on that isn't its inputs and params, its reuse key, and
    the identical run it reuses, if any."""

    sha: str
    settings: dict[str, Any]
    key: str
    original: ActionRun | None


def _made(
    user: User | None,
    action: Action,
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    values: Mapping[str, Any],
    *,
    reuse_results: bool,
) -> _Made:
    sha = plugin_sha(action.plugin)
    settings = plugin_settings(action)
    key = reuse.key(action, sha, entities, values, settings)
    original = reuse.find(user, key, project) if reuse_results and user is not None else None
    return _Made(sha, settings, key, original)


def _reuse(
    user: User,
    action: Action,
    project: Project,
    entities: Mapping[str, Sequence[Entity]],
    values: dict[str, Any],
    reason: str,
    made: _Made,
    batch: RunBatch | None = None,
    *,
    step: tuple[WorkflowRun, str] | None = None,
) -> ActionRun:
    assert made.original is not None  # noqa: S101 - only called with one
    return reuse.reuse(
        user,
        action,
        project,
        entities,
        values,
        made.settings,
        reason,
        made.original,
        hrid=_next_hrid(),
        batch=batch,
        step=step,
    )


def _seed() -> int:
    """A seed for a run's random numbers: 31 bits, so it fits every tool's seed."""
    return secrets.randbelow(2**31)


def reproduce(user: User, run: ActionRun, *, reason: str = "") -> ActionRun:
    """Queue a run that reproduces `run` exactly (ADR 0057): the same plugin commit, action
    as launched, params, plugin settings, inputs at the revisions it read, and seed. It
    writes nothing; when it ends, it says whether its results are the same.

    A run that reused another's results is reproduced as that one. Raises as :func:`launch`
    does, and :class:`Refused` if this deployment runs other code for its plugin."""
    original = reuse.reproducible(run)
    action = catalog.action(original.action)
    if action is None:
        raise Refused(f"{original.action} isn't installed now, so it can't run here.")
    may_launch(user, action, original.project)
    why = reuse.blocked(
        original, plugin_sha(action.plugin), registry.plugins()[action.plugin].version
    )
    if why:
        raise Refused(why)
    readable = set(
        access.visible(user)
        .filter(pk__in=RunInput.objects.filter(run=original).values("entity_id"))
        .values_list("pk", flat=True)
    )
    hidden = RunInput.objects.filter(run=original).exclude(entity_id__in=readable)
    if hidden.exists():
        raise PermissionDenied
    gone = list(
        Entity.objects.filter(pk__in=RunInput.objects.filter(run=original).values("entity_id"))
        .exclude(pk__in=access.in_project(original.project).values("pk"))
        .order_by("hrid")
        .values_list("hrid", flat=True)
    )
    if gone:
        listed = ", ".join(gone)
        verb = "aren't" if len(gone) > 1 else "isn't"
        raise Refused(
            f"{listed} {verb} in {original.project.name} any more, so this run can't be "
            "reproduced there: a run reads only what's in its project."
        )
    with transaction.atomic():
        budgets.check_launch(original.project, action)
        approval = approvals.ask(user, action, original.project)
        reproduction = _queue(
            user,
            action,
            original.project,
            {},
            dict(original.params),
            reason,
            approval,
            made=_Made(original.plugin_sha, dict(original.plugin_settings), "", None),
            copy_of=original,
        )
        if approval is not None:
            approvals.announce(
                approval,
                1,
                action.label or action.qualified_name,
                path=f"/runs/{reproduction.hrid}/",
            )
        return reproduction


def check_inputs(
    user: User,
    action: Action | Workflow,
    given: Mapping[str, Sequence[UUID]],
    errors: dict[str, list[str]],
    *,
    each: str | None = None,
    updated: set[str] | None = None,
) -> dict[str, list[Entity]]:
    """The entities for each input, checked; problems go in `errors`. The `each` input is
    one entity per run: it may be given several. Those of `updated` inputs (by default, the
    action's) must be ones the user can edit."""
    for name in sorted(given.keys() - action.inputs.keys()):
        errors[f"inputs/{name}"] = [f"`{action.qualified_name}` has no input `{name}`."]
    wanted = {i for name in action.inputs for i in given.get(name, ())}
    found = {e.pk: e for e in access.visible(user).filter(pk__in=wanted)}
    if updated is None:
        updated = {u.input for u in action.updates} if isinstance(action, Action) else set()
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


def check_params(
    user: User,
    action: Action | Workflow,
    project: Project,
    given: Mapping[str, Any],
    errors: dict[str, list[str]],
) -> dict[str, Any]:
    for name in sorted(given.keys() - action.params.keys()):
        errors[f"params/{name}"] = [f"`{action.qualified_name}` has no param `{name}`."]
    values: dict[str, Any] = {}
    files: Refs | None = None
    for name, f in action.params.items():
        key = f"params/{name}"
        if name in given and given[name] is not None:
            if f.type == "file":
                # A file the person can read, in the run's project, of a type and size
                # the param takes.
                files = files or Refs(
                    access.visible(user),
                    access.readable_files(user) & access.project_files(project),
                )
                if _elsewhere(user, project, given[name]):
                    errors[key] = [
                        f"That file isn't in {project.name}: a run reads only what's in its "
                        "project. Upload it there, or choose one of its files."
                    ]
                    continue
                try:
                    values[name] = str(to_db(f, given[name], files))
                except ValueError as error:
                    errors[key] = [f"The value {error}"]
                continue
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


def _elsewhere(user: User, project: Project, value: Any) -> bool:  # noqa: ANN401 - JSON
    """A file the user can read that isn't in `project`."""
    try:
        file_id = UUID(str(value))
    except ValueError:
        return False
    readable = access.readable_files(user).filter(pk=file_id)
    return readable.exists() and not access.project_files(project).filter(pk=file_id).exists()


def _next_hrid(prefix: str = HRID_PREFIX, sequence: str = HRID_SEQUENCE) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s::regclass)", [sequence])
        (number,) = cursor.fetchone()
    return hrids.format_hrid(prefix, 6, number)


def plugin_sha(plugin: str) -> str:
    record = registry.installed() or {}
    for entry in record.get("plugins", ()):
        if entry.get("name") == plugin:
            return str(entry.get("sha") or "")
    return ""


def cancel_batch(user: User, batch: RunBatch, *, reason: str = "") -> int:
    """Cancel every run of the batch that hasn't finished, as :func:`cancel` would each.
    Returns how many it cancelled or asked to stop."""
    if not permissions.can(user, TIER_PERMISSIONS[_batch_tier(batch)], batch.project):
        raise PermissionDenied
    count = 0
    with transaction.atomic():
        unfinished = (
            ActionRun.objects.filter(batch=batch, cancel_requested_at__isnull=True)
            .exclude(status__in=sorted(s.value for s in FINISHED))
            .order_by("id")
        )
        for run in unfinished:
            try:
                cancel(user, run, reason=reason)
            except Refused:
                continue  # it finished meanwhile
            count += 1
        audit.record(
            "run.batch_cancelled",
            actor=user,
            target=batch,
            project=batch.project,
            data={"runs": count, **({"reason": reason} if reason else {})},
        )
    return count


def _batch_tier(batch: RunBatch) -> str:
    tier = ActionRun.objects.filter(batch=batch).values_list("tier", flat=True).first()
    return tier or "backend"


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
            # Queued again after an interruption, it may have saved progress (ADR 0069).
            endings.ended(run)
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
            if job.state == JobState.COMMITTING and hasattr(job, "bulk_import"):
                # Its import stops at its next chunk, writing nothing (ADR 0056).
                from iki_server.runs import tasks

                tasks.import_creates.enqueue(str(job.bulk_import.pk))
            else:
                # Its worker is watching for this: the command stops within a second. In a
                # packed job, it stops once every run in it is cancelled (ADR 0055).
                queue.notify_job(job.packed_into_id or job.pk)
        live.changed(run.pk, run.batch_id)
    return run
