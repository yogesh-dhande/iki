"""Housekeeping for webhook deliveries, run by ``iki-server maintain``."""

from datetime import timedelta

from django.utils import timezone

from iki_server.webhooks.models import Delivery, DeliveryStatus

#: Deliveries, with their payloads, are kept this long (GitHub keeps 30 days).
KEEP = timedelta(days=30)
#: Longer than the retry schedule (about 28 hours): a delivery still pending
#: then lost its task (a runner died on the last attempt).
STALE = timedelta(days=2)


def prune() -> int:
    """Deletes old deliveries and closes stale ones. Returns how many were deleted."""
    now = timezone.now()
    for delivery in Delivery.objects.filter(
        status=DeliveryStatus.PENDING, created_at__lt=now - STALE
    ):
        delivery.attempts = [
            *delivery.attempts,
            {"at": now.isoformat(), "status_code": None, "error": "Iki gave up.", "duration_ms": 0},
        ]
        delivery.status = DeliveryStatus.FAILED
        delivery.finished_at = now
        delivery.save(update_fields=["attempts", "status", "finished_at"])
    deleted, _ = Delivery.objects.filter(created_at__lt=now - KEEP).delete()
    return deleted
