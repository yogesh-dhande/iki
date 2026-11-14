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

from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
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
#: And batches (BAT-000001).
BATCH_HRID_PREFIX = "BAT"
BATCH_HRID_SEQUENCE = "iki_hrid_batch"
#: And environment setup builds (SETUP-000001).
WORKFLOW_HRID_PREFIX = "WF"
WORKFLOW_HRID_SEQUENCE = "iki_hrid_workflow"

SETUP_HRID_PREFIX = "SETUP"
SETUP_HRID_SEQUENCE = "iki_hrid_setup"


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


class ProjectTrigger(models.Model):
    """A plugin's trigger in a project (ADR 0080): off until one of the project's Admins
    turns it on, and then run as whoever turned it on last."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The trigger's qualified name, such as ``protein.compute_new_protein_properties``.
    trigger = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    #: Who its runs are launched as: whoever turned it on last.
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    enabled_at = models.DateTimeField()
    #: Who turned it off last, or None if Iki did (see ``disabled_reason``).
    disabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    disabled_at = models.DateTimeField(null=True, blank=True)
    #: Why Iki turned it off, if it did: its runs could no longer be launched as that Admin.
    disabled_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "iki_project_trigger"
        db_table_comment = "Plugins' triggers, turned on or off in each project."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["project"], name="iki_project_trigger_on", condition=Q(enabled=True)
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["project", "trigger"], name="iki_project_trigger_once"),
            models.CheckConstraint(
                condition=Q(enabled=True) | Q(disabled_at__isnull=False),
                name="iki_project_trigger_disabled",
            ),
        ]

    def __str__(self) -> str:
        return self.trigger


class RunApproval(models.Model):
    """Runs an agent asked for, through MCP, that wait for the person it acts for to
    approve them in the web app (ADR 0013), or that a trigger started on the container or
    VM tier, for the Admin it runs as (ADR 0080). One request, one decision: a batch is
    approved or declined together."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The person the agent acts for, who alone may decide.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: The OAuth app that asked, as the audit log names it.
    client = models.CharField(max_length=255, blank=True, default="")
    #: The agent that asked for the person, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The trigger that asked, if one did, for the Admin who turned it on (ADR 0080).
    trigger = models.ForeignKey(
        ProjectTrigger, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
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


class RunBatch(models.Model):
    """Runs of one action launched together, one for each of several entities (ADR 0055).

    Each run keeps its own inputs, provenance, retries and results; the batch is what they
    were asked for as, and where people follow them together.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hrid = models.CharField(max_length=32, unique=True, db_comment="Such as BAT-000123.")
    action = models.CharField(max_length=200)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    client = models.CharField(max_length=255, blank=True, default="")
    #: The agent that asked for the person, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True, default="")
    #: The input each run took one entity for.
    each = models.CharField(max_length=63)
    #: How many runs it launched.
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_run_batch"
        db_table_comment = "Runs of one action launched together, one for each of several entities."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_run_batch_project"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(size__gte=1), name="iki_run_batch_size"),
        ]

    def __str__(self) -> str:
        return self.hrid


class WorkflowStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


WORKFLOW_FINISHED = frozenset(
    {WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)


class WorkflowRun(models.Model):
    """One run of a workflow (ADR 0079): its steps are ordinary runs, launched as the person
    who started it, in its project, each once the steps it takes from have succeeded."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hrid = models.CharField(max_length=32, unique=True, db_comment="Such as WF-000123.")
    #: The workflow's qualified name, such as ``protein.translate_and_characterize``.
    workflow = models.CharField(max_length=200)
    plugin_version = models.CharField(max_length=100)
    plugin_sha = models.CharField(max_length=40, blank=True, default="")
    #: The workflow as installed when it started: its steps run as it said then.
    spec = models.JSONField()
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    via = models.CharField(max_length=16)
    client = models.CharField(max_length=255, blank=True, default="")
    #: The agent that asked for the person, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True, default="")
    #: Every workflow param's value, defaults filled in.
    params = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=WorkflowStatus.choices, default=WorkflowStatus.RUNNING
    )
    created_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    #: Why it failed: ``code``, ``message``, and the ``step``.
    error = models.JSONField(null=True, blank=True)
    #: The workflow run it re-runs: its steps that succeeded are carried over, the rest run
    #: again (as GitHub's "Re-run failed jobs").
    rerun_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reruns"
    )

    class Meta:
        db_table = "iki_workflow_run"
        db_table_comment = "Every run of a workflow: who started it, where, on what, how it ended."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_workflow_run_project"),
            models.Index(fields=["workflow", "id"], name="iki_workflow_run_workflow"),
            models.Index(
                fields=["id"],
                name="iki_workflow_run_running",
                condition=Q(status="running"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(status__in=WorkflowStatus.values), name="iki_workflow_run_status"
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True) | ~Q(status="running"),
                name="iki_workflow_run_finished",
            ),
        ]

    def __str__(self) -> str:
        return self.hrid

    @property
    def finished(self) -> bool:
        return self.status in WORKFLOW_FINISHED


