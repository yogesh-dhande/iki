"""Checkpoints (ADR 0069): a ``checkpointable`` run's saved progress, so an attempt that's
interrupted, or whose machine goes away, is resumed rather than started again.

The command saves its progress in ``$IKI_CHECKPOINT_DIR``. Its worker packs the
directory into a tar file and uploads it, under the attempt's lease, every
``IKI_RUNS_CHECKPOINT_INTERVAL`` while the command runs if anything in it changed,
and once more when the attempt ends in a way the run is retried for. Each is an
upload of its own kind, checked against its SHA-256 like any other, and kept
under a key of its own.

The run's checkpoint is the newest one whose bytes were checked. A later attempt
is leased only once none is still being checked, and its worker restores that one
before the command starts. It tells the server once it has: the attempt records
the checkpoint it resumed from. One it can't restore is said in its log, and the
command starts afresh.

A checkpoint's bytes are deleted once a newer one is checked, and all of them once
the run ends. Its record stays.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import Upload, UploadKind, UploadState
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun, Job, RunCheckpoint, RunStatus, RunUpload

if TYPE_CHECKING:
    from iki_server.runs.models import Worker

__all__ = [
    "MEDIA_TYPE",
    "current",
    "delete_stale",
    "download",
    "ended",
    "for_payload",
    "ready_to_resume",
    "restored",
    "start",
    "supersede",
]

MEDIA_TYPE = "application/x-tar"
#: Why an attempt may end that its worker says, which a retry may follow.
_SAVED_ON = ("failed", "timed_out", "interrupted")
#: Checkpoints one maintenance pass deletes.
_BATCH = 500
_ENDED = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.TIMED_OUT,
    RunStatus.CANCELLED,
    RunStatus.REJECTED,
)


def _kept(run: ActionRun | uuid.UUID) -> QuerySet[RunCheckpoint]:
    return RunCheckpoint.objects.filter(run=run, deleted_at__isnull=True)


def current(run: ActionRun) -> RunCheckpoint | None:
    """The checkpoint a new attempt resumes from: the newest one whose bytes were checked."""
    return (
        _kept(run)
        .filter(upload__state=UploadState.READY)
        .select_related("job", "upload")
        .order_by("-created_at", "-id")
        .first()
    )


def ready_to_resume() -> Q:
    """For leasing: a later attempt waits while one of its run's checkpoints is checked, so
    it resumes from the newest."""
    checking = RunCheckpoint.objects.filter(
        run=OuterRef("run"), deleted_at__isnull=True, upload__state=UploadState.CHECKING
    )
    return Q(attempt=1) | ~Q(Exists(checking))


def for_payload(job: Job, run: ActionRun) -> dict[str, Any] | None:
    """What the run input's ``job.checkpoint`` says: how often to save, after which endings,
    and the checkpoint to restore. None for an action that isn't checkpointable."""
    if not run.spec.get("checkpointable"):
        return None
    retried = job.attempt < run.max_attempts
    saved = current(run) if job.attempt > 1 else None
    return {
        # On the last attempt, nothing would resume from it.
        "save_every": settings.IKI_RUNS_CHECKPOINT_INTERVAL if retried else None,
        "save_on": [r for r in _SAVED_ON if r in run.retry_on] if retried else [],
        "saved": _described(saved) if saved is not None else None,
    }


def _described(checkpoint: RunCheckpoint) -> dict[str, Any]:
    return {
        "id": str(checkpoint.pk),
        "attempt": checkpoint.job.attempt,
        "saved_at": checkpoint.created_at.isoformat(),
        "size": checkpoint.upload.size,
        "sha256": checkpoint.upload.sha256,
    }


def start(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, *, size: int, sha256: str) -> Upload:
    """A new checkpoint, for the attempt the worker holds. Uploads of the run's that are
    still waiting for their parts are cancelled: this one replaces them."""
    from iki_server.runs.queue import held

    with transaction.atomic():
        job = held(worker, job_id, token)
        run = ActionRun.objects.select_related("project__team", "requested_by").get(pk=job.run_id)
        if not run.spec.get("checkpointable"):
            raise Refused(f"{run.action} isn't checkpointable.")
        if not permissions.can(run.requested_by, Action.PROJECT_EDIT, run.project):
            raise Refused(f"{run.requested_by.name} can no longer add files to the project.")
        for waiting in (
            _kept(run).filter(upload__state=UploadState.PENDING).select_related("upload")
        ):
            files.cancel(waiting.upload)
            waiting.deleted_at = timezone.now()
            waiting.save(update_fields=["deleted_at"])
        upload = files.start(
            run.requested_by,
            run.project,
            name=f"{run.hrid}-attempt-{job.attempt}-checkpoint.tar",
            media_type=MEDIA_TYPE,
            size=size,
            sha256=sha256,
            kind=UploadKind.CHECKPOINT,
        )
        # Finished through the run's uploads, like its result files and artifacts.
        RunUpload.objects.create(upload=upload, run=run, job=job)
        RunCheckpoint.objects.create(run=run, job=job, upload=upload)
    upload.refresh_from_db()
    return upload


