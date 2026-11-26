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
hashes of either are stored. A self-hosted worker belongs to the teams (and
perhaps only some of their projects) its token was made for, and carries the
labels runs are matched to it by (ADR 0112). A Slurm cluster's worker, on its login
node, is registered for the cluster, and submits each job it leases as a Slurm job
(:class:`SlurmJob`), many at once (ADR 0133).
"""

from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Lower, Now

from iki_server.audit.models import Via
from iki_server.core.ids import uuid7
from iki_server.entities.models import Entity
from iki_server.files.models import Upload
from iki_server.org.models import Project, Team

#: Where workers run actions.
TIERS = ("self_hosted", "backend", "container", "vm")
#: Iki's own: a core action whose run Iki's task runner carries out, with no worker, such
#: as importing a spreadsheet (ADR 0178).
SERVER_TIER = "server"
#: Where a run can run: on a worker's tier, or Iki's own.
RUN_TIERS = (*TIERS, SERVER_TIER)

#: What a self-hosted machine's label may look like: lowercase, as GitHub's runners
#: compare theirs, such as ``chromeleon``, ``lab-3`` or ``windows``.
LABEL_PATTERN = r"^[a-z0-9]([a-z0-9._-]{0,48}[a-z0-9])?$"
#: The most labels a worker, a registration token or a run may have.
MAX_LABELS = 30

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
    #: Waiting on an outside API's job, holding no worker; resumed when it's done (ADR 0130).
    PARKED = "parked", "Waiting on an outside API"


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
    #: Submitted to a Slurm cluster, and waiting in its queue for nodes (ADR 0133).
    PENDING = "pending", "Waiting in the cluster's queue"
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
    #: Its command submitted an outside API's job and parked: the run resumes in a new
    #: attempt once the job is done (ADR 0130).
    PARKED = "parked", "Parked"


class ApprovalState(models.TextChoices):
    PENDING = "pending", "Waiting for a decision"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    #: Nobody decided within ``RunApproval.expires_at``.
    EXPIRED = "expired", "Expired"


class Recalculation(models.Model):
    """A parent changed since this entity's calculation, including while automation was off."""

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="+")
    action = models.CharField(max_length=200)

    class Meta:
        db_table = "iki_recalculation"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["entity", "action"], name="iki_recalculation_once")
        ]


