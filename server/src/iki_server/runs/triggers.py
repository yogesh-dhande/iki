"""Triggers: a plugin's actions run by themselves when entities change (ADR 0080).

A plugin declares triggers; none runs until a project's Admin turns it on in that project,
and then its runs are launched as that Admin, with ``via="trigger"``, checked as any run
they launched would be. One on the container or VM tier waits for the Admin's approval.
Its action may be a dependency's (ADR 0298): the run is that action's, as its own plugin
declares it, and the trigger only says when it starts and on what.

An entity created or changed in the project whose trigger it fires is recorded, in the
change's transaction, as a :class:`TriggerFiring`; a task launches its run once the change
has committed, and task runners sweep for any left behind each minute.

A request sent to a project as a service fires that project's ``request.received``
triggers (ADR 0232), as :func:`received`: nobody in the service's project made it, so, as a
schedule's, its runs are launched as a service account an Admin chooses, on their authority.

Loop protection is GitHub's: what a triggered run writes never fires a trigger, so a
trigger can't fire itself, or another that fires it back. Chaining belongs in workflows.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import Kind as UserKind
from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import Via
from iki_server.core import sweeps
from iki_server.entities import registry, search
from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.values import Invalid, subtypes
from iki_server.graph import queries
from iki_server.graph.models import Edge, EdgeKind
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import approvals, catalog, services
from iki_server.runs.models import (
    FINISHED,
    ActionRun,
    FiringState,
    ProjectTrigger,
    Recalculation,
    RunStatus,
    TriggerFiring,
)
from iki_spec import cron
from iki_spec.model import ACCOUNT_EVENTS, Action, Trigger
from iki_spec.workflows import literal

__all__ = [
    "disable",
    "enable",
    "fire",
    "firings",
    "in_project",
    "label",
    "moved",
    "next_run",
    "noticed",
    "received",
    "recheck_edit",
    "rechecks",
    "recover",
    "repointed",
    "run_as_choices",
    "source_changed",
    "tick",
    "trigger",
    "triggers",
    "why_not_enable",
    "writing",
]

#: The most related entities a trigger gives one input (ADR 0123), whatever its ``max``.
MAX_RELATED = 1000
#: How long a firing waits for its task before a sweep fires it.
RECOVER_AFTER = timedelta(minutes=1)
#: The most firings one housekeeping pass fires.
RECOVER_AT_MOST = 500
#: Tries at launching a firing's run that may fail unexpectedly before it fails.
MAX_FAILURES = 5

logger = structlog.get_logger("iki.runs")

_EVENTS = {
    RevisionKind.CREATED: "entity.created",
    RevisionKind.EDITED: "entity.updated",
    RevisionKind.STATE_CHANGED: "entity.state_changed",
}

#: The run whose results are being written, while they are.
_writing: ContextVar[ActionRun | None] = ContextVar("iki_trigger_writing", default=None)


def triggers() -> dict[str, Trigger]:
    """Every installed trigger, by qualified name, in name order."""
    found = {t.qualified_name: t for p in registry.plugins().values() for t in p.triggers.values()}
    return dict(sorted(found.items()))


def trigger(name: str) -> Trigger | None:
    return triggers().get(name)


@contextlib.contextmanager
def writing(run: ActionRun) -> Iterator[None]:
    """Marks the changes made inside as the run's results: a triggered run's fire nothing."""
    token = _writing.set(run)
    try:
        yield
    finally:
        _writing.reset(token)


# --- Noticing changes --------------------------------------------------------------------------


def noticed(entity: Entity, revision: Revision, *, registered: Revision | None = None) -> int:
    """Records the firings an entity's new revision makes in its owner project. Call in the
    change's transaction: a firing exists exactly when its change does. Returns how many.

    ``registered``: the record's creation registered a new registry entry too (ADR 0115),
    whose first revision this is, so ``entity.registered`` fires as well, for the fields
    either set."""
    event = _EVENTS.get(RevisionKind(revision.kind))
    if event is None:
        return 0
    # A state change's "changes" are the field and the state it entered (ADR 0125).
    changed = (
        [revision.state_field, revision.state_to]
        if event == "entity.state_changed"
        else revision.changed_fields
    )
    events = [(event, changed)]
    if registered is not None:
        events.append(
            ("entity.registered", sorted({*revision.changed_fields, *registered.changed_fields}))
        )
    run = _writing.get()
    if run is not None and run.via == Via.TRIGGER:
        # Loop protection: a triggered run's changes never fire a trigger.
        return 0
    if entity.owner_project_id is None:
        return 0
    on = list(ProjectTrigger.objects.filter(project_id=entity.owner_project_id, enabled=True))
    if not on:
        return 0
    installed = triggers()
    made = [
        TriggerFiring(trigger=row, event=name, entity_id=entity.pk, revision=revision.number)
        for row in on
        for name, changed in events
        if (spec := installed.get(row.trigger)) is not None
        and not spec.recheck
        and _fires(spec, name, entity.type, changed)
    ]
    if not made:
        return 0
    TriggerFiring.objects.bulk_create(made)
    from iki_server.runs.tasks import fire_trigger

    for firing in made:
        fire_trigger.enqueue(str(firing.pk))
    return len(made)


def received(request: Entity, service: Project, revision: int) -> int:
    """Records the firings a request makes as it's sent to ``service`` (ADR 0232): its
    ``request.received`` triggers for the request's type, in the service's project, not
    the asking one's. Call in the sending transaction. Returns how many."""
    run = _writing.get()
    if run is not None and run.via == Via.TRIGGER:
        return 0
    installed = triggers()
    made = [
        TriggerFiring(
            trigger=row, event="request.received", entity_id=request.pk, revision=revision
        )
        for row in ProjectTrigger.objects.filter(project=service, enabled=True)
        if (spec := installed.get(row.trigger)) is not None
        and _fires(spec, "request.received", request.type, [])
    ]
    if not made:
        return 0
    TriggerFiring.objects.bulk_create(made)
    from iki_server.runs.tasks import fire_trigger

    for firing in made:
        fire_trigger.enqueue(str(firing.pk))
    return len(made)


