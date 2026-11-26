import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models

import iki_server.core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("iki_audit", "0005_via_trigger"),
        ("iki_entities", "0027_migrated_revisions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RecordExport",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=iki_server.core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("via", models.CharField(max_length=16)),
                ("format", models.CharField(max_length=40)),
                ("selection", models.TextField()),
                ("search", models.JSONField(blank=True, null=True)),
                ("records", models.JSONField()),
                (
                    "fields",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=100), blank=True, default=list
                    ),
                ),
                (
                    "projects",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), blank=True, default=list
                    ),
                ),
                (
                    "read_in",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), blank=True, default=list
                    ),
                ),
                (
                    "references",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.UUIDField(), blank=True, null=True
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("writing", "Writing"),
                            ("ready", "Ready"),
                            ("failed", "Failed"),
                            ("expired", "Expired"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("error", models.TextField(blank=True, default="")),
                ("done", models.PositiveIntegerField(default=0)),
                ("name", models.CharField(db_comment="The file's name.", max_length=200)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                ("size", models.BigIntegerField(blank=True, db_comment="Bytes.", null=True)),
                ("notes", models.JSONField(blank=True, default=dict)),
                ("refused", models.JSONField(blank=True, default=dict)),
                ("writer", models.JSONField(blank=True, default=dict)),
                (
                    "created_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_audit.agent",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "iki_record_export",
                "db_table_comment": "Many records asked for as one file, such as FASTA or SDF.",
                "indexes": [
                    models.Index(
                        fields=["requested_by", "-created_at"], name="iki_record_export_by"
                    ),
                    models.Index(
                        condition=models.Q(("state", "ready")),
                        fields=["expires_at"],
                        name="iki_record_export_expiring",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("state__in", ["queued", "writing", "ready", "failed", "expired"])
                        ),
                        name="iki_record_export_state",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("state", "ready"), _negated=True),
                            ("sha256__regex", "^[0-9a-f]{64}$"),
                            _connector="OR",
                        ),
                        name="iki_record_export_ready_sha256",
                    ),
                ],
            },
        ),
    ]
