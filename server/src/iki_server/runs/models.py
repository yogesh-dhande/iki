"""Action runs and the jobs that carry them out (ADR 0042).

An :class:`ActionRun` is one request to run an action: who asked, in which
project, on which entities at which revisions, with which params, and how
it ended. It keeps a copy of the action as installed when it was launched,
so the record stays true after the plugin changes.

A run is carried out by one or more :class:`Job` attempts. A worker leases a
job, runs it and reports back; an attempt that's lost or interrupted is
retried as a new job, under the action's retry policy.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.entities.models import Entity
from iki_server.org.models import Project

TIERS = ("client", "backend", "container", "vm")

#: The HRID sequence runs are numbered from (RUN-000001).
HRID_PREFIX = "RUN"
HRID_SEQUENCE = "iki_hrid_run"


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    TIMED_OUT = "timed_out", "Timed out"
    #: Its results no longer fit the entities' schema, so they weren't written.
    REJECTED = "rejected", "Rejected"


#: Statuses a run ends in.
FINISHED = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.REJECTED,
    }
)


#: In a fixed order, so the constraint (and its migration) don't change between runs.
_FINISHED_VALUES = sorted(s.value for s in FINISHED)


class JobState(models.TextChoices):
    QUEUED = "queued", "Queued"
    LEASED = "leased", "Leased"
    STAGING = "staging", "Staging inputs"
    RUNNING = "running", "Running"
    UPLOADING = "uploading", "Uploading results"
    COMMITTING = "committing", "Committing results"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    TIMED_OUT = "timed_out", "Timed out"
    REJECTED = "rejected", "Rejected"
    #: The worker went away or was stopped (spot interruption); the run may be retried.
    INTERRUPTED = "interrupted", "Interrupted"


class ActionRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hrid = models.CharField(max_length=32, unique=True, db_comment="Such as RUN-000123.")
    #: The action's qualified name, such as ``protein.compute_properties``.
    action = models.CharField(max_length=200)
    plugin_version = models.CharField(max_length=100)
    #: The plugin's commit, when it was built from a lockfile.
    plugin_sha = models.CharField(max_length=40, blank=True, default="")
    #: The action as installed at launch: inputs, effects, command, environment and more.
    spec = models.JSONField()
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: How it was asked for: web, api, mcp, cli, trigger or system (as in the audit log).
    via = models.CharField(max_length=16)
    reason = models.TextField(blank=True, default="")
    tier = models.CharField(max_length=16)
    environment = models.CharField(max_length=63)
    #: Every param's value, defaults filled in.
    params = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=RunStatus.choices, default=RunStatus.QUEUED)
    created_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    #: Why it ended badly: ``code`` and ``message``.
    error = models.JSONField(null=True, blank=True)
    #: Seconds each attempt may run.
    timeout = models.PositiveIntegerField()
    #: Attempts it may take: its first, and its retries.
    max_attempts = models.PositiveSmallIntegerField()
    #: The failures it's retried after.
    retry_on = models.JSONField(default=list)

    class Meta:
        db_table = "iki_action_run"
        db_table_comment = (
            "Every run of an action: who, where, on what, with which params, and how it ended."
        )
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_action_run_project"),
            models.Index(fields=["action", "id"], name="iki_action_run_action"),
            models.Index(
                fields=["status", "id"],
                name="iki_action_run_unfinished",
                condition=Q(status__in=[RunStatus.QUEUED, RunStatus.RUNNING]),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(status__in=RunStatus.values), name="iki_action_run_status"
            ),
            models.CheckConstraint(condition=Q(tier__in=TIERS), name="iki_action_run_tier"),
            models.CheckConstraint(
                condition=Q(max_attempts__gte=1), name="iki_action_run_attempts"
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True)
                | Q(status__in=sorted(s.value for s in FINISHED)),
                name="iki_action_run_finished",
            ),
        ]

    def __str__(self) -> str:
        return self.hrid

    @property
    def finished(self) -> bool:
        return self.status in FINISHED


class RunInput(models.Model):
    """An entity a run reads, at the revision it read."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="inputs")
    #: The action's input it's given for.
    name = models.CharField(max_length=63)
    #: Its place among that input's entities, from 0.
    position = models.PositiveIntegerField()
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()

    class Meta:
        db_table = "iki_run_input"
        db_table_comment = "The entities each run read, and at which revision."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run", "name", "position"], name="iki_run_input_position"
            ),
            models.UniqueConstraint(fields=["run", "name", "entity"], name="iki_run_input_once"),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity"], name="iki_run_input_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} {self.name}[{self.position}]"


class Job(models.Model):
    """One attempt at a run, as workers see it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.CASCADE, related_name="jobs")
    #: 1 for the first attempt, then 2, 3, ...
    attempt = models.PositiveSmallIntegerField(default=1)
    state = models.CharField(max_length=16, choices=JobState.choices, default=JobState.QUEUED)
    #: Higher first.
    priority = models.SmallIntegerField(default=0)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    tier = models.CharField(max_length=16)
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    #: What the job needs: the environment's minimum, raised by the action's own.
    resources = models.JSONField(default=dict)
    #: For client-tier jobs: only this person's own worker may lease it.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    queued_at = models.DateTimeField(db_default=Now())
    #: Changes on every lease. Only the worker holding the current token may report on the
    #: attempt, so a worker whose lease ran out can't overwrite what replaced it.
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "iki_job"
        db_table_comment = "Each attempt at a run, as workers lease and report on it."
        indexes: ClassVar[list[models.Index]] = [
            # What a worker looks for: queued jobs on its tier, best first.
            models.Index(
                "tier",
                models.F("priority").desc(),
                "queued_at",
                name="iki_job_queued",
                condition=Q(state=JobState.QUEUED),
            ),
            # Leases a worker stopped renewing.
            models.Index(
                fields=["lease_expires_at"],
                name="iki_job_leased",
                condition=Q(lease_expires_at__isnull=False),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["run", "attempt"], name="iki_job_attempt"),
            models.CheckConstraint(condition=Q(state__in=JobState.values), name="iki_job_state"),
            models.CheckConstraint(condition=Q(tier__in=TIERS), name="iki_job_tier"),
            models.CheckConstraint(condition=Q(attempt__gte=1), name="iki_job_attempt_min"),
            models.CheckConstraint(
                condition=Q(tier="client", owner__isnull=False) | ~Q(tier="client"),
                name="iki_job_client_owner",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} attempt {self.attempt}"