def rechecks(entity_type: str, changed: set[str]) -> list[tuple[Trigger, tuple[str, ...]]]:
    """The proofs invalidated by an actual edit, independently of project settings."""
    found = []
    for spec in triggers().values():
        if spec.recheck and _fires(spec, "entity.updated", entity_type, list(changed)):
            action = catalog.action(spec.action)
            if action is not None:
                found.append((spec, tuple(n for u in action.updates for n in u.fields)))
    return found


def _calculations(entity_type: str) -> Iterator[tuple[Trigger, Action, frozenset[str]]]:
    """Each calculation of an ``entity_type`` record (ADR 0080), a trigger ``on:
    entity.updated`` without ``recheck`` whose action updates the record it's given, with
    the references its input includes (``protein``, ``components.part``)."""
    for spec in triggers().values():
        if (
            spec.on != "entity.updated"
            or spec.recheck
            or not spec.input
            or not services.is_a(entity_type, spec.type)
        ):
            continue
        action = catalog.action(f"{spec.plugin}.{spec.action}")
        if action is None or not any(u.input == spec.input for u in action.updates):
            continue
        put = action.inputs.get(spec.input)
        if put is not None and put.include:
            yield spec, action, frozenset(put.include)


def repointed(entity: Entity, fields: set[str]) -> int:
    """A move to a superseded entry's or lab list option's replacement (ADR 0115, ADR 0270)
    re-pointed these references of ``entity``: each calculation that reads through one
    (ADR 0080) needs running again, as what it was worked out from is another record now,
    even where all it reads of it is fixed (ADR 0143). Flags it, whether or not its project
    turned it on; the re-pointing save fires those it did, as each calculation's trigger
    watches its references, and a run clears the flag. Returns how many it flagged."""
    actions = {
        action.qualified_name
        for _, action, include in _calculations(entity.type)
        if fields & {name.partition(".")[0] for name in include}
    }
    Recalculation.objects.bulk_create(
        [Recalculation(entity=entity, action=a) for a in sorted(actions)], ignore_conflicts=True
    )
    return len(actions)