class ProjectTrigger(models.Model):
    """A plugin's trigger in a project (ADR 0080): off until one of the project's Admins
    turns it on. An entity trigger then runs as whoever turned it on last; a scheduled one
    runs as the service account they chose (``run_as``), on their authority."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The trigger's qualified name, such as ``protein.compute_new_protein_properties``.
    trigger = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    #: Last explicit enablement. A default-on recheck needs no enabling Admin.
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    enabled_at = models.DateTimeField()
    #: For a scheduled trigger: the service account its runs are launched as.
    run_as = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: For a scheduled trigger that's on: when it next runs.
    next_run_at = models.DateTimeField(null=True, blank=True)
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
            models.Index(
                fields=["next_run_at"],
                name="iki_project_trigger_due",
                condition=Q(enabled=True, next_run_at__isnull=False),
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
    #: The workflow's qualified name, in ``namespace/plugin.workflow`` form.
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
    #: The consent its launch gave for what its steps send to outside APIs (ADR 0130).
    consent = models.ForeignKey(
        "ApiConsent", null=True, blank=True, on_delete=models.PROTECT, related_name="workflows"
    )
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


def operator_change_reason(via: str, reason: str, workflow: WorkflowRun | None = None) -> str:
    """The launcher’s explanation, excluding generated trigger and workflow-step descriptions."""
    if via == Via.TRIGGER:
        return ""
    return (workflow.reason if workflow is not None else reason).strip()


class ActionRun(models.Model):
    @property
    def change_reason(self) -> str:
        """Only the launcher's own words may explain a revision made through this run."""
        return operator_change_reason(self.via, self.reason, self.workflow)

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
    #: For an action that sends data to outside APIs: the consent its launch gave, as shown
    #: (ADR 0130). An agent's run gets it when the person approves it.
    consent = models.ForeignKey(
        "ApiConsent", null=True, blank=True, on_delete=models.PROTECT, related_name="runs"
    )
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
    #: A deployment-supplied image pinned when this run was queued, if it needs one.
    environment_image = models.CharField(max_length=500, blank=True, default="")
    #: A Kubernetes Secret reference for that image. The Secret's contents aren't stored here.
    environment_image_pull_secret = models.CharField(max_length=253, blank=True, default="")
    #: What a self-hosted machine must have to run it (ADR 0112): its environment's labels,
    #: its deployment binding's and its launcher's, sorted. Empty on other tiers.
    labels = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    #: Every param's value, defaults filled in.
    params = models.JSONField(default=dict)
    #: The plugin's settings, as the deployment set them at launch (never secrets).
    plugin_settings = models.JSONField(default=dict)
    #: The setup build pinned for each managed volume, as recorded on a machine's launch.
    #: Empty when the run needs none; SQL NULL while it waits for its first ready build.
    volumes = models.JSONField(default=dict, null=True, blank=True)
    #: For the command's random numbers: given as ``IKI_SEED``, and again when it's
    #: reproduced (ADR 0057). None for runs from before seeds.
    seed = models.PositiveIntegerField(null=True, blank=True)
    #: For a deterministic action: a hash of everything its results depend on, so an
    #: identical run can reuse them (ADR 0057). While volumes are pending this holds the
    #: hash of the other launch inputs, completed when the build is pinned.
    reuse_key = models.CharField(max_length=64, blank=True, default="")
    #: Typed positions shown as unreadable in the pinned worker document (ADR 0105).
    reference_mask = models.JSONField(default=list, blank=True)
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
    #: A run imported from another deployment (ADR 0096): where from, its HRID there,
    #: the import, and what it was tied to there that didn't come with it. Empty for
    #: this deployment's own runs.
    imported = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "iki_action_run"
        db_table_comment = (
            "Every run of an action: who, where, on what, with which params, and how it ended."
        )
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["project", "id"], name="iki_action_run_project"),
            models.Index(fields=["action", "id"], name="iki_action_run_action"),
            # Searching everything (ADR 0176): a run by the start of its HRID, and the runs
            # someone launched lately.
            models.Index(
                fields=["hrid"],
                opclasses=["varchar_pattern_ops"],
                name="iki_action_run_hrid_prefix",
            ),
            models.Index(fields=["requested_by", "id"], name="iki_action_run_requester"),
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
                    status__in=[
                        RunStatus.AWAITING_APPROVAL,
                        RunStatus.QUEUED,
                        RunStatus.RUNNING,
                        RunStatus.PARKED,
                    ]
                ),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(status__in=RunStatus.values), name="iki_action_run_status"
            ),
            models.CheckConstraint(condition=Q(tier__in=RUN_TIERS), name="iki_action_run_tier"),
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
    """A change that fired a project's trigger, or its schedule coming round (ADR 0080),
    and the run it launched."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    trigger = models.ForeignKey(ProjectTrigger, on_delete=models.PROTECT, related_name="firings")
    #: ``entity.created``, ``entity.updated`` or ``schedule``.
    event = models.CharField(max_length=32)
    entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The revision the change made: the run reads the entity as it is when it's launched.
    revision = models.PositiveIntegerField(null=True, blank=True)
    #: On a schedule: the time it was for.
    scheduled_for = models.DateTimeField(null=True, blank=True)
    #: On a schedule: the time the last run that succeeded was for, which the command is
    #: given to carry on from (None for the first).
    since = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=16, choices=FiringState.choices, default=FiringState.PENDING
    )
    run = models.OneToOneField(
        ActionRun, null=True, blank=True, on_delete=models.PROTECT, related_name="firing"
    )
    #: A recheck runs with the credential of the person whose edit it verifies.
    editor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Why it failed or was skipped: ``code`` and ``message``.
    error = models.JSONField(null=True, blank=True)
    #: For a trigger that gives inputs related entities (ADR 0123): how many each got.
    related = models.JSONField(
        null=True,
        blank=True,
        db_comment="How many related entities each input was given, by input (ADR 0123).",
    )
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
            models.CheckConstraint(
                condition=Q(
                    event="schedule",
                    entity__isnull=True,
                    revision__isnull=True,
                    scheduled_for__isnull=False,
                )
                | ~Q(event="schedule")
                & Q(entity__isnull=False, revision__isnull=False, scheduled_for__isnull=True),
                name="iki_trigger_firing_what",
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
    #: For a record of a registry entry (ADR 0115): the entry's revision it read.
    entry_revision = models.PositiveIntegerField(null=True, blank=True)

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


class RunInclusion(models.Model):
    """An entity a run reads because one of its inputs' references names it, at the
    revision it read (ADR 0105): an antibody's chains, say, when the action's input
    includes ``components.part`` (ADR 0161)."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="inclusions")
    #: The input, and the place among its entities, of the entity whose reference it is.
    input = models.CharField(max_length=63)
    position = models.PositiveIntegerField()
    #: The reference, and the entity's place in it (0 for a reference, not a list).
    field = models.CharField(max_length=63)
    index = models.PositiveIntegerField()
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    revision = models.PositiveIntegerField()
    #: For a record of a registry entry (ADR 0115): the entry's revision it read.
    entry_revision = models.PositiveIntegerField(null=True, blank=True)

    #: Direct input IDs an included service request covered at launch.
    covered_materials = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "iki_run_inclusion"
        db_table_comment = (
            "The entities each run read through its inputs' references, and at which revision."
        )
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run", "input", "position", "field", "index"],
                name="iki_run_inclusion_place",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity"], name="iki_run_inclusion_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} {self.input}[{self.position}].{self.field}[{self.index}]"


