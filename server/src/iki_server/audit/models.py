"""The audit log: who did what, when, from where. Append-only.

Rows are never updated or deleted. The app's database role may only INSERT
and SELECT them (``iki-server grant_app_privileges``), and a trigger refuses
UPDATE, DELETE and TRUNCATE from any role, including the table's owner
(ADR 0025). Every event is also written to the ``iki.audit`` log stream,
so a SIEM keeps its own copy outside the database.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from iki_server.core.ids import uuid7


class Via(models.TextChoices):
    WEB = "web", "Web"
    API = "api", "API"
    MCP = "mcp", "MCP"
    CLI = "cli", "Command line"
    SCIM = "scim", "SCIM provisioning"
    SYSTEM = "system", "System"


class AuditEvent(models.Model):
    #: Tables the app may only INSERT into and SELECT from.
    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    #: Dotted name, e.g. ``project.member_added``. See ``iki_server.audit.ACTIONS``.
    action = models.CharField(max_length=64, db_index=True)
    #: Who did it. Empty for the system and for unauthenticated requests.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        db_index=True,
    )
    #: The actor's email address at the time, kept even if it changes later.
    actor_label = models.CharField(max_length=254, blank=True)
    via = models.CharField(max_length=16, choices=Via.choices)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    #: The target's name at the time, for reading the log without joins.
    target_label = models.CharField(max_length=254, blank=True)
    project = models.ForeignKey(
        "iki_org.Project", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    team = models.ForeignKey(
        "iki_org.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Details of the change. Never secrets.
    data = models.JSONField(default=dict, blank=True)
    request_id = models.CharField(max_length=128, blank=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    #: The OAuth client the actor used, e.g. "Claude Code (client …4f2a)".
    oauth_client = models.CharField(max_length=254, blank=True, db_default="")

    class Meta:
        db_table = "iki_audit_event"
        ordering = ("-occurred_at", "-id")
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "-occurred_at"], name="iki_audit_project_time"),
            models.Index(fields=["target_type", "target_id"], name="iki_audit_target"),
        ]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.actor_label or 'system'} {self.action}"
