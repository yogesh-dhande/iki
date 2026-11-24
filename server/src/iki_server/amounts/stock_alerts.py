"""Notify project stock managers once per observed low-stock episode (ADR 0190)."""

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
        for pk in catalogue.iterator():
            total += _check(project, pk)
    return total


@transaction.atomic
def _check(project: Project, pk: UUID) -> int:
    # Synchronize concurrent scans, including the first when no markers exist yet.
    item = stock.items(project).select_for_update(of=("self",)).filter(pk=pk).first()
    if item is None:
        return 0
    already = LowStockAlert.objects.filter(item=item)
    if not stock.summary(project, item).low:
        already.delete()
        return 0
    notified = set(already.values_list("recipient_id", flat=True))
    recipients = {
        user.pk: user
        for role in stock.catalogues()[item.type].notify
        for user in permissions.holders_of(project, role)
        if user.pk not in notified and access.can_read(user, item)
    }
    # The destination lists stock the reader can see. The notification itself
    # identifies only the item, even when the project has restricted locations.
    notes = notify(
        recipients.values(),
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
    return len(notes)
