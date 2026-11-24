"""Putting records away, moving them, checking them out and in, and finding them (ADR 0180).

A move locks the records and the containers involved, in ID order, so two people filling
one box's free positions take turns, and two moves in opposite directions can't wait on
each other. It plans every change, refuses all of them if any is refused, and writes each
record's place and an event of the ledger, sharing one operation. The database refuses two
records at one position, and a position outside a container's grid, whatever writes them.

A position is its container's project's: putting a record in a container, or taking it
out, needs Editor there, and a record goes only into a container of a project it's in.
Where a record is isn't one of its values: a locked record moves, and its revisions don't
change. Where it is is shown only as far as the reader can read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.log import current_agent, current_oauth_client, current_via
from iki_server.core.ids import uuid7
from iki_server.entities import access, registry
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import Invalid
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.storage import scan_identity
from iki_server.storage.grids import NotAPosition, Shape
from iki_server.storage.models import Container, Event, EventKind, Place
from iki_spec.model import EntityType, Storage

#: How deep containers nest: a room, a freezer, a shelf, a rack, a box and a tube are 6.
MAX_DEPTH = 16
#: At most this many records in one move.
MAX_MOVES = 1536


class Wrong(Invalid):
    """What's wrong with a change to where records are, by where it is in the request
    (``items/0/position``, ``reason``), or ``""`` for the request as a whole."""


# --- What a type says ---------------------------------------------------------------------------


def spec(type_name: str) -> Storage | None:
    """Where ``type_name``'s records are kept: its own ``storage``, or its nearest ancestor's."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    for t in [entity_type, *registry.ancestors(entity_type)]:
        if t.storage is not None:
            return t.storage
    return None


def _lineage(type_name: str) -> set[str]:
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return set()
    return {type_name, *(t.qualified_name for t in registry.ancestors(entity_type))}


def holds(container_type: str, record_type: str) -> bool:
    """Whether a record of ``record_type`` can be kept in one of ``container_type``."""
    theirs = spec(container_type)
    return (
        theirs is not None
        and spec(record_type) is not None
        and bool(set(theirs.holds) & _lineage(record_type))
    )


def is_container(type_name: str) -> bool:
    found = spec(type_name)
    return found is not None and bool(found.holds)


def container_types() -> list[str]:
    return [name for name, t in registry.types().items() if not t.abstract and is_container(name)]


def storable_types() -> list[str]:
    return [name for name, t in registry.types().items() if not t.abstract and spec(name)]


def _field_owner(type_name: str, name: str) -> EntityType:
    entity_type = registry.types()[type_name]
    for t in [entity_type, *registry.ancestors(entity_type)]:
        if name in t.fields:
            return t
    raise KeyError(name)


# --- Shapes -------------------------------------------------------------------------------------


def shape_of(entity: Entity, values: Mapping[str, Any] | None = None) -> Shape | None:
    """A container's grid, from its type, or its own fields: None without one."""
    found = spec(entity.type)
    if found is None or found.grid is None:
        return None
    grid = found.grid
    sides: list[int] = []
    for side in (grid.rows, grid.columns):
        if isinstance(side, int):
            sides.append(side)
            continue
        if values is None:
            from iki_server.entities.services import current_values_of

            values = current_values_of([entity])[entity.pk]
        value = values.get(side)
        if not isinstance(value, int) or value < 1:
            return None
        sides.append(value)
    return Shape(sides[0], sides[1], grid.labels, grid.order)


def _kept(container: Container) -> Shape | None:
    if container.rows is None or container.columns is None:
        return None
    return Shape(container.rows, container.columns, container.labels, container.order)


def shapes(containers: Iterable[Any]) -> dict[Any, Shape | None]:
    """The shapes of containers, by ID, as kept when each was first used, or from their
    type and values if they haven't been yet."""
    ids = {c for c in containers if c is not None}
    kept = {c.entity_id: _kept(c) for c in Container.objects.filter(entity_id__in=ids)}
    missing = list(Entity.objects.filter(pk__in=ids - set(kept)))
    if missing:
        from iki_server.entities.services import current_values_of

        values = current_values_of(missing)
        for e in missing:
            kept[e.pk] = shape_of(e, values[e.pk])
    return kept


