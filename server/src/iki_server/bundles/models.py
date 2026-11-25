"""Bundles: exports of a project's entities for another deployment, and imports from one."""

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
    #: The local identities needed to read the exact retained archive.
    references = ArrayField(models.UUIDField(), null=True)
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


class ImportState(models.TextChoices):
    #: Its archive is being uploaded (see its upload).
    UPLOADING = "uploading", "Uploading"
    #: Checking the archive and its signature, and working out the plan.
    PLANNING = "planning", "Planning"
    PLANNED = "planned", "Planned"
    #: The archive didn't check out, or isn't from a deployment this one trusts.
    REFUSED = "refused", "Refused"
    APPLYING = "applying", "Applying"
    APPLIED = "applied", "Applied"
    FAILED = "failed", "Failed"
    #: Discarded before it was applied; its archive is deleted.
    DISCARDED = "discarded", "Discarded"


class EntityPlan(models.TextChoices):
    NEW = "new", "New"
    #: Here at an earlier revision, from an earlier import: the newer ones are added.
    UPDATE = "update", "Newer revisions"
    #: Here as it is, or with later revisions of this deployment's own: nothing changes.
    SKIP = "skip", "Here already"
    CONFLICT = "conflict", "Conflict"


class BundleImport(models.Model):
    """A bundle uploaded to import into a project (ADR 0096): who brought it and from where,
    what checking it found, and the plan for each entity and person before anything changes."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    upload = models.OneToOneField(
        "iki_files.Upload", on_delete=models.PROTECT, related_name="bundle_import"
    )
    state = models.CharField(
        max_length=16, choices=ImportState.choices, default=ImportState.UPLOADING
    )
    #: Why it was refused or failed, said to the person importing it.
    error = models.TextField(blank=True, default="")
    sha256 = models.CharField(max_length=64, db_comment="The archive's, as uploaded.")
    size = models.BigIntegerField(db_comment="The archive's, in bytes.")
    #: bundle.json's ID, once it's read.
    bundle_id = models.UUIDField(null=True, blank=True)
    #: Where it's from, as its bundle.json says: deployment, URL, Iki release, who exported
    #: it, when and why, and from which project.
    source = models.JSONField(default=dict, blank=True)
    #: The trusted deployment its signature is from.
    trusted_as = models.CharField(max_length=100, blank=True, default="")
    #: The plan's summary: its counts, its types, files and what's outside it, and the
    #: problems that keep it from being applied.
    plan = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    planned_at = models.DateTimeField(null=True, blank=True)
    #: Who confirmed applying it, and why.
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True, default="")
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_bundle_import"
        db_table_comment = "Bundles from other deployments, uploaded to import into a project."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "-created_at"], name="iki_bundle_import_by_project"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(sha256__regex=r"^[0-9a-f]{64}$"), name="iki_bundle_import_sha256"
            ),
        ]

    def __str__(self) -> str:
        return f"Import of {self.sha256[:12]}"


class ImportEntity(models.Model):
    """What importing does to one of a bundle's entities."""

    id = models.BigAutoField(primary_key=True)
    bundle_import = models.ForeignKey(BundleImport, on_delete=models.CASCADE, related_name="+")
    entity_id = models.UUIDField(db_comment="Its ID, which an import keeps.")
    type = models.CharField(max_length=112)
    hrid = models.CharField(max_length=32, db_comment="Its HRID where it's from.")
    #: Its HRID here: the same, or blank when it's renumbered from this deployment's own
    #: sequence as it's applied.
    hrid_here = models.CharField(max_length=32, blank=True, default="")
    title = models.TextField(blank=True, default="")
    action = models.CharField(max_length=16, choices=EntityPlan.choices)
    revisions = models.PositiveIntegerField(db_comment="How many it has in the bundle.")
    #: How many of them are added here.
    adds = models.PositiveIntegerField(default=0)
    #: Why it's skipped or a conflict, or what else happens to it, for people.
    why = models.TextField(blank=True, default="")
    #: For a record of a registry entry (ADR 0115): the entry here, or the ID the import
    #: registers it with, and whether it does.
    entry_id = models.UUIDField(null=True, blank=True)
    registers = models.BooleanField(default=False)

    class Meta:
        db_table = "iki_bundle_import_entity"
        db_table_comment = "What an import does to each entity in its bundle."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["bundle_import", "entity_id"], name="iki_bundle_import_entity_once"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["bundle_import", "action", "id"], name="iki_import_entity_action"),
            # Finding an entity by the HRID it had where it was imported from.
            models.Index(fields=["hrid"], name="iki_import_entity_hrid"),
            models.Index(fields=["entity_id"], name="iki_import_entity_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.hrid}: {self.action}"


class ImportPerson(models.Model):
    """Someone a bundle's records name, and who they are here: matched by email, or, when
    nobody here has it, a placeholder made as the import is applied."""

    id = models.BigAutoField(primary_key=True)
    bundle_import = models.ForeignKey(BundleImport, on_delete=models.CASCADE, related_name="+")
    source_id = models.UUIDField(db_comment="Their ID where the bundle is from.")
    kind = models.CharField(max_length=16, db_comment="person or service.")
    name = models.CharField(max_length=200)
    email = models.CharField(max_length=254)
    #: Who they are here, when someone has their email; None means a placeholder.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "iki_bundle_import_person"
        db_table_comment = "Who each person an import's bundle names is on this deployment."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["bundle_import", "source_id"], name="iki_bundle_import_person_once"
            ),
        ]

    def __str__(self) -> str:
        return self.email
