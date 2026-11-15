"""Purging a detached plugin's data (ADR 0092): a dry run, a confirmed request, a wait.

An Org Admin asks for a purge of a detached plugin. :func:`report` says what would go:
each type's entities, the projects they're in (and any on legal hold), and what else in
Iki refers to them. :func:`request` records the request once they give a reason and type
the plugin's name, to run after ``IKI_PURGE_WAIT``; :func:`cancel` withdraws it. The purge
itself is the deploy's, once the wait is over and the lockfile says ``purged``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, ForeignObjectRel, Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.org import holds, permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.plugins.models import PluginPurge

MAX_REASON = 2000


def entities_of(plugin: str) -> QuerySet[Entity]:
    """Every entity of the plugin's types, archived ones included."""
    return Entity.objects.filter(type__in=list(registry.plugins()[plugin].types))


def report(plugin: str) -> dict[str, Any]:
    """What purging ``plugin`` would delete, and what would then refer to tombstones."""
    entities = entities_of(plugin)
    found = entities.values("pk")
    types = registry.plugins()[plugin].types
    counts = dict(entities.values_list("type").annotate(n=Count("pk")).values_list("type", "n"))
    in_projects = Project.objects.filter(
        Q(pk__in=entities.values("owner_project"))
        | Q(pk__in=ProjectLink.objects.filter(entity__in=found).values("project"))
    ).select_related("team")
    held = set(holds.held_projects().values_list("pk", flat=True))
    return {
        "plugin": plugin,
        "entities": sum(counts.values()),
        "types": [
            {
                "type": name,
                "label": t.label_plural or f"{t.label or t.name}s",
                "count": counts[name],
            }
            for name, t in sorted(types.items())
            if counts.get(name)
        ],
        "projects": [
            {"id": str(p.pk), "name": p.name, "team": p.team.name, "held": p.pk in held}
            for p in in_projects.order_by("team__name", "name")
        ],
        "references": _references(plugin, found),
    }


def _references(plugin: str, found: QuerySet[Entity, Any]) -> list[dict[str, Any]]:
    """Rows elsewhere in Iki that refer to the plugin's entities: runs' inputs and results,
    other plugins' fields, collections and the like. Each will refer to a tombstone."""
    out = []
    relations = [
        f
        for f in Entity._meta.get_fields(include_hidden=True)
        if isinstance(f, ForeignObjectRel) and (f.one_to_many or f.one_to_one)
    ]
    for relation in relations:
        model = relation.related_model
        field = relation.field
        if (
            getattr(field.remote_field, "parent_link", False)
            or model in (ProjectLink, Revision)
            or model._meta.app_label == plugin
        ):
            continue
        count = model._default_manager.filter(**{f"{field.name}__in": found}).count()
        if count:
            out.append(
                {"what": f"{model._meta.verbose_name_plural} ({field.name})", "count": count}
            )
    return sorted(out, key=lambda r: (-r["count"], r["what"]))


def pending(plugin: str) -> PluginPurge | None:
    return (
        PluginPurge.objects.filter(plugin=plugin, cancelled_at__isnull=True, done_at__isnull=True)
        .select_related("requested_by")
        .first()
    )


def why_not(plugin: str) -> str | None:
    """Why ``plugin`` can't be purged now, or None if it can."""
    if plugin not in registry.plugins():
        return f"There’s no plugin {plugin} installed."
    if plugin not in registry.detached():
        return (
            f"{plugin} isn’t detached. Detach it in the deployment’s lockfile first: its data "
            "is then kept and frozen, and can be purged later."
        )
    held = [p["name"] for p in report(plugin)["projects"] if p["held"]]
    if held:
        return (
            f"Some of its data is in {', '.join(held)}, which "
            f"{'is' if len(held) == 1 else 'are'} on legal hold. It can be purged once the "
            f"{'hold is' if len(held) == 1 else 'holds are'} released."
        )
    return None


def request(by: User, plugin: str, reason: str, confirm: str) -> PluginPurge:
    """Schedules the purge, once ``by`` has given a reason and typed the plugin's name."""
    permissions.require(by, Action.ORG_MANAGE)
    why = why_not(plugin)
    if why:
        raise Refused(why)
    reason = reason.strip()
    if not reason:
        raise Refused("Say why its data is being purged, for the audit log.", field="reason")
    if len(reason) > MAX_REASON:
        raise Refused(f"Keep it to {MAX_REASON:,} characters.", field="reason")
    if confirm.strip() != plugin:
        raise Refused(f"Type {plugin} to confirm.", field="confirm")
    snapshot = report(plugin)
    try:
        with transaction.atomic():
            purge = PluginPurge.objects.create(
                plugin=plugin,
                reason=reason,
                report=snapshot,
                requested_by=by,
                run_after=timezone.now() + dt.timedelta(seconds=settings.IKI_PURGE_WAIT),
            )
            audit.record(
                "plugin.purge_requested",
                actor=by,
                target=purge,
                data={
                    "plugin": plugin,
                    "reason": reason,
                    "entities": snapshot["entities"],
                    "run_after": purge.run_after.isoformat(),
                },
            )
    except IntegrityError:
        raise Refused(f"A purge of {plugin} is already scheduled.") from None
    return purge


def cancel(by: User, purge: PluginPurge) -> PluginPurge:
    """Withdraws a purge that hasn't run. Nothing was deleted."""
    permissions.require(by, Action.ORG_MANAGE)
    with transaction.atomic():
        purge = PluginPurge.objects.select_for_update().get(pk=purge.pk)
        if not purge.pending:
            return purge
        purge.cancelled_at = timezone.now()
        purge.cancelled_by = by
        purge.save(update_fields=["cancelled_at", "cancelled_by"])
        audit.record(
            "plugin.purge_cancelled", actor=by, target=purge, data={"plugin": purge.plugin}
        )
    return purge