class RegistrationToken(models.Model):
    """What a pool of workers on one tier registers with. Shown once, when made.

    On the self-hosted tier, it's for machines of one or more teams: each worker that
    registers with it gets its teams, projects and labels, which an admin can change
    on that worker afterwards (ADR 0112)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    label = models.CharField(max_length=100)
    tier = models.CharField(max_length=16)
    #: Self-hosted: the teams whose runs its workers take, and, within a team, only these
    #: projects if any of the team's are listed.
    teams = models.ManyToManyField(Team, blank=True, related_name="+")
    projects = models.ManyToManyField(Project, blank=True, related_name="+")
    #: Self-hosted: the labels its workers get, such as ``chromeleon`` or ``lab-3``.
    labels = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    #: Container tier: the Slurm cluster its workers submit jobs to, as the deployment's
    #: Slurm templates name it (ADR 0133). Empty for any other kind of worker.
    slurm_cluster = models.CharField(max_length=63, blank=True, default="")
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
            models.CheckConstraint(
                condition=Q(tier__in=TIERS), name="iki_worker_registration_tier"
            ),
            models.CheckConstraint(
                condition=Q(tier="self_hosted") | Q(labels=[]),
                name="iki_worker_registration_labels",
            ),
            models.CheckConstraint(
                condition=Q(tier="container") | Q(slurm_cluster=""),
                name="iki_worker_registration_slurm",
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
    it when the environment says to. Runs pin an available build and every attempt reads
    its volumes."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    hrid = models.CharField(max_length=32, unique=True, db_comment="Such as SETUP-000123.")
    environment = models.CharField(max_length=200, db_comment="plugin.environment")
    plugin_version = models.CharField(max_length=100)
    plugin_sha = models.CharField(max_length=64, blank=True, default="")
    image = models.CharField(max_length=500, db_comment="Pinned by its digest.")
    image_pull_secret = models.CharField(max_length=253, blank=True, default="")
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
    #: Once retired, new runs cannot pin it. Its snapshots stay while a pinned run or
    #: machine still needs them.
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
    image_pull_secret = models.CharField(max_length=253, blank=True, default="")
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
    #: Self-hosted: the teams whose runs it takes, and, within a team, only these projects
    #: if any of the team's are listed (ADR 0112). From its registration token, then as an
    #: admin changes them.
    teams = models.ManyToManyField(Team, blank=True, related_name="+")
    projects = models.ManyToManyField(Project, blank=True, related_name="+")
    #: Self-hosted: the labels an admin gave it, which runs are matched to it by.
    labels = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    #: On a Slurm cluster's login node: the cluster it submits jobs to, from its
    #: registration token (ADR 0133). It holds many jobs at once.
    slurm_cluster = models.CharField(max_length=63, blank=True, default="")
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
                condition=Q(registration__isnull=False)
                | Q(tier__in=["container", "vm"], launch__isnull=False),
                name="iki_worker_registered",
            ),
            models.CheckConstraint(
                condition=Q(tier="self_hosted") | Q(labels=[]), name="iki_worker_labels"
            ),
            models.CheckConstraint(
                condition=Q(slurm_cluster="") | Q(tier="container", launch__isnull=True),
                name="iki_worker_slurm",
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
    #: Its run's labels: only a self-hosted worker with every one of them leases it.
    labels = ArrayField(models.CharField(max_length=50), default=list, blank=True)
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
    queued_at = models.DateTimeField(db_default=Now())
    #: Changes on every lease. Only the worker holding the current token may report on the
    #: attempt, so a worker whose lease ran out can't overwrite what replaced it.
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    worker = models.ForeignKey(
        Worker, null=True, blank=True, on_delete=models.PROTECT, related_name="jobs"
    )
    #: The labels its worker had when it leased it, for the record: an admin may change
    #: them later.
    worker_labels = ArrayField(models.CharField(max_length=50), default=list, blank=True)
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
    #: The files its run input lists, set when it's leased: its inputs' and its packed
    #: runs' at their pinned revisions, their included references', and its file params'.
    #: Its worker can download these and no others.
    input_files = ArrayField(models.UUIDField(), default=list, blank=True)
    #: For an action that's ``checkpointable``: the checkpoint an earlier attempt saved that
    #: this one resumed from, once its worker restored it (ADR 0069).
    resumed_from = models.ForeignKey(
        "RunCheckpoint", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Whether its action calls outside APIs (ADR 0130): an enforced budget that's spent
    #: holds it on any tier.
    outside = models.BooleanField(default=False)
    #: For an action that calls outside APIs: the CA made for this attempt and the
    #: certificates it signed for the egress proxy's keys, by key and host (ADR 0130).
    #: The CA's private key was thrown away once they were signed.
    gateway = models.JSONField(default=dict, blank=True)
    #: The parked attempt whose job this one resumes from, if it does (ADR 0130).
    resumes = models.ForeignKey(
        "RunPark", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: When it found a file it reads still being scanned (ADR 0234), and the files that
    #: were: no worker leases it, nor is a machine started for it, until their scans
    #: are done. ``scan_wait_ended_at`` says when they were, whether clean or held.
    scan_wait_started_at = models.DateTimeField(null=True, blank=True)
    scan_wait_ended_at = models.DateTimeField(null=True, blank=True)
    scan_wait_files = ArrayField(models.UUIDField(), default=list, blank=True)

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
            # What a lease weighs: each project's best queued jobs on a tier with the
            # same labels, and the projects and labels that have any, found by skipping
            # from one to the next (ADR 0108, 0112).
            models.Index(
                "tier",
                "project",
                "labels",
                models.F("priority").desc(),
                "queued_at",
                "id",
                name="iki_job_queued_by_labels",
                condition=Q(state=JobState.QUEUED),
            ),
            # How many jobs each project has running, which the lease ranks projects by:
            # as many as there are workers, however long the history.
            models.Index(
                fields=["project"],
                name="iki_job_held",
                condition=Q(
                    state__in=["leased", "staging", "pending", "running", "uploading"],
                    packed_into__isnull=True,
                ),
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
            # Attempts waiting for their files' scans, which a scan's decision looks for.
            models.Index(
                fields=["scan_wait_started_at"],
                name="iki_job_waiting_for_scan",
                condition=Q(scan_wait_started_at__isnull=False, scan_wait_ended_at__isnull=True),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["run", "attempt"], name="iki_job_attempt"),
            models.CheckConstraint(
                condition=Q(scan_wait_ended_at__isnull=True)
                | Q(
                    scan_wait_started_at__isnull=False,
                    scan_wait_started_at__lte=models.F("scan_wait_ended_at"),
                ),
                name="iki_job_scan_wait_order",
            ),
            models.CheckConstraint(condition=Q(state__in=JobState.values), name="iki_job_state"),
            models.CheckConstraint(condition=Q(tier__in=RUN_TIERS), name="iki_job_tier"),
            models.CheckConstraint(condition=Q(attempt__gte=1), name="iki_job_attempt_min"),
            # Only the job a worker holds has a lease.
            models.CheckConstraint(
                condition=Q(packed_into__isnull=True) | Q(lease_token__isnull=True),
                name="iki_job_packed_unleased",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} attempt {self.attempt}"

    @property
    def waiting_for_scan(self) -> bool:
        return self.scan_wait_started_at is not None and self.scan_wait_ended_at is None


def not_waiting_for_scan() -> Q:
    """Jobs that aren't waiting for a file's scan (ADR 0234): those a worker may lease."""
    return Q(scan_wait_started_at__isnull=True) | Q(scan_wait_ended_at__isnull=False)


class SlurmJob(models.Model):
    """The Slurm job an attempt runs as on a cluster (ADR 0133): where the server placed
    it, and what its worker last said Slurm reports. Its usage, from ``sacct`` once it
    ended, is what its cost is recorded from."""

    job = models.OneToOneField(
        Job, on_delete=models.CASCADE, primary_key=True, related_name="slurm"
    )
    cluster = models.CharField(max_length=63)
    #: Placed by the server when it leased the attempt, from its template and project.
    partition = models.CharField(max_length=64)
    account = models.CharField(max_length=64)
    qos = models.CharField(max_length=64, blank=True, default="")
    #: What it asked Slurm for: cpu, memory, gpu_count, scratch_disk and walltime.
    requested = models.JSONField(default=dict)
    #: Slurm's ID for it, such as ``123456``, once submitted.
    slurm_id = models.CharField(max_length=32, blank=True, default="")
    #: Slurm's state, such as ``PENDING`` or ``OUT_OF_MEMORY``, and why it waits.
    state = models.CharField(max_length=32, blank=True, default="")
    reason = models.CharField(max_length=200, blank=True, default="")
    nodes = models.CharField(max_length=500, blank=True, default="")
    exit_code = models.CharField(max_length=16, blank=True, default="")
    submitted_at = models.DateTimeField(null=True, blank=True)
    #: When Slurm started it on its nodes, and when it ended there.
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    #: From ``sacct``: elapsed and CPU seconds, peak memory, and what it was given.
    usage = models.JSONField(null=True, blank=True)
    #: Who signed the image it ran, as its worker checked it; empty if unchecked.
    image_signer = models.CharField(max_length=500, blank=True, default="")
    updated_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_slurm_job"
        db_table_comment = "The Slurm job each attempt on a Slurm cluster ran as."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["cluster", "slurm_id"], name="iki_slurm_job_id"),
        ]

    def __str__(self) -> str:
        return f"{self.cluster}:{self.slurm_id or '-'}"


class SlurmAccount(models.Model):
    """The Slurm account a project's jobs are charged to on a cluster (ADR 0133). Compute
    admins set it; a project without one uses its template's ``account``."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    cluster = models.CharField(max_length=63)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    account = models.CharField(max_length=64)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    set_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_slurm_account"
        db_table_comment = "Each project's Slurm account on each cluster."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["cluster", "project"], name="iki_slurm_account_once"),
            models.CheckConstraint(
                condition=Q(account__regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
                name="iki_slurm_account_name",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.cluster}: {self.account}"


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


class RunOutputQuerySet(models.QuerySet["RunOutput"]):
    def final(self) -> "RunOutputQuerySet":
        """Each run's output: its last attempt's, if that attempt's command reported one. An
        earlier attempt's isn't the run's: a parked attempt's metrics aren't those of the
        attempt that resumed it and failed (ADR 0130)."""
        last = Job.objects.filter(run=OuterRef("run")).order_by("-attempt").values("attempt")
        return self.filter(job__attempt=Subquery(last[:1])).order_by("run_id")


class RunOutput(models.Model):
    """What an attempt's command reported, as it sent it (ADR 0044). Kept even when its
    results weren't written, so a rejected run's work isn't lost. A run has one for each
    attempt that finished its command: one that parked on an outside API's job (ADR 0130)
    keeps its own, and the attempt that resumes it another. The run's output is its latest
    attempt's (:meth:`RunOutputQuerySet.final`)."""

    APPEND_ONLY = True

    job = models.OneToOneField(
        Job, primary_key=True, on_delete=models.PROTECT, related_name="output"
    )
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="outputs")
    #: The output document (``run-output/v1``), as received.
    document = models.JSONField()
    received_at = models.DateTimeField(db_default=Now())

    objects = RunOutputQuerySet.as_manager()

    class Meta:
        db_table = "iki_run_output"
        db_table_comment = "Each attempt's output document, as its worker sent it."

    def __str__(self) -> str:
        return f"output of {self.job_id}"


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
    #: For a record of a registry entry (ADR 0115): the entry, and the revisions the run
    #: made of it, registering it or filling in its values (two lines of an import can
    #: each fill it). Kept apart from the record, which can move to another entry later.
    entry = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    entry_revisions = ArrayField(
        models.PositiveIntegerField(), default=list, blank=True, db_default=[]
    )

    class Meta:
        db_table = "iki_run_effect"
        db_table_comment = "Every entity each run created or updated, with the revision it made."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "revision"], name="iki_run_effect_revision"),
            models.Index(
                fields=["entry"], name="iki_run_effect_entry", condition=Q(entry__isnull=False)
            ),
            # A run's effects in the order it wrote them, a page at a time: a bulk import
            # writes millions (ADR 0056).
            models.Index(fields=["run", "id"], name="iki_run_effect_run"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["run", "entity"], name="iki_run_effect_once"),
            models.CheckConstraint(
                condition=Q(kind__in=EffectKind.values), name="iki_run_effect_kind"
            ),
            # An entry's revisions are of the entry the effect names.
            models.CheckConstraint(
                condition=Q(entry_revisions=[]) | Q(entry__isnull=False),
                name="iki_run_effect_entry_of",
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
    #: How the source says it made the entity (ADR 0106): its kind, method, software and
    #: details, as the run's output gave them. None when the record doesn't say.
    made = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_external_origin"
        db_table_comment = "Where imported entities came from, as the runs that made them said."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "id"], name="iki_external_origin_entity"),
            models.Index(fields=["source", "identifier"], name="iki_external_origin_record"),
        ]
        # Several to an entity in one run: one molecule is in several databases' records,
        # and a run that reads two of them keeps both (ADR 0154).
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(url="") | Q(url__startswith="https://"),
                name="iki_external_origin_https",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.identifier}"


