"""A record's audit trail (ADR 0176): who changed it, as its page's Audit trail tab filters
by, and the whole trail as CSV, for inspection.

The trail is the record's revisions and, for a record of a registry entry (ADR 0115), its
entry's, each told as ``/entities/{ref}/revisions`` and ``/entries/{ref}/revisions`` tell
it: an entry's revision made through a project the reader can't read says what it set,
never who, how or when. 21 CFR 11.10(b) and (e) ask that the trail be available to review
and copy; the CSV is that copy. Its cells are written as every Iki CSV is (``csvs``), so
none runs as a formula in a spreadsheet.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from ninja import Router

from iki_server.accounts.models import User
from iki_server.api import csvs
from iki_server.api.common import caller
from iki_server.api.entities import (
    _find,
    _holds_opened,
    _ids_in,
    _lookup,
    _people_out,
    _runs_of,
    _type_out,
    refs_in,
)
from iki_server.api.schemas import UserOut
from iki_server.audit import log as audit
from iki_server.entities import access, registry, services
from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.search_exports import text
from iki_server.entities.values import shape, to_json
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.signatures import models as signing_models
from iki_server.signatures import services as sig_services

router = Router(tags=["entities"])

#: What each kind of revision did, as the record's page says it.
KIND: dict[str, str] = {
    RevisionKind.CREATED: "Created",
    RevisionKind.EDITED: "Edited",
    RevisionKind.ARCHIVED: "Archived",
    RevisionKind.RESTORED: "Restored",
    RevisionKind.CURATED: "Facts changed",
    RevisionKind.SUPERSEDED: "Superseded",
    RevisionKind.MOVED: "Moved",
    RevisionKind.STATE_CHANGED: "State set",
}

#: How a revision was made, as the record's page says it.
VIA = {
    "web": "Web app",
    "api": "API",
    "mcp": "AI assistant (MCP)",
    "cli": "Command line",
    "scim": "Provisioning",
    "system": "Iki",
    "run": "Run",
}

#: What an entry's revision says of who made it, to a reader it isn't disclosed to.
UNDISCLOSED = "Made through a project you can’t read"

COLUMNS = [
    "Record",
    "Project",
    "Of",
    "Revision",
    "When (UTC)",
    "What",
    "Who",
    "Email",
    "How",
    "Run",
    "Agent",
    "Reason",
    "Field",
    "Before",
    "After",
    "Signatures",
    "Holds",
]

_NOT_IN_A_NAME = re.compile(r"[^\w .()-]+")


def _curates(user: User, record: Entity) -> bool:
    """Whether the reader is a registrar of the record's entry's registry: they're told
    every revision of it in full, as ``/entries/{ref}/revisions`` tells them."""
    entry = record.entry
    if entry is None or entry.registry is None:
        return False
    return permissions.can(user, Action.REGISTRY_CURATE, entry.registry)


def _entry_revisions(user: User, record: Entity) -> tuple[Any, set[Any], bool]:
    """The record's entry's revisions, the projects the reader reads, and whether they
    curate the entry. None of the first, for a record of no entry."""
    if record.entry_id is None:
        return None, set(), False
    readable = set(access.readable_projects(user).values_list("pk", flat=True))
    return Revision.objects.filter(entity_id=record.entry_id), readable, _curates(user, record)


@router.get(
    "/entities/{ref}/contributors",
    response=list[UserOut],
    summary="Who changed it",
    by_alias=True,
)
def list_contributors(request: HttpRequest, ref: str, project: UUID | None = None) -> Any:  # noqa: ANN401 - a QuerySet
    """Everyone who made one of its revisions, by name: whom its audit trail can be
    narrowed to (``person``). For a record of a registry entry, also who made the entry's
    revisions, of those you're told who made. ``project`` says whose record, as for
    reading it."""
    me = caller(request)
    record = _find(me, ref, project)
    ids = set(
        Revision.objects.filter(entity=record, user__isnull=False).values_list("user_id", flat=True)
    )
    entry, readable, curates = _entry_revisions(me, record)
    if entry is not None:
        told = entry if curates else entry.filter(services.disclosing(readable))
        ids |= set(told.filter(user__isnull=False).values_list("user_id", flat=True))
    return User.objects.filter(pk__in=ids).order_by("name", "email", "pk")


_CSV = {"responses": {200: {"description": "As CSV", "content": {"text/csv": {}}}}}


@router.get(
    "/entities/{ref}/revisions.csv",
    summary="Download its audit trail, for inspection",
    openapi_extra=_CSV,
)
def trail_csv(request: HttpRequest, ref: str, project: UUID | None = None) -> Any:  # noqa: ANN401 - a streamed response
    """Every revision, oldest first, a row for each field it set with that field's value
    before and after: who made it, how (the web app, the API, a run, …), when in UTC, why,
    the electronic signatures that set a state (ADR 0126) and the holds it opened (ADR
    0127). For a record of a registry entry, its entry's revisions follow, as you're told
    them: one made through a project you can't read says what it set, not who, how or when.

    Anyone who can read the record may download it, and each download is in the audit
    log (``entity.trail_exported``) before its first row is sent, with the record's
    ``revision`` and its entry's ``entry_revision``: the file holds those and the ones
    before them, and nothing made after. No cell starts as a
    formula would (OWASP's CSV injection guidance): one that would starts with a quote."""
    me = caller(request)
    record = _find(me, ref, project)
    entry, readable, curates = _entry_revisions(me, record)
    place = record.owner_project
    name = _file_name(record)
    with transaction.atomic():
        # The file holds the trail as of this event (ADR 0207): the record's and its
        # entry's revisions, read together in one statement, bound what's streamed below,
        # however late it's read.
        bounds = dict(
            Entity.objects.filter(pk__in=[record.pk, record.entry_id]).values_list("pk", "revision")
        )
        upto = bounds[record.pk]
        entry_upto = bounds.get(record.entry_id) if record.entry_id is not None else None
        audit.record(
            "entity.trail_exported",
            actor=me,
            target=record,
            project=place,
            data={"format": "csv", "file": name, "revision": upto, "entry_revision": entry_upto},
        )
    rows = _Trail(me, record, place.name if place is not None else "")

    def table() -> Iterator[list[Any]]:
        yield COLUMNS
        mine = Revision.objects.filter(entity=record, number__lte=upto)
        yield from rows.of(record, mine, told=None)
        if entry is not None and record.entry is not None and entry_upto is not None:
            yield from rows.of(
                record.entry,
                entry.filter(number__lte=entry_upto),
                told=None if curates else readable,
                of="Registry entry",
            )

    return csvs.streamed(request, csvs.chunks(table()), name)


def _file_name(record: Entity) -> str:
    """``PRT-000001 Kinase Atlas audit trail.csv``: a record of an entry is named for its
    project too, as other projects' records have its ID."""
    place = record.owner_project
    words = f"{record.hrid} {place.name}" if record.entry_id and place else record.hrid
    return " ".join(_NOT_IN_A_NAME.sub("_", f"{words} audit trail").split())[:150] + ".csv"


class _Trail:
    """The rows of a record's trail, a revision at a time."""

    #: Revisions read at once: their runs, holds and signatures are found together.
    BATCH = 200

    def __init__(self, user: User, record: Entity, project: str) -> None:
        self.user = user
        self.record = record
        self.project = project
        entity_type = registry.types().get(record.type)
        self.labels: dict[str, Mapping[str, Any]] = (
            {f["name"]: f for f in _type_out(entity_type)["fields"]}
            if entity_type is not None
            else {}
        )

    def of(
        self,
        entity: Entity,
        revisions: Any,  # noqa: ANN401 - a QuerySet of Revision
        *,
        told: set[Any] | None,
        of: str = "Record",
    ) -> Iterator[list[Any]]:
        """``entity``'s revisions as rows: the record's, or its entry's. ``told``, for an
        entry, says which projects' revisions the reader is told who made."""
        fields = (
            shape(entity.type).entry_fields(removed=True)
            if entity.registry_id is not None
            else shape(entity.type).fields(removed=True)
        )
        batch: list[Revision] = []
        for revision in (
            revisions.select_related("user", "agent")
            .order_by("number")
            .iterator(chunk_size=self.BATCH)
        ):
            batch.append(revision)
            if len(batch) == self.BATCH:
                yield from self._rows(entity, batch, fields, told, of)
                batch = []
        if batch:
            yield from self._rows(entity, batch, fields, told, of)

    def _rows(
        self,
        entity: Entity,
        revisions: list[Revision],
        fields: Mapping[str, Any],
        told: set[Any] | None,
        of: str,
    ) -> Iterator[list[Any]]:
        runs = _runs_of(self.user, revisions)
        holds = _holds_opened(self.record, revisions) if entity.pk == self.record.pk else {}
        signed = _signatures(revisions)
        for revision in revisions:
            disclosed = told is None or services.discloses(revision, told)
            head = [self.record.hrid, self.project, of, revision.number]
            if disclosed:
                run = runs.get((revision.entity_id, revision.number))
                head += [
                    _when(revision),
                    _what(entity, revision),
                    revision.user.name if revision.user else "",
                    revision.user.email if revision.user else "",
                    VIA.get(revision.via, revision.via),
                    run["hrid"]
                    if run
                    else ("A run in a project you can’t read" if run is not None else ""),
                    revision.agent.client_name if revision.agent else revision.client,
                    revision.reason,
                ]
            else:
                head += ["", KIND.get(revision.kind, revision.kind), UNDISCLOSED]
                head += [""] * 5
            tail = [
                " ".join(signed.get(revision.pk, [])) if disclosed else "",
                " ".join(_hold_text(h) for h in holds.get(revision.number, [])),
            ]
            changes = self._changes(entity, revision, fields)
            if not changes:
                yield [*head, "", "", "", *tail]
                continue
            for label, before, after in changes:
                yield [*head, label, before, after, *tail]

    def _changes(
        self, entity: Entity, revision: Revision, fields: Mapping[str, Any]
    ) -> list[tuple[str, str, str]]:
        """Each field the revision set, with its value before and after as the reader can
        read it: a reference by its HRID, a person by name."""
        found = {n: c for n, c in services.changes(entity, revision).items() if n in fields}
        if not found:
            return []
        wanted: dict[str, set[Any]] = {"ref": set(), "file": set(), "person": set()}
        for name, pair in found.items():
            for value in pair:
                for kind, ids in _ids_in(dict(fields), {name: value}).items():
                    wanted[kind] |= ids
        owner = self.record.owner_project_id
        refs, files = _lookup(self.user, wanted, projects={owner})
        row = {
            "refs": refs_in(refs, wanted["ref"], owner),
            "files": {str(i): f for i, f in files.items()},
            "people": _people_out(self.user, wanted["person"], self.record),
            "request": None,
        }
        out = []
        # In its type's order, as the record's page shows its fields; removed ones last.
        order = {n: i for i, n in enumerate(self.labels)}
        for name, (before, after) in sorted(
            found.items(), key=lambda item: order.get(item[0], len(order))
        ):
            f = self.labels.get(name)
            shown = [to_json(fields[name], v) for v in (before, after)]
            cells = [_cell(f, v, row) for v in shown]
            out.append((f["label"] if f else name, cells[0], cells[1]))
        return out


def _cell(f: Mapping[str, Any] | None, value: Any, row: Mapping[str, Any]) -> str:  # noqa: ANN401 - JSON
    if f is None:
        # A field since removed from its type: its value as stored.
        return text({"type": "json"}, value, row)
    return text(f, value, row)


def _utc(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def _when(revision: Revision) -> str:
    return _utc(revision.created_at)


def _what(entity: Entity, revision: Revision) -> str:
    if revision.kind != RevisionKind.STATE_CHANGED:
        return KIND.get(revision.kind, revision.kind)
    change = services.state_change(entity.type, revision)
    said = f"{change['field']['label']} set to {(change['to'] or {}).get('label', '')}"
    return f"{said}, from {change['from']['label']}" if change["from"] else said


def _signatures(revisions: Iterable[Revision]) -> dict[Any, list[str]]:
    """The signatures that set each revision's state, as people read them, with the reason
    each signer gave; one gone from the database says so."""
    wanted = {i for r in revisions for i in r.signatures or ()}
    if not wanted:
        return {}
    kept = {s.pk: s for s in signing_models.Signature.objects.filter(pk__in=wanted)}
    out: dict[Any, list[str]] = {}
    for revision in revisions:
        for i in revision.signatures or ():
            found = kept.get(i)
            if found is None:
                said = f"Signature {i} is gone from the database: something outside Iki deleted it."
            else:
                document = sig_services.parsed(found)
                said = sig_services.manifestation(document) if document else str(i)
                if reason := document.get("reason"):
                    said += f" Reason: “{reason}”."
            out.setdefault(revision.pk, []).append(said)
    return out


def _hold_text(hold: Mapping[str, Any]) -> str:
    """What a revision added to a hold (ADR 0127) and how the hold was reviewed."""
    if hold["legacy_gap"]:
        how = "Earlier hold evidence is incomplete"
    else:
        how = "Saved anyway" if hold["held_by"] is not None else "Held by Iki’s check of its rules"
    through = f", through {hold['agent']}" if hold["agent"] else ""
    message = f" {hold['message']}" if hold["message"] is not None else ""
    said = f"Held for review: {hold['label']}.{message} {how}{through}"
    said += f": “{hold['reason']}”." if hold["reason"] is not None else ". Reason unavailable."
    if hold["entry_revision"] is not None:
        said += f" Registry entry revision {hold['entry_revision']}."
    if hold["reviewer"] is not None and hold["reviewed_at"] is not None:
        who = hold["reviewer"].name or hold["reviewer"].email
        when = _utc(hold["reviewed_at"])
        said += f" Reviewed by {who} on {when} UTC: “{hold['review_reason']}”."
    else:
        said += " Still held, for review."
    return said