def supersede(run: ActionRun | uuid.UUID) -> None:
    """Deletes the bytes of checked checkpoints older than the run's newest checked one: a
    worker finished saving a newer one. Call inside a transaction."""
    if isinstance(run, uuid.UUID):
        run = ActionRun.objects.get(pk=run)
    newest = current(run)
    if newest is None:
        return
    older = (
        _kept(run)
        .filter(upload__state=UploadState.READY, created_at__lte=newest.created_at)
        .exclude(pk=newest.pk)
        .select_related("upload")
    )
    _delete(list(older))


def _checkpoint(job: Job, checkpoint_id: uuid.UUID) -> RunCheckpoint:
    found = (
        _kept(job.run_id)
        .filter(pk=checkpoint_id, upload__state=UploadState.READY)
        .select_related("upload", "job")
        .first()
    )
    if found is None:
        raise LookupError("This run has no such checkpoint, or it's been deleted.")
    return found


def download(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, checkpoint_id: uuid.UUID
) -> tuple[RunCheckpoint, str]:
    """The checkpoint and a URL that downloads it for five minutes, for the worker of a
    later attempt at its run."""
    from iki_server.runs.queue import held

    with transaction.atomic():
        job = held(worker, job_id, token)
        found = _checkpoint(job, checkpoint_id)
        if found.job.attempt >= job.attempt:
            raise LookupError("Only a later attempt resumes from a checkpoint.")
    key = storage.checkpoint_key(found.upload_id)
    url = storage.download_url(storage.uri(key), "", found.upload.name, MEDIA_TYPE, workers=True)
    return found, url


def restored(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, checkpoint_id: uuid.UUID) -> Job:
    """Records that the attempt resumed from the checkpoint: its worker restored it."""
    from iki_server.runs.queue import held

    with transaction.atomic():
        job = held(worker, job_id, token)
        found = _checkpoint(job, checkpoint_id)
        if found.job.attempt >= job.attempt:
            raise LookupError("Only a later attempt resumes from a checkpoint.")
        if job.resumed_from_id not in (None, found.pk):
            raise Refused("This attempt already resumed from another checkpoint.")
        job.resumed_from = found
        job.save(update_fields=["resumed_from"])
    return job


def ended(run: ActionRun) -> None:
    """The run ended: its checkpoints go once the transaction commits. Call inside it."""
    if not run.spec.get("checkpointable"):
        return
    if _kept(run).exists():
        from iki_server.runs.tasks import delete_checkpoints

        # Queued in the same transaction: deleted exactly when the run has ended.
        delete_checkpoints.enqueue(str(run.pk))


def delete_for(run_id: str) -> int:
    """Deletes the bytes of every checkpoint of an ended run. Returns how many."""
    with transaction.atomic():
        run = ActionRun.objects.filter(pk=run_id).first()
        if run is None or run.status not in _ENDED:
            return 0
        doomed = list(
            _kept(run).select_for_update(skip_locked=True, of=("self",)).select_related("upload")
        )
        _delete(doomed)
    return len(doomed)


def delete_stale(limit: int = 10 * _BATCH) -> int:
    """For ``maintain``: deletes the bytes of checkpoints no attempt will resume from, those
    of ended runs and those a newer one replaced, in case a task didn't. Returns how many."""
    deleted = 0
    newer = RunCheckpoint.objects.filter(
        run=OuterRef("run"),
        deleted_at__isnull=True,
        upload__state=UploadState.READY,
        created_at__gt=OuterRef("created_at"),
    )
    while deleted < limit:
        with transaction.atomic():
            doomed = list(
                RunCheckpoint.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("upload")
                .filter(deleted_at__isnull=True)
                .filter(
                    Q(run__status__in=_ENDED)
                    | (Q(upload__state=UploadState.READY) & Q(Exists(newer)))
                )
                .order_by("created_at")[:_BATCH]
            )
            if not doomed:
                break
            _delete(doomed)
            deleted += len(doomed)
    return deleted


def _delete(checkpoints: list[RunCheckpoint]) -> None:
    now = timezone.now()
    for checkpoint in checkpoints:
        if checkpoint.upload.state in (UploadState.PENDING, UploadState.CHECKING):
            files.cancel(checkpoint.upload)
        storage.delete_for_good(storage.checkpoint_key(checkpoint.upload_id))
        checkpoint.deleted_at = now
    RunCheckpoint.objects.bulk_update(checkpoints, ["deleted_at"])
