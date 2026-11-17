"""Run labels (ADR 0112): what a self-hosted machine must have to run a run, on the run
and its jobs, and the labels a job's worker had when it leased it. What a lease weighs
gains the labels (ADR 0108): the new index is built concurrently before the old one goes,
so workers go on leasing from an indexed queue meanwhile."""

from typing import Any

import django.contrib.postgres.fields
from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations, models


# Quoted: Python before 3.14 evaluates annotations, and ArrayField is generic only in its stubs.
def _labels() -> "django.contrib.postgres.fields.ArrayField[Any, Any]":
    return django.contrib.postgres.fields.ArrayField(
        base_field=models.CharField(max_length=50), blank=True, default=list
    )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_org", "0003_legal_hold"),
        ("iki_runs", "0035_self_hosted_workers"),
    ]

    operations = [
        migrations.AddField(model_name="actionrun", name="labels", field=_labels()),
        migrations.AddField(model_name="job", name="labels", field=_labels()),
        migrations.AddField(model_name="job", name="worker_labels", field=_labels()),
        AddIndexConcurrently(
            model_name="job",
            index=models.Index(
                models.F("tier"),
                models.F("project"),
                models.F("labels"),
                models.OrderBy(models.F("priority"), descending=True),
                models.F("queued_at"),
                models.F("id"),
                condition=models.Q(("state", "queued")),
                name="iki_job_queued_by_labels",
            ),
        ),
        RemoveIndexConcurrently(model_name="job", name="iki_job_queued_by_project"),
    ]
