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
from typing import Annotated, Any, Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status
from pydantic import ConfigDict

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
from iki_server.storage import receipts, reconciliations, scan_identity
from iki_server.storage import services as storage
from iki_server.storage.grids import Shape
from iki_server.storage.models import (
    Event,
    ObservationEvent,
    Place,
    Receipt,
    ReceiptLine,
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


def _destination(
    index: scan_identity.ScanIndex,
    raw: str,
    *,
    mode: scan_identity.ScanMode = "label",
    choice: UUID | None = None,
) -> Entity:
    match = index.resolve(raw, mode=mode, choice=choice, containers_only=True, direct_id=True)
    if match.status == "ambiguous":
        raise index.ambiguity(match)
    if match.record is not None:
        return match.record
    # Older API callers pass a record UUID. Keep row-level plan errors for a
    # readable record that holds nothing, after checking all eligible box labels.
    if mode == "label" and choice is None and (record_id := scan_identity.direct_uuid(raw)):
        return _find(index.user, str(record_id))
    raise Problem(404, "No such container, or you can’t read it.")


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
        max_length=200,
        description="Destination container ID, label or entity link; null: out of storage.",
    )
    to_mode: scan_identity.ScanMode = Field(default_factory=lambda: "label")
    to_choice: UUID | None = None
    position: str | None = Field(
        None,
        max_length=8,
        description="Its position there, such as A1 or 17; null for the container's next free "
        "one, in its order.",
    )


