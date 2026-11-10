"""Delivering webhook messages, retried on the Standard Webhooks schedule."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import structlog
from django.tasks import TaskContext

from iki_server.tasks import task
from iki_server.tasks.backend import Retry
from iki_server.webhooks import sender, services, signing
from iki_server.webhooks.models import Delivery, DeliveryStatus

logger = structlog.get_logger("iki.webhooks")

#: Delays after each failed attempt: about 28 hours of trying in all.
SCHEDULE = [
    timedelta(seconds=5),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=5),
    timedelta(hours=10),
    timedelta(hours=10),
]
ATTEMPTS = len(SCHEDULE) + 1


@task(takes_context=True, max_attempts=ATTEMPTS)
def deliver(context: TaskContext[Any, Any], delivery_id: str) -> str:
    """Sends one message. Running twice is safe: a delivered message isn't sent again,
    and receivers deduplicate on ``webhook-id`` anyway."""
    delivery = Delivery.objects.select_related("webhook__project").filter(pk=delivery_id).first()
    if delivery is None:
        return "deleted"
    if delivery.status != DeliveryStatus.PENDING:
        return delivery.status
    webhook = delivery.webhook
    final = context.attempt >= ATTEMPTS
    if not webhook.active or (
        webhook.project is not None and not services.project_webhooks_allowed()
    ):
        services.record_attempt(
            delivery_id, sender.Result(None, "The webhook is turned off.", 0), final=True
        )
        return "turned off"
    body = services.body(delivery)
    headers = signing.headers(
        services.secrets_for(webhook), delivery.message_id, int(time.time()), body
    )
    result = sender.post(webhook.url, body, headers)
    services.record_attempt(delivery_id, result, final=final)
    logger.info(
        "webhook_attempt",
        delivery=delivery_id,
        webhook=str(webhook.pk),
        webhook_event=delivery.event,
        attempt=context.attempt,
        status_code=result.status_code,
        ok=result.ok,
        duration_ms=result.duration_ms,
    )
    if result.ok:
        return "delivered"
    if final:
        return "failed"
    raise Retry(result.error or "failed", after=SCHEDULE[context.attempt - 1])