class Making(models.TextChoices):
    EXPERIMENTAL = "experimental"
    COMPUTATIONAL = "computational"


class MadeBy(models.TextChoices):
    #: Made by a run of an action that lists the software: Iki's own record.
    IKI = "iki"
    #: What a record the run imported says: its source's statement.
    SOURCE = "source"


class EntityMaking(models.Model):
    """How an entity was made, and who says so (ADR 0106): one row per tool. Iki records
    the software of the action whose run made it; an import keeps what its source states,
    from the origin's ``made``. Search reads these, so one condition matches one row."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="+")
    #: The run that made it, or that imported the record saying so.
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="makings")
    #: For a source's statement: the origin it's part of.
    origin = models.ForeignKey(
        ExternalOrigin, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    by = models.CharField(max_length=10, choices=MadeBy.choices)
    kind = models.CharField(max_length=20, choices=Making.choices)
    method = models.CharField(max_length=100, blank=True, default="")
    #: Empty when a source says how, but not with what.
    software = models.CharField(max_length=100, blank=True, default="")
    version = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_entity_making"
        db_table_comment = "How entities were made: Iki's record of its runs, or a source's word."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "id"], name="iki_entity_making_entity"),
            # "Made with AlphaFold", case aside, without reading every entity's rows.
            models.Index(Lower("software"), "entity", name="iki_entity_making_software"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(by="source", origin__isnull=False)
                | Q(by="iki", origin__isnull=True, kind="computational") & ~Q(software=""),
                name="iki_entity_making_by",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_id} {self.kind} {self.software} {self.version} ({self.by})"


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


class RunFileRole(models.TextChoices):
    #: One of its file params, or among a list of them.
    PARAM = "param", "A file param"
    #: Held by one of its inputs, or a reference they include, at the revision it read.
    INPUT = "input", "An input's file"
    #: Uploaded by its worker for its results.
    MADE = "made", "Made by it"


class RunFile(models.Model):
    """A file a run read or made (ADR 0177): given as a param, held by its inputs when it
    was launched, or uploaded by its worker. So a file lists the runs that used it, and a
    project's Files are found by run."""

    APPEND_ONLY = True

    pk = models.CompositePrimaryKey("run", "file", "role")
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="files")
    file = models.ForeignKey("iki_files.File", on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=8, choices=RunFileRole.choices)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_run_file"
        db_table_comment = "The files each run read (its params and inputs') and made."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["file", "run"], name="iki_run_file_by_file"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(role__in=RunFileRole.values), name="iki_run_file_role"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} {self.role} {self.file_id}"


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
    #: Registry entries its creates registered (ADR 0115): identities new to the registry.
    registered = models.BigIntegerField(default=0)
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
    type = models.CharField(max_length=112)
    identity_hash = models.CharField(max_length=64, null=True)
    #: Each table's values, by table then field: {"protein_protein": {"sequence": …}}.
    parts = models.JSONField()
    changed = ArrayField(models.CharField(max_length=63))
    #: As ExternalOrigin keeps it.
    origin = models.JSONField(null=True)
    #: [[parent ID, input name, revision read], …]
    parents = models.JSONField(null=True)
    #: [[name, version], …]: the action's software that made it (ADR 0106).
    made_with = models.JSONField(null=True)

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
    #: A call to an outside API a run made, at the deployment's price for it (ADR 0130).
    API = "api", "Outside API"