def ensure_container(entity: Entity) -> Shape | None:
    """Keeps ``entity``'s shape, the first time it holds something, for the database to
    check positions against."""
    kept = Container.objects.filter(pk=entity.pk).first()
    if kept is not None:
        return _kept(kept)
    found = spec(entity.type)
    grid = found.grid if found is not None else None
    shape = shape_of(entity)
    if grid is not None and shape is None:
        raise Wrong({"": [f"{entity.hrid} has no rows and columns recorded."]})
    Container.objects.create(
        entity=entity,
        rows=shape.rows if shape else None,
        columns=shape.columns if shape else None,
        labels=grid.labels if grid else "letter_number",
        order=grid.order if grid else "by_row",
    )
    return shape


def label(shape: Shape | None, row: int | None, column: int | None) -> str | None:
    if shape is None or row is None or column is None:
        return None
    return shape.label(row, column)


# --- Codes: what's scanned and printed ----------------------------------------------------------


def codes_of(entities: Sequence[Entity]) -> dict[Any, str]:
    """Each record's code: its type's barcode, if it has one, or its HRID."""
    out = {e.pk: e.hrid for e in entities}
    by_type: dict[str, list[Entity]] = {}
    for e in entities:
        found = spec(e.type)
        if found is not None and found.barcode is not None:
            by_type.setdefault(e.type, []).append(e)
    for type_name, of_type in by_type.items():
        name = spec(type_name).barcode  # type: ignore[union-attr]
        assert name is not None  # noqa: S101
        model = registry.model(_field_owner(type_name, name).qualified_name)._default_manager
        rows = model.filter(pk__in=[e.pk for e in of_type]).values_list("pk", name)
        out.update({pk: value for pk, value in rows if value})
    return out


def find(user: User, code: str) -> list[Entity]:
    """All readable records identified by a global scan, including non-storable Iki IDs."""
    if not code.strip():
        return []
    index = scan_identity.ScanIndex(user, [code], include_nonstorable=True)
    try:
        return list(index.resolve(code).candidates)
    except scan_identity.ScanRefused:
        # Search has always returned an empty list for an unavailable record.
        return []


# --- Who may change what ------------------------------------------------------------------------


def keeps(user: User, container: Entity) -> bool:
    """Whether ``user`` may put things in ``container``, or take them out: Editor in its
    project. It's that project's freezer."""
    if container.owner_project_id is None:
        return False
    return permissions.can(user, Action.PROJECT_EDIT, container.owner_project)


def in_project(record: Entity, project_id: Any) -> bool:  # noqa: ANN401 - an ID
    return (
        record.owner_project_id == project_id
        or ProjectLink.objects.filter(entity=record, project_id=project_id).exists()
    )


def holds_material(user: User, entity: Entity) -> bool:
    """Whether ``user`` keeps ``entity``: Editor in the project whose container it's in, as a
    core that received a project's tubes keeps them (ADR 0180)."""
    place = Place.objects.filter(pk=entity.pk).select_related("container").first()
    return place is not None and place.container is not None and keeps(user, place.container)


# --- Where it is --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One container along a record's path, and the position in it of what's inside."""

    entity: Entity
    shape: Shape | None
    #: The position, in this container, of the next step in (or of the record).
    row: int | None
    column: int | None

    @property
    def position(self) -> str | None:
        return label(self.shape, self.row, self.column)


@dataclass(frozen=True)
class Where:
    #: Outermost first, as far as the reader can read; the last holds the record.
    path: list[Step]
    #: Its path goes on, through a container the reader can't read.
    hidden: bool
    place: Place | None


def chain(entity: Entity) -> list[tuple[Entity, int | None, int | None]]:
    """The containers ``entity`` is in, innermost first, each with the position in it."""
    out: list[tuple[Entity, int | None, int | None]] = []
    current = Place.objects.filter(pk=entity.pk).first()
    seen = {entity.pk}
    while current is not None and current.container_id is not None and len(out) < MAX_DEPTH:
        if current.container_id in seen:
            break
        container = Entity.objects.get(pk=current.container_id)
        out.append((container, current.row, current.column))
        seen.add(container.pk)
        current = Place.objects.filter(pk=container.pk).first()
    return out


