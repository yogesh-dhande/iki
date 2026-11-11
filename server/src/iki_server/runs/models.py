"""Action runs and the jobs that carry them out (ADR 0042).

An :class:`ActionRun` is one request to run an action: who asked, in which
project, on which entities at which revisions, with which params, and how
it ended. It keeps a copy of the action as installed when it was launched,
so the record stays true after the plugin changes.

A run is carried out by one or more :class:`Job` attempts. A worker leases a
job, runs it and reports back; an attempt that's lost or interrupted is
retried as a new job, under the action's retry policy.

Workers (:class:`Worker`) register with a :class:`RegistrationToken` a
compute admin made for their tier, and get a token of their own. Only
hashes of either are stored.
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Now

from iki_server.core.ids import uuid7
from iki_server.entities.models import Entity
from iki_server.files.models import Upload
from iki_server.org.models import Project, Team

TIERS = ("client", "backend", "container", "vm")

#: The HRID sequence runs are numbered from (RUN-000001).
HRID_PREFIX = "RUN"
HRID_SEQUENCE = "iki_hrid_run"


class RunStatus(models.TextChoices):
    #: An agent asked for it on a tier that needs the person's approval first (ADR 0013).
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    TIMED_OUT = "timed_out", "Timed out"
    #: Its results no longer fit the entities' schema, so they weren't written.
    REJECTED = "rejected", "Rejected"
    #: The person didn't approve it, or not in time.
    DECLINED = "declined", "Declined"


#: Statuses a run ends in.
FINISHED = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.REJECTED,
        RunStatus.DECLINED,
    }
)


#: In a fixed order, so the constraint (and its migration) don't change between runs.
_FINISHED_VALUES = sorted(s.value for s in FINISHED)


class JobState(models.TextChoices):
    #: Waiting for its run to be approved: no worker leases it until then.
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
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


class ApprovalState(models.TextChoices):
    PENDING = "pending", "Waiting for a decision"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    #: Nobody decided within ``RunApproval.expires_at``.
    EXPIRED = "expired", "Expired"


class RunApproval(models.Model):
    """Runs an agent asked for, through MCP, that wait for the person it acts for to
    approve them in the web app (ADR 0013). One request, one decision: a batch is approved
    or declined together."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The person the agent acts for, who alone may decide.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The OAuth app that asked, as the audit log names it.
    client = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField()
    state = models.CharField(
        max_length=16, choices=ApprovalState.choices, default=ApprovalState.PENDING
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    #: Why it was declined, if the person said.
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "iki_run_approval"
        db_table_comment = "Runs agents asked for that wait for their person's approval."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["expires_at"],
                name="iki_run_approval_pending",
                condition=Q(state="pending"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state__in=ApprovalState.values), name="iki_run_approval_state"
            ),
            models.CheckConstraint(
                condition=Q(state="pending", decided_at__isnull=True)
                | ~Q(state="pending") & Q(decided_at__isnull=False),
                name="iki_run_approval_decided",
            ),
        ]

    def __str__(self) -> str:
        return f"approval {self.pk} ({self.state})"


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
    #: The OAuth app it was asked for through, if any, as the audit log names it.
    client = models.CharField(max_length=255, blank=True, default="")
    #: The approval it waits for, or waited for, if an agent asked for it (ADR 0013).
    approval = models.ForeignKey(
        RunApproval, null=True, blank=True, on_delete=models.PROTECT, related_name="runs"
    )
    reason = models.TextField(blank=True, default="")
    tier = models.CharField(max_length=16)
    environment = models.CharField(max_length=63)
    #: Every param's value, defaults filled in.
    params = models.JSONField(default=dict)
    #: The plugin's settings, as the deployment set them at launch (never secrets).
    plugin_settings = models.JSONField(default=dict)
    status = models.CharField(max_length=32, choices=RunStatus.choices, default=RunStatus.QUEUED)
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
                condition=Q(
                    status__in=[RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED, RunStatus.RUNNING]
                ),
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


