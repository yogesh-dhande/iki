"""A project's Files (ADR 0177): when each file's data was acquired, and the indexes that
find files by name, format and acquisition, and sort them by name. Names are searched by
pg_trgm, which the entities' search made (ADR 0037)."""

import django.contrib.postgres.indexes
import django.db.models.functions.comparison
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_audit", "0005_via_trigger"),
        ("iki_entities", "0004_search"),
        ("iki_files", "0008_bundle_uploads"),
        ("iki_org", "0005_lab_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="file",
            name="acquired_at",
            field=models.CharField(
                blank=True,
                db_comment="When its data was acquired, in ISO 8601, as precisely as its source said.",
                db_default="",
                default="",
                max_length=40,
            ),
        ),
        migrations.AddField(
            model_name="file",
            name="acquired_from",
            field=models.CharField(
                blank=True,
                db_comment="Where that came from: the file's own format (such as abif or fcs), given by whoever added it, or empty if unknown.",
                db_default="",
                default="",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="file",
            name="acquired_on",
            field=models.DateField(
                blank=True,
                db_comment="The day its data was acquired, as its source said; null if unknown.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="registration",
            name="acquired_at",
            field=models.CharField(blank=True, db_default="", default="", max_length=40),
        ),
        migrations.AddField(
            model_name="upload",
            name="acquired_at",
            field=models.CharField(blank=True, db_default="", default="", max_length=40),
        ),
        migrations.AddIndex(
            model_name="file",
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(
                    django.db.models.functions.text.Upper("name"), name="gin_trgm_ops"
                ),
                name="iki_file_name_trgm",
            ),
        ),
        migrations.AddIndex(
            model_name="file",
            index=models.Index(fields=["project", "media_type", "id"], name="iki_file_by_format"),
        ),
        migrations.AddIndex(
            model_name="file",
            index=models.Index(
                fields=["project", "acquired_on", "id"], name="iki_file_by_acquired"
            ),
        ),
        migrations.AddIndex(
            model_name="file",
            index=models.Index(
                models.F("project"),
                models.OrderBy(models.F("acquired_on"), descending=True, nulls_last=True),
                models.OrderBy(models.F("id"), descending=True),
                name="iki_file_by_last_acquired",
            ),
        ),
        migrations.AddIndex(
            model_name="file",
            index=models.Index(
                models.F("project"),
                django.db.models.functions.comparison.Collate(
                    django.db.models.functions.text.Lower("name"), "C"
                ),
                models.F("id"),
                name="iki_file_by_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="file",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("acquired_at", ""), ("acquired_from", ""), ("acquired_on__isnull", True)
                    ),
                    models.Q(
                        (
                            "acquired_at__regex",
                            "^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\\.[0-9]{1,9})?)?(Z|[+-][0-9]{2}:[0-9]{2})?)?$",
                        ),
                        ("acquired_from__regex", "^[a-z][a-z0-9-]{0,15}$"),
                        ("acquired_on__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="iki_file_acquired",
            ),
        ),
    ]
