"""Searching everything (ADR 0176): a run by the start of its HRID, and the runs someone
launched lately. Built concurrently: runs go on being launched meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("iki_runs", "0044_run_effect_entries")]

    operations = [
        AddIndexConcurrently(
            model_name="actionrun",
            index=models.Index(
                fields=["hrid"],
                name="iki_action_run_hrid_prefix",
                opclasses=["varchar_pattern_ops"],
            ),
        ),
        AddIndexConcurrently(
            model_name="actionrun",
            index=models.Index(fields=["requested_by", "id"], name="iki_action_run_requester"),
        ),
    ]
