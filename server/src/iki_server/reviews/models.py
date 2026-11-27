"""Review procedures as the deployment keeps them, and who chose which (ADRs 0301, 0302).

Each installed procedure version is kept with its flows' states, synced from its
plugin's manifest on every deploy (:mod:`iki_server.reviews.sync`). A reviewed record's
``review_procedure``, ``review_flow`` and review state are a foreign key to
:class:`FlowState`, so the database refuses a state that isn't in the record's pinned
flow, and refuses to remove a version a record pins.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q

from iki_server.core.ids import uuid7


class Procedure(models.Model):
    """An installed review procedure version, as its plugin declared it. Never changed:
    a plugin changes a procedure in a new version."""

    #: ``plugin.name@version``: ``iki/core.result_review@1``.
    qualified = models.CharField(max_length=160, primary_key=True)
    plugin = models.CharField(max_length=100)
    name = models.CharField(max_length=63)
    version = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=200)
    #: The procedure as its manifest declared it (``plugin_to_dict``'s shape).
    definition = models.JSONField()
    #: SHA-256 of the canonical definition, which a later deploy must match.
    digest = models.CharField(max_length=64)
    installed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_review_procedure"
        db_table_comment = "Installed review procedure versions (ADR 0301). Never changed."

    def __str__(self) -> str:
        return self.qualified


class Flow(models.Model):
    """A flow of a procedure version: what ``review_flow`` names."""

    id = models.BigAutoField(primary_key=True)
    procedure = models.ForeignKey(Procedure, on_delete=models.PROTECT, related_name="flows")
    name = models.CharField(max_length=63)
    label = models.CharField(max_length=200)

    class Meta:
        db_table = "iki_review_flow"
        db_table_comment = "Each installed review procedure version's flows (ADR 0301)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["procedure", "name"], name="iki_review_flow_once")
        ]

    def __str__(self) -> str:
        return f"{self.procedure_id} {self.name}"


class FlowState(models.Model):
    """A state a record pinned to a flow can be in."""

    id = models.BigAutoField(primary_key=True)
    procedure = models.ForeignKey(Procedure, on_delete=models.PROTECT, related_name="+")
    flow = models.CharField(max_length=63)
    state = models.CharField(max_length=50)
    locks = models.BooleanField()

    class Meta:
        db_table = "iki_review_flow_state"
        db_table_comment = "The states of each installed review flow (ADR 0301)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["procedure", "flow", "state"], name="iki_review_flow_state_once"
            )
        ]

    def __str__(self) -> str:
        return f"{self.procedure_id} {self.flow} {self.state}"


class Choice(models.Model):
    """A choice of procedure and flow for a type's new records (ADR 0302): a project's
    (``project`` set), or the deployment's default for every project that hasn't chosen
    (``project`` null). A change supersedes the current choice, which is kept, so each
    record's binding stays what its procedure was chosen with."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(
        "iki_org.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    #: The type, qualified; the choice applies to its subtypes that have none of their own.
    entity_type = models.CharField(max_length=200)
    procedure = models.ForeignKey(Procedure, on_delete=models.PROTECT, related_name="+")
    flow = models.CharField(max_length=63)
    #: Each of the procedure's slots, to the qualified lab role that plays it (ADR 0304).
    binding = models.JSONField(default=dict)
    #: The starter it was applied from, if any (ADR 0303).
    starter = models.CharField(max_length=200, blank=True)
    reason = models.TextField(blank=True)
    chosen_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    chosen_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_review_choice"
        db_table_comment = (
            "Which review procedure and flow each type's new records are pinned to, by "
            "project, or the deployment's default (ADR 0302)."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "entity_type"],
                condition=Q(superseded_at__isnull=True),
                nulls_distinct=False,
                name="iki_review_choice_current",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity_type", "procedure"], name="iki_review_choice_binding")
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}: {self.procedure_id} {self.flow}"