class WorkflowInput(models.Model):
    """An entity a workflow run was given, at the revision it was then."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    workflow = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="inputs")
    name = models.CharField(max_length=63)
    position = models.PositiveIntegerField()
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()

    class Meta:
        db_table = "iki_workflow_input"
        db_table_comment = "The entities each workflow run was given, and at which revision."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workflow", "name", "position"], name="iki_workflow_input_position"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity"], name="iki_workflow_input_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.workflow_id} {self.name}[{self.position}]"


class StepState(models.TextChoices):
    #: For the steps it takes from.
    WAITING = "waiting", "Waiting"
    #: Its turn has come, and it waits for a person to approve it.
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    #: The workflow was cancelled while it ran.
    CANCELLED = "cancelled", "Cancelled"
    #: The workflow failed or was cancelled before it could start.
    SKIPPED = "skipped", "Skipped"
    #: Its approver declined it, or nobody decided in time: the workflow failed.
    DECLINED = "declined", "Declined"


STEP_ENDED = frozenset(
    {
        StepState.SUCCEEDED,
        StepState.FAILED,
        StepState.CANCELLED,
        StepState.SKIPPED,
        StepState.DECLINED,
    }
)

#: How long a step waits for approval before it's declined, as GitHub's environments do.
APPROVAL_EXPIRES_AFTER = timedelta(days=30)


class WorkflowStep(models.Model):
    """A step of a workflow run: its state, and why it failed. Its run names it back
    (:attr:`ActionRun.workflow_step`)."""

    id = models.BigAutoField(primary_key=True)
    workflow = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="steps")
    #: Its ID in the workflow.
    step = models.CharField(max_length=63)
    #: Its place in the order the workflow runs its steps, from 0.
    position = models.PositiveIntegerField()
    state = models.CharField(max_length=20, choices=StepState.choices, default=StepState.WAITING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    #: Why it failed or was skipped: ``code`` and ``message``.
    error = models.JSONField(null=True, blank=True)
    #: When it began to wait for approval, and when it no longer will.
    approval_requested_at = models.DateTimeField(null=True, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    #: Who approved or declined it, when, and what they said.
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    #: For a step carried over into a re-run: the workflow run whose runs it is. Its runs
    #: name that one (:attr:`ActionRun.workflow`), not this.
    carried_from = models.ForeignKey(
        WorkflowRun, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "iki_workflow_step"
        db_table_comment = "The steps of each workflow run, and how far each has got."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["workflow", "step"], name="iki_workflow_step_once"),
            models.CheckConstraint(
                condition=Q(state__in=StepState.values), name="iki_workflow_step_state"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.workflow_id} {self.step}"

    @property
    def ended(self) -> bool:
        return self.state in STEP_ENDED


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
    #: The agent that asked for the person, if one did (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The approval it waits for, or waited for, if an agent asked for it (ADR 0013).
    approval = models.ForeignKey(
        RunApproval, null=True, blank=True, on_delete=models.PROTECT, related_name="runs"
    )
    reason = models.TextField(blank=True, default="")
    #: The batch it was launched in, if any.
    batch = models.ForeignKey(
        RunBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="runs"
    )
    #: The workflow run it's a step of, if any (ADR 0079), and the step's ID.
    workflow = models.ForeignKey(
        WorkflowRun, null=True, blank=True, on_delete=models.PROTECT, related_name="runs"
    )
    workflow_step = models.CharField(max_length=63, blank=True, default="")
    tier = models.CharField(max_length=16)
    environment = models.CharField(max_length=63)
    #: Every param's value, defaults filled in.
    params = models.JSONField(default=dict)
    #: The plugin's settings, as the deployment set them at launch (never secrets).
    plugin_settings = models.JSONField(default=dict)
    #: For the command's random numbers: given as ``IKI_SEED``, and again when it's
    #: reproduced (ADR 0057). None for runs from before seeds.
    seed = models.PositiveIntegerField(null=True, blank=True)
    #: For a deterministic action: a hash of everything its results depend on, so an
    #: identical run can reuse them (ADR 0057). Empty when it can't be reused.
    reuse_key = models.CharField(max_length=64, blank=True, default="")
    #: The identical run whose results it reused instead of running.
    reused_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        db_index=False,  # iki_action_run_reuses, built concurrently
    )
    #: The run it reproduces exactly: it writes nothing, and compares its results with them.
    reproduces = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reproductions",
        db_index=False,  # iki_action_run_reproductions, built concurrently
    )
    #: For a reproduction that ended with an output: ``matches``, and the ``differences``.
    reproduction = models.JSONField(null=True, blank=True)
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
                fields=["batch", "id"],
                name="iki_action_run_batch",
                condition=Q(batch__isnull=False),
            ),
            models.Index(
                fields=["workflow", "workflow_step", "id"],
                name="iki_action_run_workflow",
                condition=Q(workflow__isnull=False),
            ),
            models.Index(
                fields=["reuse_key", "-finished_at"],
                name="iki_action_run_reusable",
                condition=Q(status=RunStatus.SUCCEEDED)
                & ~Q(reuse_key="")
                & Q(reused_from__isnull=True, reproduces__isnull=True),
            ),
            models.Index(
                fields=["reused_from", "id"],
                name="iki_action_run_reuses",
                condition=Q(reused_from__isnull=False),
            ),
            models.Index(
                fields=["reproduces", "id"],
                name="iki_action_run_reproductions",
                condition=Q(reproduces__isnull=False),
            ),
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
                condition=Q(reused_from__isnull=True) | Q(reproduces__isnull=True),
                name="iki_action_run_reused_or_reproduces",
            ),
            models.CheckConstraint(
                condition=Q(workflow__isnull=True, workflow_step="")
                | (Q(workflow__isnull=False) & ~Q(workflow_step="")),
                name="iki_action_run_workflow_step",
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


class FiringState(models.TextChoices):
    #: Its run is yet to be launched, just after the change commits.
    PENDING = "pending", "Pending"
    #: Its run was launched (and may wait for approval), or an earlier one's reused.
    LAUNCHED = "launched", "Launched"
    #: Its run couldn't be launched: ``error`` says why.
    FAILED = "failed", "Failed"
    #: Before it could fire, the trigger was turned off, or the entity archived.
    SKIPPED = "skipped", "Skipped"


class TriggerFiring(models.Model):
    """A change that fired a project's trigger (ADR 0080), and the run it launched."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    trigger = models.ForeignKey(ProjectTrigger, on_delete=models.PROTECT, related_name="firings")
    #: ``entity.created`` or ``entity.updated``.
    event = models.CharField(max_length=32)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    #: The revision the change made: the run reads the entity as it is when it's launched.
    revision = models.PositiveIntegerField()
    state = models.CharField(
        max_length=16, choices=FiringState.choices, default=FiringState.PENDING
    )
    run = models.OneToOneField(
        ActionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="firing"
    )
    #: Why it failed or was skipped: ``code`` and ``message``.
    error = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())
    fired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_trigger_firing"
        db_table_comment = "Each change that fired a project's trigger, and the run it launched."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["trigger", "id"], name="iki_trigger_firing_trigger"),
            models.Index(
                fields=["id"], name="iki_trigger_firing_pending", condition=Q(state="pending")
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state__in=FiringState.values), name="iki_trigger_firing_state"
            ),
            models.CheckConstraint(
                condition=Q(state="launched", run__isnull=False)
                | ~Q(state="launched") & Q(run__isnull=True),
                name="iki_trigger_firing_run",
            ),
            models.CheckConstraint(
                condition=Q(state="pending", fired_at__isnull=True)
                | ~Q(state="pending") & Q(fired_at__isnull=False),
                name="iki_trigger_firing_fired",
            ),
        ]


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


