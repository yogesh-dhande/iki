"""What a file says about itself besides when (ADR 0224): its facts, a SAM or BAM file's
read groups, and the text the Files list finds them by, through pg_trgm as names are."""

import django.contrib.postgres.indexes
import django.db.models.functions.text
import django.db.models.lookups
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_files", "0009_project_files"),
    ]

    operations = [
        migrations.AddField(
            model_name="file",
            name="facts",
            field=models.JSONField(
                db_comment="What the file states about its instrument, operator, software and protocol, each with where it states it (ADR 0224); {} if it states none.",
                db_default=models.Value({}, models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AddField(
            model_name="file",
            name="read_groups",
            field=models.JSONField(
                db_comment="A SAM or BAM file's @RG lines, their standard tags; [] if none.",
                db_default=models.Value([], models.JSONField()),
                default=list,
            ),
        ),
        migrations.AddField(
            model_name="file",
            name="stated_text",
            field=models.TextField(
                blank=True,
                db_comment="The facts' values and read groups' names the Files list searches.",
                db_default="",
                default="",
            ),
        ),
        migrations.AddIndex(
            model_name="file",
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(
                    django.db.models.functions.text.Upper("stated_text"), name="gin_trgm_ops"
                ),
                name="iki_file_stated_trgm",
            ),
        ),
        migrations.AddConstraint(
            model_name="file",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    django.db.models.lookups.Exact(
                        models.Func(
                            models.F("facts"),
                            function="jsonb_typeof",
                            output_field=models.TextField(),
                        ),
                        "object",
                    ),
                    django.db.models.lookups.Exact(
                        models.Func(
                            models.F("read_groups"),
                            function="jsonb_typeof",
                            output_field=models.TextField(),
                        ),
                        "array",
                    ),
                ),
                name="iki_file_stated",
            ),
        ),
    ]
