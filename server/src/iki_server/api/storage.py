"""Where material is kept, and how it's received, moved and counted (ADR 0180).

- ``GET /storage/places``: the outermost places you can read: freezers, rooms, shelves.
- ``GET /storage/containers/{ref}``: a container, where it is, its grid and what's in it.
- ``GET /entities/{ref}/location`` and ``.../location/events``: where a record is, and its
  location history.
- ``POST /storage/moves``: put records away, move them, or take them out, all or none;
  with ``preview``, the plan without changing anything.
- ``POST /storage/check-out``, ``/storage/check-in``, ``/storage/hand-over``: custody.
- ``GET /storage/scan``: the records a scanned code names.
- ``GET /storage/checked-out``: what someone has checked out.
- Receipts: ``/entities/{ref}/receipts`` and ``/receipts/{id}``.
- Reconciliations: ``/storage/containers/{ref}/reconciliations`` and
  ``/reconciliations/{id}``.
- ``GET /storage/labels`` and ``POST /storage/labels``: label sizes, and labels to print.

A position is its container's project's: putting something in a container, or taking it
out, needs Editor there. Where a record is shows only as far as you can read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller
from iki_server.api.entities import RefOut, _find, _invalid, _lookup
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import AgentOut, Page, UserOut, agent_out
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.storage import labels as labelling
from iki_server.storage import receipts, reconciliations
from iki_server.storage import services as storage
from iki_server.storage.grids import Shape
from iki_server.storage.models import (
    Event,
    Place,
    Receipt,
    ReceiptStatus,
    Reconciliation,
)

router = Router(tags=["storage"])

Outcome = Literal["matched", "missing", "extra", "damaged", "rejected"]
Resolution = Literal["moved", "removed", "placed", "kept"]


# --- Shapes and places --------------------------------------------------------------------------


class ShapeOut(Schema):
    rows: int
    columns: int
    labels: Literal["letter_number", "number"]
    order: Literal["by_row", "by_column"]
    size: int = Field(description="How many positions it has.")


class StepOut(RefOut):
    id: UUID
    position: str | None = Field(
        description="Where, in it, the next step in is (or the record): such as C7."
    )


class LocationOut(Schema):
    path: list[StepOut] = Field(
        description="The containers it's in, outermost first, as far as you can read."
    )
    hidden: bool = Field(
        description="It's in a place you can't open: the path stops there, with nothing of "
        "what's beyond it."
    )
    stored: bool = Field(description="It's in a container, whether you can read it or not.")
    position: str | None = Field(description="Its position in the innermost container shown.")
    custodian: UserOut | None = Field(
        description="Who has it: checked out (its position kept for it), or received and not "
        "put away yet."
    )
    custody_since: datetime | None


class OccupantOut(Schema):
    id: UUID | None = Field(description="Null for a record you can't read: its position is taken.")
    ref: RefOut | None
    row: int | None
    column: int | None
    position: str | None
    code: str | None = Field(description="What's scanned to find it: its barcode or HRID.")
    custodian: UserOut | None
    custody_since: datetime | None
    container: bool = Field(description="It holds things of its own, such as a rack's boxes.")
    count: int = Field(0, description="How many things are in it, for a container.")


class ContainerCan(Schema):
    keep: bool = Field(
        description="Put things in it, take them out, check them out and reconcile it: Editor "
        "in its project."
    )
    label: bool = Field(description="Print its label.")


class ContainerOut(RefOut):
    id: UUID
    project: UUID | None
    shape: ShapeOut | None = Field(description="Its grid of positions, if it has one.")
    holds: list[str] = Field(description="The types it holds, qualified.")
    code: str
    location: LocationOut
    contents: list[OccupantOut] = Field(description="What's in it, in its grid's order.")
    free: int | None = Field(description="How many of its positions are free.")
    reconciliation: UUID | None = Field(description="Its open reconciliation, if there is one.")
    can: ContainerCan


class PlaceOut(RefOut):
    id: UUID
    project: UUID | None
    shape: ShapeOut | None
    count: int
    free: int | None


def _shape(shape: Shape | None) -> dict[str, Any] | None:
    if shape is None:
        return None
    return {
        "rows": shape.rows,
        "columns": shape.columns,
        "labels": shape.labels,
        "order": shape.order,
        "size": shape.size,
    }


def _refs(me: User, ids: Iterable[Any]) -> dict[Any, dict[str, Any]]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    found, _ = _lookup(me, {"ref": wanted, "file": set()})
    return found


def _location(me: User, entity: Entity) -> dict[str, Any]:
    where = storage.where(me, entity)
    refs = _refs(me, [s.entity.pk for s in where.path])
    place = where.place
    return {
        "path": [
            {**refs[s.entity.pk], "id": s.entity.pk, "position": s.position} for s in where.path
        ],
        "hidden": where.hidden,
        "stored": place is not None and place.container_id is not None,
        "position": where.path[-1].position if where.path and not where.hidden else None,
        "custodian": place.custodian if place is not None else None,
        "custody_since": place.custody_since if place is not None else None,
    }


def _container(me: User, ref: str) -> Entity:
    entity = _find(me, ref)
    if not storage.is_container(entity.type):
        raise Problem(404, f"{entity.hrid} is a {entity.type}, which holds nothing.")
    return entity


def _occupants(me: User, places: Sequence[Place], shape: Shape | None) -> list[dict[str, Any]]:
    entities = list(Entity.objects.filter(pk__in=[p.pk for p in places]))
    readable = set(
        access.visible(me).filter(pk__in=[e.pk for e in entities]).values_list("pk", flat=True)
    )
    shown = [e for e in entities if e.pk in readable]
    refs = _refs(me, readable)
    codes = storage.codes_of(shown)
    inner = [e.pk for e in shown if storage.is_container(e.type)]
    counted = storage.counts(inner)
    by_id = {e.pk: e for e in entities}
    out: list[dict[str, Any]] = []
    for p in places:
        mine = p.pk in readable
        e = by_id.get(p.pk)
        out.append(
            {
                "id": p.pk if mine else None,
                "ref": refs.get(p.pk) if mine else None,
                "row": p.row,
                "column": p.column,
                "position": storage.label(shape, p.row, p.column),
                "code": codes.get(p.pk) if mine else None,
                "custodian": p.custodian if mine else None,
                "custody_since": p.custody_since if mine else None,
                "container": bool(mine and e is not None and storage.is_container(e.type)),
                "count": counted.get(p.pk, 0) if mine else 0,
            }
        )

    def order(o: dict[str, Any]) -> tuple[int, str]:
        at = shape.index(o["row"], o["column"]) if shape is not None and o["row"] else 1 << 20
        return at, (o["ref"] or {}).get("hrid") or "~"

    out.sort(key=order)
    return out


@router.get("/storage/places", response=list[PlaceOut], summary="The outermost places")
def list_places(request: HttpRequest, project: UUID | None = None) -> list[dict[str, Any]]:
    """The containers you can read that aren't in another you can read: freezers, rooms,
    shelves. ``project`` narrows them to one project's."""
    me = caller(request)
    types = storage.container_types()
    readable = access.visible(me).filter(type__in=types, archived_at__isnull=True)
    if project is not None:
        readable = readable.filter(owner_project_id=project)
    candidates = list(readable.order_by("hrid")[:500])
    inside = {
        p.pk: p.container_id
        for p in Place.objects.filter(pk__in=[c.pk for c in candidates], container__isnull=False)
    }
    seen = set(access.visible(me).filter(pk__in=set(inside.values())).values_list("pk", flat=True))
    top = [c for c in candidates if inside.get(c.pk) not in seen]
    refs = _refs(me, [c.pk for c in top])
    kept = storage.shapes([c.pk for c in top])
    counted = storage.counts([c.pk for c in top])
    out = []
    for c in top:
        shape = kept.get(c.pk)
        n = counted.get(c.pk, 0)
        out.append(
            {
                **refs[c.pk],
                "id": c.pk,
                "project": c.owner_project_id,
                "shape": _shape(shape),
                "count": n,
                "free": shape.size - n if shape is not None else None,
            }
        )
    return out