class SetupState(models.TextChoices):
    #: Its environment's setup asks for a Compute admin's approval, other than whoever
    #: asked for it.
    AWAITING_APPROVAL = "awaiting_approval", "Awaiting approval"
    QUEUED = "queued", "Queued"
    #: Its machine's worker is running the setup script.
    RUNNING = "running", "Running"
    #: The script finished; its volumes are being snapshotted.
    SNAPSHOTTING = "snapshotting", "Snapshotting"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    TIMED_OUT = "timed_out", "Timed out"
    CANCELLED = "cancelled", "Cancelled"


SETUP_FINISHED = frozenset(
    {SetupState.SUCCEEDED, SetupState.FAILED, SetupState.TIMED_OUT, SetupState.CANCELLED}
)


class SetupBuild(models.Model):
    """A run of an environment's setup script that fills its volumes, in one place (a
    Kubernetes namespace), then snapshots them: a new version of each (ADR 0075). It
    belongs to the deployment, not to a project: Compute admins ask for it, and approve
    it when the environment says to. Runs then read the newest version that isn't
    retired, and record which."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hrid = models.CharField(max_length=32, unique=True, db_comment="Such as SETUP-000123.")
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    plugin_version = models.CharField(max_length=100)
    plugin_sha = models.CharField(max_length=64, blank=True, default="")
    image = models.CharField(max_length=500, db_comment="Pinned by its digest.")
    #: Where its volumes are, and runs that read them run: a Kubernetes namespace.
    place = models.CharField(max_length=63)
    #: The compute template its machine was started on.
    template = models.CharField(max_length=100)
    #: 1 for the environment's first in its place, then 2, 3, ...
    version = models.PositiveIntegerField()
    #: The setup as installed when it was asked for: its script, timeout, hosts it
    #: reaches, and the volumes it fills.
    spec = models.JSONField()
    state = models.CharField(max_length=32, choices=SetupState.choices)
    reason = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: What the data is, as the setup said (``data_version`` in its output), such as
    #: ``PDB 2026-10-01``. Runs record it with the version they read.
    data_version = models.CharField(max_length=200, blank=True, default="")
    #: Once retired, runs no longer start with it; its snapshots are deleted once no
    #: machine holds a copy.
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    #: Why it ended badly: ``code`` and ``message``.
    error = models.JSONField(null=True, blank=True)
    #: Seconds the script may run.
    timeout = models.PositiveIntegerField()
    #: The worker running it, and its lease, as for a run's job.
    worker = models.ForeignKey(
        "Worker", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    progress = models.FloatField(null=True, blank=True)
    progress_message = models.CharField(max_length=500, blank=True, default="")
    log_size = models.BigIntegerField(default=0)

    class Meta:
        db_table = "iki_setup_build"
        db_table_comment = (
            "Each run of an environment's setup: who asked, who approved, what it filled, "
            "and how it ended."
        )
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["environment", "place", "-version"], name="iki_setup_build_versions"
            ),
            models.Index(
                fields=["state"],
                name="iki_setup_build_unfinished",
                condition=Q(state__in=["awaiting_approval", "queued", "running", "snapshotting"]),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["environment", "place", "version"], name="iki_setup_build_version"
            ),
            models.CheckConstraint(
                condition=Q(state__in=SetupState.values), name="iki_setup_build_state"
            ),
            models.CheckConstraint(condition=Q(version__gte=1), name="iki_setup_build_version_min"),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True)
                | Q(state__in=sorted(s.value for s in SETUP_FINISHED)),
                name="iki_setup_build_finished",
            ),
            # Only a build that succeeded is ever used, so only one can be retired.
            models.CheckConstraint(
                condition=Q(retired_at__isnull=True) | Q(state="succeeded"),
                name="iki_setup_build_retired",
            ),
        ]

    def __str__(self) -> str:
        return self.hrid

    @property
    def finished(self) -> bool:
        return self.state in SETUP_FINISHED


class SetupLog(models.Model):
    """A chunk of what a setup script wrote, at its byte offset in the build's log."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    build = models.ForeignKey(SetupBuild, on_delete=models.PROTECT, related_name="logs")
    offset = models.BigIntegerField()
    text = models.TextField()
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_setup_log"
        db_table_comment = "What each setup build's script wrote, in chunks, in order."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["build", "offset"], name="iki_setup_log_offset"),
            models.CheckConstraint(condition=Q(offset__gte=0), name="iki_setup_log_offset_min"),
        ]

    def __str__(self) -> str:
        return f"{self.build_id} @{self.offset}"


