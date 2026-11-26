"""Many records as one file (ADR 0229): a selection, or every record a table's search finds,
as FASTA, GenBank or SDF.

Asking pins each record at the revision it has then, read in one snapshot, so editing one
afterwards doesn't change what's written. Each must be one its requester can read, of a
type that exports the format. The file is written by the format's writer
(:mod:`iki_server.entities.exports`), one record after another in the order asked for, to
a spool file, and kept in the file store only once it's whole: a writer that refuses a
record fails the export, naming it, so no file holds fewer records than were asked for.
Up to :data:`INLINE` records are written while the request waits; more, by a background
task that says how far it's got.

The file is its requester's, to download as often as they like for :data:`KEPT`, unless a
project of its records is under a legal hold. Each download checks they can still read
every record it holds and what those refer to. Asking, and each download, are in the
audit log: which records at which revisions, the file's SHA-256 and size, what it
couldn't hold of each, and which writer wrote it.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.core.snapshots import snapshot
from iki_server.entities import access, exports, registry, search
from iki_server.entities.models import Entity, ProjectLink, RecordExport, RecordExportState
from iki_server.entities.sequence_files import Written
from iki_server.files import storage
from iki_server.org import holds
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.tasks import task
from iki_spec.model import CompoundExport, ReportExport

logger = structlog.get_logger("iki.record_exports")

#: The most records one file holds.
MAX_RECORDS = 10_000
#: The largest file written: past it, the export fails, asking for fewer records.
MAX_BYTES = 512 * 1024 * 1024
#: Up to these many are written while the request waits; more, in the background.
INLINE = 100
#: How long a file is kept to download again.
KEPT = timedelta(days=7)
#: Records written between two updates of how far it's got.
PROGRESS_EVERY = 100
#: A search's records are read this many at a time, all in one snapshot.
PAGE = 500
#: Refusals a failed export names, of all its writer refused.
NAMED = 10


class NotReadable(Refused):
    """A record picked that isn't one its requester can read: answered as not found."""


@dataclass(frozen=True)
class Pinned:
    """A record at the revision it had when its export was asked for."""

    id: uuid.UUID
    hrid: str
    revision: int
    type: str
    project: uuid.UUID | None


def start(
    by: User,
    file_format: str,
    *,
    entities: Sequence[str] = (),
    wanted: search.Search | None = None,
    project: Project | None = None,
    data: Sequence[str] | None = None,
    described: str = "",
) -> RecordExport:
    """Asks for records as one ``file_format`` file: ``entities`` (by UUID, or HRID, in
    ``project`` where one names a record in several), or every one ``wanted`` finds, in its
    order. ``data`` names an SDF's fields. Raises :class:`Refused`, or a search's errors."""
    if (wanted is None) == (not entities):
        raise Refused("Pick records, or give a search, to export.", field="entities")
    if wanted is None:
        if len(entities) > MAX_RECORDS:
            raise Refused(f"Pick at most {MAX_RECORDS:,} records for one file.", field="entities")
        with snapshot():
            pinned = [_found(by, ref, project) for ref in dict.fromkeys(entities)]
        pinned = list({p.id: p for p in pinned}.values())
        selection = _listed(pinned)
    else:
        pinned = _searched(by, wanted)
        selection = described or f"{len(pinned):,} found by a search"
    if not pinned:
        raise Refused("There's nothing to export: the search finds no records.", field="search")
    chosen, fields = _checked(pinned, file_format, data)
    projects = Project.objects.filter(pk__in={p.project for p in pinned if p.project})
    read_in = _read_in(by, pinned)
    name = _file_name(pinned, chosen, list(projects))
    records = [[str(p.id), p.hrid, p.revision] for p in pinned]
    with transaction.atomic():
        made = RecordExport.objects.create(
            requested_by=by,
            agent=audit.current_agent(),
            via=audit.current_via(),
            format=chosen.format,
            selection=selection,
            search=_search_of(wanted),
            records=records,
            fields=fields,
            projects=[p.pk for p in projects],
            read_in=sorted(read_in),
            name=name,
        )
        audit.record(
            "entities.export_requested",
            actor=by,
            target=made,
            project=projects[0] if len(projects) == 1 else None,
            data={
                "export": str(made.pk),
                "format": chosen.format,
                "selection": selection,
                "search": made.search,
                "fields": fields,
                "count": len(records),
                "records": [{"id": i, "hrid": h, "revision": r} for i, h, r in records],
            },
        )
        if len(pinned) > INLINE:
            # The task is a row in the same transaction: it runs only if this commits.
            write_record_export.enqueue(str(made.pk))
    if len(pinned) <= INLINE:
        _write(made)
        made.refresh_from_db()
    return made


