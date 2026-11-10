"""Blobs: the files entities' file fields point to.

A blob is either content Iki stored itself (content-addressed by SHA-256) or
an object registered where it already is, pinned to an object version so it
can't change underneath its entities.
"""

from typing import ClassVar

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7


class Blob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    sha256 = models.CharField(max_length=64)
    size = models.BigIntegerField(db_comment="Bytes.")
    media_type = models.CharField(max_length=255)
    storage_uri = models.TextField(db_comment="Where the bytes are, such as s3://bucket/key.")
    #: Registered where it already was, rather than uploaded to Iki's bucket.
    registered_external = models.BooleanField(default=False)
    object_version = models.CharField(max_length=1024, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_blob"
        db_table_comment = "Files, in object storage, that entities' file fields point to."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(sha256__regex=r"^[0-9a-f]{64}$"), name="iki_blob_sha256"
            ),
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
        return f"{self.sha256[:12]} ({self.media_type})"