class RegistrationToken(models.Model):
    """What a pool of workers on one tier registers with. Shown once, when made."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    label = models.CharField(max_length=100)
    tier = models.CharField(max_length=16)
    token_hash = models.CharField(max_length=64, unique=True)
    #: The token's last four characters, so people can tell tokens apart.
    hint = models.CharField(max_length=4)
    #: None: made with `iki-server create_registration_token`, by whoever can run it.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_worker_registration_token"
        db_table_comment = "Tokens pools of workers register with. Only their hashes are kept."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # Client-tier workers sign in as their person instead.
            models.CheckConstraint(
                condition=Q(tier__in=["backend", "container", "vm"]),
                name="iki_worker_registration_tier",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label} (…{self.hint})"


class Worker(models.Model):
    """One running ``iki-worker``: what it can run, and when it was last heard from."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=200, db_comment="Such as its host or pod name.")
    tier = models.CharField(max_length=16)
    token_hash = models.CharField(max_length=64, unique=True)
    hint = models.CharField(max_length=4)
    registration = models.ForeignKey(
        RegistrationToken, null=True, blank=True, on_delete=models.PROTECT, related_name="workers"
    )
    #: For client-tier workers: the person whose machine it runs on.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    version = models.CharField(max_length=50)
    #: What one job may use on it: cpu, memory, gpu_count, gpu_memory and scratch_disk.
    capacity = models.JSONField(default=dict)
    #: The plugin code it has, which is all it runs: ``[{"name", "sha", "environments"}]``.
    #: ``sha`` is None for a plugin from a local directory. ``environments`` maps each
    #: environment it has built to that build's digest, or is None for a worker that runs
    #: every one with its host's own software.
    plugins = models.JSONField(default=list)
    registered_at = models.DateTimeField(db_default=Now())
    last_seen_at = models.DateTimeField(db_default=Now())
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_worker"
        db_table_comment = "Every registered worker. Only hashes of their tokens are kept."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(tier__in=TIERS), name="iki_worker_tier"),
            models.CheckConstraint(
                condition=Q(tier="client", owner__isnull=False)
                | Q(~Q(tier="client"), registration__isnull=False),
                name="iki_worker_registered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.tier})"


class Job(models.Model):
    """One attempt at a run, as workers see it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.CASCADE, related_name="jobs")
    #: 1 for the first attempt, then 2, 3, ...
    attempt = models.PositiveSmallIntegerField(default=1)
    state = models.CharField(max_length=32, choices=JobState.choices, default=JobState.QUEUED)
    #: Higher first.
    priority = models.SmallIntegerField(default=0)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    tier = models.CharField(max_length=16)
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    #: The digest of the environment build its worker ran it with, for provenance; empty
    #: when the worker used its host's own software.
    environment_build = models.CharField(max_length=100, blank=True, default="")
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
    worker = models.ForeignKey(
        Worker, null=True, blank=True, on_delete=models.PROTECT, related_name="jobs"
    )
    #: From 0 to 1, as the command reports it, with what it's doing.
    progress = models.FloatField(null=True, blank=True)
    progress_message = models.CharField(max_length=500, blank=True, default="")
    #: Bytes of log the worker has sent; past the limit they're counted, not kept.
    log_size = models.BigIntegerField(default=0)
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


class JobLog(models.Model):
    """A chunk of what a job's command wrote, at its byte offset in the job's log."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="logs")
    offset = models.BigIntegerField()
    text = models.TextField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_job_log"
        db_table_comment = "What each job's command wrote, in chunks, in order."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["job", "offset"], name="iki_job_log_offset"),
            models.CheckConstraint(condition=Q(offset__gte=0), name="iki_job_log_offset_min"),
        ]

    def __str__(self) -> str:
        return f"{self.job_id} @{self.offset}"


class RunOutput(models.Model):
    """What a run's command reported, as it sent it (ADR 0044). Kept even when its results
    weren't written, so a rejected run's work isn't lost."""

    APPEND_ONLY = True

    run = models.OneToOneField(
        ActionRun, primary_key=True, on_delete=models.PROTECT, related_name="output"
    )
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="+")
    #: The output document (``run-output/v1``), as received.
    document = models.JSONField()
    received_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_run_output"
        db_table_comment = "Each run's output document, as its worker sent it."

    def __str__(self) -> str:
        return f"output of {self.run_id}"