def moved(record: Entity) -> int:
    """``record`` was moved to its superseded entry's replacement (ADR 0115): it's the
    replacement's record now, with the replacement's facts. Each calculation of the
    project's records that reads through a reference to it (ADR 0080) needs running again,
    though the reference didn't change: flags each, and fires those the project turned on,
    for records that aren't locked. Returns how many it flagged."""
    if record.owner_project_id is None:
        return 0
    through: dict[UUID, set[str]] = {}
    for node, field in Edge.objects.filter(kind=EdgeKind.REFERENCE, on_id=record.pk).values_list(
        "node_id", "field"
    ):
        through.setdefault(node, set()).add(field)
    referrers = Entity.objects.filter(
        pk__in=through,
        owner_project_id=record.owner_project_id,
        archived_at__isnull=True,
        purged_at__isnull=True,
    ).order_by("pk")
    run = _writing.get()
    may_fire = run is None or run.via != Via.TRIGGER
    on = {
        row.trigger: row
        for row in ProjectTrigger.objects.filter(project_id=record.owner_project_id, enabled=True)
    }
    flags: dict[tuple[UUID, str], Recalculation] = {}
    firings: list[TriggerFiring] = []
    for referrer in referrers:
        for spec, action, include in _calculations(referrer.type):
            if not through[referrer.pk] & include:
                continue
            flags[(referrer.pk, action.qualified_name)] = Recalculation(
                entity=referrer, action=action.qualified_name
            )
            row = on.get(spec.qualified_name)
            if may_fire and row is not None and not referrer.locked_by:
                firings.append(
                    TriggerFiring(
                        trigger=row,
                        entity=referrer,
                        revision=referrer.revision,
                        event="entity.updated",
                    )
                )
    Recalculation.objects.bulk_create(list(flags.values()), ignore_conflicts=True)
    TriggerFiring.objects.bulk_create(firings)
    from iki_server.runs.tasks import fire_trigger

    for firing in firings:
        fire_trigger.enqueue(str(firing.pk))
    return len(flags)


def recheck_edit(user: User, entity: Entity, revision: Revision, spec: Trigger) -> str:
    """Pin inputs and queue under the editing credential while its limits are in scope."""
    run = _writing.get()
    if entity.owner_project_id is None or (run is not None and run.via == Via.TRIGGER):
        return ""
    row, _ = ProjectTrigger.objects.select_for_update().get_or_create(
        project_id=entity.owner_project_id,
        trigger=spec.qualified_name,
        defaults={"enabled_at": timezone.now()},
    )
    if not row.enabled:
        return ""
    firing = TriggerFiring.objects.create(
        trigger=row, entity=entity, revision=revision.number, event=spec.on, editor=user
    )
    _fire(firing, editor=user)
    return firing.error["message"] if firing.error else ""


def source_changed(parent: Entity, revision: Revision) -> None:
    """Queue dependent calculations for the projects that enabled them.

    Registry facts are shared. A project-record change, such as a complex's bond
    assignment, affects only records in that project. A triggered calculation can
    propagate newly filled registry values, which are write-once.
    """
    if revision.kind not in (RevisionKind.EDITED, RevisionKind.CURATED):
        return
    run = _writing.get()
    may_fire = run is None or run.via != Via.TRIGGER or parent.registry_id is not None
    changed = set(revision.changed_fields)
    for spec in triggers().values():
        refs = [ref for ref, fields in spec.watch_included.items() if changed.intersection(fields)]
        if not refs:
            continue
        targets = Edge.objects.filter(
            kind=EdgeKind.REFERENCE, on_id__in=[parent.pk, parent.entry_id], field__in=refs
        ).values("node_id")
        records = Entity.objects.filter(
            Q(pk__in=targets) | Q(entry_id__in=targets),
            type__in=subtypes(spec.type),
            owner_project_id__isnull=False,
            archived_at__isnull=True,
            purged_at__isnull=True,
        )
        if parent.registry_id is None:
            records = records.filter(owner_project_id=parent.owner_project_id)
        records_list = list(records.order_by("pk"))
        if not records_list:
            continue
        action = spec.action
        Recalculation.objects.bulk_create(
            [Recalculation(entity=r, action=action) for r in records_list],
            ignore_conflicts=True,
        )
        if not may_fire:
            continue
        rows = {
            r.project_id: r
            for r in ProjectTrigger.objects.filter(
                trigger=spec.qualified_name,
                enabled=True,
                project_id__in={r.owner_project_id for r in records_list},
            )
        }
        from iki_server.runs.tasks import fire_trigger

        for record in records_list:
            assert record.owner_project_id is not None  # noqa: S101 - selected project records
            row = rows.get(record.owner_project_id)
            if row is not None and not record.locked_by:
                firing = TriggerFiring.objects.create(
                    trigger=row,
                    entity=record,
                    revision=record.revision,
                    event="entity.updated",
                )
                fire_trigger.enqueue(str(firing.pk))