def _found(by: User, ref: str, project: Project | None) -> Pinned:
    readable = access.visible(by).filter(purged_at__isnull=True)
    try:
        matches = list(readable.filter(pk=uuid.UUID(ref)))
    except ValueError:
        matches = list(readable.filter(hrid=ref.strip().upper()))
    if len(matches) > 1:
        held = [m for m in matches if project is not None and m.owner_project_id == project.pk]
        if len(held) != 1:
            raise Refused(
                f"{matches[0].hrid} names a record in each of several projects: pick it by "
                "its UUID, or say which project's.",
                field="entities",
            )
        matches = held
    if not matches:
        raise NotReadable(f"There's no entity {ref} you can read.", field="entities")
    found = matches[0]
    return Pinned(found.pk, found.hrid, found.revision, found.type, found.owner_project_id)


def _read_in(by: User, pinned: list[Pinned]) -> set[uuid.UUID]:
    """For each record, a project its reader reads it through: its own project where they
    can, or one it's linked into."""
    readable = set(access.readable_projects(by).values_list("pk", flat=True))
    through: dict[uuid.UUID, uuid.UUID] = {
        p.id: p.project for p in pinned if p.project is not None and p.project in readable
    }
    if rest := [p.id for p in pinned if p.id not in through]:
        for entity, project in ProjectLink.objects.filter(
            entity_id__in=rest, project_id__in=readable
        ).values_list("entity_id", "project_id"):
            through.setdefault(entity, project)
    return set(through.values())


def _searched(by: User, wanted: search.Search) -> list[Pinned]:
    """Every record the search finds, in its order, each at its revision then: all of them
    read in one snapshot, each page under a search's time limit."""
    found: list[Pinned] = []
    with snapshot():
        records = search.queryset(by, wanted)
        cursor: str | None = None
        while True:
            with search.time_limit():
                rows, cursor = search.page(records, wanted, cursor, PAGE)
            found += [Pinned(e.pk, e.hrid, e.revision, e.type, e.owner_project_id) for e in rows]
            if len(found) > MAX_RECORDS:
                raise Refused(
                    f"The search finds more than {MAX_RECORDS:,} records, the most one file "
                    "holds: narrow it first.",
                    field="search",
                )
            if cursor is None:
                return found


def _checked(
    pinned: list[Pinned], file_format: str, data: Sequence[str] | None
) -> tuple[exports.Format, list[str]]:
    """The format, which each record's type must export for many records, and the fields
    an SDF carries: those asked for, or every one all its types can carry."""
    chosen: exports.Format | None = None
    compound: dict[str, CompoundExport] = {}
    unexported: dict[str, list[str]] = {}
    for one in pinned:
        match = exports.format_of(one.type, file_format)
        if match is None or match[1].many != "file":
            unexported.setdefault(one.type, []).append(one.hrid)
            continue
        export_, chosen = match
        if isinstance(export_, CompoundExport):
            compound[one.type] = export_
    if unexported or chosen is None:
        named = "; ".join(
            f"{_plural(t)} ({', '.join(h[:3])}{' and more' if len(h) > 3 else ''})"
            for t, h in unexported.items()
        )
        raise Refused(
            f"Not every record exports as one {file_format} file of many: {named} don't.",
            field="format",
        )
    if data is not None and chosen.format != "sdf":
        raise Refused(f"{chosen.label} holds no fields as data.", field="fields")
    if chosen.format != "sdf":
        return chosen, []
    if data is None:
        offered = [exports.data_fields(t, e) for t, e in compound.items()]
        return chosen, [f for f in offered[0] if all(f in o for o in offered[1:])]
    for type_name, export_ in compound.items():
        try:
            exports.check_data_fields(type_name, export_, data)
        except exports.Unexportable as why:
            raise Refused(str(why), field="fields") from None
    return chosen, list(dict.fromkeys(data))


def _plural(type_name: str) -> str:
    found = registry.types().get(type_name)
    if found is None:
        return type_name
    return found.label_plural or f"{found.label or found.name}s"


