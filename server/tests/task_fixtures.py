"""Tasks the task-queue tests enqueue. Module-level, as Django requires."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from django.tasks import TaskContext

from iki_server.tasks import task
from iki_server.tasks.backend import GiveUp, Retry

#: What the in-process tasks were called with, in order.
CALLS: list[Any] = []


@task()
def add(a: int, b: int) -> int:
    CALLS.append(("add", a, b))
    return a + b


@task(max_attempts=3)
def flaky(fail_times: int, context_marker: str = "") -> str:
    CALLS.append(("flaky", fail_times))
    if len([c for c in CALLS if c[0] == "flaky"]) <= fail_times:
        raise ConnectionError("the far end hung up")
    return "done"


@task(max_attempts=5)
def retry_in_an_hour() -> None:
    raise Retry("rate limited", after=timedelta(hours=1))


@task(max_attempts=5)
def give_up() -> None:
    raise GiveUp("that address is invalid")


@task(takes_context=True, max_attempts=2)
def attempt_number(context: TaskContext[Any, Any]) -> int:
    return context.attempt


@task(priority=10)
def urgent(label: str) -> None:
    CALLS.append(("urgent", label))


@task()
def ordinary(label: str) -> None:
    CALLS.append(("ordinary", label))


@task()
def touch(path: str) -> None:
    """For runner processes: leaves a file behind as proof it ran."""
    Path(path).write_text("ran")


#: Not a task: the runner must refuse to call it by path.
def not_a_task() -> None:
    CALLS.append(("not_a_task",))
