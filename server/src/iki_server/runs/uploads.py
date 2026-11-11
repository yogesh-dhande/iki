"""Files a run's worker uploads for its results (ADR 0044).

They're ordinary uploads (straight to object storage, in parts, then checked
against their SHA-256), made for the person who launched the run, in the
run's project. Each is tied to its run, and a run's output may attach only
its own uploads and files its inputs already had.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction

from iki_server.files import services as files
from iki_server.files.models import Upload
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun, RunUpload, Worker
from iki_server.runs.queue import held

__all__ = ["MAX_UPLOADS", "complete", "parts", "start"]

#: Uploads one run may make.
MAX_UPLOADS = 10_000


def start(
    worker: Worker,
    job_id: uuid.UUID,
    token: uuid.UUID,
    *,
    name: str,
    media_type: str,
    size: int,
    sha256: str,
) -> Upload:
    with transaction.atomic():
        job = held(worker, job_id, token)
        run = ActionRun.objects.select_related("project__team", "requested_by").get(pk=job.run_id)
        if not permissions.can(run.requested_by, Action.PROJECT_EDIT, run.project):
            raise Refused(f"{run.requested_by.name} can no longer add files to the project.")
        if RunUpload.objects.filter(run=run).count() >= MAX_UPLOADS:
            raise Refused(f"A run can upload at most {MAX_UPLOADS:,} files.")
        upload = files.start(
            run.requested_by,
            run.project,
            name=name,
            media_type=media_type,
            size=size,
            sha256=sha256,
        )
        RunUpload.objects.create(upload=upload, run=run, job=job)
    upload.refresh_from_db()
    return upload


def _upload(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, upload_id: uuid.UUID) -> Upload:
    job = held(worker, job_id, token)
    found = RunUpload.objects.select_related("upload").filter(
        upload_id=upload_id, run_id=job.run_id
    )
    link = found.first()
    if link is None:
        raise LookupError("No such upload for this run.")
    return link.upload


def parts(
    worker: Worker,
    job_id: uuid.UUID,
    token: uuid.UUID,
    upload_id: uuid.UUID,
    numbers: list[int] | None,
) -> list[dict[str, Any]]:
    with transaction.atomic():
        return files.parts(_upload(worker, job_id, token, upload_id), numbers)


def complete(worker: Worker, job_id: uuid.UUID, token: uuid.UUID, upload_id: uuid.UUID) -> Upload:
    """Finishes the upload once its parts are in; again, it says how checking it went."""
    with transaction.atomic():
        upload = _upload(worker, job_id, token, upload_id)
    finished = files.complete(upload)
    finished.refresh_from_db()
    return finished
