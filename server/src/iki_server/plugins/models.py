"""What the migrate Job keeps about plugins between deploys (ADR 0034)."""

import datetime as dt
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


class PluginBaseline(models.Model):
    """The configuration and manifests last committed by the migrator (ADR 0181).

    The next migrator runs in the candidate image. This row lets it interpret the live
    tables as they exist before deploying that image, even for a local plugin build.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.CharField(max_length=64)
    manifests = models.JSONField()

    class Meta:
        db_table = "iki_plugin_baseline"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(id=1), name="iki_plugin_baseline_singleton"),
        ]

    def __str__(self) -> str:
        return self.revision


class PluginState(models.Model):
    """Each plugin a deploy has installed, as the last deploy left it (ADR 0091).

    ``sync_plugins`` compares the build's lockfile with these, so each plugin installed,
    updated, detached or reattached is audited once, by the deploy that did it. The
    migrate Job marks one purged when it drops its tables. Only the
    migrator writes them; the app can only read them.
    """

    class State(models.TextChoices):
        ACTIVE = "active", "Active"
        DETACHED = "detached", "Detached"
        #: Its tables were dropped (ADR 0092); the row keeps the version it was at.
        PURGED = "purged", "Purged"

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
                condition=Q(state__in=["active", "detached", "purged"]),
                name="iki_plugin_state_state",
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


class HistoryChange(models.Model):
    """A change a plugin's migration made to values kept history holds (ADR 0148): a field
    renamed, a quantity converted to a new unit, or a type renamed. ``ConvertUnit``,
    ``RecordRename`` and ``RenameEntityType`` add one each, forwards or back, in the
    migration's own transaction, so an electronic signature made before reads its record's
    history through what was done since, and still finds what was signed. Only migrations
    write them, and nothing changes or deletes one. Each is signed by the deployment's
    signing key (ADR 0126), as signatures are, so the database alone can't add one that a
    signature is read through."""

    #: The format of what a change's signature signs.
    FORMAT = "iki/history-change/v1"

    APPEND_ONLY = True

    class Kind(models.TextChoices):
        RENAME = "rename", "A field renamed"
        CONVERT = "convert", "A quantity converted to another unit"
        RETYPE = "retype", "A type renamed"

    id = models.BigAutoField(primary_key=True)
    #: The plugin whose migration made it: ``acme/lab``.
    plugin = models.CharField(max_length=100)
    #: Whether it was made by reversing a migration.
    backwards = models.BooleanField(default=False)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    #: The table whose fields changed, by what it holds, as a type's shape names its parts:
    #: ``acme/lab.Sample``, ``acme/lims:acme/lab.Sample``, ``acme/lab.Sample#entry``. Empty
    #: for a type renamed.
    part = models.CharField(max_length=255, blank=True, default="")
    #: The field (or type) as it was named, and as it's named after.
    name = models.CharField(max_length=255)
    to = models.CharField(max_length=255)
    #: A conversion's units, and how it converted: ``new = old * scale + offset``.
    from_unit = models.CharField(max_length=100, blank=True, default="")
    to_unit = models.CharField(max_length=100, blank=True, default="")
    scale = models.FloatField(null=True, blank=True)
    offset = models.FloatField(null=True, blank=True)
    made_at = models.DateTimeField(db_default=Now())
    #: Ed25519 over :meth:`signed`, by the key named, which was this deployment's signing
    #: key then; empty if it had none yet, when no signature here could be read through it.
    key = models.CharField(max_length=64, blank=True, default="")
    signature = models.BinaryField(blank=True, default=b"")

    class Meta:
        db_table = "iki_history_change"
        db_table_comment = (
            "What plugin migrations did to kept history: fields renamed, units converted, "
            "types renamed (ADR 0148)."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=["rename", "convert", "retype"]),
                name="iki_history_change_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind="convert")
                | Q(scale__isnull=False, offset__isnull=False, from_unit__gt="", to_unit__gt=""),
                name="iki_history_change_conversion",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.plugin}: {self.kind} {self.name} → {self.to}"

    def signed(self) -> bytes:
        """What its signature signs: canonical JSON (RFC 8785) of it, its ID included."""
        from iki_server.signatures import proof

        return proof.canonical_bytes(
            {
                "format": self.FORMAT,
                "id": self.pk,
                "plugin": self.plugin,
                "backwards": self.backwards,
                "kind": self.kind,
                "part": self.part,
                "name": self.name,
                "to": self.to,
                "from_unit": self.from_unit,
                "to_unit": self.to_unit,
                "scale": self.scale,
                "offset": self.offset,
                "made_at": self.made_at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            }
        )

    def change(self) -> dict[str, object]:
        """As a signature reads it, and a bundle carries it: what was done, by which
        plugin's migration, when."""
        out: dict[str, object] = {
            "kind": self.kind,
            "plugin": self.plugin,
            "at": self.made_at.date().isoformat(),
        }
        if self.kind == self.Kind.CONVERT:
            out.update(
                field=self.name,
                from_unit=self.from_unit,
                to_unit=self.to_unit,
                scale=self.scale,
                offset=self.offset,
            )
        else:
            out.update({"from": self.name, "to": self.to})
        return out
