"""Background tasks: Django's Tasks framework on Postgres (see :mod:`.backend`).

Declare tasks with :func:`task`, Django's ``@task`` plus ``max_attempts``::

    from iki_server.tasks import task


    @task(max_attempts=5)
    def send_email(notification_id: str) -> str: ...


    send_email.enqueue(str(notification.pk))  # in the change's transaction
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Concatenate, Literal, overload

from django.tasks import Task, TaskContext
from django.tasks import task as django_task


@overload
def task[**P, R](
    *,
    max_attempts: int = 1,
    priority: int = 0,
    queue_name: str = "default",
    takes_context: Literal[False] = False,
) -> Callable[[Callable[P, R]], Task[P, R]]: ...


@overload
def task[**P, R](
    *,
    max_attempts: int = 1,
    priority: int = 0,
    queue_name: str = "default",
    takes_context: Literal[True],
) -> Callable[[Callable[Concatenate[TaskContext[Any, Any], P], R]], Task[P, R]]: ...


def task(
    *,
    max_attempts: int = 1,
    priority: int = 0,
    queue_name: str = "default",
    takes_context: bool = False,
) -> Callable[[Callable[..., Any]], Task[Any, Any]]:
    """Declares a task. ``max_attempts`` above 1 retries failures with backoff."""

    def declare(function: Callable[..., Any]) -> Task[Any, Any]:
        options: dict[str, Any] = {
            "priority": priority,
            "queue_name": queue_name,
            "takes_context": takes_context,
            "max_attempts": max_attempts,
        }
        return django_task(function, **options)

    return declare
