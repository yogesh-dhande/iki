"""Django's Tasks framework, on Postgres.

Code declares work with Django's ``@task`` and calls ``.enqueue()``; this
backend stores it in ``iki_task`` and ``iki-server tasks`` runs it (see
:mod:`iki_server.tasks.runner`).

- **Enqueueing is transactional.** The row is written in the caller's
  transaction: a task enqueued for a change exists exactly when the change
  does, and a rolled-back change never runs its tasks. Don't wrap
  ``enqueue()`` in ``transaction.on_commit``.
- **Runners wake at once.** Enqueueing sends ``NOTIFY iki_tasks``, which
  Postgres delivers when the transaction commits. Runners also poll, so a
  missed notification only delays a task by seconds.
- **Retries.** ``iki_server.tasks.task(max_attempts=5)`` retries a failing task with
  exponential backoff (30 seconds, doubling, at most an hour). Raise
  :class:`Retry` to choose the delay, or :class:`GiveUp` to fail at once.
- **At least once.** An attempt whose runner dies is retried once its lease
  runs out, so a task may run more than once: tasks must be idempotent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.db import connection, transaction
from django.tasks import Task, TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import TaskError
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist
from django.tasks.signals import task_enqueued
from django.utils.json import normalize_json
from django.utils.module_loading import import_string

from iki_server.tasks.models import TaskRecord

#: The channel runners LISTEN on; the payload is the queue name.
CHANNEL = "iki_tasks"
MAX_ATTEMPTS = 20


if TYPE_CHECKING:
    _TaskBase = Task[Any, Any]
else:  # Generic in the type stubs only.
    _TaskBase = Task


@dataclass(frozen=True, slots=True, kw_only=True)
class IkiTask(_TaskBase):
    """A Django task that may be attempted more than once."""

    max_attempts: int = 1


class Retry(Exception):  # noqa: N818 - reads as an instruction: raise Retry(...)
    """Try again later, after ``after`` if given (else the usual backoff).

    Counts as a failed attempt: once ``max_attempts`` is reached, the task fails.
    """

    def __init__(self, reason: str, *, after: timedelta | None = None) -> None:
        super().__init__(reason)
        self.after = after


class GiveUp(Exception):  # noqa: N818 - reads as an instruction: raise GiveUp(...)
    """Fail now: trying again wouldn't help (the input is invalid, say)."""


class LeaseExpired(Exception):  # noqa: N818 - the name of what happened
    """The runner stopped renewing its lease before the attempt finished."""


class PostgresBackend(BaseTaskBackend):
    task_class = IkiTask
    supports_defer = True
    supports_get_result = True
    supports_priority = True

    def validate_task(self, task: Task[Any, Any]) -> None:
        super().validate_task(task)
        attempts = getattr(task, "max_attempts", 1)
        if not (isinstance(attempts, int) and 1 <= attempts <= MAX_ATTEMPTS):
            raise InvalidTask(f"max_attempts must be a whole number from 1 to {MAX_ATTEMPTS}.")

    def enqueue(self, task: Task[Any, Any], args: Any, kwargs: Any) -> TaskResult[Any, Any]:  # noqa: ANN401
        self.validate_task(task)
        record = TaskRecord(
            task_path=task.module_path,
            queue_name=task.queue_name,
            priority=task.priority,
            args=normalize_json(list(args)),
            kwargs=normalize_json(dict(kwargs)),
            max_attempts=getattr(task, "max_attempts", 1),
        )
        if task.run_after is not None:
            record.run_after = task.run_after
        with transaction.atomic():
            record.save(force_insert=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, task.queue_name])
        result = to_result(record, task, self.alias)
        task_enqueued.send(type(self), task_result=result)
        return result

    def get_result(self, result_id: str) -> TaskResult[Any, Any]:
        try:
            record = TaskRecord.objects.get(pk=uuid.UUID(str(result_id)))
        except (ValueError, TaskRecord.DoesNotExist) as error:
            raise TaskResultDoesNotExist(result_id) from error
        return to_result(record, load_task(record.task_path), self.alias)


def load_task(path: str) -> Task[Any, Any]:
    """The task at ``path``. Only declared tasks load, never other functions."""
    try:
        found = import_string(path)
    except ImportError as error:
        raise InvalidTask(f"No task at {path!r}.") from error
    if not isinstance(found, Task) or found.module_path != path:
        raise InvalidTask(f"{path!r} isn't a task.")
    return found


def to_result(record: TaskRecord, task: Task[Any, Any], alias: str) -> TaskResult[Any, Any]:
    result: TaskResult[Any, Any] = TaskResult(
        task=task,
        id=str(record.pk),
        status=TaskResultStatus(record.status),
        enqueued_at=record.enqueued_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        last_attempted_at=record.last_attempted_at,
        args=record.args,
        kwargs=record.kwargs,
        backend=alias,
        errors=[TaskError(**error) for error in record.errors],
        worker_ids=list(record.worker_ids),
    )
    object.__setattr__(result, "_return_value", record.return_value)
    return result
