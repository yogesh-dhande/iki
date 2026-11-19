"""Each attempt keeps its own output (ADR 0044, 0130): an attempt that parked on an outside
API's job has one, and the attempt that resumes the run another, so outputs are keyed by
attempt, not by run. Rows are kept: each run had one output, so each job has at most one.

The run stops being the primary key before the job becomes it: a table has only one."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_runs", "0040_slurm"),
    ]

    operations = [
        migrations.AlterModelTableComment(
            name="runoutput",
            table_comment="Each attempt's output document, as its worker sent it.",
        ),
        migrations.AlterField(
            model_name="runoutput",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="outputs",
                to="iki_runs.actionrun",
            ),
        ),
        migrations.AlterField(
            model_name="runoutput",
            name="job",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                primary_key=True,
                related_name="output",
                serialize=False,
                to="iki_runs.job",
            ),
        ),
    ]
