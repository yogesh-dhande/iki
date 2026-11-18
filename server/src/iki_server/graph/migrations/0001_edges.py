"""The graph's edges (ADR 0123), and the triggers that keep them: runs' from what runs
recorded, records' from their entries. Plugins' references get theirs from
``sync_plugins``, which fills them in too."""

import django.db.models.deletion
from django.db import migrations, models

from iki_server.graph import sql

#: What runs and records said before the triggers were there.
BACKFILL = ";\n".join(
    [
        sql.backfill(sql.LINEAGE, "iki_lineage"),
        sql.backfill(sql.EFFECTS, "iki_run_effect"),
        sql.backfill(sql.RECORDS, "iki_entity"),
    ]
)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("iki_entities", "0012_states"),
        ("iki_files", "0008_bundle_uploads"),
        ("iki_runs", "0037_registries"),
    ]

    operations = [
        migrations.CreateModel(
            name="Edge",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("reference", "Reference"),
                            ("run", "Run"),
                            ("file", "File read by a run"),
                            ("record", "Record of an entry"),
                        ],
                        max_length=16,
                    ),
                ),
                ("field", models.CharField(blank=True, db_default="", default="", max_length=127)),
                ("source", models.CharField(blank=True, db_default="", default="", max_length=127)),
                ("position", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "revision",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "file",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="iki_files.file",
                    ),
                ),
                (
                    "node",
                    models.ForeignKey(
                        db_comment="What depends.",
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "on",
                    models.ForeignKey(
                        db_comment="What it depends on.",
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="iki_runs.actionrun",
                    ),
                ),
            ],
            options={
                "db_table": "iki_graph_edge",
                "db_table_comment": "What depends on what (ADR 0123): references, runs, files runs read, and records of entries. Kept by triggers; the app only reads it.",
                "indexes": [
                    models.Index(fields=["node", "kind", "on"], name="iki_graph_edge_up"),
                    models.Index(fields=["on", "kind", "node"], name="iki_graph_edge_down"),
                    models.Index(
                        condition=models.Q(("kind", "reference")),
                        fields=["source", "node", "position"],
                        name="iki_graph_edge_source",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["reference", "run", "file", "record"])),
                        name="iki_graph_edge_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("kind", "reference"), ("source__gt", "")),
                            models.Q(
                                models.Q(("kind", "reference"), _negated=True), ("source", "")
                            ),
                            _connector="OR",
                        ),
                        name="iki_graph_edge_sourced",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("run__isnull", False),
                            models.Q(("kind__in", ["run", "file"]), _negated=True),
                            _connector="OR",
                        ),
                        name="iki_graph_edge_run",
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql.CORE + sql.REFS, sql.DROP_CORE + sql.DROP_REFS),
        migrations.RunSQL(BACKFILL, migrations.RunSQL.noop),
    ]
