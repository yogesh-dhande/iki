"""Files a run's inputs hold, which its worker downloads before the command starts.

The run input lists them (``files``, by ID, with name, size and SHA-256), and the worker
asks for each through ``POST /worker/v1/jobs/{id}/files/{file_id}``, which answers a
short-lived download URL. A worker can download only files its job's inputs hold, at
the revisions the run was launched on, and only while it holds the job's lease.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction

from iki_server.entities import services as entities
from iki_server.entities.values import shape
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.runs.models import ActionRun, RunInput, Worker
from iki_server.runs.queue import held
from iki_spec.model import Field

__all__ = ["describe", "download", "file_ids", "of_run"]


def file_ids(f: Field, value: Any) -> set[uuid.UUID]:  # noqa: ANN401 - a field's value
    """The file IDs in a value of field `f`; unreadable ones are left for its own check."""
    if value is None:
        return set()
    if f.type == "list" and f.items is not None and f.items.type == "file":
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


def of_run(run: ActionRun) -> set[uuid.UUID]:
    """Every file the run's inputs hold, at the revisions it was launched on."""
    found: set[uuid.UUID] = set()
    for row in RunInput.objects.filter(run=run).select_related("entity"):
        fields = shape(row.entity.type).fields()
        for name, value in entities.values_at(row.entity, row.revision).items():
            if name in fields:
                found |= file_ids(fields[name], value)
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
    """The file and a URL that downloads it for five minutes, if the job's inputs hold it.
    LookupError if they don't, as for a file that doesn't exist."""
    with transaction.atomic():
        job = held(worker, job_id, token)
        if file_id not in of_run(job.run):
            raise LookupError("This job's inputs don't hold that file.")
        found = File.objects.select_related("blob").get(pk=file_id)
    blob = found.blob
    return found, storage.download_url(
        blob.storage_uri, blob.object_version, found.name, found.media_type, workers=True
    )
