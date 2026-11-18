"""Purging a detached plugin's data (ADR 0092): a dry run, a confirmed request, a wait.

An Org Admin asks for a purge of a detached plugin. :func:`report` says what would go:
each type's entities, the projects they're in (and any on legal hold), and what else in
Iki refers to them. :func:`request` records the request once they give a reason and type
the plugin's name, to run after ``IKI_PURGE_WAIT``; :func:`cancel` withdraws it. The purge
itself is the deploy's, once the wait is over and the lockfile says ``purged``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, ForeignObjectRel, Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import registry, services
from iki_server.entities.models import Entity, ProjectLink, Revision, Tombstone
from iki_server.entities.values import shape, to_json
from iki_server.org import holds, permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.plugins.models import PluginPurge

MAX_REASON = 2000
#: Entities tombstoned in one transaction.
BATCH = 500


def entities_of(plugin: str) -> QuerySet[Entity]:
    """Every entity of the plugin's types, archived ones included."""
    return Entity.objects.filter(type__in=list(registry.plugins()[plugin].types))


def report(plugin: str) -> dict[str, Any]:
    """What purging ``plugin`` would delete, and what would then refer to tombstones."""
    entities = entities_of(plugin)
    found = entities.values("pk")
    types = registry.plugins()[plugin].types
    # Projects' records, by type; their registry entries (ADR 0115) go with them.
    records = entities.filter(registry__isnull=True)
    counts = dict(records.values_list("type").annotate(n=Count("pk")).values_list("type", "n"))
    in_projects = Project.objects.filter(
        Q(pk__in=entities.values("owner_project"))
        | Q(pk__in=ProjectLink.objects.filter(entity__in=found).values("project"))
    ).select_related("team")
    held = set(holds.held_projects().values_list("pk", flat=True))
    return {
        "plugin": plugin,
        "entities": sum(counts.values()),
        "entries": entities.filter(registry__isnull=False).count(),
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
            # A record's entry, and the entry that superseded another, are the plugin's own.
            or (model is Entity and field.name in ("entry", "superseded_by"))
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
        if purge.tombstoned_at is not None:
            raise Refused(
                f"{purge.plugin}’s entities were already purged: only their tombstones are left."
            )
        purge.cancelled_at = timezone.now()
        purge.cancelled_by = by
        purge.save(update_fields=["cancelled_at", "cancelled_by"])
        audit.record(
            "plugin.purge_cancelled", actor=by, target=purge, data={"plugin": purge.plugin}
        )
    return purge


# --- Once the wait is over ---------------------------------------------------------------


def fingerprint(entity: Entity, values: dict[str, Any]) -> str:
    """SHA-256 of an entity's last values, as canonical JSON: its ID, HRID, type, revision
    and each value as the API gives it, keys sorted. Anyone with an export of the entity
    can check it against its tombstone."""
    fields = shape(entity.type).fields()
    document = {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        "type": entity.type,
        "revision": entity.revision,
        "values": {n: to_json(fields[n], v) for n, v in values.items() if n in fields},
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def tombstone_due(limit: int = 20 * BATCH) -> int:
    """For ``maintain``: tombstones the entities of each purge whose wait is over, while
    their plugin is still detached and none of its data is on legal hold. Their values stay
    until the deploy drops the plugin's tables. Returns how many it tombstoned."""
    done = 0
    for scheduled in PluginPurge.objects.filter(
        cancelled_at__isnull=True,
        done_at__isnull=True,
        tombstoned_at__isnull=True,
        run_after__lte=timezone.now(),
    ).order_by("run_after"):
        if why_not(scheduled.plugin) is not None:
            continue  # reattached, or a hold came: it waits, and the page says why
        while done < limit:
            made = _tombstone_batch(scheduled)
            done += made
            if made == 0:
                _finish_tombstones(scheduled)
                break
    return done


def _tombstone_batch(scheduled: PluginPurge) -> int:
    with transaction.atomic():
        batch = list(
            entities_of(scheduled.plugin)
            .filter(purged_at__isnull=True)
            .select_for_update(skip_locked=True)
            .order_by("pk")[:BATCH]
        )
        if not batch:
            return 0
        values = services.current_values_of(batch)
        now = timezone.now()
        Tombstone.objects.bulk_create(
            Tombstone(
                entity=e,
                purge=scheduled,
                content_sha256=fingerprint(e, values.get(e.pk, {})),
                revision=e.revision,
                purged_at=now,
            )
            for e in batch
        )
        Entity.objects.filter(pk__in=[e.pk for e in batch]).update(purged_at=now)
    return len(batch)


def _finish_tombstones(scheduled: PluginPurge) -> None:
    with transaction.atomic():
        locked = PluginPurge.objects.select_for_update().get(pk=scheduled.pk)
        if locked.tombstoned_at is not None:
            return
        locked.tombstoned_at = timezone.now()
        locked.save(update_fields=["tombstoned_at"])
        audit.record(
            "plugin.purge_tombstoned",
            target=locked,
            data={
                "plugin": locked.plugin,
                "entities": Tombstone.objects.filter(purge=locked).count(),
            },
        )
