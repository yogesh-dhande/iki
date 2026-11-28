"""Notify project stock managers once per observed low-stock episode (ADR 0190)."""

from collections import defaultdict
from typing import Any
from uuid import UUID

from django.db import transaction

from iki_server.amounts import stock
from iki_server.amounts.models import LowStockAlert
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.notifications import kinds
from iki_server.notifications.services import notify
from iki_server.org import permissions
from iki_server.org.models import Project

#: Catalogue items a pass checks in one transaction: locked, summarized and told about
#: together, so a pass's queries grow with its batches, not its items.
ALERT_BATCH = 100


def send_alerts() -> int:
    """Check active catalogue items from their current ledger, storage and expiry.

    The hourly maintenance pass also detects expiry when nothing was edited. A
    recovery clears the episode. Managers assigned while stock stays low can
    receive their first alert on the next pass. Returns the number notified.
    """
    configured = stock.catalogues()
    if not configured:
        return 0
    owners = Entity.objects.filter(type__in=configured).values("owner_project_id")
    total = 0
    for project in Project.objects.filter(pk__in=owners, archived_at__isnull=True).iterator():
        catalogue = stock.items(project).order_by("pk").values_list("pk", flat=True)
        after: UUID | None = None
        while ids := list(
            (catalogue if after is None else catalogue.filter(pk__gt=after))[:ALERT_BATCH]
        ):
            total += _check(project, ids)
            after = ids[-1]
    return total


@transaction.atomic
def _check(project: Project, ids: list[UUID]) -> int:
    """One batch of a project's catalogue items, under their locks."""
    # Synchronize concurrent scans, including the first when no markers exist yet. Locks
    # are taken in ID order, so overlapping passes queue rather than wait on each other.
    # No key lock, so lots and orders can still be recorded against the items meanwhile.
    items = list(
        stock.items(project)
        .select_for_update(of=("self",), no_key=True)
        .filter(pk__in=ids)
        .order_by("pk")
    )
    if not items:
        return 0
    low = [s.id for s in stock.summaries(project, items) if s.low]
    # Recovered (or never low): the episode ends, and the next one alerts again.
    LowStockAlert.objects.filter(item_id__in=[i.pk for i in items]).exclude(
        item_id__in=low
    ).delete()
    if not low:
        return 0
    notified: dict[UUID, set[Any]] = defaultdict(set)
    for item_id, recipient in LowStockAlert.objects.filter(item_id__in=low).values_list(
        "item_id", "recipient_id"
    ):
        notified[item_id].add(recipient)
    by_id = {i.pk: i for i in items}
    holders: dict[str, list[Any]] = {}
    due: dict[UUID, dict[Any, Any]] = {}
    for pk in low:
        item = by_id[pk]
        due[pk] = {}
        for role in stock.catalogues()[item.type].notify:
            if role not in holders:
                holders[role] = list(permissions.holders_of(project, role))
            due[pk].update({u.pk: u for u in holders[role] if u.pk not in notified[pk]})
    # A project grant alone doesn't disclose a record its holder can't read.
    people = {u.pk: u for found in due.values() for u in found.values()}
    readable = {
        user_id: set(
            access.visible(user)
            .filter(pk__in=[pk for pk, found in due.items() if user_id in found])
            .values_list("pk", flat=True)
        )
        for user_id, user in people.items()
    }
    sent = 0
    for pk, found in due.items():
        recipients = [u for u in found.values() if pk in readable[u.pk]]
        if not recipients:
            continue
        item = by_id[pk]
        # The destination lists stock the reader can see. The notification itself
        # identifies only the item, even when the project has restricted locations.
        notes = notify(
            recipients,
            kinds.LOW_STOCK,
            title=f"{item.hrid}: Low stock",
            body="Available stock is at or below the reorder threshold.",
            project=project,
            path=f"/entities/{item.pk}",
            about=[item],
        )
        LowStockAlert.objects.bulk_create(
            LowStockAlert(item=item, recipient_id=note.recipient_id) for note in notes
        )
        sent += len(notes)
    return sent