class CostRecord(models.Model):
    """What a machine Iki started cost, and whose it was (ADR 0070): its seconds times its
    template's price when it was recorded. Or what a run's call to an outside API cost, at
    the deployment's price for it (ADR 0130). Recorded once, never changed."""

    id = models.BigAutoField(primary_key=True)
    #: The machine, for a run's share or idle time; none for an outside API's call, or for
    #: a run on a Slurm cluster, whose Slurm job it ran as is `slurm_job` (ADR 0133).
    launch = models.ForeignKey(
        Launch, null=True, blank=True, on_delete=models.PROTECT, related_name="costs"
    )
    slurm_job = models.ForeignKey(
        SlurmJob, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=8, choices=CostKind.choices)
    #: For an outside API's call: the call, and the API (``plugin.api``).
    call = models.OneToOneField(
        "ApiCall", null=True, blank=True, on_delete=models.PROTECT, related_name="cost_record"
    )
    api = models.CharField(max_length=200, blank=True, default="")
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
    #: Empty for an outside API's call.
    template = models.CharField(max_length=100, blank=True, default="")
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
                # A run's share is of a machine Iki started or of a Slurm job; idle time
                # is a machine's alone; an outside API's call is neither's.
                condition=(
                    Q(kind="run", run__isnull=False, project__isnull=False, call__isnull=True)
                    & (
                        Q(launch__isnull=False, slurm_job__isnull=True)
                        | Q(launch__isnull=True, slurm_job__isnull=False)
                    )
                )
                | Q(
                    kind="idle",
                    run__isnull=True,
                    project__isnull=True,
                    team__isnull=True,
                    launch__isnull=False,
                    slurm_job__isnull=True,
                    call__isnull=True,
                )
                | (
                    Q(
                        kind="api",
                        run__isnull=False,
                        project__isnull=False,
                        launch__isnull=True,
                        slurm_job__isnull=True,
                        call__isnull=False,
                    )
                    & ~Q(api="")
                ),
                name="iki_cost_whose",
            ),
            models.CheckConstraint(
                condition=Q(price_per_hour__isnull=True, amount__isnull=True, currency="")
                | Q(
                    price_per_hour__isnull=False,
                    amount__isnull=False,
                    currency__regex=r"^[A-Z]{3}$",
                )
                | Q(
                    kind="api",
                    price_per_hour__isnull=True,
                    amount__isnull=False,
                    currency__regex=r"^[A-Z]{3}$",
                ),
                name="iki_cost_priced",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.template or self.api} {self.kind} {self.amount} {self.currency}"


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


