"""Exports: a project's Admin asks for a bundle, a task writes it, and they download it.

What's picked is checked when it's asked for. A background task writes the archive
(:mod:`.export`) and keeps it in the file store for :data:`KEPT`, then ``maintain``
deletes it, unless the project is under a legal hold. Asking, the archive and each
download are in the audit log.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import structlog
from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.bundles import export
from iki_server.bundles.models import BundleExport, ExportState
from iki_server.entities import access, collections, registry
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.org import holds
from iki_server.org.models import Project
from iki_server.org.permissions import Action, can
from iki_server.org.services import Refused
from iki_server.tasks import task

logger = structlog.get_logger("iki.bundles")

#: How long an export's archive is kept to download.
KEPT = timedelta(days=7)
MEDIA_TYPE = "application/gzip"
#: The most entities one export may pick (what they refer to comes as well).
MAX_PICKED = 10_000


def start(
    by: User,
    project: Project,
    *,
    reason: str,
    collection: str | None = None,
    entities: Sequence[str] = (),
) -> BundleExport:
    """Asks for a bundle of a collection (with what it holds), or of entities by ID or HRID."""
    if not can(by, Action.PROJECT_EXPORT, project):
        raise Refused(f"Exporting from {project.name} needs its Admin role.")
    reason = reason.strip()
    if not reason:
        raise Refused(
            "Say why you're exporting these: the bundle and the audit log keep it.", field="reason"
        )
    if (collection is None) == (not entities):
        raise Refused("Pick a collection, or entities, to export.", field="collection")
    if collection is not None:
        picked, selection = _collection(by, project, collection)
    else:
        if len(entities) > MAX_PICKED:
            raise Refused(f"Pick at most {MAX_PICKED:,} entities at a time.", field="entities")
        picked = [_find(by, project, ref, "entities") for ref in dict.fromkeys(entities)]
        selection = _listed(picked)
    with transaction.atomic():
        made = BundleExport.objects.create(
            project=project,
            requested_by=by,
            agent=audit.current_agent(),
            via=audit.current_via(),
            reason=reason,
            selection=selection,
            picked=[e.pk for e in picked],
        )
        audit.record(
            "bundle.export_requested",
            actor=by,
            target=made,
            project=project,
            data={"export": str(made.pk), "selection": selection, "reason": reason},
        )
        # The task is a row in the same transaction: it runs only if this commits.
        write_export.enqueue(str(made.pk))
    return made


def _find(by: User, project: Project, ref: str, field: str) -> Entity:
    readable = access.visible(by)
    try:
        found = readable.filter(pk=uuid.UUID(ref)).first()
    except ValueError:
        found = readable.filter(hrid=ref.strip().upper()).first()
    if found is None:
        raise Refused(f"There's no entity {ref} you can read.", field=field)
    if found.owner_project_id != project.pk:
        raise Refused(
            f"{found.hrid} belongs to another project, so it can't go from this one.", field=field
        )
    return found


def _collection(by: User, project: Project, ref: str) -> tuple[list[Entity], str]:
    entity = _find(by, project, ref, "collection")
    if not collections.is_collection(entity.type):
        raise Refused(f"{entity.hrid} isn't a collection.", field="collection")
    row = registry.model(entity.type)._default_manager.get(pk=entity.pk)
    name = row.name  # type: ignore[attr-defined]
    if row.search is None:  # type: ignore[attr-defined]
        # Its members are references: they go with it, those in this project.
        return [entity], f"{entity.hrid} ({name}), with its members"
    try:
        found = collections.found_now(by, row.search)  # type: ignore[attr-defined]
    except collections.TooMany:
        raise Refused(
            f"{entity.hrid}'s search finds more than {collections.max_members():,} entities, "
            "the most one export takes: narrow its search first.",
            field="collection",
        ) from None
    mine = list(Entity.objects.filter(pk__in=found, owner_project=project))
    elsewhere = len(found) - len(mine)
    note = f"; {elsewhere:,} it finds in other projects stay out" if elsewhere else ""
    return [entity, *mine], f"{entity.hrid} ({name}), with the {len(mine):,} it finds now{note}"


def _listed(picked: list[Entity]) -> str:
    shown = ", ".join(e.hrid for e in picked[:5])
    more = f" and {len(picked) - 5:,} more" if len(picked) > 5 else ""
    return f"{shown}{more}"


@task()
def write_export(export_id: str) -> str:
    """Writes the export's bundle and keeps it in the file store."""
    made = BundleExport.objects.select_related("project", "requested_by", "agent").get(pk=export_id)
    if made.state != ExportState.QUEUED:
        return made.state
    made.state = ExportState.WRITING
    made.save(update_fields=["state"])
    picked = list(Entity.objects.filter(pk__in=made.picked))
    try:
        with tempfile.TemporaryDirectory(prefix="iki-export-") as tmp:
            out = Path(tmp) / "bundle.tar.gz"
            written = export.write(
                made.project,
                picked,
                out,
                by=made.requested_by,
                reason=made.reason,
                selection=made.selection,
                agent=made.agent,
                export_id=made.pk,
            )
            storage.upload_file(out, storage.bundle_key(made.pk), MEDIA_TYPE)
    except export.ExportError as error:
        return _failed(made, str(error))
    except Exception:
        logger.exception("bundle_export_failed", export=str(made.pk))
        _failed(made, "Something went wrong writing it. Try again, or tell your administrator.")
        raise
    now = timezone.now()
    made.state = ExportState.READY
    made.sha256 = written.sha256
    made.size = written.size
    made.counts = written.counts
    made.references = sorted(written.references)
    made.finished_at = now
    made.expires_at = now + KEPT
    made.save(
        update_fields=[
            "state",
            "sha256",
            "size",
            "counts",
            "references",
            "finished_at",
            "expires_at",
        ]
    )
    return made.state


def _failed(made: BundleExport, message: str) -> str:
    made.state = ExportState.FAILED
    made.error = message
    made.finished_at = timezone.now()
    made.save(update_fields=["state", "error", "finished_at"])
    return made.state


def file_name(made: BundleExport) -> str:
    return f"{made.project.slug}-{made.created_at:%Y%m%d-%H%M%S}.iki-bundle.tar.gz"


def download_url(by: User, made: BundleExport) -> str:
    """A short-lived URL for the archive. Each one is in the audit log."""
    if made.state != ExportState.READY:
        raise Refused(
            "It expired: its archive was deleted. Export again."
            if made.state == ExportState.EXPIRED
            else "It isn't ready yet."
        )
    if made.references is None:
        raise Refused("Make a fresh export to check read access to its complete history.")
    if not access.all_references_readable(by, made.references):
        raise Refused("This bundle refers to a record you can't read.")
    audit.record(
        "bundle.downloaded",
        actor=by,
        target=made,
        project=made.project,
        data={"export": str(made.pk), "sha256": made.sha256},
    )
    return storage.download_url(
        storage.uri(storage.bundle_key(made.pk)), "", file_name(made), MEDIA_TYPE
    )


def expire() -> int:
    """Deletes the archives of exports kept their week, except in held projects."""
    due = BundleExport.objects.filter(
        state=ExportState.READY, expires_at__lte=timezone.now()
    ).exclude(project__in=holds.held_projects())
    count = 0
    for made in due:
        storage.delete_for_good(storage.bundle_key(made.pk))
        made.state = ExportState.EXPIRED
        made.save(update_fields=["state"])
        count += 1
    return count