class IkiIdConfirmationIn(Schema):
    """The operator independently entered the HRID printed on the selected record."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["iki_id"]
    code: str = Field(min_length=1, max_length=200)


ConfirmationIn = Annotated[str, Field(max_length=200)] | IkiIdConfirmationIn


class MovesIn(Schema):
    items: list[MoveItemIn] = Field(min_length=1, max_length=storage.MAX_MOVES)
    reason: str = Field("", max_length=2000, description="Why: required to change anything.")
    confirmations: dict[str, ConfirmationIn] = Field(
        default_factory=dict,
        description="Proof for each selected record that requires confirmation, keyed by its ID or "
        "HRID. Scan a unique label or a record UUID link, or explicitly enter its printed HRID.",
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


def _wanted(me: User, items: Sequence[MoveItemIn | CheckInItemIn]) -> list[storage.Wanted]:
    records = _records(me, [i.record for i in items])
    boxes = scan_identity.ScanIndex(me, (item.to for item in items if item.to is not None))
    wanted = []
    for i, (item, record) in enumerate(zip(items, records, strict=True)):
        destination = None
        if item.to is not None:
            destination = _destination(boxes, item.to, mode=item.to_mode, choice=item.to_choice)
        elif item.to_choice is not None or item.to_mode != "label":
            raise _invalid(storage.Wrong({f"items/{i}/to": ["Supply the destination code."]}))
        wanted.append(storage.Wanted(record, destination, item.position))
    return wanted


def _confirmations(
    wanted: Sequence[storage.Wanted], given: dict[str, ConfirmationIn]
) -> dict[UUID, storage.Confirmation | str]:
    out: dict[UUID, storage.Confirmation | str] = {}
    ids = {w.record.pk for w in wanted}
    hrids = {w.record.hrid: w.record.pk for w in wanted}
    for ref, proof in given.items():
        pk: UUID | None
        try:
            pk = UUID(ref)
        except ValueError:
            pk = hrids.get(ref.upper())
        if pk is None or pk not in ids:
            raise _invalid(
                storage.Wrong({"confirmations": ["Supply proof only for selected records."]})
            )
        if pk in out:
            raise _invalid(storage.Wrong({"confirmations": ["Supply one proof per record."]}))
        out[pk] = proof if isinstance(proof, str) else storage.Confirmation(proof.code, proof.mode)
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
    confirmations = _confirmations(wanted, payload.confirmations)
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
        None,
        max_length=200,
        description="Destination container ID, label or entity link; null: where it was.",
    )
    to_mode: scan_identity.ScanMode = Field(default_factory=lambda: "label")
    to_choice: UUID | None = None
    position: str | None = Field(None, max_length=8)


class CheckInIn(Schema):
    items: list[CheckInItemIn] = Field(min_length=1, max_length=storage.MAX_MOVES)
    reason: str = Field("", max_length=2000)
    confirmations: dict[str, ConfirmationIn] = Field(default_factory=dict)


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
    wanted = _wanted(me, payload.items)
    try:
        events = storage.check_in(
            me,
            wanted,
            reason=payload.reason,
            confirmations=_confirmations(wanted, payload.confirmations),
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


# --- Observation history ------------------------------------------------------------------------


class ObservationEventOut(Schema):
    id: UUID
    kind: Literal["outcome", "handoff", "scan", "resolution", "status"]
    receipt_id: UUID | None
    reconciliation_id: UUID | None
    line_id: UUID | None = Field(description="The original line, including after its removal.")
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    actor: UserOut
    created_at: datetime
    operation: UUID
    revision: int = Field(ge=1)
    previous: UUID | None = Field(description="The prior observation of this kind on this line.")
    reason: str


class ObservationEventsOut(Page[ObservationEventOut]):
    pass


def _observation_history(
    me: User, parent: Receipt | Reconciliation, line_id: UUID | None, params: PageParams
) -> dict[str, Any]:
    events = parent.observations.select_related("actor")
    if line_id is not None:
        events = events.filter(line_id=line_id)
    result = page(events, params, newest_first=True)
    items: list[ObservationEvent] = result["items"]
    # Look up the historical links on this page. The working line may have been changed
    # or deleted, and the two sides of an amendment may refer to different records.
    ids = {
        UUID(snapshot["entity_id"])
        for event in items
        for snapshot in (event.before, event.after)
        if snapshot and snapshot.get("entity_id") is not None
    }
    readable = {str(pk) for pk, ref in _refs(me, ids).items() if not ref["purged"]}

    def snapshot_out(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        public = snapshot.copy()
        entity_id = public.pop("entity_id", None)
        if entity_id is not None and entity_id not in readable:
            if "code" in public:
                public["code"] = ""
            public.pop("scan", None)
        elif entity_id is not None and isinstance(parent, Receipt) and "scan" in public:
            public["entity_id"] = entity_id
        return public

    result["items"] = [
        {
            "id": e.pk,
            "kind": e.kind,
            "receipt_id": e.receipt_id,
            "reconciliation_id": e.reconciliation_id,
            "line_id": e.line_id,
            "before": snapshot_out(e.before),
            "after": snapshot_out(e.after),
            "actor": e.actor,
            "created_at": e.created_at,
            "operation": e.operation,
            "revision": e.revision,
            "previous": e.previous_id,
            "reason": e.reason,
        }
        for e in items
    ]
    return result


# --- Receipts -----------------------------------------------------------------------------------


class ReceiptLineOut(Schema):
    id: UUID
    line_revision: int = Field(ge=1, description="The version of this line’s saved observation.")
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
    plan_revision: int = Field(ge=1, description="The receipt revision shown for review.")
    request: StepOut
    project: UUID = Field(description="The service receiving it.")
    status: Literal["open", "completed", "cancelled"]
    handed_over_by: str
    note: str
    started_by: UserOut
    started_at: datetime
    received_by: UserOut | None
    cancelled_by: UserOut | None
    cancel_reason: str
    completed_at: datetime | None
    lines: list[ReceiptLineOut]
    removed_lines: list[ReceiptLineOut] = Field(
        description="Removed extras kept for review and restoration."
    )
    outcomes: dict[str, int] = Field(
        description="How many lines are each outcome, and `unchecked`."
    )
    can_receive: bool = Field(description="You may change it, while it's open.")


class ReceiptsOut(Schema):
    receipts: list[ReceiptOut]
    can_receive: bool = Field(description="You may receive what it sends.")
    why_not: str | None = Field(description="Why you may not, when you may not.")
    expected: int = Field(description="How many it expects that no receipt has received.")


def _receipt_out(
    me: User,
    receipt: Receipt,
    refused: str | None = None,
    *,
    locked: bool = False,
) -> dict[str, Any]:
    def lines_of(r: Receipt) -> list[ReceiptLine]:
        return list(r.lines.select_related("checked_by").order_by("-expected", "id"))

    # All writers lock the receipt. Capture parent fields and lines under that same
    # lock so a returned revision always describes the handoff and checklist shown.
    if receipt.status == ReceiptStatus.OPEN and not locked:
        with transaction.atomic():
            receipt = (
                Receipt.objects.select_for_update(of=("self",))
                .select_related("request", "project", "started_by", "received_by", "cancelled_by")
                .get(pk=receipt.pk)
            )
            lines = lines_of(receipt)
    else:
        lines = lines_of(receipt)
    request = receipt.request
    ids = {request.pk, *(x.entity_id for x in lines), *(x.put_in_id for x in lines)}
    refs = _refs(me, ids)
    kept = storage.shapes({x.put_in_id for x in lines if x.put_in_id})
    if refused is None:
        refused = receipts.why_not_receiver(me, request)
    active: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for line in lines:
        ref = refs.get(line.entity_id)
        if ref and ref["purged"]:
            ref = None
        item = {
            "id": line.pk,
            "line_revision": line.line_revision,
            "entity": line.entity_id if ref else None,
            "ref": ref,
            "code": "" if line.entity_id and ref is None else line.code,
            "expected": line.expected,
            "outcome": line.outcome,
            "note": line.note,
            "checked_by": line.checked_by,
            "checked_at": line.checked_at,
            "put_away": {
                **refs[line.put_in_id],
                "id": line.put_in_id,
                "position": storage.label(kept.get(line.put_in_id), line.put_row, line.put_column),
            }
            if line.put_in_id in refs
            else None,
        }
        (removed if line.removed else active).append(item)
    return {
        "id": receipt.pk,
        "plan_revision": receipt.plan_revision,
        "request": {**refs[request.pk], "id": request.pk, "position": None},
        "project": receipt.project_id,
        "status": receipt.status,
        "handed_over_by": receipt.handed_over_by,
        "note": receipt.note,
        "started_by": receipt.started_by,
        "started_at": receipt.started_at,
        "received_by": receipt.received_by,
        "cancelled_by": receipt.cancelled_by,
        "cancel_reason": receipt.cancel_reason,
        "completed_at": receipt.completed_at,
        "lines": active,
        "removed_lines": removed,
        "outcomes": receipts.tally([line for line in lines if not line.removed]),
        "can_receive": receipt.status == ReceiptStatus.OPEN and refused is None,
    }


def _receipt(me: User, pk: UUID) -> Receipt:
    found = (
        Receipt.objects.select_related(
            "request", "project", "started_by", "received_by", "cancelled_by"
        )
        .filter(pk=pk)
        .first()
    )
    if found is None or not access.can_read(me, found.request):
        raise Problem(404, "No such receipt, or you can't read it.")
    return found


def _receiving(call: Any) -> Any:  # noqa: ANN401 - what it answers
    try:
        return call()
    except reconciliations.LinesChanged as changed:
        raise Problem(
            409,
            str(changed),
            code="count_lines_changed",
            extensions={"affected_lines": changed.lines},
        ) from None
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
        .select_related("request", "started_by", "received_by", "cancelled_by")
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
    with transaction.atomic():
        receipt = _receiving(lambda: receipts.start(me, entity))
        return Status(201, _receipt_out(me, receipt, locked=True))


@router.get("/receipts/{receipt_id}", response=ReceiptOut, summary="A receipt")
def get_receipt(request: HttpRequest, receipt_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _receipt_out(me, _receipt(me, receipt_id))


@router.get(
    "/receipts/{receipt_id}/history",
    response=ObservationEventsOut,
    summary="A receipt's observation history",
)
def receipt_history(
    request: HttpRequest, receipt_id: UUID, params: Query[PageParams], line_id: UUID | None = None
) -> dict[str, Any]:
    """Newest first. Supply a line ID to follow its observations, even after it was removed."""
    me = caller(request)
    return _observation_history(me, _receipt(me, receipt_id), line_id, params)


_PATCH_NOTE = (
    "Omit to keep the current note. Send null, an empty string, or whitespace to clear it."
)


def _patch_note(payload: ReceiptIn | ReceiptLineIn | ResolveIn) -> str | None:
    if "note" not in payload.model_fields_set:
        return None
    return (payload.note or "").strip()


class ReviewedReceiptIn(Schema):
    plan_revision: int = Field(ge=1, strict=True, description="The receipt revision you reviewed.")


class ReceiptIn(ReviewedReceiptIn):
    reason: str | None = Field(
        None,
        max_length=2000,
        description="Defaults to empty. Required when correcting existing handoff information.",
    )
    handed_over_by: str | None = Field(
        None, max_length=200, description="Who handed it over: a courier and waybill, a person."
    )
    note: str | None = Field(None, max_length=2000, description=_PATCH_NOTE)


@router.patch("/receipts/{receipt_id}", response=ReceiptOut, summary="Describe a receipt")
def update_receipt(request: HttpRequest, receipt_id: UUID, payload: ReceiptIn) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        changed = _receiving(
            lambda: receipts.describe(
                me,
                receipt,
                handed_over_by=payload.handed_over_by,
                note=_patch_note(payload),
                plan_revision=payload.plan_revision,
                reason=payload.reason or "",
            )
        )
        return _receipt_out(me, changed, locked=True)


class ReceiptScanIn(Schema):
    code: str = Field(min_length=1, max_length=receipts.MAX_SCAN_CODE)
    mode: scan_identity.ScanMode = Field(default_factory=lambda: "label")
    choice: UUID | None = None


class ReceiptScanOut(Schema):
    found: Literal["matched", "again", "extra", "ambiguous_extra", "removed"] = Field(
        description="`matched`: an expected line ticked; `again`: one already checked; "
        "`extra`: what wasn't expected; `ambiguous_extra`: an extra left unlinked because "
        "several readable records share its label; `removed`: an extra that can be restored."
    )
    line: UUID
    receipt: ReceiptOut


@router.post("/receipts/{receipt_id}/scan", response=ReceiptScanOut, summary="Scan into a receipt")
def scan_receipt(request: HttpRequest, receipt_id: UUID, payload: ReceiptScanIn) -> dict[str, Any]:
    """Ticks the expected line the code names as matched, or adds an extra line for it."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        scanned = _receiving(
            lambda: receipts.scan(
                me, receipt, payload.code, mode=payload.mode, choice=payload.choice
            )
        )
        return {
            "found": scanned.found,
            "line": scanned.line.pk,
            "receipt": _receipt_out(me, scanned.receipt, locked=True),
        }


