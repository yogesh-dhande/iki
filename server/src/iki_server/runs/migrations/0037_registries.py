"""Registries (ADR 0115): a bulk import counts the registry entries it registers, in place
of the entities it linked into the run's project, which no longer happens; and a run pins
the revision of each record's entry it reads, as the entry changes without the record."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_runs", "0036_run_labels"),
    ]

    operations = [
        migrations.RemoveField(model_name="runimport", name="linked"),
        migrations.AddField(
            model_name="runimport",
            name="registered",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="runinput",
            name="entry_revision",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="runinclusion",
            name="entry_revision",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
