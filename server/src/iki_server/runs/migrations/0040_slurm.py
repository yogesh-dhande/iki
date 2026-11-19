"""Slurm clusters as a compute target (ADR 0133): the jobs a cluster runs, the account
each project's are charged to there, and what a Slurm job costs."""

import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models

import iki_server.core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("iki_org", "0005_lab_roles"),
        ("iki_runs", "0039_outside_apis"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SlurmAccount",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=iki_server.core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("cluster", models.CharField(max_length=63)),
                ("account", models.CharField(max_length=64)),
                (
                    "set_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
            ],
            options={
                "db_table": "iki_slurm_account",
                "db_table_comment": "Each project's Slurm account on each cluster.",
            },
        ),
        migrations.CreateModel(
            name="SlurmJob",
            fields=[
                (
                    "job",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="slurm",
                        serialize=False,
                        to="iki_runs.job",
                    ),
                ),
                ("cluster", models.CharField(max_length=63)),
                ("partition", models.CharField(max_length=64)),
                ("account", models.CharField(max_length=64)),
                ("qos", models.CharField(blank=True, default="", max_length=64)),
                ("requested", models.JSONField(default=dict)),
                ("slurm_id", models.CharField(blank=True, default="", max_length=32)),
                ("state", models.CharField(blank=True, default="", max_length=32)),
                ("reason", models.CharField(blank=True, default="", max_length=200)),
                ("nodes", models.CharField(blank=True, default="", max_length=500)),
                ("exit_code", models.CharField(blank=True, default="", max_length=16)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("usage", models.JSONField(blank=True, null=True)),
                ("image_signer", models.CharField(blank=True, default="", max_length=500)),
                (
                    "updated_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
            ],
            options={
                "db_table": "iki_slurm_job",
                "db_table_comment": "The Slurm job each attempt on a Slurm cluster ran as.",
            },
        ),
        migrations.RemoveConstraint(
            model_name="costrecord",
            name="iki_cost_whose",
        ),
        migrations.RemoveConstraint(
            model_name="job",
            name="iki_job_state",
        ),
        migrations.RemoveIndex(
            model_name="job",
            name="iki_job_held",
        ),
        migrations.AddField(
            model_name="registrationtoken",
            name="slurm_cluster",
            field=models.CharField(blank=True, default="", max_length=63),
        ),
        migrations.AddField(
            model_name="worker",
            name="slurm_cluster",
            field=models.CharField(blank=True, default="", max_length=63),
        ),
        migrations.AlterField(
            model_name="job",
            name="state",
            field=models.CharField(
                choices=[
                    ("awaiting_approval", "Awaiting approval"),
                    ("queued", "Queued"),
                    ("leased", "Leased"),
                    ("staging", "Staging inputs"),
                    ("pending", "Waiting in the cluster's queue"),
                    ("running", "Running"),
                    ("uploading", "Uploading results"),
                    ("committing", "Committing results"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                    ("timed_out", "Timed out"),
                    ("rejected", "Rejected"),
                    ("interrupted", "Interrupted"),
                    ("parked", "Parked"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(
                condition=models.Q(
                    ("packed_into__isnull", True),
                    ("state__in", ["leased", "staging", "pending", "running", "uploading"]),
                ),
                fields=["project"],
                name="iki_job_held",
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        [
                            "awaiting_approval",
                            "queued",
                            "leased",
                            "staging",
                            "pending",
                            "running",
                            "uploading",
                            "committing",
                            "succeeded",
                            "failed",
                            "cancelled",
                            "timed_out",
                            "rejected",
                            "interrupted",
                            "parked",
                        ],
                    )
                ),
                name="iki_job_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="registrationtoken",
            constraint=models.CheckConstraint(
                condition=models.Q(("tier", "container"), ("slurm_cluster", ""), _connector="OR"),
                name="iki_worker_registration_slurm",
            ),
        ),
        migrations.AddConstraint(
            model_name="worker",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("slurm_cluster", ""),
                    models.Q(("launch__isnull", True), ("tier", "container")),
                    _connector="OR",
                ),
                name="iki_worker_slurm",
            ),
        ),
        migrations.AddField(
            model_name="slurmaccount",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="+", to="iki_org.project"
            ),
        ),
        migrations.AddField(
            model_name="slurmaccount",
            name="set_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="slurmjob",
            index=models.Index(fields=["cluster", "slurm_id"], name="iki_slurm_job_id"),
        ),
        migrations.AddField(
            model_name="costrecord",
            name="slurm_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="iki_runs.slurmjob",
            ),
        ),
        migrations.AddConstraint(
            model_name="costrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("call__isnull", True),
                        ("kind", "run"),
                        ("project__isnull", False),
                        ("run__isnull", False),
                        models.Q(
                            models.Q(("launch__isnull", False), ("slurm_job__isnull", True)),
                            models.Q(("launch__isnull", True), ("slurm_job__isnull", False)),
                            _connector="OR",
                        ),
                    ),
                    models.Q(
                        ("call__isnull", True),
                        ("kind", "idle"),
                        ("launch__isnull", False),
                        ("project__isnull", True),
                        ("run__isnull", True),
                        ("slurm_job__isnull", True),
                        ("team__isnull", True),
                    ),
                    models.Q(
                        ("call__isnull", False),
                        ("kind", "api"),
                        ("launch__isnull", True),
                        ("project__isnull", False),
                        ("run__isnull", False),
                        ("slurm_job__isnull", True),
                        models.Q(("api", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="iki_cost_whose",
            ),
        ),
        migrations.AddConstraint(
            model_name="slurmaccount",
            constraint=models.UniqueConstraint(
                fields=("cluster", "project"), name="iki_slurm_account_once"
            ),
        ),
        migrations.AddConstraint(
            model_name="slurmaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("account__regex", "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")),
                name="iki_slurm_account_name",
            ),
        ),
    ]
