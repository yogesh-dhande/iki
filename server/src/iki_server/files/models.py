"""Files: their bytes (blobs), the files people add (with a name and type), and uploads.

- A **blob** is content. Iki's own are content-addressed: stored once per
  SHA-256, under a key only the server writes, after it checked the bytes. A
  registered one is an object where it already was, pinned to an object
  version so it can't change underneath its entities.
- A **file** is a blob as someone added it: its name, its media type, the
  project it was added to, who added it and when. Two files can share a blob.
- A **use** records that an entity refers to a file, so that the entity's
  readers can read the file too.
- A file may say when its data was **acquired** (ADR 0177): as the file's own
  format records it, such as an AB1 trace's run date, or as whoever added it
  said. Otherwise it's unknown, never the time it was added.
- A file may also say which **instrument**, who and what recorded it, as its format
  records them (ADR 0224): its facts, each with where in the file it's stated, and a SAM
  or BAM file's read groups. Read once, when it's added; nothing else stands in for them.
- An **upload** sends a file's bytes straight to object storage, in parts,
  and is checked before it becomes a file (ADR 0036).
- A **registration** makes a file of an object where it already is, in an
  approved, versioned bucket, once Iki has read it to learn its SHA-256.

Blobs, files and uses never change: the app may only add and read them.
"""

from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import F, Func, JSONField, Q, Value
from django.db.models.functions import Collate, Lower, Now, Upper
from django.db.models.lookups import Exact

from iki_server.core.ids import uuid7
from iki_server.org.models import Project

SHA256 = r"^[0-9a-f]{64}$"
#: A media type, lowercased and without parameters (RFC 6838's restricted names).
MEDIA_TYPE = r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
#: When a file's data was acquired, in ISO 8601, no more precise than its source: a date, or
#: a date and time, with an offset only when the source stated one.
ACQUIRED = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?(Z|[+-][0-9]{2}:[0-9]{2})?)?$"
)


class Blob(models.Model):
    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    sha256 = models.CharField(max_length=64)
    size = models.BigIntegerField(db_comment="Bytes.")
    storage_uri = models.TextField(db_comment="Where the bytes are, such as s3://bucket/key.")
    #: Registered where it already was, rather than uploaded to Iki's bucket.
    registered_external = models.BooleanField(default=False)
    object_version = models.CharField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_blob"
        db_table_comment = "File contents, in object storage. Files and file fields point to them."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(sha256__regex=SHA256), name="iki_blob_sha256"),
            models.CheckConstraint(condition=Q(size__gte=0), name="iki_blob_size"),
            # Iki's own uploads are stored once per content.
            models.UniqueConstraint(
                fields=["sha256"],
                condition=Q(registered_external=False),
                name="iki_blob_content",
            ),
            models.UniqueConstraint(
                fields=["storage_uri", "object_version"],
                condition=Q(registered_external=True),
                name="iki_blob_external_object",
            ),
        ]

    def __str__(self) -> str:
        return self.sha256[:12]


