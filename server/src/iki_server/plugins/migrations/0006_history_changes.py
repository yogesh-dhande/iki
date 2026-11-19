import django.db.models.functions.datetime
from django.db import migrations, models

import iki_server.plugins.operations


class Migration(migrations.Migration):
    """What plugin migrations do to kept history, so a signature made before reads its
    record through it (ADR 0148). Append-only for every role, the table's owner included.
    Migrations that record a change sign it with the deployment's signing key, so they come
    after its table."""

    dependencies = [
        ("iki_entities", "0002_history_append_only"),
        ("iki_plugins", "0005_purged_state"),
        ("iki_signatures", "0001_signatures"),
    ]

    operations = [
        migrations.CreateModel(
            name="HistoryChange",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("plugin", models.CharField(max_length=100)),
                ("backwards", models.BooleanField(default=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("rename", "A field renamed"),
                            ("convert", "A quantity converted to another unit"),
                            ("retype", "A type renamed"),
                        ],
                        max_length=10,
                    ),
                ),
                ("part", models.CharField(blank=True, default="", max_length=255)),
                ("name", models.CharField(max_length=255)),
                ("to", models.CharField(max_length=255)),
                ("from_unit", models.CharField(blank=True, default="", max_length=100)),
                ("to_unit", models.CharField(blank=True, default="", max_length=100)),
                ("scale", models.FloatField(blank=True, null=True)),
                ("offset", models.FloatField(blank=True, null=True)),
                (
                    "made_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                ("key", models.CharField(blank=True, default="", max_length=64)),
                ("signature", models.BinaryField(blank=True, default=b"")),
            ],
            options={
                "db_table": "iki_history_change",
                "db_table_comment": "What plugin migrations did to kept history: fields renamed, units converted, types renamed (ADR 0148).",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("kind__in", ["rename", "convert", "retype"])),
                        name="iki_history_change_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("kind", "convert"), _negated=True),
                            models.Q(
                                ("from_unit__gt", ""),
                                ("offset__isnull", False),
                                ("scale__isnull", False),
                                ("to_unit__gt", ""),
                            ),
                            _connector="OR",
                        ),
                        name="iki_history_change_conversion",
                    ),
                ],
            },
        ),
        iki_server.plugins.operations.AppendOnly("HistoryChange"),
    ]