class ReceiptLineIn(Schema):
    outcome: Outcome | None = Field(
        description="Null clears an expected line. First observations can omit line_revision."
    )
    note: str | None = Field(None, max_length=2000, description=_PATCH_NOTE)
    line_revision: int | None = Field(
        None, ge=1, strict=True, description="Version of the line being corrected."
    )
    reason: str | None = Field(
        None,
        max_length=2000,
        description=(
            "Defaults to empty. Required to change an existing outcome or replace a saved note."
        ),
    )


class CheckedReceiptOut(Schema):
    found: Literal["recorded", "again"] = Field(description="Whether this observation changed.")
    line: UUID
    receipt: ReceiptOut


@router.patch(
    "/receipts/{receipt_id}/lines/{line_id}",
    response=CheckedReceiptOut,
    summary="Check a receipt's line",
)
def check_line(
    request: HttpRequest, receipt_id: UUID, line_id: UUID, payload: ReceiptLineIn
) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        result = _receiving(
            lambda: receipts.check(
                me,
                receipt,
                line_id,
                payload.outcome,
                _patch_note(payload),
                line_revision=payload.line_revision,
                reason=payload.reason or "",
            )
        )
        return {
            "found": result.found,
            "line": result.line.pk,
            "receipt": _receipt_out(me, result.receipt, locked=True),
        }