def _fires(spec: Trigger, event: str, entity_type: str, changed: list[str]) -> bool:
    if spec.on != event or not services.is_a(entity_type, spec.type):
        return False
    if event == "entity.state_changed":
        field, state = changed
        return spec.state_field == field and (not spec.states or state in spec.states)
    return not spec.fields or bool(set(spec.fields) & set(changed))


# --- Firing ------------------------------------------------------------------------------------


def fire(firing_id: str) -> str:
    """Launches a firing's run: as the Admin who turned the trigger on, or for a schedule,
    as the service account they chose. Safe to run twice: it locks the firing, and fires
    only one that's pending."""
    with transaction.atomic():
        firing = (
            TriggerFiring.objects.select_for_update(of=("self",), skip_locked=True)
            .select_related("trigger__project", "trigger__enabled_by", "trigger__run_as", "entity")
            .filter(pk=firing_id, state=FiringState.PENDING)
            .first()
        )
        if firing is None:
            return "already fired"
        try:
            with transaction.atomic():
                _fire(firing)
        except Exception:
            # What it doesn't end with why (a bug, a constraint the launch breaks) is
            # undone and counted: the sweep tries it again, and past a few, it fails.
            _failed(firing)
        return firing.state


def _failed(firing: TriggerFiring) -> None:
    firing.refresh_from_db(fields=["state", "run", "related", "error", "fired_at", "failures"])
    firing.failures += 1
    logger.exception("trigger_fire_failed", firing=str(firing.pk), failures=firing.failures)
    firing.save(update_fields=["failures"])
    if firing.failures >= MAX_FAILURES:
        _end(
            firing,
            FiringState.FAILED,
            "launch_failed",
            f"Its run couldn't start: launching it failed {firing.failures} times with an "
            "unexpected error, which the server's log records.",
        )


