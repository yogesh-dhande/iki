"""The background task queue: Django's Tasks framework on Postgres, and its runner."""

import json
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import task_fixtures
from django.db import transaction
from django.tasks import TaskResultStatus, task_backends
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist
from django.utils import timezone
from real_server import app_role, free_port, server_env
from task_fixtures import (
    add,
    attempt_number,
    flaky,
    give_up,
    ordinary,
    retry_in_an_hour,
    touch,
    urgent,
)

from iki_server.tasks import runner as runner_module
from iki_server.tasks.backend import CHANNEL, IkiTask, PostgresBackend, load_task
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner, backoff, listen_connection

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _calls() -> Iterator[list[Any]]:
    task_fixtures.CALLS.clear()
    yield task_fixtures.CALLS
    task_fixtures.CALLS.clear()


def runner() -> Runner:
    return Runner(queues=["default"])


def due(result_id: str) -> None:
    """Makes a waiting retry due now."""
    TaskRecord.objects.filter(pk=result_id).update(run_after=timezone.now())


def test_the_default_backend_is_postgres() -> None:
    assert isinstance(task_backends["default"], PostgresBackend)
    assert isinstance(add, IkiTask)


def test_enqueued_tasks_are_stored_until_a_runner_takes_them() -> None:
    result = add.enqueue(2, 3)
    record = TaskRecord.objects.get(pk=result.id)
    assert record.task_path == "task_fixtures.add"
    assert (record.args, record.kwargs, record.status) == ([2, 3], {}, TaskResultStatus.READY)
    assert task_fixtures.CALLS == []

    assert runner().run_ready() == 1
    assert task_fixtures.CALLS == [("add", 2, 3)]
    result.refresh()
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == 5
    assert result.attempts == 1
    assert result.finished_at is not None


def test_tasks_belong_to_the_transaction_that_enqueued_them() -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        add.enqueue(1, 1)
        raise RuntimeError("the change failed")
    assert not TaskRecord.objects.exists()


def test_arguments_must_be_json() -> None:
    with pytest.raises(TypeError):
        add.enqueue(object(), 1)  # type: ignore[arg-type]
    assert not TaskRecord.objects.exists()


def test_failing_tasks_are_retried_with_backoff_up_to_their_limit() -> None:
    result = flaky.enqueue(fail_times=5)
    run = runner()

    assert run.run_ready() == 1
    record = TaskRecord.objects.get(pk=result.id)
    assert record.status == TaskResultStatus.READY
    assert record.attempts == 1
    # The first retry waits about 30 seconds, so it isn't due yet.
    assert record.run_after > timezone.now() + timedelta(seconds=20)
    assert run.run_ready() == 0

    due(result.id)
    run.run_ready()
    due(result.id)
    run.run_ready()
    result.refresh()
    assert result.status == TaskResultStatus.FAILED
    assert result.attempts == 3
    assert [e.exception_class_path for e in result.errors] == ["builtins.ConnectionError"] * 3
    assert "the far end hung up" in result.errors[-1].traceback


def test_a_retry_that_succeeds_keeps_the_earlier_errors() -> None:
    result = flaky.enqueue(fail_times=1)
    run = runner()
    run.run_ready()
    due(result.id)
    run.run_ready()
    result.refresh()
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.return_value == "done"
    assert len(result.errors) == 1


def test_tasks_can_choose_when_to_retry_or_give_up() -> None:
    later, never = retry_in_an_hour.enqueue(), give_up.enqueue()
    runner().run_ready()

    record = TaskRecord.objects.get(pk=later.id)
    assert record.status == TaskResultStatus.READY
    assert record.run_after > timezone.now() + timedelta(minutes=59)

    never.refresh()
    assert never.status == TaskResultStatus.FAILED
    assert never.attempts == 1
    assert never.errors[0].exception_class_path == "iki_server.tasks.backend.GiveUp"


def test_tasks_can_see_which_attempt_this_is() -> None:
    result = attempt_number.enqueue()
    runner().run_ready()
    result.refresh()
    assert result.return_value == 1


def test_backoff_doubles_up_to_an_hour() -> None:
    assert timedelta(seconds=24) <= backoff(1) <= timedelta(seconds=36)
    assert timedelta(seconds=48) <= backoff(2) <= timedelta(seconds=72)
    assert backoff(30) <= timedelta(hours=1.2)