class EffectKind(models.TextChoices):
    CREATED = "created", "Created"
    UPDATED = "updated", "Updated"
    #: The run made an entity that existed already (its type reuses duplicates).
    REUSED = "reused", "Reused"


class RunEffect(models.Model):
    """One entity a run wrote, and the revision it made: where a value set by a run points."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="effects")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=EffectKind.choices)
    #: The revision it made (none when it reused an entity, or changed nothing).
    revision = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "iki_run_effect"
        db_table_comment = "Every entity each run created or updated, with the revision it made."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "revision"], name="iki_run_effect_revision"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["run", "entity"], name="iki_run_effect_once"),
            models.CheckConstraint(
                condition=Q(kind__in=EffectKind.values), name="iki_run_effect_kind"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} {self.kind} {self.entity_id}"


class ExternalOrigin(models.Model):
    """Where a run says an entity it made came from: a public database's record, or one in
    a file the run was given. Kept for anyone who reads the entity, as imports' provenance."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="origins")
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    #: The revision the run made (none when it found the entity already there).
    revision = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=100, db_comment="Such as NCBI Nucleotide.")
    identifier = models.CharField(max_length=255, db_comment="An accession, or a record's name.")
    version = models.CharField(max_length=100, blank=True, default="")
    url = models.CharField(max_length=2000, blank=True, default="")
    retrieved_at = models.DateTimeField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    #: The file it was read from: one of the run's file params.
    file = models.ForeignKey(
        "iki_files.File", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_external_origin"
        db_table_comment = "Where imported entities came from, as the runs that made them said."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "id"], name="iki_external_origin_entity"),
            models.Index(fields=["source", "identifier"], name="iki_external_origin_record"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["run", "entity"], name="iki_external_origin_once"),
            models.CheckConstraint(
                condition=Q(url="") | Q(url__startswith="https://"),
                name="iki_external_origin_https",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.identifier}"


class Lineage(models.Model):
    """An entity a run made, and an input it was made from, at the revision the run read."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    parent = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    parent_revision = models.PositiveIntegerField()
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="lineage")
    #: The run's input the parent was given as.
    input = models.CharField(max_length=63)

    class Meta:
        db_table = "iki_lineage"
        db_table_comment = "Which entities each run made from which, at which revisions."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity"], name="iki_lineage_entity"),
            models.Index(fields=["parent"], name="iki_lineage_parent"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "parent", "run"], name="iki_lineage_once"),
        ]

    def __str__(self) -> str:
        return f"{self.parent_id} -> {self.entity_id} ({self.run_id})"


class RunUpload(models.Model):
    """A file a run's worker uploaded, for its results (ADR 0044). A run's output may attach
    only these, and files its inputs already had."""

    upload = models.OneToOneField(
        Upload, primary_key=True, on_delete=models.PROTECT, related_name="run_upload"
    )
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="uploads")
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_run_upload"
        db_table_comment = "Files each run's worker uploaded for its results."

    def __str__(self) -> str:
        return f"{self.run_id} {self.upload_id}"


class RunQuota(models.Model):
    """A Compute admin's limit on runs: for the organization, a team or a project, on one
    tier or all of them (ADR 0045)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: Neither: the whole organization.
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    #: None: every tier together.
    tier = models.CharField(max_length=16, null=True, blank=True)
    #: Jobs running at once. 0 pauses the scope: nothing new starts.
    max_running = models.PositiveIntegerField(null=True, blank=True)
    #: Runs waiting to start. Launching more is refused.
    max_queued = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_run_quota"
        db_table_comment = "Limits on running and waiting runs, per organization, team or project."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["team", "project", "tier"],
                name="iki_run_quota_once",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(team__isnull=True) | Q(project__isnull=True),
                name="iki_run_quota_one_scope",
            ),
            models.CheckConstraint(
                condition=Q(tier__isnull=True) | Q(tier__in=TIERS), name="iki_run_quota_tier"
            ),
            models.CheckConstraint(
                condition=Q(max_running__isnull=False) | Q(max_queued__isnull=False),
                name="iki_run_quota_limits",
            ),
        ]

    def __str__(self) -> str:
        scope = self.project or self.team or "organization"
        return f"{scope} ({self.tier or 'all tiers'})"