def _fire(firing: TriggerFiring, *, editor: User | None = None) -> None:
    row = firing.trigger
    spec = trigger(row.trigger)
    if not row.enabled:
        _end(firing, FiringState.SKIPPED, "turned_off", "The trigger was turned off first.")
        return
    if spec is None:
        _end(firing, FiringState.SKIPPED, "not_installed", "The trigger is no longer installed.")
        return
    if spec.plugin in registry.detached():
        # It stays on, so it runs again once its plugin is reattached (ADR 0091).
        _end(
            firing,
            FiringState.SKIPPED,
            "plugin_detached",
            f"The {spec.plugin} plugin is detached, so its triggers don't run.",
        )
        return
    if firing.entity is not None and firing.entity.archived_at is not None:
        _end(firing, FiringState.SKIPPED, "archived", f"{firing.entity.hrid} was archived first.")
        return
    action = catalog.action(spec.action)
    if action is None:
        _end(
            firing, FiringState.FAILED, "not_installed", f"`{spec.action}` is no longer installed."
        )
        return
    if action.plugin in registry.detached():
        # A dependency's action (ADR 0298): detached, it stops this trigger as its own
        # plugin's detachment would, and the trigger stays on for when it's reattached.
        _end(
            firing,
            FiringState.SKIPPED,
            "plugin_detached",
            f"The {action.plugin} plugin is detached, so its action `{action.name}` doesn't run.",
        )
        return
    authority = editor or row.enabled_by
    if authority is None or (spec.recheck and editor is None):
        _end(firing, FiringState.FAILED, "not_allowed", "The editing credential is unavailable.")
        return
    project = row.project
    params = {name: literal(value) for name, value in spec.params.items()}
    inputs: dict[str, list[UUID]] = {}
    try:
        if firing.entity is not None:
            inputs[spec.input] = [firing.entity.pk]
            if spec.related:
                # What they read is theirs to give it: first, that they still may.
                services.may_launch(authority, action, project)
                found = _related(firing, spec, action)
                if found is None:
                    return
                inputs.update(found)
        with (
            audit.using(audit.Context(via=Via.TRIGGER)),
            approvals.for_trigger(row),
            transaction.atomic(),
        ):
            if firing.entity is not None and spec.on in ACCOUNT_EVENTS:
                # A request arrived (ADR 0232): nobody here sent it, so its run is the
                # service account's, on the Admin's authority, as a schedule's is.
                assert row.run_as is not None  # noqa: S101 - turned on with one
                user = row.run_as
                services.may_launch(authority, action, project)
                asking = firing.entity.owner_project
                run = services.launch(
                    user,
                    action,
                    project,
                    inputs,
                    params,
                    reason=f"Trigger “{spec.label}”: {firing.entity.hrid} arrived"
                    + (f" from {asking.name}." if asking is not None else "."),
                )
            elif firing.entity is None:
                assert row.run_as is not None and firing.scheduled_for is not None  # noqa: S101
                user = row.run_as
                # Its authority is the Admin's: they must still be able to run it too.
                services.may_launch(authority, action, project)
                when = firing.scheduled_for.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
                run = services.launch(
                    user,
                    action,
                    project,
                    {},
                    params,
                    reason=f"Scheduled trigger “{spec.label}”, for {when}.",
                    # Each run picks up from the last: an identical earlier run is no answer.
                    reuse_results=False,
                )
            else:
                user = authority
                did = {
                    "entity.created": "was created",
                    "entity.registered": "was registered",
                }.get(firing.event, "changed")
                given = "".join(
                    f" With {len(ids)} related {name}."
                    for name, ids in inputs.items()
                    if name != spec.input
                )
                run = services.launch(
                    user,
                    action,
                    project,
                    inputs,
                    params,
                    reuse_results=not bool(editor),
                    reason=f"Trigger “{spec.label}”: {firing.entity.hrid} {did}.{given}",
                )
    except PermissionDenied:
        if editor is not None:
            _end(
                firing,
                FiringState.FAILED,
                "not_allowed",
                f"{editor.name} cannot run {action.label} in {project.name}.",
            )
            return
        message = _not_allowed(row, action, project)
        _end(firing, FiringState.FAILED, "not_allowed", message)
        _turn_off(row, spec, message)
        return
    except Invalid as error:
        problems = "; ".join(
            f"{key.split('/', 1)[-1]}: {' '.join(said)}" for key, said in error.errors.items()
        )
        _end(firing, FiringState.FAILED, "invalid", f"Its run couldn't start: {problems}")
        return
    except (Refused, services.OutsideProject) as error:
        _end(firing, FiringState.FAILED, "launch_failed", f"Its run couldn't start: {error}")
        return
    firing.run = run
    _end(firing, FiringState.LAUNCHED)


