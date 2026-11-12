"""Finding a run to reuse, and the runs that reused or reproduce one (ADR 0057). Built
concurrently: the table may be large, and runs go on being launched meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_runs", "0015_reuse_and_reproduction"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="actionrun",
            index=models.Index(
                condition=models.Q(
                    ("status", "succeeded"),
                    models.Q(("reuse_key", ""), _negated=True),
                    ("reproduces__isnull", True),
                    ("reused_from__isnull", True),
                ),
                fields=["reuse_key", "-finished_at"],
                name="iki_action_run_reusable",
            ),
        ),
        AddIndexConcurrently(
            model_name="actionrun",
            index=models.Index(
                condition=models.Q(("reused_from__isnull", False)),
                fields=["reused_from", "id"],
                name="iki_action_run_reuses",
            ),
        ),
        AddIndexConcurrently(
            model_name="actionrun",
            index=models.Index(
                condition=models.Q(("reproduces__isnull", False)),
                fields=["reproduces", "id"],
                name="iki_action_run_reproductions",
            ),
        ),
    ]