def test_higher_priority_runs_first_and_deferred_tasks_wait() -> None:
    ordinary.enqueue("first in")
    urgent.enqueue("jumps the queue")
    ordinary.using(run_after=timezone.now() + timedelta(hours=1)).enqueue("tomorrow")
    runner().run_ready()
    assert task_fixtures.CALLS == [("urgent", "jumps the queue"), ("ordinary", "first in")]
    assert TaskRecord.objects.filter(status=TaskResultStatus.READY).count() == 1


def test_only_declared_tasks_run_never_arbitrary_functions() -> None:
    # A row naming any other function, written by whoever could write to the
    # table, mustn't run it.
    for path in ("task_fixtures.not_a_task", "os.system", "no.such.module"):
        TaskRecord.objects.create(task_path=path, queue_name="default", args=["id"])
        with pytest.raises(InvalidTask):
            load_task(path)
    runner().run_ready()
    assert task_fixtures.CALLS == []
    assert set(TaskRecord.objects.values_list("status", flat=True)) == {TaskResultStatus.FAILED}


def test_results_can_be_looked_up_by_id() -> None:
    result = add.enqueue(1, 2)
    found = task_backends["default"].get_result(result.id)
    assert found.task.func is add.func
    assert found.status == TaskResultStatus.READY
    for missing in (str(uuid.uuid4()), "not-an-id"):
        with pytest.raises(TaskResultDoesNotExist):
            task_backends["default"].get_result(missing)


def test_attempt_limits_are_checked_when_tasks_are_declared() -> None:
    from iki_server.tasks import task

    for bad in (0, 21, 2.5):
        with pytest.raises(InvalidTask):
            task(max_attempts=bad)(add.func)  # type: ignore[arg-type]


# --- Leases -------------------------------------------------------------------


def test_running_attempts_keep_their_lease() -> None:
    result = add.enqueue(1, 1)
    run = runner()
    record = run.claim()
    assert record is not None
    TaskRecord.objects.filter(pk=record.pk).update(lease_expires_at=timezone.now())
    assert record.lease_token is not None
    run.running[record.pk] = record.lease_token
    run.renew_leases()
    renewed = TaskRecord.objects.get(pk=result.id).lease_expires_at
    assert renewed is not None
    assert renewed > timezone.now() + timedelta(seconds=50)


def test_attempts_whose_runner_died_go_back_to_the_queue() -> None:
    result = flaky.enqueue(fail_times=0)
    crashed = runner()
    assert crashed.claim() is not None
    TaskRecord.objects.filter(pk=result.id).update(lease_expires_at=timezone.now())
    assert Runner.recover_expired() == 1

    record = TaskRecord.objects.get(pk=result.id)
    assert record.status == TaskResultStatus.READY
    assert record.errors[0]["exception_class_path"] == "iki_server.tasks.backend.LeaseExpired"
    assert crashed.worker_id in record.errors[0]["traceback"]

    due(result.id)
    other = runner()
    other.run_ready()
    result.refresh()
    assert result.status == TaskResultStatus.SUCCESSFUL
    assert result.worker_ids == [crashed.worker_id, other.worker_id]


def test_a_dead_runner_at_the_attempt_limit_fails_the_task() -> None:
    result = add.enqueue(1, 1)
    assert runner().claim() is not None
    TaskRecord.objects.filter(pk=result.id).update(lease_expires_at=timezone.now())
    Runner.recover_expired()
    result.refresh()
    assert result.status == TaskResultStatus.FAILED


def test_a_runner_that_lost_its_lease_cant_record_an_outcome() -> None:
    result = add.enqueue(1, 1)
    slow = runner()
    stale = slow.claim()
    assert stale is not None
    # Its lease runs out, and another runner takes the task over...
    TaskRecord.objects.filter(pk=result.id).update(
        status=TaskResultStatus.READY, lease_token=None, attempts=0
    )
    current = runner().claim()
    assert current is not None
    # ...then the first runner finishes. Its outcome is ignored (fencing).
    slow.run(stale)
    record = TaskRecord.objects.get(pk=result.id)
    assert record.status == TaskResultStatus.RUNNING
    assert record.lease_token == current.lease_token