class File(models.Model):
    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT, related_name="files")
    name = models.CharField(max_length=255, db_comment="The file's name, as it was added.")
    media_type = models.CharField(max_length=255, db_comment="Such as chemical/x-pdb.")
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, related_name="+", db_comment="Where it was added."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that added it for ``created_by``, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    #: When its data was acquired (ADR 0177): the day, for finding it by date, and the
    #: moment as precisely as its source gave it.
    acquired_on = models.DateField(
        null=True,
        blank=True,
        db_comment="The day its data was acquired, as its source said; null if unknown.",
    )
    acquired_at = models.CharField(
        max_length=40,
        blank=True,
        default="",
        db_default="",
        db_comment="When its data was acquired, in ISO 8601, as precisely as its source said.",
    )
    acquired_from = models.CharField(
        max_length=16,
        blank=True,
        default="",
        db_default="",
        db_comment="Where that came from: the file's own format (such as abif or fcs), given "
        "by whoever added it, or empty if unknown.",
    )
    #: What the file says about where its data came from (ADR 0224): {"instrument":
    #: {"value": ..., "stated_in": ...}, ...}, as :mod:`iki_server.files.metadata` keeps it.
    facts = models.JSONField(
        default=dict,
        db_default=Value({}, JSONField()),
        db_comment="What the file states about its instrument, operator, software and "
        "protocol, each with where it states it (ADR 0224); {} if it states none.",
    )
    read_groups = models.JSONField(
        default=list,
        db_default=Value([], JSONField()),
        db_comment="A SAM or BAM file's @RG lines, their standard tags; [] if none.",
    )
    #: The facts' values and the read groups' names, one per line, for finding the file.
    stated_text = models.TextField(
        blank=True,
        default="",
        db_default="",
        db_comment="The facts' values and read groups' names the Files list searches.",
    )

    class Meta:
        db_table = "iki_file"
        db_table_comment = "Files people added: a name and media type for a blob's bytes."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_file_by_project"),
            # A project's Files (ADR 0177): found by any part of their names, of a format,
            # by when acquired, and sorted by name.
            # On the names in upper case: a case-insensitive match compares them so.
            GinIndex(OpClass(Upper("name"), name="gin_trgm_ops"), name="iki_file_name_trgm"),
            # And by what they state (ADR 0224), as their names are.
            GinIndex(
                OpClass(Upper("stated_text"), name="gin_trgm_ops"), name="iki_file_stated_trgm"
            ),
            models.Index(fields=["project", "media_type", "id"], name="iki_file_by_format"),
            models.Index(fields=["project", "acquired_on", "id"], name="iki_file_by_acquired"),
            # The latest acquired first, those not known last.
            models.Index(
                F("project"),
                F("acquired_on").desc(nulls_last=True),
                F("id").desc(),
                name="iki_file_by_last_acquired",
            ),
            # By name, as Python compares them (bytes), so pages merge in Postgres's order.
            models.Index(
                F("project"), Collate(Lower("name"), "C"), F("id"), name="iki_file_by_name"
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=~Q(name=""), name="iki_file_name"),
            models.CheckConstraint(
                condition=Q(media_type__regex=MEDIA_TYPE),
                name="iki_file_media_type",
            ),
            # Known, it says when and how it's known, and its day is its moment's; unknown,
            # it says nothing.
            models.CheckConstraint(
                condition=Q(acquired_on__isnull=True, acquired_at="", acquired_from="")
                | Q(
                    acquired_on__isnull=False,
                    acquired_at__regex=ACQUIRED,
                    acquired_from__regex=r"^[a-z][a-z0-9-]{0,15}$",
                ),
                name="iki_file_acquired",
            ),
            models.CheckConstraint(
                condition=Exact(
                    Func(F("facts"), function="jsonb_typeof", output_field=models.TextField()),
                    "object",
                )
                & Exact(
                    Func(
                        F("read_groups"), function="jsonb_typeof", output_field=models.TextField()
                    ),
                    "array",
                ),
                name="iki_file_stated",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def size(self) -> int:
        return self.blob.size

    @property
    def sha256(self) -> str:
        return self.blob.sha256


class FileUse(models.Model):
    """An entity that refers to a file, at any revision: whoever can read it can read the file.

    Kept when the entity stops referring to it, since its history still does.
    """

    APPEND_ONLY = True

    pk = models.CompositePrimaryKey("file", "entity")
    file = models.ForeignKey(File, on_delete=models.PROTECT, related_name="uses")
    entity = models.ForeignKey("iki_entities.Entity", on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField(db_comment="The entity's revision that first used it.")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_file_use"
        db_table_comment = "Entities that refer to files, so their readers can read the files."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity"], name="iki_file_use_by_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.file_id} in {self.entity_id}"


class UploadKind(models.TextChoices):
    #: Becomes a file: content-addressed, kept for good.
    FILE = "file", "File"
    #: Becomes a run's artifact (ADR 0068): kept apart, under its own key, and deleted when
    #: its retention ends.
    ARTIFACT = "artifact", "Artifact"
    #: A run's saved progress (ADR 0069): kept apart, under its own key, until the run ends.
    CHECKPOINT = "checkpoint", "Checkpoint"
    #: A bundle to import (ADR 0096): kept apart, under its own key, until it's applied or
    #: discarded.
    BUNDLE = "bundle", "Bundle"


class UploadState(models.TextChoices):
    #: Waiting for its parts.
    PENDING = "pending", "Waiting for its parts"
    #: All parts arrived; Iki is checking the bytes.
    CHECKING = "checking", "Checking"
    SCANNING = "scanning", "Scanning"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class Upload(models.Model):
    """A file on its way in: parts sent straight to object storage, then checked."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that uploaded it for ``created_by``, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=255)
    #: When its data was acquired, as whoever added it said (ADR 0177): its file's, unless
    #: the file's own format says.
    acquired_at = models.CharField(max_length=40, blank=True, default="", db_default="")
    size = models.BigIntegerField(db_comment="Bytes, as declared.")
    sha256 = models.CharField(max_length=64, db_comment="As declared; checked when complete.")
    part_size = models.BigIntegerField(db_comment="Bytes per part; the last part may be smaller.")
    state = models.CharField(
        max_length=16, choices=UploadState.choices, default=UploadState.PENDING
    )
    kind = models.CharField(
        max_length=16,
        choices=UploadKind.choices,
        default=UploadKind.FILE,
        db_default=UploadKind.FILE,
        db_comment="What it becomes: a file, a run's artifact (ADR 0068) or its checkpoint "
        "(ADR 0069), or a bundle to import (ADR 0096).",
    )
    #: The staging object the parts go to, and the store's ID for the multipart upload.
    staging_key = models.TextField()
    multipart_id = models.TextField(blank=True, default="")
    file = models.OneToOneField(
        File, null=True, blank=True, on_delete=models.PROTECT, related_name="upload"
    )
    error = models.TextField(blank=True, default="", db_comment="Why it failed, for people.")
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(db_comment="Unfinished uploads are cancelled then.")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_upload"
        db_table_comment = "Files on their way in: sent in parts, then checked."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["created_by", "id"], name="iki_upload_by_user"),
            models.Index(
                fields=["expires_at"],
                name="iki_upload_unfinished",
                condition=Q(state__in=["pending", "checking"]),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(sha256__regex=SHA256), name="iki_upload_sha256"),
            models.CheckConstraint(condition=Q(size__gte=0), name="iki_upload_size"),
            models.CheckConstraint(condition=Q(part_size__gt=0), name="iki_upload_part_size"),
            models.CheckConstraint(
                condition=Q(state__in=[s.value for s in UploadState]), name="iki_upload_state"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=[k.value for k in UploadKind]), name="iki_upload_kind"
            ),
            # A file upload is ready once its file exists; artifacts, checkpoints and bundles
            # never have one.
            models.CheckConstraint(
                condition=Q(kind="file", state="ready", file__isnull=False)
                | Q(kind="file") & ~Q(state="ready")
                | Q(kind__in=["artifact", "checkpoint", "bundle"], file__isnull=True),
                name="iki_upload_ready_has_file",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class RegistrationState(models.TextChoices):
    #: Iki is reading the object to learn its SHA-256.
    CHECKING = "checking", "Checking"
    SCANNING = "scanning", "Scanning"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class Registration(models.Model):
    """A file registered where it already is: one version of an object in an approved bucket."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The agent that registered it for ``created_by``, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=255)
    #: When its data was acquired, as whoever registered it said (ADR 0177).
    acquired_at = models.CharField(max_length=40, blank=True, default="", db_default="")
    storage_uri = models.TextField(db_comment="Such as s3://lab-data/runs/plate-7.csv.")
    object_version = models.CharField(max_length=1024, db_comment="The version it's pinned to.")
    size = models.BigIntegerField(db_comment="Bytes, as the store reported them.")
    state = models.CharField(
        max_length=16, choices=RegistrationState.choices, default=RegistrationState.CHECKING
    )
    file = models.OneToOneField(
        File, null=True, blank=True, on_delete=models.PROTECT, related_name="registration"
    )
    error = models.TextField(blank=True, default="", db_comment="Why it failed, for people.")
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(db_comment="Still checking then, it fails.")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_registration"
        db_table_comment = "Files registered in place: an object version, read to learn its hash."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["created_by", "id"], name="iki_registration_by_user"),
            models.Index(
                fields=["expires_at"],
                name="iki_registration_unfinished",
                condition=Q(state="checking"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(size__gte=0), name="iki_registration_size"),
            models.CheckConstraint(
                condition=~Q(object_version="") & ~Q(object_version="null"),
                name="iki_registration_version",
            ),
            models.CheckConstraint(
                condition=Q(state__in=[s.value for s in RegistrationState]),
                name="iki_registration_state",
            ),
            models.CheckConstraint(
                condition=Q(state="ready", file__isnull=False) | ~Q(state="ready"),
                name="iki_registration_ready_has_file",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class BlobScan(models.Model):
    """One completed inspection of an immutable source (ADR 0216). No row means pending."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    blob = models.OneToOneField(
        Blob, null=True, on_delete=models.PROTECT, related_name="scan_result"
    )
    upload = models.OneToOneField(
        Upload, null=True, on_delete=models.PROTECT, related_name="scan_result"
    )
    state = models.CharField(max_length=16)
    scanner = models.CharField(max_length=255)
    signature_version = models.CharField(max_length=255, blank=True)
    scanned_at = models.DateTimeField(db_default=Now())
    reason = models.CharField(max_length=1024, blank=True)
    allow_unscanned = models.BooleanField(default=False)

    class Meta:
        db_table = "iki_blob_scan"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(blob__isnull=False, upload__isnull=True)
                | Q(blob__isnull=True, upload__isnull=False),
                name="iki_scan_one_source",
            ),
            models.CheckConstraint(
                condition=~Q(scanner="")
                & (
                    Q(state="clean", reason="") & ~Q(signature_version="")
                    | Q(state="infected") & ~Q(reason="") & ~Q(signature_version="")
                    | Q(state="not_scanned") & ~Q(reason="")
                ),
                name="iki_scan_evidence",
            ),
            models.CheckConstraint(
                condition=Q(allow_unscanned=False) | Q(state="not_scanned", reason="too_large"),
                name="iki_scan_size_exception",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["id"],
                condition=Q(state="infected") | Q(state="not_scanned", allow_unscanned=False),
                name="iki_scan_held",
            )
        ]

    def __str__(self) -> str:
        return f"{self.scanner}: {self.state}"


class ScanRelease(models.Model):
    """An operator's exception to a particular infected finding (ADR 0217)."""

    APPEND_ONLY = True

    decision = models.OneToOneField(
        BlobScan, primary_key=True, on_delete=models.PROTECT, related_name="release"
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=2000)
    released_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_scan_release"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(reason__regex=r"\S"), name="iki_scan_release_reason")
        ]