def _related(firing: TriggerFiring, spec: Trigger, action: Action) -> dict[str, list[UUID]] | None:
    """The entities related to the firing's that its trigger gives inputs (ADR 0123): each
    input's type's records in the project, not archived, that the Admin who turned it on
    reads there, walking only that project's records. All of them or none: a hold or a
    recall that reached only some would look done when it isn't. ``None`` once the firing
    has ended, with why."""
    assert firing.entity is not None  # noqa: S101 - only an entity's firing has related
    row = firing.trigger
    assert row.enabled_by is not None  # noqa: S101 - related triggers require an Admin
    out: dict[str, list[UUID]] = {}
    for name, how in sorted(spec.related.items()):
        put = action.inputs[name]  # the spec's checks make sure
        at_most = min(put.max or MAX_RELATED, MAX_RELATED)
        types = () if put.type == "iki/core.Entity" else (put.type,)
        q = queries.Query(
            firing.entity.pk, how.direction, how.depth, queries.kinds_of(how.edges), types
        )
        try:
            ids = queries.related(row.enabled_by, row.project_id, q, at_most)
        except search.TooSlow:
            _end(
                firing,
                FiringState.FAILED,
                "too_slow",
                f"Finding what's {how.direction} of {firing.entity.hrid} for `{name}` took "
                "too long.",
            )
            return None
        if len(ids) > at_most:
            _end(
                firing,
                FiringState.FAILED,
                "too_many",
                f"More than {at_most} entities are {how.direction} of {firing.entity.hrid} "
                f"for `{name}`, the most it takes, so no run started.",
            )
            return None
        if ids:
            out[name] = ids
        elif not put.optional:
            _end(
                firing,
                FiringState.SKIPPED,
                "nothing_related",
                f"Nothing is {how.direction} of {firing.entity.hrid} for `{name}`.",
            )
            return None
    firing.related = {name: len(out.get(name, ())) for name in sorted(spec.related)}
    return out


def _not_allowed(row: ProjectTrigger, action: Action, project: Project) -> str:
    """Who can no longer run the trigger's action: the Admin, or its service account."""
    for who in (row.enabled_by, row.run_as):
        if who is not None and not permissions.can(
            who, services.TIER_PERMISSIONS[action.tier], project
        ):
            return f"{who.name} can no longer run `{action.qualified_name}` in {project.name}."
    return f"`{action.qualified_name}` can no longer be run in {project.name} as the trigger runs."


def _end(firing: TriggerFiring, state: str, code: str = "", message: str = "") -> None:
    firing.state = state
    firing.fired_at = timezone.now()
    firing.error = {"code": code, "message": message} if code else None
    firing.save(update_fields=["state", "fired_at", "error", "run", "related"])


def _turn_off(row: ProjectTrigger, spec: Trigger, why: str) -> None:
    """Iki turns the trigger off: its runs can no longer be launched as it runs them. The
    project's Admins, and the Admin who turned it on, are told."""
    row.enabled = False
    row.disabled_by = None
    row.disabled_at = timezone.now()
    row.disabled_reason = why
    row.next_run_at = None
    row.save(
        update_fields=["enabled", "disabled_by", "disabled_at", "disabled_reason", "next_run_at"]
    )
    audit.record(
        "trigger.disabled",
        actor=None,
        target=row,
        project=row.project,
        data={"trigger": row.trigger, "reason": why},
    )
    admins = permissions.members_with(row.project, ProjectRole.ADMIN)
    notify(
        [*admins.exclude(pk=row.enabled_by_id), *([row.enabled_by] if row.enabled_by else [])],
        kinds.TRIGGER_TURNED_OFF,
        title=f"“{spec.label}” was turned off in {row.project.name}",
        body=f"{why} An Admin who can run it can turn it on again; its runs are then theirs.",
        path=f"/projects/{row.project.slug}/automation/triggers/",
        project=row.project,
    )


# --- Schedules ---------------------------------------------------------------------------------


def tick(now: datetime | None = None) -> int:
    """Fires each scheduled trigger whose time has come, once however many times it was
    missed, as GitHub's schedules do. One whose last run hasn't finished is skipped this
    time: runs never overlap. Returns how many it fired or skipped."""
    now = now or timezone.now()
    done = 0
    installed = triggers()
    with transaction.atomic():
        due = list(
            ProjectTrigger.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_run_at__lte=now)
            .order_by("next_run_at")[:RECOVER_AT_MOST]
        )
        for row in due:
            assert row.next_run_at is not None  # noqa: S101 - filtered on it
            spec = installed.get(row.trigger)
            if spec is None or spec.on != "schedule":
                row.next_run_at = None
                row.save(update_fields=["next_run_at"])
                continue
            at = row.next_run_at
            row.next_run_at = next_run(spec, now)
            row.save(update_fields=["next_run_at"])
            _schedule(row, at, now)
            done += 1
    return done


