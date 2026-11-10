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
