"""Webhooks, and every message sent to them."""

from typing import ClassVar
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.core.ids import uuid7


class DisabledReason(models.TextChoices):
    #: On (``active``), or turned off by whoever manages it.
    NONE = "", "—"
    #: Every delivery failed for too long; Iki turned it off and said so.
    FAILING = "failing", "Iki turned it off: deliveries kept failing"
    #: An Org Admin turned off a project's webhook. Only an Org Admin turns it back on.
    ORG_ADMIN = "org_admin", "An Org Admin turned it off"


class Webhook(models.Model):
    """Where to send which events: a project's, or the organization's (no project).

    The signing secret is stored encrypted and shown only when it's created
    or rotated. While a rotation is under way, messages are signed with both
    the new secret and the previous one, so receivers can switch over.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(
        "iki_org.Project",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="webhooks",
    )
    url = models.URLField(max_length=2000)
    description = models.CharField(max_length=300, blank=True)
    #: The event types sent, such as ``project.updated``.
    events = ArrayField(models.CharField(max_length=100))
    secret = models.TextField(help_text="Encrypted (core.crypto).")
    previous_secret = models.TextField(blank=True, help_text="Encrypted, during a rotation.")
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    disabled_reason = models.CharField(
        max_length=20, choices=DisabledReason.choices, blank=True, default=DisabledReason.NONE
    )
    #: When deliveries started failing without a success since; None while healthy.
    failing_since = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iki_webhook"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project"], name="iki_webhook_active", condition=Q(active=True)),
        ]

    def __str__(self) -> str:
        # Audit labels and logs: the host only. A path or query may carry a token.
        return f"Webhook to {urlsplit(self.url).netloc}"

    @property
    def scope(self) -> str:
        return "project" if self.project_id else "org"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Sending"
    SUCCEEDED = "succeeded", "Delivered"
    FAILED = "failed", "Failed"


class Delivery(models.Model):
    """One message to one webhook, and each attempt to send it.

    ``message_id`` is the ``webhook-id`` header. Sending a delivery again
    creates a new delivery with the same message id, so receivers that
    deduplicate on it see the same message.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    message_id = models.CharField(max_length=64)
    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=100)
    #: The body sent: ``{"type", "timestamp", "data"}``.
    payload = models.JSONField()
    status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    #: Each attempt: ``{"at", "status_code", "error", "duration_ms"}``. No
    #: response bodies: they could carry what an internal receiver would
    #: rather not show.
    attempts = models.JSONField(default=list)
    #: Who asked for it to be sent again, if anyone.
    redelivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_webhook_delivery"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["webhook", "-id"], name="iki_delivery_log"),
            models.Index(fields=["created_at"], name="iki_delivery_age"),
        ]

    def __str__(self) -> str:
        return f"{self.event} → {self.webhook_id}"
