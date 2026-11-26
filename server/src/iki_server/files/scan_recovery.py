"""Recover pending scans after upgrades and scanner outages (ADR 0216)."""

from __future__ import annotations

from django.db import connection, transaction
from django.db.models import Q, QuerySet, UUIDField
from django.db.models.fields.json import KT
from django.db.models.functions import Cast
from django.tasks import Task, TaskResultStatus

from iki_server.files import services
from iki_server.files.models import Blob, Registration, RegistrationState, Upload, UploadState
from iki_server.files.tasks import check_registration, check_upload
from iki_server.tasks import task
from iki_server.tasks.models import TaskRecord


def _retained() -> QuerySet[Upload]:
    """Kept temporary objects that already finished the upload path."""
    return Upload.objects.filter(state=UploadState.READY).filter(
        Q(run_artifact__deleted_at__isnull=True, run_artifact__isnull=False)
        | Q(run_checkpoint__deleted_at__isnull=True, run_checkpoint__isnull=False)
        | Q(bundle_import__isnull=False) & ~Q(bundle_import__state="discarded")
    )


@task(max_attempts=5, priority=-1)
def scan_existing_blob(blob_id: str) -> str:
    return services.scan_source(Blob.objects.get(pk=blob_id)).state


@task(max_attempts=5, priority=-1)
def scan_kept_upload(upload_id: str) -> str:
    source = _retained().filter(pk=upload_id).first()
    return services.scan_source(source).state if source is not None else "expired"


def _enqueue(
    sources: QuerySet[Blob] | QuerySet[Upload] | QuerySet[Registration],
    job: Task[[str], str],
    limit: int,
) -> int:
    attempts = TaskRecord.objects.filter(task_path=job.module_path)
    active = attempts.filter(status__in=(TaskResultStatus.READY, TaskResultStatus.RUNNING))
    waiting = sources.exclude(
        pk__in=active.annotate(source_id=Cast(KT("args__0"), UUIDField())).values("source_id")
    ).order_by("pk")
    # Rotate through IDs so a missing external object or a prolonged outage can't
    # starve later files. Resume at the oldest IDs once we reach the end.
    last = attempts.order_by("-id").values_list("args", flat=True).first()
    if last:
        after = waiting.filter(pk__gt=last[0]).values_list("pk", flat=True)[:limit]
        ids = list(after)
        remaining = limit - len(ids)
        if remaining:
            ids.extend(waiting.filter(pk__lte=last[0]).values_list("pk", flat=True)[:remaining])
    else:
        ids = list(waiting.values_list("pk", flat=True)[:limit])
    count = 0
    for pk in ids:
        job.enqueue(str(pk))
        count += 1
    return count


@transaction.atomic
def recover(limit: int = 200) -> int:
    """Schedule a bounded batch per path. Streaming is done by retryable task workers."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('iki.scan.recovery', 0))")
        locked = cursor.fetchone()
        if not locked or not locked[0]:
            return 0
    counts = (
        _enqueue(Upload.objects.filter(state=UploadState.SCANNING), check_upload, limit),
        _enqueue(
            Registration.objects.filter(state=RegistrationState.SCANNING), check_registration, limit
        ),
        _enqueue(Blob.objects.filter(scan_result__isnull=True), scan_existing_blob, limit),
        _enqueue(_retained().filter(scan_result__isnull=True), scan_kept_upload, limit),
    )
    # Run behind the work just enqueued. A large installation keeps making
    # progress at the scanner's pace instead of waiting for the next CronJob.
    scheduled = TaskRecord.objects.filter(
        task_path=continue_backfill.module_path, status=TaskResultStatus.READY
    )
    if limit > 0 and max(counts) == limit and not scheduled.exists():
        continue_backfill.enqueue(limit)
    return sum(counts)


@task(max_attempts=5, priority=-2)
def continue_backfill(limit: int) -> int:
    return recover(limit)