def _listed(pinned: list[Pinned]) -> str:
    shown = ", ".join(p.hrid for p in pinned[:5])
    more = f" and {len(pinned) - 5:,} more" if len(pinned) > 5 else ""
    return f"{shown}{more}"


#: What a download's name may not hold: path separators, and what some systems refuse.
_NOT_IN_A_NAME = str.maketrans(dict.fromkeys('\\/:*?"<>|', "_"))


def _file_name(pinned: list[Pinned], chosen: exports.Format, projects: list[Project]) -> str:
    """``Kinase Atlas Proteins (12).fasta``: the project's name where they're all one's,
    what they are, and how many."""
    kinds = {p.type for p in pinned}
    what = _plural(next(iter(kinds))) if len(kinds) == 1 else "Records"
    words = f"{projects[0].name} {what}" if len(projects) == 1 else what
    safe = " ".join("".join(c for c in words if c.isprintable()).translate(_NOT_IN_A_NAME).split())
    return f"{safe[:150]} ({len(pinned):,}).{chosen.extension}"


def _search_of(wanted: search.Search | None) -> dict[str, Any] | None:
    if wanted is None:
        return None
    return {
        "type": wanted.type,
        "exact_type": wanted.exact_type,
        "project": str(wanted.project) if wanted.project else None,
        "archived": wanted.archived,
        "q": wanted.q,
        "collection": wanted.collection,
        "run": wanted.run,
        "where": dict(wanted.where) if wanted.where else None,
        "sort": wanted.sort,
        "descending": wanted.descending,
    }


# --- Writing ------------------------------------------------------------------------------------


@task()
def write_record_export(export_id: str) -> str:
    """Writes an export's file and keeps it in the file store."""
    made = RecordExport.objects.select_related("requested_by").get(pk=export_id)
    # Again if a runner stopped partway: the file is written afresh, to the same key.
    if made.state not in (RecordExportState.QUEUED, RecordExportState.WRITING):
        return str(made.state)
    return _write(made)


class _TooBig(Exception):  # noqa: N818 - caught here, never raised beyond
    pass


def _write(made: RecordExport) -> str:
    made.state = RecordExportState.WRITING
    made.save(update_fields=["state"])
    by = made.requested_by
    chosen = next(f for fs in exports.FORMATS.values() for f in fs if f.format == made.format)
    file_writer = exports.writer(made.format)
    notes: dict[str, list[str]] = {}
    refused: dict[str, str] = {}
    read: set[uuid.UUID] = set()
    digest = hashlib.sha256()
    size = 0
    try:
        with tempfile.TemporaryDirectory(prefix="iki-record-export-") as tmp:
            spool = Path(tmp) / "file"
            with spool.open("wb") as out:

                def add(content: bytes) -> None:
                    nonlocal size
                    size += len(content)
                    if size > MAX_BYTES:
                        raise _TooBig
                    digest.update(content)
                    out.write(content)

                add(file_writer.begin(len(made.records)))
                for done, (key, hrid, revision) in enumerate(made.records, start=1):
                    gathered = _gathered(by, key, hrid, revision, chosen, made.fields, refused)
                    written = (
                        _written(file_writer, gathered, refused) if gathered is not None else None
                    )
                    if gathered is not None and written is not None:
                        read |= gathered.read
                        if written.left_out:
                            notes[hrid] = written.left_out
                        if not refused:
                            add(written.content)
                    if done % PROGRESS_EVERY == 0:
                        RecordExport.objects.filter(pk=made.pk).update(done=done)
                tail = file_writer.end()
                add(tail.content)
            if refused:
                return _failed(made, _refusal(refused), refused=refused)
            storage.upload_file(spool, storage.record_export_key(made.pk), chosen.media_type)
    except _TooBig:
        return _failed(
            made,
            f"The file would be over {MAX_BYTES // 1024 // 1024:,} MB: export fewer records "
            "at a time.",
        )
    except Exception:
        logger.exception("record_export_failed", export=str(made.pk))
        _failed(made, "Something went wrong writing it. Try again, or tell your administrator.")
        raise
    now = timezone.now()
    made.state = RecordExportState.READY
    made.done = len(made.records)
    made.sha256 = digest.hexdigest()
    made.size = size
    made.notes = notes
    made.references = sorted(read)
    made.writer = dict(file_writer.identity)
    made.finished_at = now
    made.expires_at = now + KEPT
    made.save(
        update_fields=[
            "state",
            "done",
            "sha256",
            "size",
            "notes",
            "references",
            "writer",
            "finished_at",
            "expires_at",
        ]
    )
    return str(made.state)


