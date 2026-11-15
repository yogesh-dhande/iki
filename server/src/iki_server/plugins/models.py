"""What the migrate Job keeps about plugins between deploys (ADR 0034)."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7


class PluginMigration(models.Model):
    """A copy of each plugin migration the database has applied.

    Rolling a deployment back deploys an older build, which doesn't have the
    newer migrations it must reverse. The migrate Job reverses them from
    these copies, which it checks against their hashes. Each copy is code
    that was reviewed in the plugin's repository and has already run here.
    Only the migrator writes them; the app can only read them.
    """

    APPEND_ONLY = True

    app = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    source = models.TextField()
    archived_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_plugin_migration"
        db_table_comment = (
            "A copy of every plugin migration applied here, so a rollback can reverse it."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["app", "name", "sha256"], name="iki_plugin_migration_once"
            ),
            models.CheckConstraint(
                condition=Q(sha256__regex=r"^[0-9a-f]{64}$"), name="iki_plugin_migration_sha256"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.app}.{self.name}"


class PluginState(models.Model):
    """Each plugin a deploy has installed, as the last deploy left it (ADR 0091).

    ``sync_plugins`` compares the build's lockfile with these, so each plugin installed,
    updated, detached or reattached is audited once, by the deploy that did it. Only the
    migrator writes them; the app can only read them.
    """

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        DETACHED = "detached", "Detached"

    name = models.CharField(max_length=100, primary_key=True)
    state = models.CharField(max_length=20, choices=State.choices)
    version = models.CharField(max_length=100)
    #: The commit installed, or ``local`` for a plugin from a directory (development).
    sha = models.CharField(max_length=64)
    changed_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_plugin_state"
        db_table_comment = "Each installed plugin's state, version and commit, as deployed."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state__in=["active", "detached"]), name="iki_plugin_state_state"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PluginPurge(models.Model):
    """An Org Admin's request to purge a detached plugin's data (ADR 0092).

    It keeps the dry run's report, as it was when they confirmed, and waits until
    ``run_after``. Until then it can be cancelled; a legal hold placed meanwhile stops it.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    plugin = models.CharField(max_length=100)
    reason = models.TextField(max_length=2000)
    report = models.JSONField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField(db_default=Now())
    run_after = models.DateTimeField()
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    #: Every entity has its tombstone: their values wait for the deploy to drop them.
    tombstoned_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_plugin_purge"
        db_table_comment = "Requests to purge a detached plugin's data, and when (ADR 0092)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["plugin"],
                condition=Q(cancelled_at__isnull=True, done_at__isnull=True),
                name="iki_plugin_purge_one_pending",
            ),
        ]

    def __str__(self) -> str:
        return f"Purge of {self.plugin}"

    @property
    def pending(self) -> bool:
        return self.cancelled_at is None and self.done_at is None
