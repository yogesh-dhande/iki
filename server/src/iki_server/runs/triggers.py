"""Triggers: a plugin's actions run by themselves when entities change (ADR 0080).

A plugin declares triggers; none runs until a project's Admin turns it on in that project,
and then its runs are launched as that Admin, with ``via="trigger"``, checked as any run
they launched would be. One on the container or VM tier waits for the Admin's approval.

An entity created or changed in the project whose trigger it fires is recorded, in the
change's transaction, as a :class:`TriggerFiring`; a task launches its run once the change
has committed, and task runners sweep for any left behind each minute.

Loop protection is GitHub's: what a triggered run writes never fires a trigger, so a
trigger can't fire itself, or another that fires it back. Chaining belongs in workflows.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import Via
from iki_server.entities import registry
from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.values import Invalid
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import approvals, catalog, services
from iki_server.runs.models import ActionRun, FiringState, ProjectTrigger, TriggerFiring
from iki_spec.model import Trigger
from iki_spec.workflows import literal

__all__ = [
    "disable",
    "enable",
    "fire",
    "firings",
    "in_project",
    "label",
    "noticed",
    "recover",
    "trigger",
    "triggers",
    "why_not_enable",
    "writing",
]

#: How long a firing waits for its task before a sweep fires it.
RECOVER_AFTER = timedelta(minutes=1)
#: The most firings one housekeeping pass fires.
RECOVER_AT_MOST = 500

_EVENTS = {RevisionKind.CREATED: "entity.created", RevisionKind.EDITED: "entity.updated"}

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


def noticed(entity: Entity, revision: Revision) -> int:
    """Records the firings an entity's new revision makes in its owner project. Call in the
    change's transaction: a firing exists exactly when its change does. Returns how many."""
    event = _EVENTS.get(RevisionKind(revision.kind))
    if event is None:
        return 0
    run = _writing.get()
    if run is not None and run.via == Via.TRIGGER:
        # Loop protection: a triggered run's changes never fire a trigger.
        return 0
    on = list(ProjectTrigger.objects.filter(project_id=entity.owner_project_id, enabled=True))
    if not on:
        return 0
    installed = triggers()
    made = [
        TriggerFiring(trigger=row, event=event, entity_id=entity.pk, revision=revision.number)
        for row in on
        if (spec := installed.get(row.trigger)) is not None
        and _fires(spec, event, entity.type, revision.changed_fields)
    ]
    if not made:
        return 0
    TriggerFiring.objects.bulk_create(made)
    from iki_server.runs.tasks import fire_trigger

    for firing in made:
        fire_trigger.enqueue(str(firing.pk))
    return len(made)


def _fires(spec: Trigger, event: str, entity_type: str, changed: list[str]) -> bool:
    if spec.on != event or not services.is_a(entity_type, spec.type):
        return False
    return not spec.fields or bool(set(spec.fields) & set(changed))


# --- Firing ------------------------------------------------------------------------------------


def fire(firing_id: str) -> str:
    """Launches a firing's run, as the Admin who turned the trigger on. Safe to run twice:
    it locks the firing, and fires only one that's pending."""
    with transaction.atomic():
        firing = (
            TriggerFiring.objects.select_for_update(of=("self",), skip_locked=True)
            .select_related("trigger__project", "trigger__enabled_by", "entity")
            .filter(pk=firing_id, state=FiringState.PENDING)
            .first()
        )
        if firing is None:
            return "already fired"
        _fire(firing)
        return firing.state


def _fire(firing: TriggerFiring) -> None:
    row = firing.trigger
    spec = trigger(row.trigger)
    if not row.enabled:
        _end(firing, FiringState.SKIPPED, "turned_off", "The trigger was turned off first.")
        return
    if spec is None:
        _end(firing, FiringState.SKIPPED, "not_installed", "The trigger is no longer installed.")
        return
    if firing.entity.archived_at is not None:
        _end(firing, FiringState.SKIPPED, "archived", f"{firing.entity.hrid} was archived first.")
        return
    action = catalog.action(f"{spec.plugin}.{spec.run}")
    if action is None:
        _end(firing, FiringState.FAILED, "not_installed", f"`{spec.run}` is no longer installed.")
        return
    user, project = row.enabled_by, row.project
    did = "was created" if firing.event == "entity.created" else "changed"
    try:
        with (
            audit.using(audit.Context(via=Via.TRIGGER)),
            approvals.for_trigger(row),
            transaction.atomic(),
        ):
            run = services.launch(
                user,
                action,
                project,
                {spec.input: [firing.entity_id]},
                {name: literal(value) for name, value in spec.params.items()},
                reason=f"Trigger “{spec.label}”: {firing.entity.hrid} {did}.",
            )
    except PermissionDenied:
        message = f"{user.name} can no longer run `{action.qualified_name}` in {project.name}."
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


