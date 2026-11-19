"""A run keeps every origin an entity has in it: one molecule is in several databases'
records, and a run that reads two of them keeps both (ADR 0154)."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("iki_runs", "0042_job_input_files"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="externalorigin",
            name="iki_external_origin_once",
        ),
    ]