# --- Outside APIs (ADR 0130) -------------------------------------------------------------------


class ApiApproval(models.Model):
    """An Org Admin's approval of an outside API for a project: runs there may call it while
    its plugin names the host and purpose approved, until it's revoked (ADR 0130)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    api = models.CharField(max_length=200, db_comment="plugin.api, such as iki/protein.fold.")
    #: What was approved, as the plugin said it then.
    label = models.CharField(max_length=200)
    host = models.CharField(max_length=253)
    purpose = models.TextField()
    note = models.TextField(blank=True, default="")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(db_default=Now())
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_api_approval"
        db_table_comment = "Org Admins' approvals of outside APIs, per project (ADR 0130)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "api"],
                condition=Q(revoked_at__isnull=True),
                name="iki_api_approval_current",
            ),
            models.CheckConstraint(
                condition=Q(revoked_at__isnull=True, revoked_by__isnull=True)
                | Q(revoked_at__isnull=False),
                name="iki_api_approval_revoked",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.api} in {self.project_id}"


class ApiConsent(models.Model):
    """What a person agreed to send to outside APIs when they launched, as it was shown to
    them (ADR 0130). Runs and workflows point to it."""

    APPEND_ONLY = True

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="+")
    #: The action, or the workflow, it was given for.
    launched = models.CharField(max_length=200)
    given_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    #: How: in the web app, through the API, or approving an agent's request.
    via = models.CharField(max_length=16)
    client = models.CharField(max_length=255, blank=True, default="")
    #: The agent that asked, when the person consented by approving its request (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Each API, with what's sent and the statement shown: ``[{api, label, host, purpose,
    #: inputs, params, statement}]``.
    statements = models.JSONField()
    given_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_api_consent"
        db_table_comment = "What people agreed to send to outside APIs, as shown (ADR 0130)."

    def __str__(self) -> str:
        return f"{self.launched} by {self.given_by_id}"


class ParkOutcome(models.TextChoices):
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    TIMED_OUT = "timed_out", "Timed out"
    CANCELLED = "cancelled", "Cancelled"
    #: Its API stopped being approved, or Iki couldn't poll it.
    ENDED = "ended", "Ended"


class RunPark(models.Model):
    """A run waiting on an outside API's job, holding no worker (ADR 0130). The egress
    proxy's poller polls it until it's done, failed, cancelled or out of time."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="parks")
    #: The attempt that submitted the job and parked.
    job = models.OneToOneField(Job, on_delete=models.PROTECT, related_name="park")
    api = models.CharField(max_length=63, db_comment="The API's name in its plugin.")
    external_id = models.CharField(max_length=200, db_comment="The API's ID for the job.")
    state = models.JSONField(default=dict, blank=True)
    parked_at = models.DateTimeField(db_default=Now())
    next_poll_at = models.DateTimeField()
    deadline = models.DateTimeField()
    polls = models.PositiveIntegerField(default=0)
    #: Polls in a row that failed: they back off, and too many end the park.
    failures = models.PositiveSmallIntegerField(default=0)
    #: The job's state, as the API last said it.
    status = models.CharField(max_length=100, blank=True, default="")
    last_polled_at = models.DateTimeField(null=True, blank=True)
    #: When a signed callback last said it might be done.
    woken_at = models.DateTimeField(null=True, blank=True)
    #: Set when the run was cancelled: the poller asks the API to cancel, then ends it.
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    #: What the API answered when asked to cancel the job, if it was.
    cancel_status = models.PositiveSmallIntegerField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=ParkOutcome.choices, blank=True, default="")
    message = models.CharField(max_length=2000, blank=True, default="")
    #: The last poll's answer, which the resuming attempt gets: null when it wasn't JSON or
    #: was too large.
    answer = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "iki_run_park"
        db_table_comment = "Runs waiting on outside APIs' jobs, and how each wait ended."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["next_poll_at"],
                name="iki_run_park_due",
                condition=Q(ended_at__isnull=True),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["run"], condition=Q(ended_at__isnull=True), name="iki_run_park_open"
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True, outcome="")
                | Q(ended_at__isnull=False, outcome__in=ParkOutcome.values),
                name="iki_run_park_outcome",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.run_id} on {self.api} {self.external_id}"


