"""Runs enqueued tasks: ``iki-server tasks`` (see :mod:`iki_server.tasks.backend`).

Each of ``concurrency`` threads claims the best ready task (``FOR UPDATE SKIP
LOCKED``, so any number of runners share the queue without blocking each
other), runs it, and records the outcome. A claim is a lease: a housekeeping
thread renews the leases of running attempts, and returns attempts whose
runner stopped renewing (it crashed, or its pod was killed) to the queue.

Each claim gets a new fencing token, and only the runner holding the current
token can record an outcome. A runner that was presumed dead and comes back
can't overwrite the attempt that replaced it.
"""

import contextlib
import json
import random
import signal
import threading
import time
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any, ClassVar

import psycopg
import structlog
from django.db import close_old_connections, connection, transaction
from django.tasks import TaskContext, TaskResultStatus
from django.tasks.signals import task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json
from opentelemetry import metrics, trace

from iki_server import health
from iki_server.tasks.backend import CHANNEL, GiveUp, LeaseExpired, Retry, load_task, to_result
from iki_server.tasks.models import TaskRecord

logger = structlog.get_logger("iki.tasks")
tracer = trace.get_tracer("iki.tasks")
meter = metrics.get_meter("iki.tasks")
attempts_counter = meter.create_counter(
    "iki.tasks.attempts", description="Task attempts, by task and outcome."
)
duration_histogram = meter.create_histogram(
    "iki.tasks.duration", unit="s", description="How long task attempts take."
)

#: How long a claim lasts without being renewed.
LEASE = timedelta(seconds=60)
#: How often running attempts' leases are renewed, and expired ones recovered.
HOUSEKEEPING_EVERY = 15.0
#: How often idle threads look for work without being woken.
POLL_EVERY = 5.0
#: How long the listener waits for a notification before checking for shutdown.
LISTEN_TICK = 1.0
FIRST_RETRY = timedelta(seconds=30)
LONGEST_RETRY = timedelta(hours=1)
#: Kept per failed attempt, so one runaway error can't bloat the row.
TRACEBACK_LIMIT = 8000


def backoff(attempt: int) -> timedelta:
    """30s, 60s, 2m, 4m, ... up to an hour, with ±20% jitter so retries spread out."""
    delay = min(FIRST_RETRY * (1 << min(max(attempt - 1, 0), 20)), LONGEST_RETRY)
    return delay * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not security


def _error(error: BaseException) -> dict[str, str]:
    kind = type(error)
    text = "".join(traceback.format_exception(error))
    if len(text) > TRACEBACK_LIMIT:
        text = "…\n" + text[-TRACEBACK_LIMIT:]
    return {"exception_class_path": f"{kind.__module__}.{kind.__qualname__}", "traceback": text}


