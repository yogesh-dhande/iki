"""A run's effects in the order it wrote them, a page at a time (ADR 0056). Built
concurrently: the table may be large, and writes to it go on meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_runs", "0013_bulk_imports"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="runeffect",
            index=models.Index(fields=["run", "id"], name="iki_run_effect_run"),
        ),
    ]