def where(user: User, entity: Entity) -> Where:
    place = Place.objects.filter(pk=entity.pk).select_related("custodian").first()
    found = chain(entity)
    readable = set(
        access.visible(user).filter(pk__in=[c.pk for c, _, _ in found]).values_list("pk", flat=True)
    )
    kept = shapes([c.pk for c, _, _ in found])
    path: list[Step] = []
    hidden = False
    for container, row, column in found:
        if container.pk not in readable:
            hidden = True
            break
        path.append(Step(container, kept.get(container.pk), row, column))
    return Where(list(reversed(path)), hidden, place)


def inside(container: Entity) -> list[Place]:
    """What's in ``container`` now."""
    return list(Place.objects.filter(container=container).select_related("custodian"))


def free(container: Entity, shape: Shape) -> list[tuple[int, int]]:
    """``container``'s free positions, in its order."""
    taken = set(
        Place.objects.filter(container=container, row__isnull=False).values_list("row", "column")
    )
    return [p for p in shape.positions() if p not in taken]


def counts(containers: Iterable[Any]) -> dict[Any, int]:
    """How many records each container holds, by ID."""
    from django.db.models import Count

    return dict(
        Place.objects.filter(container__in=list(containers))
        .values_list("container")
        .annotate(n=Count("pk"))
        .values_list("container", "n")
    )


# --- Moving -------------------------------------------------------------------------------------


@dataclass
class Wanted:
    """A record to put somewhere: ``to`` a container (None: out of storage), at
    ``position`` (a label such as ``A1``, or None for its next free one)."""

    record: Entity
    to: Entity | None
    position: str | None = None
    #: A reconciliation can explain each move with a different observation or decision.
    evidence: str = ""


@dataclass(frozen=True)
class Confirmation:
    code: str
    mode: scan_identity.ScanMode = "label"


class ConfirmationScope:
    """The selection and all its source boxes. A tube without a known source
    requires checking the caller's whole readable inventory."""

    def __init__(self, user: User, records: Sequence[Entity], codes: Sequence[str]) -> None:
        self.selected = {record.pk for record in records}
        sources = list(
            Place.objects.filter(pk__in=self.selected, container__isnull=False).values_list(
                "container_id", flat=True
            )
        )
        boxes = set(sources)
        occupants = Place.objects.filter(container_id__in=boxes).values_list("pk", flat=True)
        members = self.selected.union(occupants)
        scope_ids = boxes | members
        self.index = scan_identity.ScanIndex(user, codes, scope_ids=scope_ids)
        self.candidates = members if len(sources) == len(self.selected) else None
        self.selection_readable = self.selected <= self.index.readable_ids
        self.has_concealed = not scope_ids <= self.index.readable_ids

    def resolve(self, code: str, *, mode: scan_identity.ScanMode = "label") -> scan_identity.Match:
        if not self.selection_readable:
            raise scan_identity.ScanRefused()
        match = self.index.resolve(
            code, mode=mode, candidates=self.candidates, has_concealed=self.has_concealed
        )
        if match.candidates and not any(record.pk in self.selected for record in match.candidates):
            raise scan_identity.ScanRefused()
        return match


@dataclass
class Planned:
    record: Entity
    to: Entity | None
    row: int | None
    column: int | None
    shape: Shape | None
    was: Place | None
    was_shape: Shape | None
    problems: list[str] = field(default_factory=list)
    confirmed: str = ""

    @property
    def position(self) -> str | None:
        return label(self.shape, self.row, self.column)

    @property
    def from_position(self) -> str | None:
        if self.was is None:
            return None
        return label(self.was_shape, self.was.row, self.was.column)

    @property
    def kind(self) -> str:
        if self.was is None or self.was.container_id is None:
            return EventKind.PLACED if self.to is not None else EventKind.REMOVED
        return EventKind.MOVED if self.to is not None else EventKind.REMOVED


