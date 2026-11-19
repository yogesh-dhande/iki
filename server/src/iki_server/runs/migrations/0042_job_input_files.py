"""A job keeps the files its run input listed when it was leased: its worker downloads
those, packed runs' and included references' too, and no others."""

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_runs", "0041_run_output_per_attempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="input_files",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.UUIDField(), blank=True, default=list
            ),
        ),
    ]
