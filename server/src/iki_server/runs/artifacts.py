"""Runs' artifacts (ADR 0068): files a run's command writes besides its results, such as
an alignment, MSAs or a debugging log, kept for people to download.

The worker uploads each file in the output directory that one of the action's
``artifacts.keep`` patterns matches, whether the command succeeded or not. Each
is an upload of its own kind, made for the person who launched the run, in its
project. Its bytes are kept under a key of their own, not by their content, so
deleting them deletes nothing else.

Each is kept for its action's ``retention``, or the deployment's default, and
never less than its minimum. Then ``iki-server maintain`` deletes its bytes,
every version, and records that it did. The artifact's record stays: its
path, size and SHA-256, and when it was deleted.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import Upload, UploadKind
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.runs.models import ActionRun, RunArtifact, RunUpload
from iki_server.runs.queue import held
from iki_server.runs.uploads import MAX_UPLOADS
from iki_spec.globs import matches, valid_path

if TYPE_CHECKING:
    from iki_server.runs.models import Worker

__all__ = ["MAX_ARTIFACTS", "delete_expired", "retention", "start"]

#: Artifacts one attempt may keep.
MAX_ARTIFACTS = 1_000
#: Artifacts one maintenance pass deletes.
_BATCH = 500


def retention(run: ActionRun) -> timedelta:
    """How long the run's artifacts are kept: as its action says, or the deployment's
    default, and never less than its minimum."""
    declared = run.spec.get("artifact_retention") or settings.IKI_FILES_ARTIFACT_RETENTION
    return timedelta(seconds=max(declared, settings.IKI_FILES_ARTIFACT_MIN_RETENTION))


def start(
    worker: Worker,
    job_id: uuid.UUID,
    token: uuid.UUID,
    *,
    path: str,
    media_type: str,
    size: int,
    sha256: str,
) -> Upload:
    with transaction.atomic():
        job = held(worker, job_id, token)
        run = ActionRun.objects.select_related("project__team", "requested_by").get(pk=job.run_id)
        if not permissions.can(run.requested_by, Action.PROJECT_EDIT, run.project):
            raise Refused(f"{run.requested_by.name} can no longer add files to the project.")
        keep: list[str] = run.spec.get("artifacts") or []
        if not keep:
            raise Refused(f"{run.action} keeps no artifacts.", field="path")
        if not valid_path(path):
            raise Refused(
                "An artifact's path is relative to the output directory, with no empty, "
                "`.` or `..` parts, and at most 500 characters.",
                field="path",
            )
        if not any(matches(pattern, path) for pattern in keep):
            raise Refused(
                f"{run.action} doesn't keep {path}: it keeps {', '.join(keep)}.", field="path"
            )
        if RunArtifact.objects.filter(job=job, path=path).exists():
            raise Refused(f"This attempt already kept {path}.", field="path")
        if RunArtifact.objects.filter(job=job).count() >= MAX_ARTIFACTS:
            raise Refused(f"A run's attempt can keep at most {MAX_ARTIFACTS:,} artifacts.")
        if RunUpload.objects.filter(run=run).count() >= MAX_UPLOADS:
            raise Refused(f"A run can upload at most {MAX_UPLOADS:,} files.")
        upload = files.start(
            run.requested_by,
            run.project,
            name=path.rsplit("/", 1)[-1],
            media_type=media_type,
            size=size,
            sha256=sha256,
            kind=UploadKind.ARTIFACT,
        )
        # Finished through the run's uploads, like its result files.
        RunUpload.objects.create(upload=upload, run=run, job=job)
        RunArtifact.objects.create(
            run=run,
            job=job,
            upload=upload,
            path=path,
            expires_at=timezone.now() + retention(run),
        )
    upload.refresh_from_db()
    return upload


def delete_expired(limit: int = 10 * _BATCH) -> int:
    """Deletes the bytes of artifacts whose retention is over, and records it once per run.
    Returns how many it deleted."""
    deleted = 0
    while deleted < limit:
        with transaction.atomic():
            due = list(
                RunArtifact.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("upload", "run__project")
                .filter(deleted_at__isnull=True, expires_at__lte=timezone.now())
                .order_by("expires_at")[:_BATCH]
            )
            if not due:
                break
            now = timezone.now()
            per_run: dict[uuid.UUID, list[RunArtifact]] = defaultdict(list)
            for artifact in due:
                if artifact.upload.state in ("pending", "checking"):
                    files.cancel(artifact.upload)
                storage.delete_for_good(storage.artifact_key(artifact.upload_id))
                artifact.deleted_at = now
                per_run[artifact.run_id].append(artifact)
            RunArtifact.objects.bulk_update(due, ["deleted_at"])
            for artifacts in per_run.values():
                run = artifacts[0].run
                audit.record(
                    "run.artifacts_deleted",
                    target=run,
                    project=run.project,
                    data=_deleted(run, artifacts),
                )
            deleted += len(due)
    return deleted


def _deleted(run: ActionRun, artifacts: list[RunArtifact]) -> dict[str, Any]:
    return {
        "count": len(artifacts),
        "bytes": sum(a.upload.size for a in artifacts),
        "retention_days": retention(run).days,
        "paths": [a.path for a in artifacts[:20]],
    }