class ReviewedExtraIn(Schema):
    line_revision: int = Field(
        ge=1, strict=True, description="The reviewed version of the extra line."
    )
    reason: str = Field(min_length=1, max_length=2000)


@router.delete(
    "/receipts/{receipt_id}/lines/{line_id}",
    response=ReceiptOut,
    summary="Remove an observed extra",
)
def drop_line(
    request: HttpRequest,
    receipt_id: UUID,
    line_id: UUID,
    payload: ReviewedExtraIn,
) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        changed = _receiving(
            lambda: receipts.set_removed(
                me,
                receipt,
                line_id,
                removed=True,
                line_revision=payload.line_revision,
                reason=payload.reason,
            )
        )
        return _receipt_out(me, changed, locked=True)


@router.post(
    "/receipts/{receipt_id}/lines/{line_id}/restore",
    response=ReceiptOut,
    summary="Restore a removed extra on an open receipt",
)
def restore_line(
    request: HttpRequest,
    receipt_id: UUID,
    line_id: UUID,
    payload: ReviewedExtraIn,
) -> dict[str, Any]:
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        changed = _receiving(
            lambda: receipts.set_removed(
                me,
                receipt,
                line_id,
                removed=False,
                line_revision=payload.line_revision,
                reason=payload.reason,
            )
        )
        return _receipt_out(me, changed, locked=True)


