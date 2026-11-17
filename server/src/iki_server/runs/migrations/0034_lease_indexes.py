"""What a lease weighs (ADR 0043): each project's best queued jobs on a tier, and how many
each project has running. Built concurrently: the table may be large, and workers go on
leasing from it meanwhile."""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_runs", "0033_makings"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="job",
            index=models.Index(
                models.F("tier"),
                models.F("project"),
                models.OrderBy(models.F("priority"), descending=True),
                models.F("queued_at"),
                models.F("id"),
                condition=models.Q(("state", "queued")),
                name="iki_job_queued_by_project",
            ),
        ),
        AddIndexConcurrently(
            model_name="job",
            index=models.Index(
                condition=models.Q(
                    ("packed_into__isnull", True),
                    ("state__in", ["leased", "staging", "running", "uploading"]),
                ),
                fields=["project"],
                name="iki_job_held",
            ),
        ),
    ]
