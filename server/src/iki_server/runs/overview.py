"""What compute is doing now: the queues, the workers and the machines Iki started.

Compute admins see it on Compute → Overview, and the task runners export the same numbers
as metrics (``observe``), so dashboards and alerts read what the page shows. Each is a
few grouped counts over indexed rows, cheap enough to read every minute.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from django.db import connection
from django.db.models import Count, Min, Q
from django.utils import timezone
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from iki_server.runs.models import HELD, TIERS, Job, JobState, Launch, LaunchState, Worker
from iki_server.runs.workers import OFFLINE_AFTER

#: A run's attempt from when a worker leases it until it ends; committing its results
#: is the server's part, and still running as people see it.
ACTIVE = (*HELD, JobState.COMMITTING)


@dataclass(frozen=True)
class Queue:
    """One environment's runs on one tier."""

    tier: str
    environment: str
    #: Waiting for a person to approve them (ADR 0013): not yet in the queue.
    awaiting_approval: int
    queued: int
    running: int
    #: When the longest-waiting queued attempt was queued; None when none is.
    oldest_queued_at: datetime | None

    def waiting(self, now: datetime) -> timedelta:
        """How long the longest-waiting attempt has waited; a retry's backoff isn't waiting."""
        if self.oldest_queued_at is None:
            return timedelta(0)
        return max(now - self.oldest_queued_at, timedelta(0))


@dataclass(frozen=True)
class Workers:
    """A tier's workers that aren't revoked."""

    tier: str
    #: Heard from lately, and holding a job.
    busy: int
    #: Heard from lately, with nothing to do.
    idle: int
    #: Not heard from for OFFLINE_AFTER.
    offline: int


@dataclass(frozen=True)
class Machines:
    """The machines Iki started on one template (ADR 0061, 0074) that haven't ended."""

    template: str
    provisioner: str
    #: Asked for: being scheduled, pulling the image, booting.
    starting: int
    #: Their worker registered.
    running: int


@dataclass(frozen=True)
class Overview:
    queues: list[Queue]
    workers: list[Workers]
    machines: list[Machines]


def queues() -> list[Queue]:
    """Each tier and environment with runs waiting or running. An attempt packed into
    another (ADR 0055) is counted once, as the one its worker holds."""
    rows = (
        Job.objects.filter(
            state__in=[JobState.AWAITING_APPROVAL, JobState.QUEUED, *ACTIVE],
            packed_into__isnull=True,
        )
        .values("tier", "environment")
        .annotate(
            awaiting_approval=Count("id", filter=Q(state=JobState.AWAITING_APPROVAL)),
            queued=Count("id", filter=Q(state=JobState.QUEUED)),
            running=Count("id", filter=Q(state__in=ACTIVE)),
            oldest_queued_at=Min("queued_at", filter=Q(state=JobState.QUEUED)),
        )
        .order_by("tier", "environment")
    )
    return [Queue(**row) for row in rows]


def workers(now: datetime) -> list[Workers]:
    """Every tier, even one with no workers: an empty tier is what an admin looks for.
    One row per worker that isn't revoked, counted here (a count of counts isn't SQL's)."""
    counts = {tier: {"busy": 0, "idle": 0, "offline": 0} for tier in TIERS}
    rows = (
        Worker.objects.filter(revoked_at__isnull=True)
        .annotate(held=Count("jobs", filter=Q(jobs__state__in=HELD)))
        .values_list("tier", "last_seen_at", "held")
    )
    for tier, last_seen_at, held in rows:
        online = now - last_seen_at <= OFFLINE_AFTER
        state = ("busy" if held else "idle") if online else "offline"
        counts.setdefault(tier, {"busy": 0, "idle": 0, "offline": 0})[state] += 1
    return [Workers(tier=tier, **found) for tier, found in counts.items()]


def machines() -> list[Machines]:
    rows = (
        Launch.objects.exclude(state=LaunchState.ENDED)
        .values("template", "provisioner")
        .annotate(
            starting=Count("id", filter=Q(state=LaunchState.STARTING)),
            running=Count("id", filter=Q(state=LaunchState.RUNNING)),
        )
        .order_by("template", "provisioner")
    )
    return [Machines(**row) for row in rows]


def overview(now: datetime) -> Overview:
    return Overview(queues=queues(), workers=workers(now), machines=machines())


# --- Metrics -----------------------------------------------------------------------------

meter = metrics.get_meter("iki.compute")


logger = structlog.get_logger("iki.compute")

Read = Callable[[CallbackOptions], Iterable[Observation]]


def _queued(options: CallbackOptions) -> Iterable[Observation]:
    for q in queues():
        labels = {"tier": q.tier, "environment": q.environment}
        yield Observation(q.queued, {**labels, "state": "queued"})
        yield Observation(q.running, {**labels, "state": "running"})
        yield Observation(q.awaiting_approval, {**labels, "state": "awaiting_approval"})


def _oldest(options: CallbackOptions) -> Iterable[Observation]:
    now = timezone.now()
    for q in queues():
        if q.queued:
            seconds = q.waiting(now).total_seconds()
            yield Observation(seconds, {"tier": q.tier, "environment": q.environment})


def _workers(options: CallbackOptions) -> Iterable[Observation]:
    for w in workers(timezone.now()):
        for state in ("busy", "idle", "offline"):
            yield Observation(getattr(w, state), {"tier": w.tier, "state": state})


def _machines(options: CallbackOptions) -> Iterable[Observation]:
    for m in machines():
        labels = {"template": m.template, "provisioner": m.provisioner}
        yield Observation(m.starting, {**labels, "state": "starting"})
        yield Observation(m.running, {**labels, "state": "running"})


_observing = False


def observe() -> None:
    """Exports the overview as gauges, read as metrics are exported. Every task runner
    exports the same numbers, from the database, so dashboards take their max."""
    global _observing  # once per process
    if _observing:
        return
    _observing = True
    meter.create_observable_gauge(
        "iki.runs.attempts",
        [_safe(_queued)],
        description="Run attempts by tier, environment and state.",
    )
    meter.create_observable_gauge(
        "iki.runs.oldest_queued",
        [_safe(_oldest)],
        unit="s",
        description="How long the oldest queued attempt has waited, by tier and environment.",
    )
    meter.create_observable_gauge(
        "iki.workers",
        [_safe(_workers)],
        description="Workers that aren't revoked, by tier and state: busy, idle, offline.",
    )
    meter.create_observable_gauge(
        "iki.machines",
        [_safe(_machines)],
        description="Machines Iki started that haven't ended, by template and state.",
    )


def _safe(read: Read) -> Read:
    """A callback that reports nothing rather than failing the export when the database
    can't be read. It runs on the exporter's own thread, so it closes its connection."""

    def callback(options: CallbackOptions) -> Iterable[Observation]:
        try:
            return list(read(options))
        except Exception:
            logger.warning("compute_metrics_unreadable", exc_info=True)
            return []
        finally:
            connection.close()

    return callback