def _schedule(row: ProjectTrigger, at: datetime, now: datetime) -> None:
    from iki_server.runs.tasks import fire_trigger

    earlier = row.firings.filter(event="schedule")
    going = earlier.filter(
        Q(state=FiringState.PENDING)
        | (Q(state=FiringState.LAUNCHED) & ~Q(run__status__in=[s.value for s in FINISHED]))
    ).first()
    if going is not None:
        TriggerFiring.objects.create(
            trigger=row,
            event="schedule",
            scheduled_for=at,
            state=FiringState.SKIPPED,
            fired_at=now,
            error={
                "code": "still_running",
                "message": "Its last run hadn't finished, and runs on a schedule never overlap.",
            },
        )
        return
    last = (
        earlier.filter(state=FiringState.LAUNCHED, run__status=RunStatus.SUCCEEDED)
        .order_by("-scheduled_for")
        .values_list("scheduled_for", flat=True)
        .first()
    )
    firing = TriggerFiring.objects.create(
        trigger=row, event="schedule", scheduled_for=at, since=last
    )
    fire_trigger.enqueue(str(firing.pk))


def recover() -> int:
    """Fires firings whose task was lost. Returns how many it fired or ended. One whose
    launch fails unexpectedly stays pending for the next pass, and fails after
    ``MAX_FAILURES``, behind none of the others."""
    due = timezone.now() - RECOVER_AFTER
    ids = list(
        TriggerFiring.objects.filter(state=FiringState.PENDING, created_at__lte=due)
        .order_by("id")
        .values_list("pk", flat=True)[:RECOVER_AT_MOST]
    )
    return sweeps.each(
        ids,
        lambda pk: fire(str(pk)) not in ("already fired", FiringState.PENDING),
        failed="trigger_recovery_failed",
        key="firing",
    )


# --- Turning on and off ------------------------------------------------------------------------


def why_not_enable(user: User, project: Project, spec: Trigger) -> str | None:
    """Why `user` can't turn the trigger on in the project, or None if they can."""
    if not permissions.can(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project):
        return "Only the project's Admins can turn its triggers on."
    if spec.recheck:
        return None
    action = catalog.action(spec.action)
    if action is None:
        return f"`{spec.action}` is no longer installed."
    try:
        services.may_launch(user, action, project)
    except PermissionDenied:
        if spec.on in ACCOUNT_EVENTS:
            return (
                f"Its runs are launched on your authority, and you can't run {action.tier}-tier "
                "actions in this project."
            )
        return (
            f"Its runs would be launched as you, and you can't run {action.tier}-tier actions "
            "in this project."
        )
    except Refused as error:
        return str(error)
    if spec.on in ACCOUNT_EVENTS and not run_as_choices(project, spec):
        what = (
            "A scheduled trigger" if spec.on == "schedule" else "A trigger on a request's arrival"
        )
        return (
            f"{what} runs as a service account, and none in {project.name} can run "
            f"{action.tier}-tier actions. Add one as a member first."
        )
    return None


def run_as_choices(project: Project, spec: Trigger) -> list[User]:
    """The service accounts a scheduled trigger may run as in the project: active members
    who can run its action's tier, by name."""
    action = catalog.action(spec.action)
    if spec.on not in ACCOUNT_EVENTS or action is None:
        return []
    least = permissions.least_role(services.TIER_PERMISSIONS[action.tier])
    return list(
        permissions.members_with(project, least)
        .filter(kind=UserKind.SERVICE)
        .distinct()
        .order_by("name", "pk")
    )


def next_run(spec: Trigger, after: datetime) -> datetime | None:
    """When a scheduled trigger next runs after `after`, or None if it never does."""
    return cron.parse(spec.schedule).next_after(after, cron.zone(spec.timezone or "UTC"))