def _end(firing: TriggerFiring, state: str, code: str = "", message: str = "") -> None:
    firing.state = state
    firing.fired_at = timezone.now()
    firing.error = {"code": code, "message": message} if code else None
    firing.save(update_fields=["state", "fired_at", "error", "run"])


def _turn_off(row: ProjectTrigger, spec: Trigger, why: str) -> None:
    """Iki turns the trigger off: its runs can no longer be launched as its Admin. The
    project's Admins, and that Admin, are told."""
    row.enabled = False
    row.disabled_by = None
    row.disabled_at = timezone.now()
    row.disabled_reason = why
    row.save(update_fields=["enabled", "disabled_by", "disabled_at", "disabled_reason"])
    audit.record(
        "trigger.disabled",
        actor=None,
        target=row,
        project=row.project,
        data={"trigger": row.trigger, "reason": why},
    )
    admins = permissions.members_with(row.project, ProjectRole.ADMIN)
    notify(
        [*admins.exclude(pk=row.enabled_by_id), row.enabled_by],
        kinds.TRIGGER_TURNED_OFF,
        title=f"“{spec.label}” was turned off in {row.project.name}",
        body=f"{why} An Admin who can run it can turn it on again; its runs are then theirs.",
        path=f"/projects/{row.project.slug}/triggers/",
        project=row.project,
    )


def recover() -> int:
    """Fires firings whose task was lost. Returns how many it fired."""
    due = timezone.now() - RECOVER_AFTER
    ids = list(
        TriggerFiring.objects.filter(state=FiringState.PENDING, created_at__lte=due)
        .order_by("id")
        .values_list("pk", flat=True)[:RECOVER_AT_MOST]
    )
    return sum(fire(str(pk)) != "already fired" for pk in ids)


# --- Turning on and off ------------------------------------------------------------------------


def why_not_enable(user: User, project: Project, spec: Trigger) -> str | None:
    """Why `user` can't turn the trigger on in the project, or None if they can."""
    if not permissions.can(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project):
        return "Only the project's Admins can turn its triggers on."
    action = catalog.action(f"{spec.plugin}.{spec.run}")
    if action is None:
        return f"`{spec.run}` is no longer installed."
    try:
        services.may_launch(user, action, project)
    except PermissionDenied:
        return (
            f"Its runs would be launched as you, and you can't run {action.tier}-tier actions "
            "in this project."
        )
    except Refused as error:
        return str(error)
    return None


def enable(user: User, project: Project, name: str) -> ProjectTrigger:
    """Turns the trigger on in the project, its runs launched as `user` from now on.
    Raises :class:`PermissionDenied` for anyone but the project's Admins, and
    :class:`Refused` if it can't run here, or as them."""
    permissions.require(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project)
    spec = trigger(name)
    if spec is None:
        raise Refused(f"No trigger `{name}` is installed.")
    why = why_not_enable(user, project, spec)
    if why is not None:
        raise Refused(why)
    now = timezone.now()
    with transaction.atomic():
        row, made = ProjectTrigger.objects.select_for_update().get_or_create(
            project=project, trigger=name, defaults={"enabled_by": user, "enabled_at": now}
        )
        if not made and row.enabled and row.enabled_by_id == user.pk:
            return row
        row.enabled = True
        row.enabled_by = user
        row.enabled_at = now
        row.disabled_reason = ""
        row.save()
        audit.record(
            "trigger.enabled",
            actor=user,
            target=row,
            project=project,
            data={"trigger": name, "action": f"{spec.plugin}.{spec.run}", "on": spec.on},
        )
    return row


def disable(user: User, project: Project, name: str) -> ProjectTrigger | None:
    """Turns the trigger off in the project. Raises :class:`PermissionDenied` for anyone but
    the project's Admins. Returns None if it was never turned on."""
    permissions.require(user, permissions.Action.PROJECT_TRIGGERS_MANAGE, project)
    with transaction.atomic():
        row = (
            ProjectTrigger.objects.select_for_update().filter(project=project, trigger=name).first()
        )
        if row is None or not row.enabled:
            return row
        row.enabled = False
        row.disabled_by = user
        row.disabled_at = timezone.now()
        row.disabled_reason = ""
        row.save(update_fields=["enabled", "disabled_by", "disabled_at", "disabled_reason"])
        audit.record(
            "trigger.disabled", actor=user, target=row, project=project, data={"trigger": name}
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
        .select_related("trigger", "entity", "run")
        .order_by("-id")
    )


def label(row: ProjectTrigger) -> str:
    spec = trigger(row.trigger)
    return spec.label if spec is not None else row.trigger