@router.get(
    "/storage/containers/{ref}", response=ContainerOut, summary="A container and what's in it"
)
def get_container(request: HttpRequest, ref: str) -> dict[str, Any]:
    """Where it is, its grid, and what's in each position: a record you can't read shows as
    a position taken, and nothing more."""
    me = caller(request)
    entity = _container(me, ref)
    shape = storage.shapes([entity.pk]).get(entity.pk)
    places = storage.inside(entity)
    found = storage.spec(entity.type)
    open_now = (
        Reconciliation.objects.filter(container=entity, status=ReceiptStatus.OPEN)
        .values_list("pk", flat=True)
        .first()
    )
    return {
        **_refs(me, [entity.pk])[entity.pk],
        "id": entity.pk,
        "project": entity.owner_project_id,
        "shape": _shape(shape),
        "holds": list(found.holds) if found is not None else [],
        "code": storage.codes_of([entity])[entity.pk],
        "location": _location(me, entity),
        "contents": _occupants(me, places, shape),
        "free": shape.size - sum(1 for p in places if p.row is not None)
        if shape is not None
        else None,
        "reconciliation": open_now,
        "can": {"keep": storage.keeps(me, entity), "label": True},
    }


@router.get("/entities/{ref}/location", response=LocationOut, summary="Where it is")
def get_location(request: HttpRequest, ref: str) -> dict[str, Any]:
    """Its path, outermost first, its position, and who has it. 404 for a type that isn't
    kept anywhere."""
    me = caller(request)
    entity = _find(me, ref)
    if storage.spec(entity.type) is None:
        raise Problem(404, f"{entity.hrid} is a {entity.type}, which isn't kept anywhere.")
    return _location(me, entity)


class EndOut(Schema):
    container: UUID | None = Field(description="Null for a place you can't read.")
    ref: RefOut | None
    position: str | None


class EventOut(Schema):
    id: UUID
    kind: Literal[
        "placed", "moved", "removed", "checked_out", "checked_in", "handed_over", "received"
    ] = Field(
        description="`placed`: put away from nowhere; `moved`; `removed`: out of storage; "
        "`checked_out`, its position kept; `checked_in`; `handed_over`, from one person to "
        "another; `received`, with a request's receipt."
    )
    from_place: EndOut | None = Field(description="Where it was: null if nowhere.")
    to_place: EndOut | None = Field(description="Where it went: null if out of storage.")
    from_custodian: UserOut | None
    custodian: UserOut | None
    handed_over_by: str
    confirmed: str = Field(description="The code scanned to confirm it, if one was.")
    operation: UUID = Field(
        description="A bulk move's, receipt's or reconciliation's events share it."
    )
    user: UserOut | None
    via: str
    client: str
    agent: AgentOut | None
    reason: str
    created_at: datetime


