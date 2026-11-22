"""The files each run read and made (ADR 0177), append-only."""

import django.db.models.deletion
import django.db.models.functions.datetime
from django.db import migrations, models

import iki_server.plugins.operations


class Migration(migrations.Migration):
    dependencies = [
        ("iki_files", "0009_project_files"),
        ("iki_runs", "0046_action_run_volumes"),
    ]

    operations = [
        migrations.CreateModel(
            name="RunFile",
            fields=[
                (
                    "pk",
                    models.CompositePrimaryKey(
                        "run",
                        "file",
                        "role",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("param", "A file param"),
                            ("input", "An input's file"),
                            ("made", "Made by it"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "file",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_files.file",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="files",
                        to="iki_runs.actionrun",
                    ),
                ),
            ],
            options={
                "db_table": "iki_run_file",
                "db_table_comment": "The files each run read (its params and inputs') and made.",
                "indexes": [models.Index(fields=["file", "run"], name="iki_run_file_by_file")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("role__in", ["param", "input", "made"])),
                        name="iki_run_file_role",
                    )
                ],
            },
        ),
        iki_server.plugins.operations.AppendOnly("RunFile"),
    ]