class VolumeState(models.TextChoices):
    #: Its snapshot is being taken.
    CREATING = "creating", "Creating"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    #: Its snapshot was deleted: its build was retired, and nothing held a copy.
    DELETED = "deleted", "Deleted"


class ManagedVolume(models.Model):
    """One version of an environment's volume in one place: the snapshot a setup build
    made of it, which each machine that runs the environment there gets a copy of (ADR
    0075)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    build = models.ForeignKey(SetupBuild, on_delete=models.PROTECT, related_name="volumes")
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    name = models.CharField(max_length=63, db_comment="The volume's name in its environment.")
    place = models.CharField(max_length=63)
    #: The VolumeSnapshot's name in its namespace.
    handle = models.CharField(max_length=253)
    #: Bytes, as the environment declares it.
    size = models.BigIntegerField()
    state = models.CharField(
        max_length=16, choices=VolumeState.choices, default=VolumeState.CREATING
    )
    #: What the platform said when it couldn't be made.
    message = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())
    ready_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_managed_volume"
        db_table_comment = "Each version of each environment volume, in each place."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["build", "name"], name="iki_managed_volume_name"),
            models.UniqueConstraint(fields=["place", "handle"], name="iki_managed_volume_handle"),
            models.CheckConstraint(
                condition=Q(state__in=VolumeState.values), name="iki_managed_volume_state"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.environment}/{self.name} ({self.build_id})"


class LaunchState(models.TextChoices):
    #: Asked for: its pod is being scheduled, pulling its image or starting.
    STARTING = "starting", "Starting"
    #: Its worker registered.
    RUNNING = "running", "Running"
    ENDED = "ended", "Ended"


class Launch(models.Model):
    """A machine Iki started for runs (ADR 0061, ADR 0074): a container-tier pod or a
    vm-tier EC2 instance, for one environment on one compute template. Its worker
    registers with a token made for it alone, which gives it only that environment's
    runs, with the image it was launched with."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    template = models.CharField(max_length=100)
    provisioner = models.CharField(max_length=32)
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    plugin_sha = models.CharField(max_length=64)
    image = models.CharField(max_length=500, db_comment="Pinned by its digest.")
    #: Where it runs: a Kubernetes namespace and object name, or an AWS region.
    namespace = models.CharField(max_length=63)
    name = models.CharField(max_length=63, unique=True)
    #: For a VM (ADR 0074): its instance's ID, whether it's spot or on-demand, and its zone.
    machine = models.CharField(max_length=64, blank=True, default="")
    market = models.CharField(max_length=16, blank=True, default="")
    zone = models.CharField(max_length=32, blank=True, default="")
    #: For a machine started to run an environment's setup (ADR 0075): that build. Its
    #: worker runs nothing else.
    setup = models.ForeignKey(
        SetupBuild, null=True, blank=True, on_delete=models.PROTECT, related_name="launches"
    )
    #: The versions of the environment's volumes it got a copy of: by name, the build,
    #: its version and its ``data_version``. Each job it runs records them.
    volumes = models.JSONField(default=dict, blank=True)
    #: Whether its worker runs one job and stops (an ephemeral machine). A persistent one
    #: runs one after another until it's stopped (ADR 0062).
    once = models.BooleanField(default=True)
    #: A digest of the pod it was started as. When the template or Iki's settings for run
    #: pods change, persistent machines started before are replaced once they're idle.
    spec = models.CharField(max_length=64, blank=True, default="")
    token_hash = models.CharField(max_length=64, unique=True)
    state = models.CharField(
        max_length=16, choices=LaunchState.choices, default=LaunchState.STARTING
    )
    #: What it's doing or why it ended, for people: "Waiting for a node with 1 GPU", say.
    message = models.CharField(max_length=500, blank=True, default="")
    #: Why it ended, for code: ``finished``, ``image_unavailable``, ``never_started``,
    #: ``gone``, ``removed``, ``failed``, ``refused`` (by the cluster's admission policy, or
    #: EC2), ``idle``, ``replaced``, ``no_capacity`` (EC2 had none) or ``reclaimed`` (AWS
    #: took its spot instance back).
    ended_reason = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())
    #: Its token works until then, or until its worker registers with it.
    expires_at = models.DateTimeField()
    registered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_worker_launch"
        db_table_comment = "Machines Iki started for runs. Only hashes of their tokens are kept."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["environment", "template"],
                condition=~Q(state="ended"),
                name="iki_launch_live",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(state="ended", ended_at__isnull=False)
                | Q(~Q(state="ended"), ended_at__isnull=True),
                name="iki_launch_ended",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.template})"


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
    #: For a machine Iki started (ADR 0061): the launch it registered for.
    launch = models.OneToOneField(
        Launch, null=True, blank=True, on_delete=models.PROTECT, related_name="worker"
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
                | Q(~Q(tier="client"), registration__isnull=False)
                | Q(tier__in=["container", "vm"], launch__isnull=False),
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
    #: The compute template of the machine Iki started for it, if it did (ADR 0061).
    template = models.CharField(max_length=100, blank=True, default="")
    #: The kind of machine its action asks for, if it does: ``ephemeral`` (a fresh one, that
    #: runs nothing else) or ``persistent``.
    lifecycle = models.CharField(max_length=16, blank=True, default="")
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
    #: The names of the secrets its worker was given, never their values (ADR 0054).
    secrets = models.JSONField(default=list)
    #: For an action that's ``batchable``: the job this attempt ran in, with other runs of
    #: its batch (ADR 0055). Its worker holds and reports on that one; this one follows it.
    packed_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.RESTRICT, related_name="packed"
    )
    #: The versions of its environment's volumes its machine had (ADR 0075): by name, the
    #: setup build, its version and ``data_version``, for provenance.
    volumes = models.JSONField(default=dict, blank=True)
    #: For an action that's ``checkpointable``: the checkpoint an earlier attempt saved that
    #: this one resumed from, once its worker restored it (ADR 0069).
    resumed_from = models.ForeignKey(
        "RunCheckpoint", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

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
            models.Index(
                fields=["packed_into"],
                name="iki_job_packed",
                condition=Q(packed_into__isnull=False),
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
            # Only the job a worker holds has a lease.
            models.CheckConstraint(
                condition=Q(packed_into__isnull=True) | Q(lease_token__isnull=True),
                name="iki_job_packed_unleased",
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
            # A run's effects in the order it wrote them, a page at a time: a bulk import
            # writes millions (ADR 0056).
            models.Index(fields=["run", "id"], name="iki_run_effect_run"),
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


class JobEgress(models.Model):
    """The hosts an attempt reached, or tried to, through the egress proxy: one row per
    host, port and answer, counting connections and bytes."""

    id = models.BigAutoField(primary_key=True)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="egress")
    host = models.CharField(max_length=253)
    port = models.PositiveIntegerField()
    #: Why the proxy refused; empty when it let the connection through.
    refused = models.CharField(max_length=300, blank=True, default="")
    connections = models.PositiveIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    first_at = models.DateTimeField(db_default=Now())
    last_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_job_egress"
        db_table_comment = "Hosts each attempt reached, or was refused, through the egress proxy."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["job", "host", "port", "refused"], name="iki_job_egress_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.job_id} {self.host}:{self.port}"


class SetupEgress(models.Model):
    """The hosts a setup build's script reached, or tried to, through the egress proxy, as
    :class:`JobEgress` keeps for runs: where its reference data came from."""

    id = models.BigAutoField(primary_key=True)
    build = models.ForeignKey(SetupBuild, on_delete=models.CASCADE, related_name="egress")
    host = models.CharField(max_length=253)
    port = models.PositiveIntegerField()
    refused = models.CharField(max_length=300, blank=True, default="")
    connections = models.PositiveIntegerField(default=0)
    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    first_at = models.DateTimeField(db_default=Now())
    last_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_setup_egress"
        db_table_comment = "Hosts each setup build reached, or was refused, through the proxy."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["build", "host", "port", "refused"], name="iki_setup_egress_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.build_id} {self.host}:{self.port}"


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


class RunArtifact(models.Model):
    """A file a run's command wrote besides its results, kept for people to download until
    its retention ends (ADR 0068). Its record stays after its bytes are deleted."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="artifacts")
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="artifacts")
    upload = models.OneToOneField(Upload, on_delete=models.PROTECT, related_name="run_artifact")
    path = models.CharField(
        max_length=500, db_comment="Where the command wrote it, relative to its output directory."
    )
    created_at = models.DateTimeField(db_default=Now())
    expires_at = models.DateTimeField(db_comment="When its retention ends and it's deleted.")
    deleted_at = models.DateTimeField(
        null=True, blank=True, db_comment="When its bytes were deleted, its retention over."
    )

    class Meta:
        db_table = "iki_run_artifact"
        db_table_comment = "Files runs wrote besides their results, kept for a while."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["run", "path"], name="iki_run_artifact_by_run"),
            models.Index(
                fields=["expires_at"],
                name="iki_run_artifact_to_delete",
                condition=Q(deleted_at__isnull=True),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["job", "path"], name="iki_run_artifact_once"),
        ]

    def __str__(self) -> str:
        return self.path


class RunCheckpoint(models.Model):
    """Progress an attempt at a ``checkpointable`` run saved, for a later attempt to resume
    from (ADR 0069). Its bytes are deleted once a newer one is saved or the run ends; its
    record stays, with what it held, for the attempt that resumed from it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="checkpoints")
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="checkpoints")
    upload = models.OneToOneField(Upload, on_delete=models.PROTECT, related_name="run_checkpoint")
    created_at = models.DateTimeField(db_default=Now())
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_comment="When its bytes were deleted: a newer one was saved, or the run ended.",
    )

    class Meta:
        db_table = "iki_run_checkpoint"
        db_table_comment = "Progress checkpointable runs saved, kept until they end."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["run", "created_at"],
                name="iki_run_checkpoint_kept",
                condition=Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} checkpoint {self.pk}"


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


