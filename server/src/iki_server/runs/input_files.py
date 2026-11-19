"""Files a run's inputs and file params hold, which its worker downloads before the command
starts.

The run input lists them (``files``, by ID, with name, size and SHA-256), and the worker
asks for each through ``POST /worker/v1/jobs/{id}/files/{file_id}``, which answers a
short-lived download URL. The job keeps the set its run input listed when it was leased:
the files its inputs hold, and those of the runs packed into it (ADR 0055), at the
revisions they were launched on; those their included references hold, at the revisions
pinned (ADR 0105); and those its file params name. Its worker can download those, and
only those, and only while it holds the job's lease.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction

from iki_server.files import storage
from iki_server.files.models import File
from iki_server.runs.models import ActionRun, Worker
from iki_server.runs.queue import held
from iki_spec.model import Field

__all__ = ["describe", "download", "file_ids", "is_file_list", "of_params"]


def is_file_list(f: Field) -> bool:
    """A list of files: a field's, or a param of several files."""
    return f.type == "list" and f.items is not None and f.items.type == "file"


def file_ids(f: Field, value: Any) -> set[uuid.UUID]:  # noqa: ANN401 - a field's value
    """The file IDs in a value of field `f`; unreadable ones are left for its own check."""
    if value is None:
        return set()
    if is_file_list(f):
        items = value if isinstance(value, list) else []
    elif f.type == "file":
        items = [value]
    else:
        return set()
    out: set[uuid.UUID] = set()
    for item in items:
        try:
            out.add(item if isinstance(item, uuid.UUID) else uuid.UUID(str(item)))
        except ValueError:
            continue
    return out


def of_params(run: ActionRun) -> set[uuid.UUID]:
    """The files the run's file params name, single or in lists, as the action was when it
    was launched."""
    declared = run.spec.get("params") or {}
    found: set[uuid.UUID] = set()
    for name, value in run.params.items():
        spec = declared.get(name) or {}
        if value is None:
            continue
        if spec.get("type") == "file":
            found.add(uuid.UUID(str(value)))
        elif spec.get("type") == "list" and (spec.get("items") or {}).get("type") == "file":
            found |= {uuid.UUID(str(v)) for v in value}
    return found


def describe(ids: set[uuid.UUID]) -> dict[str, dict[str, Any]]:
    """What the run input says of each file: enough for the worker to check what it gets."""
    return {
        str(f.pk): {
            "name": f.name,
            "media_type": f.media_type,
            "size": f.blob.size,
            "sha256": f.blob.sha256,
        }
        for f in File.objects.filter(pk__in=ids).select_related("blob").order_by("pk")
    }


def download(
    worker: Worker, job_id: uuid.UUID, token: uuid.UUID, file_id: uuid.UUID
) -> tuple[File, str]:
    """The file and a URL that downloads it for five minutes, if the job's run input lists
    it. LookupError if it doesn't, as for a file that doesn't exist."""
    with transaction.atomic():
        job = held(worker, job_id, token)
        if file_id not in job.input_files:
            raise LookupError("This job's run input doesn't list that file.")
        found = File.objects.select_related("blob").get(pk=file_id)
    blob = found.blob
    return found, storage.download_url(
        blob.storage_uri, blob.object_version, found.name, found.media_type, workers=True
    )