def enable(
    user: User, project: Project, name: str, *, run_as: User | None = None
) -> ProjectTrigger:
    """Turns the trigger on in the project. An entity trigger's runs are launched as `user`
    from now on; a scheduled one's, or one on a request's arrival, as the service account
    `run_as`, on `user`'s authority.
    Raises :class:`PermissionDenied` for anyone but the project's Admins, and
    :class:`Refused` if it can't run here, or as them."""
    permissions.require(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project)
    spec = trigger(name)
    if spec is None:
        raise Refused(f"No trigger `{name}` is installed.")
    why = why_not_enable(user, project, spec)
    if why is not None:
        raise Refused(why)
    scheduled = spec.on == "schedule"
    account = spec.on in ACCOUNT_EVENTS
    if account and run_as is None:
        raise Refused("Choose the service account it runs as.")
    if not account and run_as is not None:
        raise Refused("A trigger that fires on changes runs as whoever turns it on.")
    if account and run_as not in run_as_choices(project, spec):
        raise Refused(
            "It can only run as an active service account that's a member of the project and "
            "can run its action here."
        )
    now = timezone.now()
    with transaction.atomic():
        row, made = ProjectTrigger.objects.select_for_update().get_or_create(
            project=project, trigger=name, defaults={"enabled_by": user, "enabled_at": now}
        )
        unchanged = row.enabled and row.enabled_by_id == user.pk and row.run_as == run_as
        if not made and unchanged:
            return row
        row.enabled = True
        row.enabled_by = user
        row.enabled_at = now
        row.run_as = run_as
        row.next_run_at = next_run(spec, now) if scheduled else None
        row.disabled_reason = ""
        row.save()
        audit.record(
            "trigger.enabled",
            actor=user,
            target=row,
            project=project,
            data={
                "trigger": name,
                "action": spec.action,
                "on": spec.on,
                **({"run_as": run_as.name} if run_as else {}),
                **({"schedule": spec.schedule} if scheduled else {}),
            },
        )
    return row


def disable(user: User, project: Project, name: str, *, reason: str = "") -> ProjectTrigger | None:
    """Turns the trigger off in the project. Raises :class:`PermissionDenied` for anyone but
    the project's Admins. Returns None if it was never turned on."""
    permissions.require(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project)
    spec = trigger(name)
    reason = reason.strip()
    if spec and spec.recheck and not reason:
        raise Refused("Give a reason for turning off automatic re-verification.")
    with transaction.atomic():
        rows = ProjectTrigger.objects.select_for_update()
        row: ProjectTrigger | None
        if spec and spec.recheck:
            row, _ = rows.get_or_create(
                project=project, trigger=name, defaults={"enabled_at": timezone.now()}
            )
        else:
            row = rows.filter(project=project, trigger=name).first()
        if row is None or not row.enabled:
            return row
        row.enabled = False
        row.disabled_by = user
        row.disabled_at = timezone.now()
        row.disabled_reason = reason
        row.next_run_at = None
        row.save(
            update_fields=[
                "enabled",
                "disabled_by",
                "disabled_at",
                "disabled_reason",
                "next_run_at",
            ]
        )
        audit.record(
            "trigger.disabled",
            actor=user,
            target=row,
            project=project,
            data={"trigger": name, **({"reason": reason} if reason else {})},
        )
    return row


def in_project(project: Project) -> dict[str, ProjectTrigger]:
    """The project's triggers that were ever turned on, by name."""
    return {
        r.trigger: r
        for r in ProjectTrigger.objects.filter(project=project).select_related(
            "enabled_by", "disabled_by"
        )
    }


def firings(project: Project) -> QuerySet[TriggerFiring]:
    """The project's firings, newest first."""
    return (
        TriggerFiring.objects.filter(trigger__project=project)
        .select_related("trigger", "entity", "run", "editor")
        .order_by("-id")
    )


def label(row: ProjectTrigger) -> str:
    spec = trigger(row.trigger)
    return spec.label if spec is not None else row.trigger