class ImportPhase(models.TextChoices):
    #: Reading the file, checking each line and staging it.
    CHECKING = "checking", "Checking"
    #: Writing what was staged, in one transaction.
    WRITING = "writing", "Writing"
    ENDED = "ended", "Ended"


class RunImport(models.Model):
    """A run's creates from a file, too many for output.json (ADR 0056).

    The file is checked a chunk of lines at a time, each staged in a table of the import's
    own and committed with how far it got, so an import that's interrupted goes on from
    there. Once every line is checked, all of it is written in one transaction, or none.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.CASCADE, related_name="imports")
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name="bulk_import")
    file = models.ForeignKey("iki_files.File", on_delete=models.PROTECT, related_name="+")
    phase = models.CharField(
        max_length=16, choices=ImportPhase.choices, default=ImportPhase.CHECKING
    )
    size = models.BigIntegerField(db_comment="The file's size, in bytes.")
    #: Bytes read and checked so far: where the next chunk starts.
    offset = models.BigIntegerField(default=0)
    #: Lines read so far, blank ones included, so problems can say which line.
    lines = models.BigIntegerField(default=0)
    #: Creates staged so far.
    staged = models.BigIntegerField(default=0)
    #: Problems found so far, as output errors are: {pointer, detail}.
    errors = models.JSONField(default=list)
    created = models.BigIntegerField(default=0)
    reused = models.BigIntegerField(default=0)
    #: Entities the run's output updated (its inputs), written with the creates.
    updated = models.BigIntegerField(default=0)
    #: Reused entities that weren't in the run's project yet.
    linked = models.BigIntegerField(default=0)
    #: The installed plugins it was checked against: if they change before it's written,
    #: it's checked again, from the start.
    schema = models.CharField(max_length=64, default="")
    #: Lines are staged afresh each round: a new one starts when a plugin changes.
    round = models.PositiveSmallIntegerField(default=1)
    #: Its staged lines are all deleted (they are, a batch at a time, once it ends).
    cleared = models.BooleanField(default=False)
    #: Attempts at a step that failed unexpectedly, in a row; past a few, it gives up.
    failures = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_run_import"
        db_table_comment = "Runs' creates from a file: how far each import has got."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["updated_at"],
                name="iki_run_import_open",
                condition=~Q(phase="ended"),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(phase__in=ImportPhase.values), name="iki_run_import_phase"
            ),
            models.CheckConstraint(
                condition=Q(offset__gte=0, offset__lte=models.F("size")),
                name="iki_run_import_offset",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} import"


class ImportLine(models.Model):
    """A line of a creates file, checked and ready to write, until its import ends
    (ADR 0056). Written with COPY, read back with INSERT … SELECT, then deleted."""

    pk = models.CompositePrimaryKey("bulk_import_id", "round", "line")
    #: Not a foreign key: millions of rows needn't each check it, and they go once it ends.
    bulk_import_id = models.UUIDField()
    round = models.PositiveSmallIntegerField()
    #: Its line number in the file, from 1.
    line = models.BigIntegerField()
    #: The new entity's ID, if it's new.
    entity_id = models.UUIDField()
    type = models.CharField(max_length=81)
    identity_hash = models.CharField(max_length=64, null=True)
    #: Each table's values, by table then field: {"protein_protein": {"sequence": …}}.
    parts = models.JSONField()
    changed = ArrayField(models.CharField(max_length=63))
    #: As ExternalOrigin keeps it.
    origin = models.JSONField(null=True)
    #: [[parent ID, input name, revision read], …]
    parents = models.JSONField(null=True)

    class Meta:
        db_table = "iki_import_line"
        db_table_comment = "Creates staged by bulk imports, until each is written or ends."

    def __str__(self) -> str:
        return f"{self.bulk_import_id} line {self.line}"


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


class CostKind(models.TextChoices):
    #: A run's share of a machine: all of an ephemeral one's life, or the time a persistent
    #: one held its job.
    RUN = "run", "Run"
    #: A persistent machine's time without a job, or a machine that never ran one: the
    #: organization's, not any project's.
    IDLE = "idle", "Idle"


class CostRecord(models.Model):
    """What a machine Iki started cost, and whose it was (ADR 0070): its seconds times its
    template's price when it was recorded. Recorded once, never changed."""

    id = models.BigAutoField(primary_key=True)
    launch = models.ForeignKey(Launch, on_delete=models.PROTECT, related_name="costs")
    kind = models.CharField(max_length=8, choices=CostKind.choices)
    #: For a run's share: the attempt, its run and where the run was, when it was recorded.
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    run = models.ForeignKey(
        ActionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="costs"
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    action = models.CharField(max_length=200, blank=True, default="")
    template = models.CharField(max_length=100)
    tier = models.CharField(max_length=16)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    seconds = models.DecimalField(max_digits=14, decimal_places=3)
    #: None for a template without a price: its time is recorded, at no cost.
    price_per_hour = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_cost_record"
        db_table_comment = "What each machine Iki started cost, by run, project and team."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["ended_at"], name="iki_cost_by_time"),
            models.Index(fields=["project", "ended_at"], name="iki_cost_by_project"),
            models.Index(fields=["team", "ended_at"], name="iki_cost_by_team"),
            models.Index(fields=["run"], name="iki_cost_by_run"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(kind__in=CostKind.values), name="iki_cost_kind"),
            models.CheckConstraint(condition=Q(seconds__gte=0), name="iki_cost_seconds"),
            models.CheckConstraint(
                condition=Q(kind="run", run__isnull=False, project__isnull=False)
                | Q(kind="idle", run__isnull=True, project__isnull=True, team__isnull=True),
                name="iki_cost_whose",
            ),
            models.CheckConstraint(
                condition=Q(price_per_hour__isnull=True, amount__isnull=True, currency="")
                | Q(
                    price_per_hour__isnull=False,
                    amount__isnull=False,
                    currency__regex=r"^[A-Z]{3}$",
                ),
                name="iki_cost_priced",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.template} {self.kind} {self.amount} {self.currency}"


