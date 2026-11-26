import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0054_append_only_run_records")]

    operations = [
        migrations.AddField(
            model_name="job",
            name="scan_wait_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="job",
            name="scan_wait_ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="job",
            name="scan_wait_files",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.UUIDField(), blank=True, default=list, size=None
            ),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(
                condition=models.Q(
                    ("scan_wait_ended_at__isnull", True), ("scan_wait_started_at__isnull", False)
                ),
                fields=["scan_wait_started_at"],
                name="iki_job_waiting_for_scan",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scan_wait_ended_at__isnull", True),
                    models.Q(
                        ("scan_wait_started_at__isnull", False),
                        ("scan_wait_started_at__lte", models.F("scan_wait_ended_at")),
                    ),
                    _connector="OR",
                ),
                name="iki_job_scan_wait_order",
            ),
        ),
    ]