def _gathered(
    by: User,
    key: str,
    hrid: str,
    revision: int,
    chosen: exports.Format,
    fields: list[str],
    refused: dict[str, str],
) -> exports.Gathered | None:
    """What a pinned record is written from; or None, saying why it can't be."""
    entity = Entity.objects.filter(pk=key, purged_at__isnull=True).first()
    if entity is None:
        refused[hrid] = "it was purged after the export was asked for."
        return None
    match = exports.format_of(entity.type, chosen.format)
    if match is None or isinstance(match[0], ReportExport):
        refused[hrid] = f"its type no longer exports {chosen.label}."
        return None
    try:
        return exports.gather(
            by, entity, revision, match[0], chosen, data=fields if chosen.format == "sdf" else None
        )
    except exports.Unexportable as why:
        refused[hrid] = str(why)
        return None


def _written(
    file_writer: exports.Writer, gathered: exports.Gathered, refused: dict[str, str]
) -> Written | None:
    """The record as the file's writer writes it; or None, saying why it refused."""
    try:
        return exports.written_by(file_writer, gathered)
    except exports.Unexportable as why:
        refused[gathered.record.hrid] = str(why)
        return None


def _refusal(refused: dict[str, str]) -> str:
    shown = [f"{hrid}: {why}" for hrid, why in list(refused.items())[:NAMED]]
    more = len(refused) - NAMED
    rest = f" And {more:,} more." if more > 0 else ""
    count = len(refused)
    return (
        f"{count:,} record{'s' if count > 1 else ''} can't be written, so no file was made: "
        "export again without "
        f"{'them' if count > 1 else 'it'}. " + " ".join(shown) + rest
    )


def _failed(made: RecordExport, message: str, *, refused: dict[str, str] | None = None) -> str:
    made.state = RecordExportState.FAILED
    made.error = message
    made.refused = refused or {}
    made.finished_at = timezone.now()
    made.save(update_fields=["state", "error", "refused", "finished_at"])
    return str(made.state)


# --- Downloading ---------------------------------------------------------------------------------


def theirs(by: User) -> QuerySet[RecordExport]:
    """The exports ``by`` asked for whose records are all in projects they can read now, as
    a token given some projects reads them (ADR 0152)."""
    readable = list(access.readable_projects(by).values_list("pk", flat=True))
    return RecordExport.objects.filter(requested_by=by, read_in__contained_by=readable)


def download_url(by: User, made: RecordExport) -> str:
    """A short-lived URL for the file, for its requester while they can read all of it.
    Each one is in the audit log."""
    if made.state != RecordExportState.READY:
        raise Refused(
            "It expired: its file was deleted. Export again."
            if made.state == RecordExportState.EXPIRED
            else "It failed: there's no file to download."
            if made.state == RecordExportState.FAILED
            else "It isn't ready yet."
        )
    if made.references is None or not access.all_references_readable(by, made.references):
        raise Refused("It holds a record you can no longer read, so it can't be downloaded.")
    chosen = next(f for fs in exports.FORMATS.values() for f in fs if f.format == made.format)
    projects = list(Project.objects.filter(pk__in=made.projects))
    audit.record(
        "entities.exported",
        actor=by,
        target=made,
        project=projects[0] if len(projects) == 1 else None,
        data={
            "export": str(made.pk),
            "format": made.format,
            "file": made.name,
            "sha256": made.sha256,
            "size": made.size,
            "count": len(made.records),
            "records": [{"id": i, "hrid": h, "revision": r} for i, h, r in made.records],
            # What the file couldn't hold of each record, as it says it where it can.
            "left_out": made.notes,
            "fields": made.fields,
            "writer": made.writer,
        },
    )
    return storage.download_url(
        storage.uri(storage.record_export_key(made.pk)), "", made.name, chosen.media_type
    )


def expire() -> int:
    """Deletes the files of exports kept their week, except those of held projects."""
    held = [p.pk for p in holds.held_projects()]
    due = RecordExport.objects.filter(state=RecordExportState.READY, expires_at__lte=timezone.now())
    if held:
        due = due.exclude(projects__overlap=held)
    count = 0
    for made in due:
        storage.delete_for_good(storage.record_export_key(made.pk))
        made.state = RecordExportState.EXPIRED
        made.save(update_fields=["state"])
        count += 1
    return count
