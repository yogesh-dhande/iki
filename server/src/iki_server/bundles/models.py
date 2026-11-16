"""Exports: bundles of a project's entities being made, or made, for another deployment."""

from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.org.models import Project


class ExportState(models.TextChoices):
    QUEUED = "queued", "Queued"
    WRITING = "writing", "Writing"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    #: Its archive was deleted when it expired; the record stays.
    EXPIRED = "expired", "Expired"


class BundleExport(models.Model):
    """A bundle someone asked for (ADR 0096): what they picked and why, and once it's written,
    the archive's SHA-256, size and what it holds. The archive is kept for a week."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    reason = models.TextField()
    #: What was picked, in words, as the bundle says it.
    selection = models.TextField()
    picked = ArrayField(models.UUIDField())
    state = models.CharField(max_length=16, choices=ExportState.choices, default=ExportState.QUEUED)
    #: Why it failed, said to the person who asked.
    error = models.TextField(blank=True, default="")
    sha256 = models.CharField(max_length=64, blank=True, default="")
    size = models.BigIntegerField(null=True, blank=True, db_comment="Bytes.")
    #: Its entities, revisions, files and what's outside it, as bundle.json counts them.
    counts = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_bundle_export"
        db_table_comment = "Bundles of a project's entities asked for, for another deployment."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "-created_at"], name="iki_bundle_export_by_project"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=~Q(reason=""), name="iki_bundle_export_reason"),
            models.CheckConstraint(
                condition=~Q(state="ready") | Q(sha256__regex=r"^[0-9a-f]{64}$"),
                name="iki_bundle_export_ready_sha256",
            ),
        ]

    def __str__(self) -> str:
        return f"Bundle of {self.selection}"[:254]