class PutAwayIn(Schema):
    line: UUID
    to: str = Field(max_length=200, description="The destination container's ID, HRID or label.")
    to_mode: scan_identity.ScanMode = Field(default_factory=lambda: "label")
    to_choice: UUID | None = None
    position: str | None = Field(None, max_length=8, description="Null: its next free one.")


class CompleteReceiptIn(ReviewedReceiptIn):
    put_away: list[PutAwayIn] = Field(default_factory=list, max_length=storage.MAX_MOVES)
    missing_rest: bool = Field(
        default_factory=bool,
        description="Mark every unchecked expected line missing. Defaults to false.",
    )


@router.post("/receipts/{receipt_id}/complete", response=ReceiptOut, summary="Complete a receipt")
def complete_receipt(
    request: HttpRequest, receipt_id: UUID, payload: CompleteReceiptIn
) -> dict[str, Any]:
    """Takes custody of what was received, puts each away where it's given, and tells the
    requester of anything missing, extra, damaged or rejected: all of it, or none."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        boxes = scan_identity.ScanIndex(me, (p.to for p in payload.put_away))
        put = [
            receipts.PutAway(
                p.line, _destination(boxes, p.to, mode=p.to_mode, choice=p.to_choice), p.position
            )
            for p in payload.put_away
        ]
        changed = _receiving(
            lambda: receipts.complete(
                me,
                receipt,
                put,
                plan_revision=payload.plan_revision,
                missing_rest=payload.missing_rest,
            )
        )
        return _receipt_out(me, changed, locked=True)


class CancelReceiptIn(ReviewedReceiptIn):
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/receipts/{receipt_id}/cancel", response=ReceiptOut, summary="Cancel a receipt")
def cancel_receipt(
    request: HttpRequest, receipt_id: UUID, payload: CancelReceiptIn
) -> dict[str, Any]:
    """An open receipt, abandoned: nothing is received with it. It's kept, cancelled."""
    me = caller(request)
    receipt = _receipt(me, receipt_id)
    with transaction.atomic():
        changed = _receiving(
            lambda: receipts.cancel(
                me, receipt, plan_revision=payload.plan_revision, reason=payload.reason
            )
        )
        return _receipt_out(me, changed, locked=True)


# --- Reconciliations ----------------------------------------------------------------------------


