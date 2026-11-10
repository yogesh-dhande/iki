"""The core entity row (ADR 0007).

Every entity, of any type, has one ``Entity`` row: its identity and
bookkeeping. Plugin types are generated models that extend it with
multi-table inheritance, so a protein is an ``iki_entity`` row joined to a
``protein_protein`` row by ``entity_id``.
"""

from typing import Any, ClassVar

from django.conf import settings
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

    class Meta:
        db_table = "iki_entity"
        db_table_comment = "Every entity, of every type. Each type's own table joins on entity_id."
        verbose_name_plural = "entities"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["type", "created_at"], name="iki_entity_type"),
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