class EventsOut(Page[EventOut]):
    pass


def _end(
    container: Any,  # noqa: ANN401 - an ID
    row: int | None,
    column: int | None,
    refs: dict[Any, Any],
    kept: dict[Any, Any],
) -> dict[str, Any] | None:
    if container is None:
        return None
    if container not in refs:
        return {"container": None, "ref": None, "position": None}
    return {
        "container": container,
        "ref": refs[container],
        "position": storage.label(kept.get(container), row, column),
    }


@router.get("/entities/{ref}/location/events", response=EventsOut, summary="Its location history")
def list_events(request: HttpRequest, ref: str, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first: each time it was put away, moved, taken out, checked out or in, handed
    over or received, who did it, how, when and why."""
    me = caller(request)
    entity = _find(me, ref)
    events = Event.objects.filter(entity=entity).select_related(
        "user", "agent", "custodian", "from_custodian"
    )
    result = page(events, params, key="id", newest_first=True)
    items: list[Event] = result["items"]
    ends = {x for e in items for x in (e.from_container_id, e.to_container_id) if x}
    refs = _refs(me, ends)
    kept = storage.shapes(ends)
    result["items"] = [
        {
            "id": e.id,
            "kind": e.kind,
            "from_place": _end(e.from_container_id, e.from_row, e.from_column, refs, kept),
            "to_place": _end(e.to_container_id, e.to_row, e.to_column, refs, kept),
            "from_custodian": e.from_custodian,
            "custodian": e.custodian,
            "handed_over_by": e.handed_over_by,
            "confirmed": e.confirmed,
            "operation": e.operation,
            "user": e.user,
            "via": e.via,
            "client": e.client,
            "agent": agent_out(e.agent, e.client),
            "reason": e.reason,
            "created_at": e.created_at,
        }
        for e in items
    ]
    return result


# --- Moves --------------------------------------------------------------------------------------


class MoveItemIn(Schema):
    record: str = Field(max_length=64, description="By ID or HRID.")
    to: str | None = Field(
        max_length=64, description="The container it goes in, by ID or HRID; null: out of storage."
    )
    position: str | None = Field(
        None,
        max_length=8,
        description="Its position there, such as A1 or 17; null for the container's next free "
        "one, in its order.",
    )


class MovesIn(Schema):
    items: list[MoveItemIn] = Field(min_length=1, max_length=storage.MAX_MOVES)
    reason: str = Field("", max_length=2000, description="Why: required to change anything.")
    confirmations: dict[str, str] = Field(
        default_factory=dict,
        description="The code scanned from each record, by its ID or HRID, for a type that "
        "asks for one before a move.",
    )
    preview: bool = Field(False, description="Say what each move would do, changing nothing.")


class PlannedOut(Schema):
    record: StepOut
    to: StepOut | None
    from_place: EndOut | None = Field(description="Where it is now: null if nowhere.")
    kind: Literal["placed", "moved", "removed"]
    problems: list[str]


class MovesOut(Schema):
    items: list[PlannedOut]
    done: bool = Field(description="Every move was made. False for a preview.")
    operation: UUID | None = Field(description="The moves' operation, once they're made.")


def _records(me: User, refs: Sequence[str]) -> list[Entity]:
    """The records named, each one the caller reads: 404 for any that isn't, as a record
    they can't read doesn't exist for them."""
    return [_find(me, ref) for ref in refs]


def _wanted(me: User, items: Sequence[MoveItemIn]) -> list[storage.Wanted]:
    records = _records(me, [i.record for i in items])
    return [
        storage.Wanted(record, _find(me, item.to) if item.to is not None else None, item.position)
        for item, record in zip(items, records, strict=True)
    ]


def _confirmations(me: User, given: dict[str, str]) -> dict[Any, str]:
    out: dict[Any, str] = {}
    for ref, code in given.items():
        try:
            out[_find(me, ref).pk] = code
        except Problem:
            continue
    return out


def _planned_out(me: User, planned: Sequence[storage.Planned]) -> list[dict[str, Any]]:
    ids = {
        x
        for p in planned
        for x in (p.record.pk, p.to.pk if p.to else None, p.was.container_id if p.was else None)
        if x is not None
    }
    refs = _refs(me, ids)
    out = []
    for p in planned:
        was = p.was.container_id if p.was is not None else None
        out.append(
            {
                "record": {**refs[p.record.pk], "id": p.record.pk, "position": None},
                "to": {**refs[p.to.pk], "id": p.to.pk, "position": p.position}
                if p.to is not None and p.to.pk in refs
                else None,
                "from_place": (
                    {
                        "container": was if was in refs else None,
                        "ref": refs.get(was),
                        "position": p.from_position if was in refs else None,
                    }
                    if was is not None
                    else None
                ),
                "kind": p.kind,
                "problems": p.problems,
            }
        )
    return out


@router.post("/storage/moves", response=MovesOut, summary="Move records, all or none")
def move(request: HttpRequest, payload: MovesIn) -> dict[str, Any]:
    """Puts each record in its container, at its position or the next free one, or takes it
    out of storage, as one change with one reason. Any move refused refuses them all (422),
    saying which and why. ``preview`` answers the plan, with each problem, and changes
    nothing. Needs Editor in the project of each container a record goes into or leaves."""
    me = caller(request)
    wanted = _wanted(me, payload.items)
    confirmations = _confirmations(me, payload.confirmations)
    if payload.preview:
        planned = storage.plan(me, wanted, confirmations=confirmations)
        return {"items": _planned_out(me, planned), "done": False, "operation": None}
    try:
        events = storage.move(me, wanted, reason=payload.reason, confirmations=confirmations)
    except Invalid as error:
        raise _invalid(error) from None
    ends = {x for e in events for x in (e.entity_id, e.from_container_id, e.to_container_id) if x}
    refs = _refs(me, ends)
    kept = storage.shapes(ends)
    items: list[dict[str, Any]] = []
    for e in events:
        to = _end(e.to_container_id, e.to_row, e.to_column, refs, kept)
        items.append(
            {
                "record": {**refs[e.entity_id], "id": e.entity_id, "position": None},
                "to": {**to["ref"], "id": to["container"], "position": to["position"]}
                if to is not None and to["ref"] is not None
                else None,
                "from_place": _end(e.from_container_id, e.from_row, e.from_column, refs, kept),
                "kind": e.kind,
                "problems": [],
            }
        )
    return {"items": items, "done": True, "operation": events[0].operation}


class CheckOutIn(Schema):
    records: list[str] = Field(min_length=1, max_length=storage.MAX_MOVES)
    to: UUID | None = Field(None, description="Who has them: you, if null.")
    reason: str = Field("", max_length=2000)


class CheckInItemIn(Schema):
    record: str = Field(max_length=64)
    to: str | None = Field(
        None, max_length=64, description="Another container to put it in; null: where it was."
    )
    position: str | None = Field(None, max_length=8)


class CheckInIn(Schema):
    items: list[CheckInItemIn] = Field(min_length=1, max_length=storage.MAX_MOVES)
    reason: str = Field("", max_length=2000)
    confirmations: dict[str, str] = Field(default_factory=dict)


class HandOverIn(Schema):
    record: str = Field(max_length=64)
    to: UUID
    reason: str = Field("", max_length=2000)


class CustodyOut(Schema):
    records: list[StepOut]
    operation: UUID


def _person(me: User, pk: UUID | None) -> User:
    if pk is None:
        return me
    found = User.objects.filter(pk=pk, is_active=True).first()
    if found is None:
        raise _invalid(storage.Wrong({"to": ["isn't someone in this organization."]}))
    return found


def _custody_out(me: User, records: Sequence[Entity], events: Sequence[Event]) -> dict[str, Any]:
    refs = _refs(me, [r.pk for r in records])
    kept = storage.shapes({e.to_container_id for e in events})
    return {
        "records": [
            {
                **refs[r.pk],
                "id": r.pk,
                "position": storage.label(kept.get(e.to_container_id), e.to_row, e.to_column),
            }
            for r, e in zip(records, events, strict=True)
        ],
        "operation": events[0].operation,
    }


@router.post("/storage/check-out", response=CustodyOut, summary="Check records out")
def check_out(request: HttpRequest, payload: CheckOutIn) -> dict[str, Any]:
    """Each keeps its position, which nothing else can take, until it's checked in. ``to``
    must be able to read them. Needs Editor in their containers' projects."""
    me = caller(request)
    records = _records(me, payload.records)
    try:
        events = storage.check_out(me, records, to=_person(me, payload.to), reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _custody_out(me, records, events)


@router.post("/storage/check-in", response=CustodyOut, summary="Check records in")
def check_in(request: HttpRequest, payload: CheckInIn) -> dict[str, Any]:
    """Each goes back to the position it kept, or to ``to`` and ``position``, in the same
    change."""
    me = caller(request)
    wanted = _wanted(
        me, [MoveItemIn(record=i.record, to=i.to, position=i.position) for i in payload.items]
    )
    try:
        events = storage.check_in(
            me,
            wanted,
            reason=payload.reason,
            confirmations=_confirmations(me, payload.confirmations),
        )
    except Invalid as error:
        raise _invalid(error) from None
    return _custody_out(me, [w.record for w in wanted], events)


@router.post("/storage/hand-over", response=CustodyOut, summary="Hand a record over")
def hand_over(request: HttpRequest, payload: HandOverIn) -> dict[str, Any]:
    """Passes a record that's checked out to someone else: whoever has it, or an Editor of
    its container's project, hands it over."""
    me = caller(request)
    (record,) = _records(me, [payload.record])
    try:
        event = storage.hand_over(me, record, to=_person(me, payload.to), reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _custody_out(me, [record], [event])


class ScanMatchOut(StepOut):
    storable: bool = Field(description="Its type is kept somewhere.")
    container: bool = Field(description="Its type holds things.")
    location: LocationOut | None


class ScanOut(Schema):
    code: str
    items: list[ScanMatchOut] = Field(
        description="Every record the code names that you can read: usually one."
    )


class ScanParams(Schema):
    code: str = Field(min_length=1, max_length=200, description="What was scanned.")


@router.get("/storage/scan", response=ScanOut, summary="Find what a code names")
def scan(request: HttpRequest, params: Query[ScanParams]) -> dict[str, Any]:
    """By a link to it in Iki, its HRID, or a storable type's barcode, among the records you
    can read."""
    me = caller(request)
    code = params.code
    found = storage.find(me, code)[:20]
    refs = _refs(me, [e.pk for e in found])
    items = []
    for e in found:
        if e.pk not in refs:
            continue
        where = _location(me, e) if storage.spec(e.type) is not None else None
        items.append(
            {
                **refs[e.pk],
                "id": e.pk,
                # Its position in the last container its path shows.
                "position": where["position"] if where else None,
                "storable": where is not None,
                "container": storage.is_container(e.type),
                "location": where,
            }
        )
    return {"code": code, "items": items}


class CheckedOutOut(StepOut):
    custodian: UserOut
    custody_since: datetime
    location: LocationOut


@router.get(
    "/storage/checked-out", response=list[CheckedOutOut], summary="What someone has checked out"
)
def list_checked_out(request: HttpRequest, custodian: UUID | None = None) -> list[dict[str, Any]]:
    """What ``custodian`` (you, by default) has, of what you can read, longest held first."""
    me = caller(request)
    who = _person(me, custodian)
    places = storage.checked_out(me, who)[:500]
    refs = _refs(me, [p.pk for p in places])
    entities = {e.pk: e for e in Entity.objects.filter(pk__in=[p.pk for p in places])}
    return [
        {
            **refs[p.pk],
            "id": p.pk,
            "position": None,
            "custodian": who,
            "custody_since": p.custody_since,
            "location": _location(me, entities[p.pk]),
        }
        for p in places
        if p.pk in refs
    ]


# --- Receipts -----------------------------------------------------------------------------------


class ReceiptLineOut(Schema):
    id: UUID
    entity: UUID | None
    ref: RefOut | None
    code: str
    expected: bool
    outcome: Outcome | None
    note: str
    checked_by: UserOut | None
    checked_at: datetime | None
    put_away: StepOut | None = Field(description="Where it was put away, once completed.")


class ReceiptOut(Schema):
    id: UUID
    request: StepOut
    project: UUID = Field(description="The service receiving it.")
    status: Literal["open", "completed", "cancelled"]
    handed_over_by: str
    note: str
    started_by: UserOut
    started_at: datetime
    received_by: UserOut | None
    completed_at: datetime | None
    lines: list[ReceiptLineOut]
    outcomes: dict[str, int] = Field(
        description="How many lines are each outcome, and `unchecked`."
    )
    can_receive: bool = Field(description="You may change it, while it's open.")


class ReceiptsOut(Schema):
    receipts: list[ReceiptOut]
    can_receive: bool = Field(description="You may receive what it sends.")
    why_not: str | None = Field(description="Why you may not, when you may not.")
    expected: int = Field(description="How many it expects that no receipt has received.")


def _receipt_out(me: User, receipt: Receipt, refused: str | None = None) -> dict[str, Any]:
    lines = list(receipt.lines.select_related("checked_by").order_by("-expected", "id"))
    request = receipt.request
    ids = {request.pk, *(x.entity_id for x in lines), *(x.put_in_id for x in lines)}
    refs = _refs(me, ids)
    kept = storage.shapes({x.put_in_id for x in lines if x.put_in_id})
    if refused is None:
        refused = receipts.why_not_receiver(me, request)
    return {
        "id": receipt.pk,
        "request": {**refs[request.pk], "id": request.pk, "position": None},
        "project": receipt.project_id,
        "status": receipt.status,
        "handed_over_by": receipt.handed_over_by,
        "note": receipt.note,
        "started_by": receipt.started_by,
        "started_at": receipt.started_at,
        "received_by": receipt.received_by,
        "completed_at": receipt.completed_at,
        "lines": [
            {
                "id": x.pk,
                "entity": x.entity_id if x.entity_id in refs else None,
                "ref": refs.get(x.entity_id),
                "code": x.code,
                "expected": x.expected,
                "outcome": x.outcome,
                "note": x.note,
                "checked_by": x.checked_by,
                "checked_at": x.checked_at,
                "put_away": {
                    **refs[x.put_in_id],
                    "id": x.put_in_id,
                    "position": storage.label(kept.get(x.put_in_id), x.put_row, x.put_column),
                }
                if x.put_in_id in refs
                else None,
            }
            for x in lines
        ],
        "outcomes": receipts.tally(lines),
        "can_receive": receipt.status == ReceiptStatus.OPEN and refused is None,
    }


def _receipt(me: User, pk: UUID) -> Receipt:
    found = (
        Receipt.objects.select_related("request", "project", "started_by", "received_by")
        .filter(pk=pk)
        .first()
    )
    if found is None or not access.can_read(me, found.request):
        raise Problem(404, "No such receipt, or you can't read it.")
    return found


def _receiving(call: Any) -> Any:  # noqa: ANN401 - what it answers
    try:
        return call()
    except PermissionError as refused:
        raise Problem(403, str(refused)) from None
    except Invalid as error:
        raise _invalid(error) from None


@router.get("/entities/{ref}/receipts", response=ReceiptsOut, summary="A request's receipts")
def list_receipts(request: HttpRequest, ref: str) -> dict[str, Any]:
    """Newest first, for whoever can read the request: what each received, and how."""
    me = caller(request)
    entity = _find(me, ref)
    if receipts.receives(entity) is None:
        raise Problem(404, f"{entity.hrid} is a {entity.type}, whose material isn't received.")
    refused = receipts.why_not_receiver(me, entity)
    found = (
        Receipt.objects.filter(request=entity)
        .select_related("request", "started_by", "received_by")
        .order_by("-started_at")
    )
    return {
        "receipts": [_receipt_out(me, r, refused) for r in found[:50]],
        "can_receive": refused is None,
        "why_not": refused,
        "expected": len(receipts._expected(entity)),
    }


@router.post(
    "/entities/{ref}/receipts", response={201: ReceiptOut}, summary="Start receiving a request"
)
def start_receipt(request: HttpRequest, ref: str) -> Status[dict[str, Any]]:
    """Its open receipt, or a new one listing what it expects that no receipt received.
    Needs Editor in the service it's sent to, and the lab roles its type names."""
    me = caller(request)
    entity = _find(me, ref)
    if receipts.receives(entity) is None:
        raise Problem(404, f"{entity.hrid} is a {entity.type}, whose material isn't received.")
    receipt = _receiving(lambda: receipts.start(me, entity))
    return Status(201, _receipt_out(me, _receipt(me, receipt.pk)))


@router.get("/receipts/{receipt_id}", response=ReceiptOut, summary="A receipt")
def get_receipt(request: HttpRequest, receipt_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _receipt_out(me, _receipt(me, receipt_id))


class ReceiptIn(Schema):
    handed_over_by: str | None = Field(
        None, max_length=200, description="Who handed it over: a courier and waybill, a person."
    )
    note: str | None = Field(
        None, max_length=2000, description="Such as its temperature on arrival."
    )


@router.patch("/receipts/{receipt_id}", response=ReceiptOut, summary="Describe a receipt")
def update_receipt(request: HttpRequest, receipt_id: UUID, payload: ReceiptIn) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    _receiving(
        lambda: receipts.describe(
            me, receipt, handed_over_by=payload.handed_over_by, note=payload.note
        )
    )
    return _receipt_out(me, _receipt(me, receipt_id))


class ReceiptScanIn(Schema):
    code: str = Field(min_length=1, max_length=200)


class ReceiptScanOut(Schema):
    found: Literal["matched", "again", "extra"] = Field(
        description="`matched`: an expected line ticked; `again`: one already checked; "
        "`extra`: a line added for what isn't expected."
    )
    line: UUID
    receipt: ReceiptOut


@router.post("/receipts/{receipt_id}/scan", response=ReceiptScanOut, summary="Scan into a receipt")
def scan_receipt(request: HttpRequest, receipt_id: UUID, payload: ReceiptScanIn) -> dict[str, Any]:
    """Ticks the expected line the code names as matched, or adds an extra line for it."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    scanned = _receiving(lambda: receipts.scan(me, receipt, payload.code))
    return {
        "found": scanned.found,
        "line": scanned.line.pk,
        "receipt": _receipt_out(me, _receipt(me, receipt_id)),
    }


class ReceiptLineIn(Schema):
    outcome: Outcome | None = Field(
        description="Null clears an expected line's. Damaged, rejected and extra need a note."
    )
    note: str = Field("", max_length=2000)


@router.patch(
    "/receipts/{receipt_id}/lines/{line_id}", response=ReceiptOut, summary="Check a receipt's line"
)
def check_line(
    request: HttpRequest, receipt_id: UUID, line_id: UUID, payload: ReceiptLineIn
) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    _receiving(lambda: receipts.check(me, receipt, line_id, payload.outcome, payload.note))
    return _receipt_out(me, _receipt(me, receipt_id))


@router.delete(
    "/receipts/{receipt_id}/lines/{line_id}", response=ReceiptOut, summary="Drop an extra line"
)
def drop_line(request: HttpRequest, receipt_id: UUID, line_id: UUID) -> dict[str, Any]:
    """An extra line scanned by mistake, while the receipt is open."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    _receiving(lambda: receipts.drop(me, receipt, line_id))
    return _receipt_out(me, _receipt(me, receipt_id))


class PutAwayIn(Schema):
    line: UUID
    to: str = Field(max_length=64, description="The container, by ID or HRID.")
    position: str | None = Field(None, max_length=8, description="Null: its next free one.")


class CompleteReceiptIn(Schema):
    put_away: list[PutAwayIn] = Field(default_factory=list, max_length=storage.MAX_MOVES)
    missing_rest: bool = Field(
        False, description="Mark every expected line not yet checked missing."
    )


@router.post("/receipts/{receipt_id}/complete", response=ReceiptOut, summary="Complete a receipt")
def complete_receipt(
    request: HttpRequest, receipt_id: UUID, payload: CompleteReceiptIn
) -> dict[str, Any]:
    """Takes custody of what was received, puts each away where it's given, and tells the
    requester of anything missing, extra, damaged or rejected: all of it, or none."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    put = [receipts.PutAway(p.line, _find(me, p.to), p.position) for p in payload.put_away]
    _receiving(lambda: receipts.complete(me, receipt, put, missing_rest=payload.missing_rest))
    return _receipt_out(me, _receipt(me, receipt_id))


@router.post("/receipts/{receipt_id}/cancel", response=ReceiptOut, summary="Cancel a receipt")
def cancel_receipt(request: HttpRequest, receipt_id: UUID) -> dict[str, Any]:
    """An open receipt, abandoned: nothing is received with it. It's kept, cancelled."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    _receiving(lambda: receipts.cancel(me, receipt))
    return _receipt_out(me, _receipt(me, receipt_id))


# --- Reconciliations ----------------------------------------------------------------------------


class ReconciliationLineOut(Schema):
    id: UUID
    entity: UUID | None
    ref: RefOut | None
    code: str
    expected: str | None = Field(description="Its recorded position, when this started.")
    found: str | None = Field(description="Where it was scanned.")
    finding: Literal[
        "matched",
        "missing",
        "unexpected",
        "wrong_slot",
        "unchecked",
        "empty",
        "checked_out",
        "no_longer_found",
    ]
    resolution: Resolution | None
    resolution_stale: bool = Field(description="The open count's saved decision needs review.")
    note: str
    resolved_by: UserOut | None


class ReconciliationOut(Schema):
    id: UUID
    plan_revision: int = Field(
        ge=1, description="The version of the count's observations and decisions."
    )
    container: StepOut
    shape: ShapeOut
    status: Literal["open", "completed", "cancelled"]
    note: str
    started_by: UserOut
    started_at: datetime
    completed_by: UserOut | None
    completed_at: datetime | None
    lines: list[ReconciliationLineOut]
    findings: dict[str, int]
    can_change: bool


def _reconciliation(me: User, pk: UUID) -> Reconciliation:
    found = (
        Reconciliation.objects.select_related("container", "started_by", "completed_by")
        .filter(pk=pk)
        .first()
    )
    if found is None or not access.can_read(me, found.container):
        raise Problem(404, "No such reconciliation, or you can't read it.")
    return found


def _reconciliation_out(me: User, r: Reconciliation) -> dict[str, Any]:
    # Return the reviewed lines and their revision from the same plan. Every scan,
    # decision and completion takes this lock. Finalized plans are immutable.
    if r.status == ReceiptStatus.OPEN:
        with transaction.atomic():
            r = (
                Reconciliation.objects.select_for_update(of=("self",))
                .select_related("container", "started_by", "completed_by")
                .get(pk=r.pk)
            )
            found = reconciliations.findings(r)
    else:
        found = reconciliations.findings(r)
    shape = reconciliations.shape_of(r)
    refs = _refs(me, {r.container_id, *(x.entity_id for x, _ in found)})
    counted: dict[str, int] = {}
    for _, what in found:
        counted[what] = counted.get(what, 0) + 1
    return {
        "id": r.pk,
        "plan_revision": r.plan_revision,
        "container": {**refs[r.container_id], "id": r.container_id, "position": None},
        "shape": _shape(shape),
        "status": r.status,
        "note": r.note,
        "started_by": r.started_by,
        "started_at": r.started_at,
        "completed_by": r.completed_by,
        "completed_at": r.completed_at,
        "lines": [
            {
                "id": x.pk,
                "entity": x.entity_id if x.entity_id in refs else None,
                "ref": refs.get(x.entity_id),
                "code": x.code if x.entity_id is None or x.entity_id in refs else "",
                "expected": storage.label(shape, x.expected_row, x.expected_column),
                "found": storage.label(shape, x.found_row, x.found_column),
                "finding": what,
                "resolution": x.resolution,
                "resolution_stale": r.status == ReceiptStatus.OPEN
                and reconciliations.resolution_stale(
                    x, what, reconciliations.decision_position(x, what, shape)
                ),
                "note": x.note,
                "resolved_by": x.resolved_by,
            }
            for x, what in found
        ],
        "findings": counted,
        "can_change": r.status == ReceiptStatus.OPEN and storage.keeps(me, r.container),
    }


@router.get(
    "/storage/containers/{ref}/reconciliations",
    response=list[ReconciliationOut],
    summary="A container's reconciliations",
)
def list_reconciliations(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Newest first."""
    me = caller(request)
    container = _container(me, ref)
    found = Reconciliation.objects.filter(container=container).select_related(
        "container", "started_by", "completed_by"
    )
    return [_reconciliation_out(me, r) for r in found.order_by("-started_at")[:20]]


@router.post(
    "/storage/containers/{ref}/reconciliations",
    response={201: ReconciliationOut},
    summary="Start reconciling a container",
)
def start_reconciliation(request: HttpRequest, ref: str) -> Status[dict[str, Any]]:
    """Its open reconciliation, or a new one noting what's recorded in each position. Needs
    Editor in its project."""
    me = caller(request)
    container = _container(me, ref)
    r = _receiving(lambda: reconciliations.start(me, container))
    return Status(201, _reconciliation_out(me, _reconciliation(me, r.pk)))


@router.get(
    "/reconciliations/{reconciliation_id}", response=ReconciliationOut, summary="A reconciliation"
)
def get_reconciliation(request: HttpRequest, reconciliation_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


class ScanIn(Schema):
    position: str = Field(max_length=8)
    code: str | None = Field(None, max_length=200, description="Null: it's empty.")


class ScansIn(Schema):
    scans: list[ScanIn] = Field(
        min_length=1,
        max_length=1536,
        description="Each position scanned: one at a time, or a rack scanner's file.",
    )


@router.post(
    "/reconciliations/{reconciliation_id}/scans",
    response=ReconciliationOut,
    summary="Scan positions",
)
def scan_positions(
    request: HttpRequest, reconciliation_id: UUID, payload: ScansIn
) -> dict[str, Any]:
    """What's in each position, or that it's empty. A position scanned again replaces what
    was scanned there."""
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    _receiving(
        lambda: reconciliations.scan(
            me, r, [reconciliations.Scan(s.position, s.code) for s in payload.scans]
        )
    )
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


class ReviewedReconciliationIn(Schema):
    plan_revision: int = Field(
        ge=1, strict=True, description="The revision of the reconciliation plan being reviewed."
    )


class ResolveIn(ReviewedReconciliationIn):
    resolution: Resolution | None = Field(
        description="`moved` (a record in the wrong position), `removed` (one missing), "
        "`placed` (one unexpected), or `kept`, with a note; null clears it."
    )
    note: str = Field("", max_length=2000)


@router.patch(
    "/reconciliations/{reconciliation_id}/lines/{line_id}",
    response=ReconciliationOut,
    summary="Resolve a reconciliation's line",
)
def resolve_line(
    request: HttpRequest, reconciliation_id: UUID, line_id: UUID, payload: ResolveIn
) -> dict[str, Any]:
    """Saves or clears a line decision for the reviewed revision. Returns 409 if the
    observations or another decision changed in the meantime."""
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    _receiving(
        lambda: reconciliations.resolve(
            me, r, line_id, payload.resolution, payload.note, plan_revision=payload.plan_revision
        )
    )
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


class CompleteReconciliationIn(ReviewedReconciliationIn):
    missing_rest: bool = Field(
        False, description="Take each recorded position nobody scanned to be missing, removed."
    )
    note: str = Field("", max_length=2000)


@router.post(
    "/reconciliations/{reconciliation_id}/complete",
    response=ReconciliationOut,
    summary="Complete a reconciliation",
)
def complete_reconciliation(
    request: HttpRequest, reconciliation_id: UUID, payload: CompleteReconciliationIn
) -> dict[str, Any]:
    """Applies the reviewed plan atomically. A stale plan revision returns 409. A decision
    that needs review or a change in the container's recorded contents returns 422."""
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    _receiving(
        lambda: reconciliations.complete(
            me,
            r,
            plan_revision=payload.plan_revision,
            missing_rest=payload.missing_rest,
            note=payload.note,
        )
    )
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


@router.post(
    "/reconciliations/{reconciliation_id}/cancel",
    response=ReconciliationOut,
    summary="Cancel a reconciliation",
)
def cancel_reconciliation(request: HttpRequest, reconciliation_id: UUID) -> dict[str, Any]:
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    _receiving(lambda: reconciliations.cancel(me, r))
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


# --- Labels -------------------------------------------------------------------------------------


class LabelSizeOut(Schema):
    key: str
    label: str
    width_mm: float
    height_mm: float
    code128: bool = Field(description="It has room for a Code 128 beside the 2D code.")


class LabelSizesOut(Schema):
    sizes: list[LabelSizeOut]
    dpis: list[int]


@router.get("/storage/labels", response=LabelSizesOut, summary="Label sizes")
def label_sizes(request: HttpRequest) -> dict[str, Any]:
    caller(request)
    return {"sizes": labelling.describe(), "dpis": list(labelling.DPIS)}


class LabelsIn(Schema):
    records: list[str] = Field(min_length=1, max_length=labelling.MAX_LABELS)
    size: str = Field(max_length=32, description="One of `GET /storage/labels`' sizes.")
    format: Literal["print", "zpl"] = Field(
        description="`print`: for the browser to print; `zpl`: ZPL II for a Zebra printer."
    )
    symbology: Literal["datamatrix", "qr"] = "datamatrix"
    dpi: int = Field(203, description="For ZPL: the printer's resolution, 203 or 300 dpi.")
    copies: int = Field(1, ge=1, le=labelling.MAX_COPIES)
    preview: bool = Field(
        False, description="Only to show them: not recorded as prepared, and with no ZPL."
    )


class LabelOut(Schema):
    id: UUID
    hrid: str
    title: str
    line: str
    code: str = Field(description="What its barcodes encode: its barcode, or its HRID.")


class LabelsOut(Schema):
    labels: list[LabelOut]
    size: LabelSizeOut
    zpl: str | None


@router.post("/storage/labels", response=LabelsOut, summary="Prepare labels")
def prepare_labels(request: HttpRequest, payload: LabelsIn) -> dict[str, Any]:
    """Each record's label, recorded as prepared on it, as ZPL or for the browser to lay out
    and print."""
    me = caller(request)
    size = labelling.size_of(payload.size)
    if size is None:
        raise _invalid(storage.Wrong({"size": [f"isn't one of {', '.join(labelling.SIZES)}."]}))
    if payload.dpi not in labelling.DPIS:
        raise _invalid(storage.Wrong({"dpi": ["is 203 or 300."]}))
    records = _records(me, payload.records)
    for index, r in enumerate(records):
        if storage.spec(r.type) is None:
            raise _invalid(
                storage.Wrong({f"records/{index}": [f"{r.hrid} isn't kept anywhere to label."]})
            )
    made = labelling.labels_of(records)
    if not payload.preview:
        labelling.recorded(me, made, size=size, how=payload.format, copies=payload.copies)
    return {
        "labels": [
            {"id": x.entity.pk, "hrid": x.hrid, "title": x.title, "line": x.line, "code": x.code}
            for x in made
        ],
        "size": labelling.describe({size.key: size})[0],
        "zpl": labelling.zpl(
            made, size=size, dpi=payload.dpi, symbology=payload.symbology, copies=payload.copies
        )
        if payload.format == "zpl" and not payload.preview
        else None,
    }
