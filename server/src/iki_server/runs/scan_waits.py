"""Runs that wait for the files they read to be scanned (ADR 0234).

A run whose files are all usable, or held, is decided at once: it's queued, or refused with
why (ADR 0216). One that finds a file still being scanned, when it's launched or when a
worker would lease it, waits instead: queued, leased by no worker and given no machine,
with its attempt recording when the wait began and which files it waited for. Each scan's
decision looks for the attempts waiting for that file, and a sweep every 15 seconds for any
it missed. When every file is usable the attempt is queued as any other, and its run's
history says so, with the decisions that released it; when one is held, the attempt and
its run end with why. A scanner outage leaves the files pending, so the run waits on
through it, as the scans themselves are retried.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import BooleanField, Exists, F, Func, OuterRef, QuerySet
from django.db.models.sql.compiler import SQLCompiler
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.core import sweeps
from iki_server.files import scan_state
from iki_server.files.models import BlobScan, File
from iki_server.files.scan_state import ScanStatus, statuses_for_blobs
from iki_server.runs import live
from iki_server.runs.models import ActionRun, Job, JobState

__all__ = ["any_waiting", "begin", "decided", "pending_files", "recover"]

#: The most waiting attempts a sweep looks at, or a decision releases at once.
AT_MOST = 500


def begin(job: Job, run: ActionRun, pending: Iterable[uuid.UUID]) -> None:
    """The attempt waits for `pending`'s scans. Call inside a transaction, with the job
    locked. An attempt waits once: its files are its run's, pinned when it was launched."""
    if job.scan_wait_started_at is not None:
        return
    job.scan_wait_started_at = timezone.now()
    job.scan_wait_files = sorted(set(pending))
    job.save(update_fields=["scan_wait_started_at", "scan_wait_files"])
    audit.record(
        "run.waiting_for_scan",
        actor=None,
        target=run,
        project=run.project,
        data={"attempt": job.attempt, "files": [str(f) for f in job.scan_wait_files]},
    )
    live.changed(run.pk, run.batch_id)


def _waiting() -> QuerySet[Job]:
    return Job.objects.filter(
        scan_wait_started_at__isnull=False,
        scan_wait_ended_at__isnull=True,
        state__in=(JobState.QUEUED, JobState.AWAITING_APPROVAL),
    )


def any_waiting() -> bool:
    """Whether any attempt waits for a scan: so a decision needn't look, most of the time.
    One that begins waiting after its file's decision is found by :func:`recover`."""
    return _waiting().exists()


def decided(blob_id: str) -> int:
    """A file's bytes have a scan decision: check the attempts waiting for them. Returns
    how many ended their wait."""
    files = list(File.objects.filter(blob_id=blob_id).values_list("pk", flat=True))
    if not files:
        return 0
    ids = list(
        _waiting()
        .filter(scan_wait_files__overlap=files)
        .order_by("scan_wait_started_at")
        .values_list("pk", flat=True)[:AT_MOST]
    )
    return _check_each(ids)


def recover(limit: int = AT_MOST) -> int:
    """Ends the waits whose files' scans are done, oldest first, in case a decision's task
    was lost or came before their wait began. Returns how many ended. It selects only those
    that are done, so however many still wait through a long scan or a scanner outage, the
    rest are found, in one query."""
    ids = list(
        _waiting()
        .filter(~Exists(_files().filter(blob__scan_result__isnull=True)) | Exists(_held()))
        .order_by("scan_wait_started_at")
        .values_list("pk", flat=True)[:limit]
    )
    return _check_each(ids)


def _files() -> QuerySet[File]:
    """The files the attempt in the outer query waits for."""
    return File.objects.filter(InArray(F("pk"), OuterRef("scan_wait_files")))


def _held() -> QuerySet[File]:
    """Those of them that a decision holds."""
    return _files().filter(blob__scan_result__in=BlobScan.objects.exclude(scan_state.USABLE))