@dataclass
class Runner:
    queues: list[str]
    concurrency: int = 4
    worker_id: str = field(default_factory=lambda: f"runner-{uuid.uuid4().hex[:12]}")
    #: Attempts running in this process: task ID → fencing token.
    running: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    stopping: threading.Event = field(default_factory=threading.Event)
    last_housekeeping: float = field(default_factory=time.monotonic)
    _wake: threading.Condition = field(default_factory=threading.Condition)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _threads: list[threading.Thread] = field(default_factory=list)

    # --- One attempt ----------------------------------------------------------

    def claim(self) -> TaskRecord | None:
        """Leases the best ready task, or returns None if there is none."""
        now = timezone.now()
        with transaction.atomic():
            record = (
                TaskRecord.objects.select_for_update(skip_locked=True)
                .filter(
                    queue_name__in=self.queues,
                    status=TaskResultStatus.READY,
                    run_after__lte=now,
                )
                .order_by("-priority", "run_after")
                .first()
            )
            if record is None:
                return None
            record.status = TaskResultStatus.RUNNING
            record.attempts += 1
            record.started_at = record.started_at or now
            record.last_attempted_at = now
            record.lease_token = uuid.uuid4()
            record.lease_expires_at = now + LEASE
            record.worker_ids = [*record.worker_ids, self.worker_id]
            record.save(
                update_fields=[
                    "status",
                    "attempts",
                    "started_at",
                    "last_attempted_at",
                    "lease_token",
                    "lease_expires_at",
                    "worker_ids",
                ]
            )
        return record

    def run(self, record: TaskRecord) -> None:
        """Runs one claimed attempt and records how it went."""
        if record.lease_token is None:
            raise ValueError("Only claimed tasks can run.")
        with self._lock:
            self.running[record.pk] = record.lease_token
        log = logger.bind(**record.summary())
        started = time.monotonic()
        outcome = "failed"
        try:
            with (
                tracer.start_as_current_span(
                    f"task {record.name}",
                    attributes={"iki.task": record.task_path, "iki.task.attempt": record.attempts},
                ) as span,
                structlog.contextvars.bound_contextvars(task_id=str(record.pk)),
            ):
                try:
                    task = load_task(record.task_path)
                except Exception as error:
                    # The code that enqueued it is gone (renamed, or a plugin removed).
                    outcome = self._failed(record, error, retry=False, log=log)
                    return
                result = to_result(record, task, task.backend)
                task_started.send(type(self), task_result=result)
                log.info("task_started")
                try:
                    if task.takes_context:
                        value = task.call(
                            TaskContext(task_result=result), *record.args, **record.kwargs
                        )
                    else:
                        value = task.call(*record.args, **record.kwargs)
                    value = normalize_json(value)
                except Exception as error:
                    span.record_exception(error)
                    span.set_status(trace.StatusCode.ERROR)
                    outcome = self._failed(
                        record, error, retry=not isinstance(error, GiveUp), log=log
                    )
                else:
                    outcome = self._succeeded(record, value, log=log)
                with contextlib.suppress(TaskRecord.DoesNotExist):
                    record.refresh_from_db()
                    task_finished.send(
                        type(self), task_result=to_result(record, task, task.backend)
                    )
        finally:
            with self._lock:
                self.running.pop(record.pk, None)
            elapsed = time.monotonic() - started
            attributes = {"iki.task": record.task_path, "iki.task.outcome": outcome}
            attempts_counter.add(1, attributes)
            duration_histogram.record(elapsed, attributes)

    def _finish(self, record: TaskRecord, **fields: Any) -> bool:  # noqa: ANN401
        """Records the outcome, if this runner still holds the lease."""
        updated = TaskRecord.objects.filter(
            pk=record.pk, status=TaskResultStatus.RUNNING, lease_token=record.lease_token
        ).update(lease_token=None, lease_expires_at=None, **fields)
        return updated == 1

    def _succeeded(self, record: TaskRecord, value: Any, *, log: Any) -> str:  # noqa: ANN401
        if not self._finish(
            record,
            status=TaskResultStatus.SUCCESSFUL,
            finished_at=timezone.now(),
            return_value=value,
        ):
            log.warning("task_lease_lost")
            return "lease_lost"
        log.info("task_succeeded")
        return "succeeded"

    def _failed(self, record: TaskRecord, error: Exception, *, retry: bool, log: Any) -> str:  # noqa: ANN401
        errors = [*record.errors, _error(error)]
        if retry and record.attempts < record.max_attempts:
            delay = error.after if isinstance(error, Retry) and error.after else None
            run_after = timezone.now() + (delay or backoff(record.attempts))
            if not self._finish(
                record, status=TaskResultStatus.READY, run_after=run_after, errors=errors
            ):
                log.warning("task_lease_lost")
                return "lease_lost"
            log.warning("task_will_retry", error=str(error), run_after=run_after.isoformat())
            return "retrying"
        if not self._finish(
            record, status=TaskResultStatus.FAILED, finished_at=timezone.now(), errors=errors
        ):
            log.warning("task_lease_lost")
            return "lease_lost"
        log.error("task_failed", error=str(error), exc_info=error)
        return "failed"

    # --- Leases ---------------------------------------------------------------

    def renew_leases(self) -> None:
        with self._lock:
            held = list(self.running.items())
        for task_id, token in held:
            TaskRecord.objects.filter(
                pk=task_id, status=TaskResultStatus.RUNNING, lease_token=token
            ).update(lease_expires_at=timezone.now() + LEASE)

    @staticmethod
    def recover_expired() -> int:
        """Returns attempts whose lease ran out to the queue (or fails them, at the limit)."""
        now = timezone.now()
        recovered = 0
        with transaction.atomic():
            expired = TaskRecord.objects.select_for_update(skip_locked=True).filter(
                status=TaskResultStatus.RUNNING, lease_expires_at__lt=now
            )[:100]
            for record in expired:
                error = _error(
                    LeaseExpired(f"Runner {record.worker_ids[-1]} stopped before finishing.")
                    if record.worker_ids
                    else LeaseExpired("The runner stopped before finishing.")
                )
                error["traceback"] = error["traceback"].strip()
                record.errors = [*record.errors, error]
                record.lease_token = None
                record.lease_expires_at = None
                if record.attempts < record.max_attempts:
                    record.status = TaskResultStatus.READY
                    record.run_after = now + backoff(record.attempts)
                else:
                    record.status = TaskResultStatus.FAILED
                    record.finished_at = now
                record.save(
                    update_fields=[
                        "errors",
                        "lease_token",
                        "lease_expires_at",
                        "status",
                        "run_after",
                        "finished_at",
                    ]
                )
                logger.warning("task_lease_expired", **record.summary(), status=record.status)
                recovered += 1
        return recovered

    # --- Running until stopped ------------------------------------------------

    def run_ready(self, *, limit: int = 1000) -> int:
        """Runs ready tasks in this thread until there are none. For tests and one-off use."""
        ran = 0
        while ran < limit and (record := self.claim()) is not None:
            self.run(record)
            ran += 1
        return ran

    def wake(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def _work(self) -> None:
        while not self.stopping.is_set():
            record = None
            try:
                close_old_connections()
                record = self.claim()
                if record is not None:
                    self.run(record)
            except Exception:
                logger.exception("task_runner_error")
                time.sleep(1)
            finally:
                close_old_connections()
            if record is None:
                with self._wake:
                    self._wake.wait(POLL_EVERY)

    def _housekeep(self) -> None:
        while not self.stopping.wait(HOUSEKEEPING_EVERY):
            try:
                self.renew_leases()
                self.recover_expired()
                self.last_housekeeping = time.monotonic()
            except Exception:
                logger.exception("task_housekeeping_error")
            finally:
                close_old_connections()

    def _listen(self) -> None:
        """Wakes idle threads when a task is enqueued (LISTEN/NOTIFY)."""
        delay = 1.0
        while not self.stopping.is_set():
            try:
                with listen_connection() as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    delay = 1.0
                    self.wake()  # Anything enqueued while not listening.
                    while not self.stopping.is_set():
                        for notify in conn.notifies(timeout=LISTEN_TICK):
                            if notify.payload in self.queues:
                                self.wake()
            except psycopg.Error as error:
                logger.warning("task_listen_error", error=str(error), retry_in=delay)
                self.stopping.wait(delay)
                delay = min(delay * 2, 30.0)

    def alive(self) -> bool:
        """The threads are running and housekeeping isn't stuck."""
        stale = time.monotonic() - self.last_housekeeping > 4 * HOUSEKEEPING_EVERY
        return not stale and all(thread.is_alive() for thread in self._threads)

    def start(self) -> None:
        targets = [(self._listen, "listen"), (self._housekeep, "housekeep")]
        targets += [(self._work, f"work-{n}") for n in range(self.concurrency)]
        for target, name in targets:
            # Daemon threads: an attempt still running after the shutdown grace
            # period is abandoned, and its lease brings it back for another runner.
            thread = threading.Thread(target=target, name=f"iki-tasks-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info(
            "task_runner_started",
            worker_id=self.worker_id,
            queues=self.queues,
            concurrency=self.concurrency,
        )

    def stop(self, grace: float) -> list[uuid.UUID]:
        """Stops claiming, waits up to ``grace`` seconds for running attempts,
        and returns the IDs of any still running."""
        self.stopping.set()
        self.wake()
        deadline = time.monotonic() + grace
        for thread in self._threads:
            thread.join(max(deadline - time.monotonic(), 0))
        with self._lock:
            abandoned = list(self.running)
        logger.info("task_runner_stopped", worker_id=self.worker_id, abandoned=abandoned)
        return abandoned

    def serve(self, *, grace: float, health_port: int | None) -> None:
        """Runs until SIGTERM or SIGINT."""
        stop = threading.Event()

        def on_signal(signum: int, frame: FrameType | None) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        server = HealthServer.start(self, health_port) if health_port else None
        self.start()
        stop.wait()
        if server is not None:
            server.shutdown()
        self.stop(grace)


@contextlib.contextmanager
def listen_connection() -> Iterator[psycopg.Connection[Any]]:
    """A dedicated connection for LISTEN: pooled ones are shared, so can't listen."""
    params = connection.get_connection_params()
    for key in ("cursor_factory", "context", "pool"):
        params.pop(key, None)
    params["application_name"] = "iki-server-listen"
    conn = psycopg.connect(**params, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


class HealthServer(ThreadingHTTPServer):
    """``/healthz/live`` and ``/healthz/ready`` for Kubernetes probes."""

    daemon_threads = True
    runner: Runner

    @classmethod
    def start(cls, runner: Runner, port: int) -> "HealthServer":
        server = cls(("0.0.0.0", port), _HealthHandler)  # noqa: S104 - probes use the pod IP
        server.runner = runner
        threading.Thread(target=server.serve_forever, name="iki-tasks-health", daemon=True).start()
        return server


class _HealthHandler(BaseHTTPRequestHandler):
    server: HealthServer
    server_version = "iki"
    sys_version = ""
    routes: ClassVar[set[str]] = {health.LIVE_PATH, health.READY_PATH}

    def do_GET(self) -> None:
        if self.path not in self.routes:
            self._answer(404, {"status": "not_found"})
            return
        if self.path == health.LIVE_PATH:
            alive = self.server.runner.alive()
            self._answer(200 if alive else 503, {"status": "ok" if alive else "fail"})
            return
        try:
            checks = health.readiness()
        finally:
            close_old_connections()
        healthy = all(result == "ok" for result in checks.values())
        self._answer(
            200 if healthy else 503, {"status": "ok" if healthy else "fail", "checks": checks}
        )

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
        """Probes every few seconds would flood the log."""
