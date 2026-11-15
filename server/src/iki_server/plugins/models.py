"""What the migrate Job keeps about plugins between deploys (ADR 0034)."""

from typing import ClassVar

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


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
