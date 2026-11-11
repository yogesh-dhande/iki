"""The core entity row, its revisions, and the projects it belongs to (ADR 0007, ADR 0035).

Every entity, of any type, has one ``Entity`` row: its identity and
bookkeeping. Plugin types are generated models that extend it with
multi-table inheritance, so a protein is an ``iki_entity`` row joined to a
``protein_protein`` row by ``entity_id``.

Every change to an entity is a numbered :class:`Revision`: who made it, how,
when, why, and which fields it changed. The values at each revision are in
the plugin tables' history tables. Both are append-only.
"""

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.entities import hrids, identity, registry
from iki_server.org.models import Project

#: A qualified type name, ``plugin.Type``.
TYPE_PATTERN = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*\.[A-Z][A-Za-z0-9]*$"
#: ``PRT-000123``: a type's prefix and a number, zero-padded to the type's digits.
HRID_PATTERN = r"^[A-Z0-9]{2,8}-[0-9]{3,}$"


class Entity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: The entity's own type, the most specific one, such as ``protein.Protein``.
    type = models.CharField(max_length=81, db_comment="The entity's type, such as protein.Protein.")
    hrid = models.CharField(
        max_length=32, unique=True, db_comment="Human-readable ID, such as PRT-000123."
    )
    owner_project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    archived_at = models.DateTimeField(null=True, blank=True)
    #: SHA-256 of the type's identity fields, for finding duplicates.
    identity_hash = models.CharField(max_length=64, null=True, blank=True)
    #: The latest revision's number. Edits name it, so two people's edits can't cross.
    revision = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_entity"
        db_table_comment = "Every entity, of every type. Each type's own table joins on entity_id."
        verbose_name_plural = "entities"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["type", "created_at"], name="iki_entity_type"),
            models.Index(fields=["type", "updated_at", "id"], name="iki_entity_updated"),
            GinIndex(fields=["hrid"], opclasses=["gin_trgm_ops"], name="iki_entity_hrid_trgm"),
            models.Index(
                fields=["identity_hash"],
                name="iki_entity_identity",
                condition=Q(identity_hash__isnull=False),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(type__regex=TYPE_PATTERN), name="iki_entity_type"),
            models.CheckConstraint(condition=Q(hrid__regex=HRID_PATTERN), name="iki_entity_hrid"),
            models.CheckConstraint(
                condition=Q(identity_hash__regex=r"^[0-9a-f]{64}$"),
                name="iki_entity_identity_hash",
            ),
        ]

    def __str__(self) -> str:
        return self.hrid

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Django's signature
        """A new entity of a plugin type gets its type, HRID and identity hash here."""
        iki_type = getattr(type(self), "IKI_TYPE", None)
        if self._state.adding and iki_type is not None:
            entity_type = registry.types()[iki_type]
            if entity_type.abstract:
                raise ValueError(f"{iki_type} is abstract: create one of its subtypes.")
            if self.type and self.type != iki_type:
                raise ValueError(f"A {iki_type} can't have the type {self.type}.")
            self.type = iki_type
            if not self.hrid:
                self.hrid = hrids.next_hrid(entity_type)
            if self.identity_hash is None:
                self.identity_hash = identity.hash_for(self, entity_type)
        super().save(*args, **kwargs)


class ProjectLink(models.Model):
    """A project an entity belongs to. Its owner project has one too.

    Reading an entity takes a role in any of its projects. Changing it takes
    Editor in its owner project. An entity reused from another project (a
    duplicate) is linked into the new one rather than copied.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="links")
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    linked_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_entity_project"
        db_table_comment = "The projects each entity belongs to, its owner project included."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "project"], name="iki_entity_project_once"),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "entity"], name="iki_entity_project_by_project"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} in {self.project_id}"


class RevisionKind(models.TextChoices):
    CREATED = "created", "Created"
    EDITED = "edited", "Edited"
    ARCHIVED = "archived", "Archived"
    RESTORED = "restored", "Restored"


class Revision(models.Model):
    """One change to an entity. Append-only, for every role (ADR 0035).

    A field's provenance is the latest revision that changed it: who or what
    set its current value, how, and when.
    """

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="revisions")
    #: 1 for the entity's creation, then 2, 3, ...
    number = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=RevisionKind.choices)
    #: The fields whose values this revision set.
    changed_fields = ArrayField(models.CharField(max_length=63), default=list, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    #: How the change was made: web, api, mcp, cli or system (as in the audit log).
    via = models.CharField(max_length=16)
    #: Why, if the person said.
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_revision"
        db_table_comment = "Every change to every entity: who, how, when, why, and which fields."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "number"], name="iki_revision_number"),
            models.CheckConstraint(condition=Q(number__gte=1), name="iki_revision_number_min"),
            models.CheckConstraint(
                condition=Q(kind__in=RevisionKind.values), name="iki_revision_kind"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} r{self.number}"
