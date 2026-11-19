"""The graph's edges (ADR 0123): each one says that one entity depends on another, and why.

An edge runs from the entity that depends (``node``) to the one it depends on (``on``):

- **reference**: ``node``'s ref field (one, or an item of a list) names ``on``. Kept by
  triggers on the plugins' tables, which ``sync_plugins`` installs, so whatever writes a
  reference (a person, a run, a bulk import, a bundle or SQL) writes its edge, and
  changing or clearing it takes the edge away. References are as they are now.
- **run**: a run made ``node`` from its input ``on`` (from ``iki_lineage``, and what the
  input's references included, ADR 0105), or updated ``node`` and read ``on`` beside it.
- **file**: a run made or updated ``node`` from a file param, which ``on`` held, or which
  a run that read ``on`` wrote.
- **record**: ``node`` is a project's record of the registry entry ``on``, or the entry
  of the record ``on`` (ADR 0115): both ways, since a record and its entry are one thing.

Runs' edges are as they happened. Triggers in ``iki_graph``'s migration keep them from
``iki_lineage``, ``iki_run_effect`` and ``iki_entity``, so they're written in the same
statement as what they come from. The app's role only reads the table: its triggers are
the migrator's (``SECURITY DEFINER``), so no one can write an edge that didn't happen.
"""

from typing import ClassVar

from django.db import models
from django.db.models import Q

from iki_server.entities.models import Entity


class EdgeKind(models.TextChoices):
    REFERENCE = "reference", "Reference"
    RUN = "run", "Run"
    FILE = "file", "File read by a run"
    RECORD = "record", "Record of an entry"
    #: Split from a record's amount (ADR 0139): an aliquot depends on what it was split from.
    ALIQUOT = "aliquot", "Aliquot of"
    #: Some of its amount came from another record.
    TRANSFER = "transfer", "Transferred from"


class Edge(models.Model):
    """``node`` depends on ``on``: why is its ``kind``, its ``field`` and its ``run``."""

    id = models.BigAutoField(primary_key=True)
    node = models.ForeignKey(
        Entity, on_delete=models.DO_NOTHING, related_name="+", db_comment="What depends."
    )
    on = models.ForeignKey(
        Entity, on_delete=models.DO_NOTHING, related_name="+", db_comment="What it depends on."
    )
    kind = models.CharField(max_length=16, choices=EdgeKind.choices)
    #: A reference's field; a run's input (``antibody.components.part`` for what it included);
    #: a file param's name; empty for a record.
    field = models.CharField(max_length=127, blank=True, default="", db_default="")
    #: For a reference: the column (``table.column``) or list table that holds it, by which
    #: its triggers keep it. Empty for the rest.
    source = models.CharField(max_length=127, blank=True, default="", db_default="")
    #: For an item of a list of references: its place.
    position = models.PositiveIntegerField(null=True, blank=True)
    run = models.ForeignKey(
        "iki_runs.ActionRun",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
    )
    #: For a run's input: its revision the run read. Empty for a file a record holds, and
    #: for a reference, whose target is current.
    revision = models.PositiveIntegerField(null=True, blank=True)
    #: For a file edge: the file the run read.
    file = models.ForeignKey(
        "iki_files.File", null=True, blank=True, on_delete=models.DO_NOTHING, related_name="+"
    )

    class Meta:
        db_table = "iki_graph_edge"
        db_table_comment = (
            "What depends on what (ADR 0123): references, runs, files runs read, and records of "
            "entries. Kept by triggers; the app only reads it."
        )
        indexes: ClassVar[list[models.Index]] = [
            # Upstream: what a node depends on.
            models.Index(fields=["node", "kind", "on"], name="iki_graph_edge_up"),
            # Downstream: what depends on a node.
            models.Index(fields=["on", "kind", "node"], name="iki_graph_edge_down"),
            # The triggers find a reference's edge by where it's held.
            models.Index(
                fields=["source", "node", "position"],
                name="iki_graph_edge_source",
                condition=Q(kind="reference"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(kind__in=EdgeKind.values), name="iki_graph_edge_kind"
            ),
            models.CheckConstraint(
                condition=Q(kind="reference", source__gt="") | ~Q(kind="reference") & Q(source=""),
                name="iki_graph_edge_sourced",
            ),
            models.CheckConstraint(
                condition=Q(run__isnull=False) | ~Q(kind__in=["run", "file"]),
                name="iki_graph_edge_run",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.node_id} -{self.kind}-> {self.on_id}"
