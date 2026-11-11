"""Saved searches: a search of a project's entities, kept under a name (ADR 0037).

Each is its owner's, or shared with everyone who can read the project.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.org.models import Project


class SavedSearch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=100)
    #: The search, as POST /entities/search takes it, without paging.
    query = models.JSONField()
    shared = models.BooleanField(
        default=False, db_comment="Shown to everyone who can read the project, not only its owner."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_saved_search"
        db_table_comment = "Searches of a project's entities, kept under a name."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "created_by"], name="iki_saved_search_mine"),
            models.Index(
                fields=["project"], name="iki_saved_search_shared", condition=Q(shared=True)
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~Q(name__regex=r"^\s*$"), name="iki_saved_search_named"
            ),
            models.UniqueConstraint(
                fields=["project", "created_by", "name"], name="iki_saved_search_name_once"
            ),
        ]

    def __str__(self) -> str:
        return self.name