class CountScanEvidenceOut(Schema):
    identity_basis: scan_identity.IdentityBasis | None = Field(
        description=(
            "How the record was identified. A shared label at its expected slot uses position. "
            "Null for an unidentified, empty or older scan."
        )
    )
    id: UUID = Field(description="The immutable scan event.")
    line_id: UUID = Field(description="The physical scan line, usable as a history filter.")
    position: str
    code: str | None = Field(
        description="The actual scanned label, null for empty; hidden labels are redacted."
    )
    scanned_by: UserOut
    scanned_at: datetime


class ReconciliationLineOut(Schema):
    line_revision: int = Field(
        ge=1, description="The version of this line and its supporting slot evidence."
    )
    observation: CountScanEvidenceOut | None = Field(
        description="The scan supporting the finding, including a missing tube's expected slot."
    )
    reviewed_observation: CountScanEvidenceOut | None = Field(
        description="The exact scan on which the saved decision was based."
    )
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
    cancelled_by: UserOut | None
    cancel_reason: str
    completed_by: UserOut | None
    completed_at: datetime | None
    lines: list[ReconciliationLineOut]
    findings: dict[str, int]
    can_change: bool


def _reconciliation(me: User, pk: UUID) -> Reconciliation:
    found = (
        Reconciliation.objects.select_related(
            "container", "started_by", "completed_by", "cancelled_by"
        )
        .filter(pk=pk)
        .first()
    )
    if found is None or not access.can_read(me, found.container):
        raise Problem(404, "No such reconciliation, or you can't read it.")
    return found


def _count_scan(
    event: ObservationEvent | None, refs: dict[Any, dict[str, Any]]
) -> dict[str, Any] | None:
    if event is None or not event.after:
        return None
    snapshot = event.after
    linked = snapshot.get("entity_id")
    readable = refs.get(UUID(linked)) if linked else None
    code = "" if linked and (not readable or readable["purged"]) else snapshot["code"]
    return {
        "id": event.pk,
        "line_id": event.line_id,
        "identity_basis": snapshot.get("identity_basis"),
        "position": snapshot["position"],
        "code": code,
        "scanned_by": event.actor,
        "scanned_at": snapshot["scanned_at"],
    }