def _check_each(ids: list[uuid.UUID]) -> int:
    def check(pk: uuid.UUID) -> bool:
        # Jobs before their run, as the queue locks them, so the two never deadlock.
        job = (
            _waiting()
            .select_for_update(skip_locked=True, of=("self",))
            .filter(pk=pk)
            .select_related("run__project")
            .first()
        )
        return job is not None and _check(job)

    return sweeps.each(ids, check, failed="scan_wait_check_failed", key="job")


class InArray(Func):
    """``value = ANY(array)``: whether an array holds the value."""

    output_field = BooleanField()

    def as_sql(  # type: ignore[override]
        self, compiler: SQLCompiler, connection: BaseDatabaseWrapper
    ) -> tuple[str, list[Any]]:
        value, array = self.get_source_expressions()
        (value_sql, given), (array_sql, taken) = compiler.compile(value), compiler.compile(array)
        return f"{value_sql} = ANY({array_sql})", [*given, *taken]


def _check(job: Job) -> bool:
    """Ends the attempt's wait if its files' scans are done: queued as any other when they
    are all usable, ended with why when one is held. Call with the job locked."""
    from iki_server.runs import included, queue

    files = {
        f.pk: f
        for f in File.objects.filter(pk__in=job.scan_wait_files).only("pk", "name", "blob_id")
    }
    scans = statuses_for_blobs({f.blob_id for f in files.values()})
    decisions = {pk: scans[f.blob_id] for pk, f in files.items()}
    # A held one ends it now, whatever else is still being scanned.
    held = sorted(
        ((files[pk], s) for pk, s in decisions.items() if not s.usable and s.state != "pending"),
        key=lambda found: found[0].name,
    )
    if not held and any(s.state == "pending" for s in decisions.values()):
        return False
    run = job.run
    now = timezone.now()
    started = job.scan_wait_started_at
    assert started is not None  # noqa: S101 - only waiting attempts are checked
    job.scan_wait_ended_at = now
    job.save(update_fields=["scan_wait_ended_at"])
    audit.record(
        "run.scan_wait_ended",
        actor=None,
        target=run,
        project=run.project,
        data={
            "attempt": job.attempt,
            "outcome": "held" if held else "usable",
            "waited_seconds": round((now - started).total_seconds(), 3),
            "decisions": [_decision(pk, s) for pk, s in sorted(decisions.items())],
        },
    )
    if held:
        file, scan = held[0]
        code = "file_quarantined" if scan.state == "infected" else "file_not_scanned"
        if job.state == JobState.QUEUED:
            queue.fail_queued(job, code, included.held(file.name, scan))
        # One waiting for its approval ends once it's approved, when it's leased.
        return True
    if job.state == JobState.QUEUED:
        queue.notify(job.tier)
    live.changed(run.pk, run.batch_id)
    return True


def _decision(file_id: uuid.UUID, scan: ScanStatus) -> dict[str, Any]:
    return {
        "file": str(file_id),
        "decision": str(scan.decision_id) if scan.decision_id else None,
        "state": scan.state,
        "scanner": scan.scanner,
        "signature_version": scan.signature_version,
        "scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else None,
        "reason": scan.reason,
        "released": scan.released,
        "allow_unscanned": scan.allow_unscanned,
    }


def pending_files(jobs: Iterable[Job]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """For each waiting attempt among `jobs`, by run: the files it still waits for."""
    waiting = [j for j in jobs if j.waiting_for_scan]
    ids = {f for j in waiting for f in j.scan_wait_files}
    if not ids:
        return {}
    blobs = dict(File.objects.filter(pk__in=ids).values_list("pk", "blob_id"))
    scans = statuses_for_blobs(set(blobs.values()))
    return {
        j.run_id: [
            f for f in j.scan_wait_files if f in blobs and scans[blobs[f]].state == "pending"
        ]
        for j in waiting
    }