# --- The runner, running ----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_enqueueing_wakes_idle_runners_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Polling is slowed right down, so only the notification can explain a quick start.
    monkeypatch.setattr(runner_module, "POLL_EVERY", 30.0)
    run = Runner(queues=["default"], concurrency=2)
    run.start()
    try:
        # Wait until the runner listens before enqueueing.
        deadline = time.monotonic() + 10
        with listen_connection() as probe:
            while time.monotonic() < deadline:
                row = probe.execute(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = 'iki-server-listen' AND query LIKE 'LISTEN%%'"
                ).fetchone()
                if row and row[0] >= 1:
                    break
                time.sleep(0.05)
        time.sleep(0.2)  # Let the idle threads start waiting.
        started = time.monotonic()
        result = add.enqueue(4, 5)
        while time.monotonic() - started < 10:
            result.refresh()
            if result.is_finished:
                break
            time.sleep(0.05)
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert time.monotonic() - started < 5
        assert run.alive()
    finally:
        assert run.stop(grace=5) == []


@pytest.mark.django_db(transaction=True)
def test_enqueueing_notifies_when_the_transaction_commits() -> None:
    with listen_connection() as conn:
        conn.execute(f"LISTEN {CHANNEL}")
        with transaction.atomic():
            add.enqueue(1, 1)
            assert list(conn.notifies(timeout=0.3)) == []
        notified = list(conn.notifies(timeout=2, stop_after=1))
    assert [n.payload for n in notified] == ["default"]


@pytest.fixture
def runner_process(
    django_db_blocker: Any, tmp_path: Path
) -> Iterator[tuple[subprocess.Popen[str], int]]:
    """``iki-server tasks`` in its own process, as the least-privileged role."""
    with django_db_blocker.unblock(), app_role() as role:
        port = free_port()
        env = server_env(
            role,
            tmp_path,
            IKI_TASKS_HEALTH_PORT=str(port),
            IKI_TASKS_GRACE_SECONDS="5",
            PYTHONPATH=str(Path(__file__).parent),
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "iki_server.manage", "tasks", "--concurrency", "2"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            yield proc, port
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            assert proc.stdout is not None
            proc.stdout.close()


def _get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read())


@pytest.mark.django_db(transaction=True)
def test_the_runner_process_runs_tasks_answers_probes_and_stops_on_sigterm(
    runner_process: tuple[subprocess.Popen[str], int], tmp_path: Path
) -> None:
    proc, port = runner_process
    deadline = time.monotonic() + 30
    while True:
        try:
            status, body = _get(port, "/healthz/ready")
            break
        except (urllib.error.URLError, ConnectionError):
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
            assert time.monotonic() < deadline, "the runner never answered its probes"
            time.sleep(0.2)
    assert status == 200, body
    assert body["checks"]["database_role"] == "ok"
    assert _get(port, "/healthz/live") == (200, {"status": "ok"})
    assert _get(port, "/elsewhere")[0] == 404

    proof = tmp_path / "ran"
    touch.enqueue(str(proof))
    while not proof.exists():
        assert time.monotonic() < deadline + 30, "the task never ran"
        time.sleep(0.1)

    stopped = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 0
    assert time.monotonic() - stopped < 10
    assert proc.stdout is not None
    lines = [json.loads(line) for line in proc.stdout.read().splitlines() if line.startswith("{")]
    events = [line["event"] for line in lines]
    assert "task_runner_started" in events
    assert "task_succeeded" in events
    assert "task_runner_stopped" in events


def test_health_answers_unavailable_when_the_runner_is_stuck() -> None:
    run = Runner(queues=["default"])
    run._threads.append(threading.Thread(target=lambda: None))  # never started: not alive
    assert run.alive() is False


def test_maintenance_deletes_finished_tasks_after_their_keep_time() -> None:
    from io import StringIO

    from django.core.management import call_command

    now = timezone.now()

    def finished(status: str, days_ago: int) -> TaskRecord:
        return TaskRecord.objects.create(
            task_path="task_fixtures.add",
            queue_name="default",
            status=status,
            finished_at=now - timedelta(days=days_ago),
        )

    kept = [
        finished(TaskResultStatus.SUCCESSFUL, 6),
        finished(TaskResultStatus.FAILED, 29),
        TaskRecord.objects.create(task_path="task_fixtures.add", queue_name="default"),
    ]
    finished(TaskResultStatus.SUCCESSFUL, 8)
    finished(TaskResultStatus.FAILED, 31)
    out = StringIO()
    call_command("maintain", stdout=out)
    assert "and 2 finished task(s)." in out.getvalue()
    assert set(TaskRecord.objects.values_list("pk", flat=True)) == {r.pk for r in kept}