def plan(
    user: User,
    wanted: Sequence[Wanted],
    *,
    confirmations: Mapping[Any, str | Confirmation] | None = None,
    receiving: bool = False,
) -> list[Planned]:
    """What each move would do, and each problem with it, changing nothing. ``receiving``
    takes custody from wherever a record was recorded, as a receipt does."""
    if len(wanted) > MAX_MOVES:
        raise Wrong({"items": [f"has more than {MAX_MOVES} moves."]})
    records = [w.record.pk for w in wanted]
    places = {p.pk: p for p in Place.objects.filter(pk__in=records)}
    targets = {w.to.pk: w.to for w in wanted if w.to is not None}
    sources = {p.container_id for p in places.values() if p.container_id is not None}
    kept = shapes([*targets, *sources])
    readable = set(
        access.visible(user)
        .filter(pk__in=[*records, *targets, *sources])
        .values_list("pk", flat=True)
    )
    moving = {w.record.pk for w in wanted}
    # Who's in each target now, and stays: they aren't moving in this plan.
    staying: dict[Any, set[tuple[int, int]]] = {pk: set() for pk in targets}
    for p in Place.objects.filter(container__in=list(targets), row__isnull=False):
        if p.pk not in moving:
            staying[p.container_id].add((p.row, p.column))  # type: ignore[arg-type]
    claimed: dict[Any, set[tuple[int, int]]] = {pk: set() for pk in targets}
    out: list[Planned] = []
    seen: set[Any] = set()
    for w in wanted:
        was = places.get(w.record.pk)
        planned = Planned(
            w.record,
            w.to,
            None,
            None,
            kept.get(w.to.pk) if w.to is not None else None,
            was,
            kept.get(was.container_id) if was is not None and was.container_id else None,
        )
        out.append(planned)
        problems = planned.problems
        record = w.record
        if record.pk in seen:
            problems.append(f"{record.hrid} is named twice.")
            continue
        seen.add(record.pk)
        if record.pk not in readable:
            problems.append(f"{record.hrid} isn't a record you can read.")
            continue
        if spec(record.type) is None:
            problems.append(f"{record.hrid} is a {record.type}, which isn't kept anywhere.")
            continue
        source = was.container if was is not None and was.container_id else None
        if source is not None and not receiving and not keeps(user, source):
            problems.append(
                f"{record.hrid} is in "
                + (source.hrid if source.pk in readable else "a place you can't open")
                + ": taking it out needs Editor in its project."
            )
        if w.to is None:
            if was is None or was.container_id is None:
                problems.append(f"{record.hrid} isn't in storage.")
            continue
        _into(user, planned, readable, receiving=receiving)
        if problems:
            continue
        shape = planned.shape
        if shape is None:
            if w.position is not None:
                problems.append(f"{w.to.hrid} has no positions, so {record.hrid} has none in it.")
            elif was is not None and was.container_id == w.to.pk:
                problems.append(f"{record.hrid} is in {w.to.hrid} already.")
            continue
        if w.position is None:
            continue  # filled below, in the container's order
        try:
            row, column = shape.parse(w.position)
        except NotAPosition as error:
            problems.append(f"{w.to.hrid}: {error}")
            continue
        position = (row, column)
        if was is not None and was.container_id == w.to.pk and (was.row, was.column) == position:
            problems.append(f"{record.hrid} is at {shape.label(row, column)} already.")
        elif position in staying[w.to.pk] or position in claimed[w.to.pk]:
            problems.append(f"{w.to.hrid} has something at {shape.label(row, column)} already.")
        else:
            claimed[w.to.pk].add(position)
            planned.row, planned.column = position
    # Then the next free positions, in each container's order.
    for planned in out:
        if planned.problems or planned.to is None or planned.shape is None:
            continue
        if planned.row is not None:
            continue
        taken = staying[planned.to.pk] | claimed[planned.to.pk]
        nxt = next((p for p in planned.shape.positions() if p not in taken), None)
        if nxt is None:
            planned.problems.append(f"{planned.to.hrid} has no free position left.")
            continue
        claimed[planned.to.pk].add(nxt)
        planned.row, planned.column = nxt
    proofs, errors = _confirmed(
        user,
        [w.record for w in wanted],
        [p.record for p in out if not p.problems],
        confirmations or {},
    )
    for planned in out:
        if planned.record.pk in errors and not planned.problems:
            planned.problems.append(errors[planned.record.pk])
        planned.confirmed = proofs.get(planned.record.pk, "")
    return out


