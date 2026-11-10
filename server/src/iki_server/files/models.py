"""Files: their bytes (blobs), the files people add (with a name and type), and uploads.

- A **blob** is content. Iki's own are content-addressed: stored once per
  SHA-256, under a key only the server writes, after it checked the bytes. A
  registered one is an object where it already was, pinned to an object
  version so it can't change underneath its entities.
- A **file** is a blob as someone added it: its name, its media type, the
  project it was added to, who added it and when. Two files can share a blob.
- An **upload** sends a file's bytes straight to object storage, in parts,
  and is checked before it becomes a file (ADR 0036).

Blobs and files never change: the app may only add and read them.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.org.models import Project

SHA256 = r"^[0-9a-f]{64}$"


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
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_file"
        db_table_comment = "Files people added: a name and media type for a blob's bytes."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_file_by_project"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=~Q(name=""), name="iki_file_name"),
            models.CheckConstraint(
                condition=Q(media_type__regex=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$"),
                name="iki_file_media_type",
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


class UploadState(models.TextChoices):
    #: Waiting for its parts.
    PENDING = "pending", "Waiting for its parts"
    #: All parts arrived; Iki is checking the bytes.
    CHECKING = "checking", "Checking"
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
    name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=255)
    size = models.BigIntegerField(db_comment="Bytes, as declared.")
    sha256 = models.CharField(max_length=64, db_comment="As declared; checked when complete.")
    part_size = models.BigIntegerField(db_comment="Bytes per part; the last part may be smaller.")
    state = models.CharField(
        max_length=16, choices=UploadState.choices, default=UploadState.PENDING
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
                condition=Q(state="ready", file__isnull=False) | ~Q(state="ready"),
                name="iki_upload_ready_has_file",
            ),
        ]

    def __str__(self) -> str:
        return self.name