class CallKind(models.TextChoices):
    #: A request the run's command made.
    CALL = "call", "Call"
    #: The poller asking after a parked run's job.
    POLL = "poll", "Poll"
    #: The poller asking the API to cancel a parked run's job.
    CANCEL = "cancel", "Cancel"


class ApiCall(models.Model):
    """One call to an outside API, as the egress proxy made it (ADR 0130): never its body,
    but hashes of what went and came, what the answer said answered, the declared
    parameters and usage, and the cost. The provenance of an outside model's answer."""

    APPEND_ONLY = True

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(ActionRun, on_delete=models.PROTECT, related_name="api_calls")
    #: The attempt it was made for.
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="+")
    park = models.ForeignKey(
        RunPark, null=True, blank=True, on_delete=models.PROTECT, related_name="calls"
    )
    api = models.CharField(max_length=200, db_comment="plugin.api")
    host = models.CharField(max_length=253)
    kind = models.CharField(max_length=8, choices=CallKind.choices)
    method = models.CharField(max_length=16)
    path = models.CharField(max_length=2000, db_comment="Without the query, which may hold data.")
    #: The HTTP status the API answered; none when it didn't answer.
    status = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True, default="")
    started_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField()
    request_bytes = models.BigIntegerField(default=0)
    response_bytes = models.BigIntegerField(default=0)
    request_sha256 = models.CharField(max_length=64, blank=True, default="")
    response_sha256 = models.CharField(max_length=64, blank=True, default="")
    model = models.CharField(max_length=200, blank=True, default="")
    version = models.CharField(max_length=200, blank=True, default="")
    parameters = models.JSONField(default=dict, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True, default="")
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_api_call"
        db_table_comment = "Each call runs made to outside APIs, for provenance and cost."
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["run", "id"], name="iki_api_call_run"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(kind__in=CallKind.values), name="iki_api_call_kind"),
            models.CheckConstraint(
                condition=Q(cost__isnull=True, currency="")
                | Q(cost__isnull=False, currency__regex=r"^[A-Z]{3}$"),
                name="iki_api_call_priced",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.host}{self.path} {self.status}"


class EgressGatewayKey(models.Model):
    """An egress proxy's public key: each attempt's certificates for its outside APIs are
    signed for it (ADR 0130). Each proxy says it has it as it starts, and while it runs."""

    fingerprint = models.CharField(max_length=64, primary_key=True, db_comment="SHA-256, hex.")
    public_key = models.TextField(db_comment="PEM, SubjectPublicKeyInfo.")
    first_seen_at = models.DateTimeField(db_default=Now())
    last_seen_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "iki_egress_gateway_key"
        db_table_comment = "The egress proxies' public keys, for outside APIs' certificates."

    def __str__(self) -> str:
        return self.fingerprint[:16]