def _into(user: User, planned: Planned, readable: set[Any], *, receiving: bool) -> None:
    to, record, problems = planned.to, planned.record, planned.problems
    assert to is not None  # noqa: S101
    if to.pk not in readable:
        problems.append("Where it goes isn't a record you can read.")
        return
    if to.archived_at is not None:
        problems.append(f"{to.hrid} is archived: nothing more is put in it.")
        return
    if not is_container(to.type):
        problems.append(f"{to.hrid} is a {to.type}, which holds nothing.")
        return
    if not holds(to.type, record.type):
        problems.append(f"{to.hrid} doesn't hold a {registry.types()[record.type].label}.")
        return
    if record.archived_at is not None:
        problems.append(f"{record.hrid} is archived: restore it to put it away.")
        return
    if to.pk == record.pk or any(c.pk == record.pk for c, _, _ in chain(to)):
        problems.append(f"{to.hrid} is inside {record.hrid}, so it can't go in it.")
        return
    if not keeps(user, to):
        problems.append(f"Putting things in {to.hrid} needs Editor in its project.")
        return
    if not in_project(record, to.owner_project_id):
        problems.append(
            f"{record.hrid} isn't in {to.hrid}'s project, so it can't be kept there: whoever "
            "reads the box reads what's in it."
        )


def _confirmed(
    user: User,
    selected: Sequence[Entity],
    checking: Sequence[Entity],
    confirmations: Mapping[Any, str | Confirmation],
) -> tuple[dict[UUID, str], dict[UUID, str]]:
    """Verify required physical evidence once against the selection and all its
    source boxes. Only the accepted proof is written to its movement event."""
    errors: dict[UUID, str] = {}
    submitted: list[tuple[Entity, Confirmation]] = []
    for record in checking:
        found = spec(record.type)
        if found is None or "move" not in found.confirm:
            continue
        value = confirmations.get(record.pk, "")
        proof = Confirmation(value) if isinstance(value, str) else value
        if not proof.code.strip():
            errors[record.pk] = f"Scan {record.hrid} to confirm it's the one you're moving."
        elif len(proof.code) > 200:
            errors[record.pk] = "The confirmation must be at most 200 characters."
        else:
            submitted.append((record, proof))
    verified: dict[UUID, str] = {}
    if submitted:
        scope = ConfirmationScope(user, selected, [proof.code for _, proof in submitted])
        for record, proof in submitted:
            try:
                match = scope.resolve(proof.code, mode=proof.mode)
            except scan_identity.ScanRefused:
                match = None
            if match is not None and match.status == "one" and match.record == record:
                verified[record.pk] = proof.code.strip()
            else:
                errors[record.pk] = (
                    f"Could not confirm {record.hrid}. "
                    "Scan its record link or enter its printed Iki ID."
                )
    return verified, errors