def _reconciliation_out(me: User, r: Reconciliation, *, locked: bool = False) -> dict[str, Any]:
    # Return the reviewed lines and their revision from the same plan. Every scan,
    # decision and completion takes this lock. Finalized plans are immutable.
    if r.status == ReceiptStatus.OPEN and not locked:
        with transaction.atomic():
            r = (
                Reconciliation.objects.select_for_update(of=("self",))
                .select_related("container", "started_by", "completed_by", "cancelled_by")
                .get(pk=r.pk)
            )
            found = reconciliations.findings(r)
    else:
        found = reconciliations.findings(r)
    slots = reconciliations.observed_slots([line for line, _ in found])
    shape = reconciliations.shape_of(r)
    # A stale decision can still refer to a different physical line that is gone from
    # the working map. Resolve both historical links against this reader's current access.
    events = {
        e.pk: e
        for x, _ in found
        for e in (x.current_observation, x.reviewed_observation)
        if e is not None
    }
    ids = {
        UUID(e.after["entity_id"]) for e in events.values() if e.after and e.after.get("entity_id")
    }
    refs = _refs(me, {r.container_id, *(x.entity_id for x, _ in found), *ids})
    readable_codes = {pk for pk, ref in refs.items() if not ref["purged"]}
    evidence: dict[UUID | None, dict[str, Any] | None] = {
        pk: _count_scan(e, refs) for pk, e in events.items()
    }
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
        "cancelled_by": r.cancelled_by,
        "cancel_reason": r.cancel_reason,
        "completed_at": r.completed_at,
        "lines": [
            {
                "id": x.pk,
                "entity": x.entity_id if x.entity_id in refs else None,
                "ref": refs.get(x.entity_id),
                "code": x.code if x.entity_id is None or x.entity_id in readable_codes else "",
                "expected": storage.label(shape, x.expected_row, x.expected_column),
                "found": storage.label(shape, x.found_row, x.found_column),
                "finding": what,
                "line_revision": x.line_revision,
                "observation": evidence.get(reconciliations.evidence_id(x, what, slots)),
                "reviewed_observation": evidence.get(x.reviewed_observation_id),
                "resolution": x.resolution,
                "resolution_stale": r.status == ReceiptStatus.OPEN
                and reconciliations.resolution_stale(
                    x,
                    what,
                    reconciliations.decision_position(x, what, shape),
                    reconciliations.evidence_id(x, what, slots),
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
        "container", "started_by", "completed_by", "cancelled_by"
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
    with transaction.atomic():
        r = _receiving(lambda: reconciliations.start(me, container))
        return Status(201, _reconciliation_out(me, r))


@router.get(
    "/reconciliations/{reconciliation_id}", response=ReconciliationOut, summary="A reconciliation"
)
def get_reconciliation(request: HttpRequest, reconciliation_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _reconciliation_out(me, _reconciliation(me, reconciliation_id))


@router.get(
    "/reconciliations/{reconciliation_id}/history",
    response=ObservationEventsOut,
    summary="A reconciliation's observation history",
)
def reconciliation_history(
    request: HttpRequest,
    reconciliation_id: UUID,
    params: Query[PageParams],
    line_id: UUID | None = None,
) -> dict[str, Any]:
    """Newest first. Supply a line ID to follow its observations, even after it was removed."""
    me = caller(request)
    return _observation_history(me, _reconciliation(me, reconciliation_id), line_id, params)


class ScanIn(Schema):
    position: str = Field(max_length=8)
    code: str | None = Field(None, max_length=200, description="Null: it's empty.")
    mode: scan_identity.ScanMode = Field(
        default_factory=lambda: "label",
        description="Defaults to label. Use iki_id for a printed Iki ID.",
    )
    choice: UUID | None = Field(
        None, description="The readable candidate selected for this scanned label."
    )


class ReviewedReconciliationIn(Schema):
    plan_revision: int = Field(
        ge=1, strict=True, description="The revision of the reconciliation plan being reviewed."
    )


class ReviewedCountLine(Schema):
    line_id: UUID
    line_revision: int = Field(ge=1, strict=True)


class ScansIn(Schema):
    reviewed_lines: list[ReviewedCountLine] = Field(
        default_factory=list,
        max_length=3072,
        description="Prior observations to recheck or replace, including empty slots.",
    )
    reason: str | None = Field(
        None,
        max_length=2000,
        description="Defaults to empty. Required when replacing an earlier observation.",
    )
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
    """Record the scans in one transaction. Fresh positions and tubes need no precondition.
    Supply the reviewed line revisions for observations being replaced or repeated.
    A change of slot or contents also needs a reason. A conflict returns 409 with the
    currently affected line IDs and versions in `affected_lines`. A shared label at its
    expected slot records a positional match. An ambiguous label elsewhere returns 409
    with the readable candidates and the zero-based scan index. A selected candidate
    is rechecked against the original label when the rack is submitted again."""
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    with transaction.atomic():
        _receiving(
            lambda: reconciliations.scan(
                me,
                r,
                [reconciliations.Scan(s.position, s.code, s.mode, s.choice) for s in payload.scans],
                reviewed_lines=[
                    (line.line_id, line.line_revision) for line in payload.reviewed_lines
                ],
                reason=payload.reason or "",
            )
        )
        return _reconciliation_out(me, r, locked=True)


class ResolveIn(Schema):
    line_revision: int = Field(ge=1, strict=True)
    reason: str | None = Field(
        None,
        max_length=2000,
        description="Defaults to empty. Required when changing or clearing a saved decision.",
    )
    resolution: Resolution | None = Field(
        description="`moved` (a record in the wrong position), `removed` (one missing), "
        "`placed` (one unexpected), or `kept`, with a note. Null clears the decision and its "
        "attached note. The earlier decision and note remain in the history."
    )
    note: str | None = Field(None, max_length=2000, description=_PATCH_NOTE)


@router.patch(
    "/reconciliations/{reconciliation_id}/lines/{line_id}",
    response=ReconciliationOut,
    summary="Resolve a reconciliation's line",
)
def resolve_line(
    request: HttpRequest, reconciliation_id: UUID, line_id: UUID, payload: ResolveIn
) -> dict[str, Any]:
    """Save or clear the decision for the reviewed line_revision. A changed finding,
    supporting scan, or decision on this line returns 409. Superseding a decision
    requires a reason in addition to any note supporting the new decision."""
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    with transaction.atomic():
        _receiving(
            lambda: reconciliations.resolve(
                me,
                r,
                line_id,
                payload.resolution,
                _patch_note(payload),
                line_revision=payload.line_revision,
                reason=payload.reason or "",
            )
        )
        return _reconciliation_out(me, r, locked=True)


class CompleteReconciliationIn(ReviewedReconciliationIn):
    missing_rest: bool = Field(
        default_factory=bool,
        description="Treat unscanned occupants as missing and remove them. Defaults to false.",
    )
    note: str | None = Field(
        None, max_length=2000, description="Completion note. Defaults to empty."
    )


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
    with transaction.atomic():
        _receiving(
            lambda: reconciliations.complete(
                me,
                r,
                plan_revision=payload.plan_revision,
                missing_rest=payload.missing_rest,
                note=payload.note or "",
            )
        )
        return _reconciliation_out(me, r, locked=True)


class CancelReconciliationIn(ReviewedReconciliationIn):
    reason: str = Field(
        min_length=1, max_length=2000, description="Why this count is being cancelled."
    )


@router.post(
    "/reconciliations/{reconciliation_id}/cancel",
    response=ReconciliationOut,
    summary="Cancel a reconciliation",
)
def cancel_reconciliation(
    request: HttpRequest, reconciliation_id: UUID, payload: CancelReconciliationIn
) -> dict[str, Any]:
    me = caller(request)
    r = _reconciliation(me, reconciliation_id)
    with transaction.atomic():
        _receiving(
            lambda: reconciliations.cancel(
                me, r, plan_revision=payload.plan_revision, reason=payload.reason
            )
        )
        return _reconciliation_out(me, r, locked=True)


# --- Labels -------------------------------------------------------------------------------------


class LabelSizeOut(Schema):
    key: str
    label: str
    width_mm: float
    height_mm: float
    code128: bool = Field(
        description="This stock supports an additional vendor Code 128 if it fits."
    )


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
    include_vendor_code: bool = Field(
        default_factory=bool,
        description="Also print the vendor barcode as Code 128 when it fits. Defaults to false.",
    )
    preview: bool = Field(
        False, description="Only to show them: not recorded as prepared, and with no ZPL."
    )


class LabelOut(Schema):
    id: UUID
    hrid: str
    title: str
    line: str
    code: str = Field(description="Unique Iki record identity encoded in the Data Matrix or QR.")
    vendor_code: str | None = Field(
        description="Additional vendor Code 128 requested for this print, otherwise null."
    )


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
    try:
        made = labelling.labels_of(
            records, size=size, dpi=payload.dpi, include_vendor_code=payload.include_vendor_code
        )
    except storage.Wrong as error:
        raise _invalid(error) from None
    zpl = None
    if payload.format == "zpl" and not payload.preview:
        zpl = labelling.zpl(
            made, size=size, dpi=payload.dpi, symbology=payload.symbology, copies=payload.copies
        )
    if not payload.preview:
        labelling.recorded(me, made, size=size, how=payload.format, copies=payload.copies)
    return {
        "labels": [
            {
                "id": x.entity.pk,
                "hrid": x.hrid,
                "title": x.title,
                "line": x.line,
                "code": x.code,
                "vendor_code": x.vendor_code,
            }
            for x in made
        ],
        "size": labelling.describe({size.key: size})[0],
        "zpl": zpl,
    }