def _warn_at() -> list[int]:
    return [50, 80, 100]


class Budget(models.Model):
    """What the organization, a team or a project may spend on runs' machines in a month
    (ADR 0071): warned of as spending passes each of `warn_at` percent, and, with
    `enforce`, no more runs on the container and VM tiers once it's spent."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: Neither: the whole organization.
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, db_comment="A month's.")
    currency = models.CharField(max_length=3, db_comment="The deployment's prices' currency.")
    warn_at = ArrayField(
        models.PositiveSmallIntegerField(),
        default=_warn_at,
        db_comment="Percentages of the amount at which to warn, once a month each.",
    )
    enforce = models.BooleanField(
        default=False, db_comment="Once spent, no more runs start in its scope that month."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(db_default=Now())
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_budget"
        db_table_comment = "Monthly spending limits on runs, per organization, team or project."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["team", "project"], name="iki_budget_once", nulls_distinct=False
            ),
            models.CheckConstraint(
                condition=Q(team__isnull=True) | Q(project__isnull=True),
                name="iki_budget_one_scope",
            ),
            models.CheckConstraint(condition=Q(amount__gt=0), name="iki_budget_amount"),
            models.CheckConstraint(
                condition=Q(currency__regex=r"^[A-Z]{3}$"), name="iki_budget_currency"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} a month"


class BudgetAlert(models.Model):
    """That a budget's spending passed one of its thresholds in a month: each is told once."""

    id = models.BigAutoField(primary_key=True)
    budget = models.ForeignKey(Budget, on_delete=models.CASCADE, related_name="alerts")
    month = models.DateField(db_comment="Its first day, in UTC.")
    threshold = models.PositiveSmallIntegerField(db_comment="Percent of the budget.")
    spent = models.DecimalField(max_digits=14, decimal_places=4)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_budget_alert"
        db_table_comment = "Each budget threshold passed, once a month."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["budget", "month", "threshold"], name="iki_budget_alert_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.threshold}% of {self.budget_id} in {self.month}"