def _lock(ids: Iterable[Any]) -> None:
    """Lock records and containers in ID order, so moves can't wait on each other."""
    list(
        Entity.objects.select_for_update(no_key=True)
        .filter(pk__in=list(set(ids)))
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def problems_of(planned: Sequence[Planned]) -> dict[str, list[str]]:
    return {f"items/{i}": p.problems for i, p in enumerate(planned) if p.problems}


def _lock_moves(wanted: Sequence[Wanted]) -> None:
    """Serialize changes to the selected records and the inventories used to confirm them."""
    ids = [w.record.pk for w in wanted]
    places = Place.objects.filter(pk__in=ids, container__isnull=False)
    locked = {*ids, *(w.to.pk for w in wanted if w.to is not None)}
    locked.update(places.values_list("container_id", flat=True))
    _lock(locked)
    # Another move could have finished while we were acquiring these locks. A
    # newly introduced source box wasn't locked, so its occupancy could still change.
    if places.exclude(container_id__in=locked).exists():
        raise Wrong(
            {"items": ["A record changed location. Review its current position and retry."]}
        )


def move(
    user: User,
    wanted: Sequence[Wanted],
    *,
    reason: str,
    confirmations: Mapping[Any, str | Confirmation] | None = None,
    receiving: bool = False,
    operation: Any = None,  # noqa: ANN401 - a UUID
    handed_over_by: str = "",
) -> list[Event]:
    """Puts each record where it's wanted, all of them or none, as one operation."""
    if not reason.strip():
        raise Wrong({"reason": ["is required: say why they're moved."]})
    if not wanted:
        raise Wrong({"items": ["names nothing to move."]})
    with transaction.atomic():
        _lock_moves(wanted)
        for to in {w.to.pk: w.to for w in wanted if w.to is not None}.values():
            if is_container(to.type):
                ensure_container(to)
        planned = plan(user, wanted, confirmations=confirmations, receiving=receiving)
        wrong = problems_of(planned)
        if wrong:
            raise Wrong(wrong)
        operation = operation or uuid7()
        events = [
            _apply(
                user,
                p,
                f"{reason} {w.evidence}" if w.evidence else reason,
                operation,
                p.confirmed,
                handed_over_by,
            )
            for w, p in zip(wanted, planned, strict=True)
        ]
        _check_now()
        return events


def _check_now() -> None:
    """Check one record per position now, so a clash is refused as a problem, not at commit;
    then defer it again, for whatever else the transaction moves."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS iki_storage_place_one_per_position IMMEDIATE")
    except IntegrityError:
        raise Wrong(
            {"": ["Something else was put in one of those positions: try again."]}
        ) from None
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS iki_storage_place_one_per_position DEFERRED")


def _apply(
    user: User,
    p: Planned,
    reason: str,
    operation: Any,  # noqa: ANN401 - a UUID
    confirmed: str,
    handed_over_by: str,
) -> Event:
    was = p.was
    # Checked out, it keeps a position and its custodian wherever that moves; in someone's
    # hands without one (received, not put away), it's put away now, and with nobody.
    kept = was is not None and was.container_id is not None
    custodian = was.custodian if was is not None and kept and p.to is not None else None
    Place.objects.update_or_create(
        entity=p.record,
        defaults={
            "container": p.to,
            "row": p.row,
            "column": p.column,
            "custodian": custodian,
            "custody_since": (was.custody_since if was is not None and custodian else None),
            "updated_at": timezone.now(),
        },
    )
    return record_event(
        user,
        p.record,
        p.kind,
        was=was,
        was_shape=p.was_shape,
        to=p.to,
        row=p.row,
        column=p.column,
        shape=p.shape,
        custodian=custodian,
        reason=reason,
        operation=operation,
        confirmed=confirmed,
        handed_over_by=handed_over_by,
    )


def record_event(
    user: User | None,
    entity: Entity,
    kind: str,
    *,
    was: Place | None,
    was_shape: Shape | None,
    to: Entity | None,
    row: int | None,
    column: int | None,
    shape: Shape | None,
    custodian: User | None,
    reason: str = "",
    operation: Any = None,  # noqa: ANN401 - a UUID
    confirmed: str = "",
    handed_over_by: str = "",
) -> Event:
    """Keeps one change of where ``entity`` is, as an event of its ledger and an audit event."""
    event = Event.objects.create(
        entity=entity,
        kind=kind,
        from_container_id=was.container_id if was is not None else None,
        from_row=was.row if was is not None else None,
        from_column=was.column if was is not None else None,
        to_container=to,
        to_row=row,
        to_column=column,
        from_custodian=was.custodian if was is not None else None,
        custodian=custodian,
        handed_over_by=handed_over_by.strip(),
        confirmed=confirmed.strip(),
        operation=operation or uuid7(),
        user=user,
        via=current_via(),
        client=current_oauth_client(),
        agent=current_agent(),
        reason=reason.strip(),
    )
    source = (
        Entity.objects.filter(pk=was.container_id).values_list("hrid", flat=True).first()
        if was is not None and was.container_id
        else None
    )
    log.record(
        "storage.moved",
        actor=user,
        target=entity,
        project=entity.owner_project,
        data={
            "kind": kind,
            "from": {
                "container": str(was.container_id),
                "hrid": source,
                "position": label(was_shape, was.row, was.column),
            }
            if was is not None and was.container_id
            else None,
            "to": {"container": str(to.pk), "hrid": to.hrid, "position": label(shape, row, column)}
            if to is not None
            else None,
            "custodian": str(custodian.pk) if custodian is not None else None,
            "operation": str(event.operation),
            "reason": event.reason,
            **({"confirmed": event.confirmed} if event.confirmed else {}),
        },
    )
    return event


def remove(
    user: User,
    records: Sequence[Entity],
    *,
    reason: str,
    operation: Any = None,  # noqa: ANN401 - a UUID
) -> list[Event]:
    """Takes records out of storage: used up, discarded or shipped."""
    return move(user, [Wanted(r, None) for r in records], reason=reason, operation=operation)


# --- Custody ------------------------------------------------------------------------------------


def _custody(user: User, records: Sequence[Entity]) -> dict[Any, Place]:
    _lock([r.pk for r in records])
    return {
        p.pk: p for p in Place.objects.select_for_update().filter(pk__in=[r.pk for r in records])
    }


def check_out(user: User, records: Sequence[Entity], *, to: User, reason: str = "") -> list[Event]:
    """Checks records out of storage to ``to``: each keeps its position, which nothing else
    can take, until it's checked in."""
    if not records:
        raise Wrong({"records": ["names nothing to check out."]})
    with transaction.atomic():
        places = _custody(user, records)
        wrong: dict[str, list[str]] = {}
        for i, r in enumerate(records):
            p = places.get(r.pk)
            if p is None or p.container_id is None:
                wrong[f"records/{i}"] = [f"{r.hrid} isn't in storage, so it isn't checked out."]
            elif p.custodian_id is not None:
                wrong[f"records/{i}"] = [f"{r.hrid} is checked out already."]
            elif not keeps(user, p.container):  # type: ignore[arg-type]
                wrong[f"records/{i}"] = [
                    f"Checking {r.hrid} out needs Editor in its container's project."
                ]
            elif not access.can_read(to, r):
                wrong[f"records/{i}"] = [f"{to} can't read {r.hrid}, so can't have it."]
        if wrong:
            raise Wrong(wrong)
        operation = uuid7()
        now = timezone.now()
        kept = shapes({p.container_id for p in places.values()})
        out = []
        for r in records:
            p = places[r.pk]
            was = Place(**{f.attname: getattr(p, f.attname) for f in Place._meta.concrete_fields})
            p.custodian, p.custody_since, p.updated_at = to, now, now
            p.save(update_fields=["custodian", "custody_since", "updated_at"])
            shape = kept.get(p.container_id)
            out.append(
                record_event(
                    user,
                    r,
                    EventKind.CHECKED_OUT,
                    was=was,
                    was_shape=shape,
                    to=p.container,
                    row=p.row,
                    column=p.column,
                    shape=shape,
                    custodian=to,
                    reason=reason,
                    operation=operation,
                )
            )
        return out


def hand_over(user: User, record: Entity, *, to: User, reason: str = "") -> Event:
    """Passes a record that's checked out, or in someone's custody, to another person."""
    with transaction.atomic():
        p = _custody(user, [record]).get(record.pk)
        if p is None or p.custodian_id is None:
            raise Wrong({"": [f"{record.hrid} isn't with anyone, so it isn't handed over."]})
        allowed = p.custodian_id == user.pk or (
            keeps(user, p.container) if p.container is not None else access.can_edit(user, record)
        )
        if not allowed:
            raise Wrong({"": [f"{record.hrid} is with someone else: they hand it over."]})
        if p.custodian_id == to.pk:
            raise Wrong({"to": [f"{to} has {record.hrid} already."]})
        if not access.can_read(to, record):
            raise Wrong({"to": [f"{to} can't read {record.hrid}, so can't have it."]})
        was = Place(**{f.attname: getattr(p, f.attname) for f in Place._meta.concrete_fields})
        now = timezone.now()
        p.custodian, p.custody_since, p.updated_at = to, now, now
        p.save(update_fields=["custodian", "custody_since", "updated_at"])
        shape = shapes([p.container_id]).get(p.container_id) if p.container_id else None
        return record_event(
            user,
            record,
            EventKind.HANDED_OVER,
            was=was,
            was_shape=shape,
            to=p.container,
            row=p.row,
            column=p.column,
            shape=shape,
            custodian=to,
            reason=reason,
        )


def check_in(
    user: User,
    wanted: Sequence[Wanted],
    *,
    reason: str = "",
    confirmations: Mapping[Any, str | Confirmation] | None = None,
) -> list[Event]:
    """Checks records back in: each to the position it kept, or, where ``to`` is given, to
    another, in the same change."""
    if not wanted:
        raise Wrong({"records": ["names nothing to check in."]})
    with transaction.atomic():
        _lock_moves(wanted)
        records = [w.record for w in wanted]
        places = {
            p.pk: p
            for p in Place.objects.select_for_update().filter(pk__in=[r.pk for r in records])
        }
        wrong: dict[str, list[str]] = {}
        seen: set[UUID] = set()
        for i, w in enumerate(wanted):
            p = places.get(w.record.pk)
            if w.record.pk in seen:
                wrong[f"records/{i}"] = [f"{w.record.hrid} is named twice."]
            elif p is None or p.custodian_id is None:
                wrong[f"records/{i}"] = [f"{w.record.hrid} isn't checked out."]
            elif w.to is None and p.container_id is None:
                wrong[f"records/{i}"] = [
                    f"Say where {w.record.hrid} goes: it has no position kept."
                ]
            elif w.to is None and not keeps(user, p.container):  # type: ignore[arg-type]
                wrong[f"records/{i}"] = [
                    f"Checking {w.record.hrid} in needs Editor in its container's project."
                ]
            seen.add(w.record.pk)
        if wrong:
            raise Wrong(wrong)
        verified, errors = _confirmed(user, records, records, confirmations or {})
        if errors:
            raise Wrong(
                {f"items/{i}": [errors[r.pk]] for i, r in enumerate(records) if r.pk in errors}
            )
        operation = uuid7()
        moving = [w for w in wanted if w.to is not None]
        if moving:
            # Moved and checked in as one operation: the move first, keeping custody...
            move(
                user,
                moving,
                reason=reason or "Checked in elsewhere.",
                confirmations=confirmations,
                operation=operation,
            )
            places = {p.pk: p for p in Place.objects.filter(pk__in=[w.record.pk for w in wanted])}
        kept = shapes({p.container_id for p in places.values()})
        out = []
        for w in wanted:
            p = places[w.record.pk]
            was = Place(**{f.attname: getattr(p, f.attname) for f in Place._meta.concrete_fields})
            p.custodian, p.custody_since, p.updated_at = None, None, timezone.now()
            p.save(update_fields=["custodian", "custody_since", "updated_at"])
            shape = kept.get(p.container_id)
            out.append(
                record_event(
                    user,
                    w.record,
                    EventKind.CHECKED_IN,
                    was=was,
                    was_shape=shape,
                    to=p.container,
                    row=p.row,
                    column=p.column,
                    shape=shape,
                    custodian=None,
                    reason=reason,
                    operation=operation,
                    confirmed=verified.get(w.record.pk, ""),
                )
            )
        return out


def checked_out(user: User, custodian: User) -> list[Place]:
    """What ``custodian`` has, of what ``user`` can read."""
    return list(
        Place.objects.filter(
            custodian=custodian, entity__in=access.visible(user).values("pk")
        ).order_by("custody_since")
    )


def took_custody(
    user: User,
    record: Entity,
    *,
    handed_over_by: str,
    reason: str,
    operation: Any,  # noqa: ANN401 - a UUID
) -> Event:
    """A receipt's custody of ``record``: it's with ``user`` now, out of wherever its
    project recorded it, until it's put away."""
    p = Place.objects.select_for_update().filter(pk=record.pk).first()
    was = (
        Place(**{f.attname: getattr(p, f.attname) for f in Place._meta.concrete_fields})
        if p is not None
        else None
    )
    now = timezone.now()
    Place.objects.update_or_create(
        entity=record,
        defaults={
            "container": None,
            "row": None,
            "column": None,
            "custodian": user,
            "custody_since": now,
            "updated_at": now,
        },
    )
    was_shape = (
        shapes([was.container_id]).get(was.container_id) if was and was.container_id else None
    )
    return record_event(
        user,
        record,
        EventKind.RECEIVED,
        was=was,
        was_shape=was_shape,
        to=None,
        row=None,
        column=None,
        shape=None,
        custodian=user,
        reason=reason,
        operation=operation,
        handed_over_by=handed_over_by,
    )


def events_since(container: Entity, since: datetime) -> bool:
    """Whether anything moved into or out of ``container`` since ``since``."""
    return Event.objects.filter(
        Q(from_container=container) | Q(to_container=container), created_at__gt=since
    ).exists()
