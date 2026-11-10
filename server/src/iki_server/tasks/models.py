"""The background task queue's table (see :mod:`iki_server.tasks`)."""

from typing import Any, ClassVar

from django.db import models
from django.db.models import Q
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from iki_server.core.ids import uuid7


class TaskRecord(models.Model):
    """One enqueued task: what to run, when, and how each attempt went."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    #: Import path of the task, such as ``iki_server.notifications.tasks.send_email``.
    task_path = models.CharField(max_length=300)
    queue_name = models.CharField(max_length=100)
    priority = models.SmallIntegerField(default=0)
    args = models.JSONField(default=list)
    kwargs = models.JSONField(default=dict)

    status = models.CharField(
        max_length=10, choices=TaskResultStatus.choices, default=TaskResultStatus.READY
    )
    #: The earliest the next attempt may start.
    run_after = models.DateTimeField(default=timezone.now)
    enqueued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True)
    last_attempted_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=1)
    #: Changes on every claim. Only the runner holding the current token may
    #: finish the attempt, so a runner whose lease ran out can't overwrite the
    #: outcome of the attempt that replaced it (a fencing token).
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)

    worker_ids = models.JSONField(default=list)
    #: One entry per failed attempt: ``exception_class_path`` and ``traceback``.
    errors = models.JSONField(default=list)
    return_value = models.JSONField(null=True)

    class Meta:
        db_table = "iki_task"
        indexes: ClassVar[list[models.Index]] = [
            # What a runner looks for: ready tasks, by queue, best first.
            models.Index(
                "queue_name",
                models.F("priority").desc(),
                "run_after",
                name="iki_task_ready",
                condition=Q(status=TaskResultStatus.READY),
            ),
            # Attempts whose runner stopped renewing its lease.
            models.Index(
                fields=["lease_expires_at"],
                name="iki_task_leased",
                condition=Q(status=TaskResultStatus.RUNNING),
            ),
            models.Index(
                fields=["finished_at"],
                name="iki_task_finished",
                condition=Q(finished_at__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_path} ({self.status}, attempt {self.attempts}/{self.max_attempts})"

    @property
    def name(self) -> str:
        return self.task_path.rsplit(".", 1)[-1]

    def summary(self) -> dict[str, Any]:
        return {"task_id": str(self.pk), "task": self.task_path, "attempt": self.attempts}
