"""A type's records edited as a sheet (ADR 0228): its cells typed, pasted and filled down,
and rows added, checked as they change, then saved at once with a reason.

A sheet's cells are text, as a spreadsheet's are, and each is read as its field reads a
workbook's cell (:mod:`.cells`, ADR 0178): a quantity in its column's unit or its own, a
reference by its ID, HRID or name, a choice in any case, numbers and dates as the sheet's
locale says they're written, never guessed. A new row's place in storage and how much it
starts with are read too: ``BOX-000002 A1``, and ``500`` or ``0.5 mL``.

What a row would do is found by doing it. :func:`check` makes the changes as :func:`save`
makes them (each edit by :func:`entities.update`, each new record by
:func:`entities.create` with its amount, each place by :func:`storage.plan`), then rolls
them all back, its new records numbered apart (:func:`hrids.provisional`) so no HRID is
spent. So every rule an edit is held to, a check holds a cell to: a locked or signed
record, a value that's another record's, a plugin's rule, a reference the person can't use.

:func:`save` is given what the person reviewed: each row's values as the check read them,
the revision of each record they saw, and the exact place of each new record. It locks
the records, refuses (:class:`Stale`) if any has changed since, or if another placement
took a place the check found, and then makes every change, all of them or none
(:class:`Failed`), each edit a revision with the reason, by the person and any agent
acting for them, as one operation in the audit trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.functions import Lower

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, hrids, registry
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid, shape, subtypes
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.spreadsheets import cells
from iki_server.spreadsheets.cells import Column, Locale, Reference
from iki_server.spreadsheets.services import Resolver, resolved
from iki_spec.model import Field

__all__ = [
    "MAX_CHECK_ROWS",
    "MAX_ROWS",
    "Failed",
    "Given",
    "Outcome",
    "Place",
    "Problem",
    "Row",
    "Stale",
    "Taken",
    "check",
    "read",
    "save",
]

#: The most rows one save changes or adds. More are a spreadsheet import (ADR 0178).
MAX_ROWS = 1000
#: The most rows one check reads: a large paste is checked a chunk at a time.
MAX_CHECK_ROWS = 200
#: Where a row's place in storage and its starting amount are, in its problems.
LOCATION = "location"
AMOUNT = "amount"


@dataclass(frozen=True)
class Problem:
    """What's wrong with a row: ``at`` a cell (``values/concentration``), its ``location``
    or ``amount``, or ``""`` for the row as a whole."""

    at: str
    detail: str
    #: ``invalid``; ``ambiguous``, a cell that reads two ways by the sheet's locale; or
    #: ``stale``, a record that changed since the person read it.
    kind: str = "invalid"


@dataclass(frozen=True)
class Place:
    """Where a new record goes: a container, at a position (``A1``). A check finds a
    container's next free one, for none; a save takes the one the check found."""

    container: uuid.UUID
    position: str | None = None


@dataclass(frozen=True)
class Given:
    """A row as the sheet has it: a record's (``entity``, at the ``revision`` read) or a
    new one, and the text of each cell changed, ``None`` or ``""`` to empty it."""

    key: str
    entity: uuid.UUID | None = None
    revision: int | None = None
    cells: Mapping[str, str | None] = field(default_factory=dict)
    location: str | None = None
    amount: str | None = None


@dataclass
class Row:
    """A row's changes, as values: each field's as an edit or a create gives it."""

    key: str
    entity: uuid.UUID | None = None
    revision: int | None = None
    values: dict[str, Any] = field(default_factory=dict)
    location: Place | None = None
    amount: dict[str, Any] | None = None
    #: What's wrong with what its cells say, found reading them.
    problems: list[Problem] = field(default_factory=list)


@dataclass
class Outcome:
    """What a row did, or would do."""

    key: str
    #: ``edited``, ``unchanged``, ``created`` (``registered`` a new entry, or ``added`` a
    #: record of one), ``blank`` (a new row with nothing in it), or ``failed``.
    kind: str
    entity: Entity | None = None
    #: The fields it changed, or a new record's set.
    changed: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    #: The rules that hold it for review (ADR 0127), saved with a reason.
    held: list[str] = field(default_factory=list)
    #: A new record's container and position, as planned or made.
    container: Entity | None = None
    position: str | None = None
    #: What the API says of it, added while its changes are still there to read.
    shown: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Taken:
    """A new row's place, as the check found it, that another placement took since: its
    row, the container, and the position."""

    key: str
    container: Entity
    position: str

    @property
    def shown(self) -> str:
        return f"{self.container.hrid} {self.position}"


class Stale(Exception):  # noqa: N818 - reads as "the sheet was stale"
    """What the person reviewed has moved on: records changed since they read them (their
    rows and current revisions), or places taken since the check. Nothing was written."""

    def __init__(self, rows: list[tuple[str, Entity]], places: Sequence[Taken] = ()) -> None:
        self.rows = rows
        self.places = list(places)
        said = []
        if rows:
            names = ", ".join(e.hrid for _, e in rows[:5]) + (", …" if len(rows) > 5 else "")
            said.append(f"{names} changed since you read {'it' if len(rows) == 1 else 'them'}")
        if self.places:
            where = ", ".join(p.shown for p in self.places[:5])
            more = ", …" if len(self.places) > 5 else ""
            said.append(f"something else was put at {where}{more} since you checked")
        text = "; ".join(said)
        if rows and not self.places:
            one = len(rows) == 1
            advice = f"Review {'its' if one else 'their'} latest values, then save again."
        elif self.places and not rows:
            advice = "Check the sheet again, and review where the new rows go now."
        else:
            advice = "Review the latest values, and check where the new rows go now."
        super().__init__(f"{text[0].upper()}{text[1:]}: nothing was saved. {advice}")


class Failed(Exception):  # noqa: N818 - reads as "the save failed"
    """A save with problems: every row's outcome, its problems where they are. Nothing was
    written."""

    def __init__(self, outcomes: list[Outcome]) -> None:
        self.outcomes = outcomes
        failed = sum(1 for o in outcomes if o.problems)
        super().__init__(
            f"{failed} {'row has' if failed == 1 else 'rows have'} problems: nothing was saved."
        )


class _RollBack(Exception):  # noqa: N818 - what a check does with what it made
    """Undoes a check's changes."""


# --- The sheet's type ------------------------------------------------------------------------


def sheet_type(type_name: str) -> Any:  # noqa: ANN401 - a values.Shape
    """The type a sheet edits, or :class:`Invalid` saying why it can't be."""
    found = registry.types().get(type_name)
    if found is None or type_name == "iki/core.Entity":
        raise Invalid({"type": [f"There's no entity type {type_name}."]})
    form = shape(type_name)
    why = entities.frozen(form)
    if why:
        raise Invalid({"type": [why]})
    return form


def input_unit(f: Field) -> str | None:
    """The unit a quantity's cell is in when it gives none: the one its column says, its
    display unit if it has one, as the grid shows it (ADR 0175); None for the field's."""
    item = f.items if f.type == "list" and f.items is not None else f
    if item.type != "quantity":
        return None
    return item.display_unit or None


def amount_unit(type_name: str) -> str | None:
    """The unit a new record's starting amount is in, when its cell gives none."""
    from iki_server.amounts import services as amounts

    wanted = amounts.spec(type_name)
    return None if wanted is None else wanted.display_unit or wanted.unit


def placeable(type_name: str) -> str | None:
    """Why a sheet can't say where a new record of ``type_name`` is kept, or None if it
    can: its type is kept in storage, and putting one away needs no scan."""
    from iki_server.storage import services as storage

    found = storage.spec(type_name)
    if found is None:
        return f"{_label(type_name)} records aren't kept in storage."
    if "move" in found.confirm:
        return "Putting one away needs it scanned: put new ones away in Storage."
    return None


# --- Reading cells ---------------------------------------------------------------------------


def read(
    user: User,
    project: Project,
    type_name: str,
    given: Sequence[Given],
    locale: Locale,
    time_zone: str | None = None,
) -> list[Row]:
    """Each row's cells as values, each cell read as an import reads its field's, by the
    sheet's ``locale``; dates and times without a zone are in ``time_zone``. A cell that
    can't be read is a problem of its row, by the cell."""
    form = sheet_type(type_name)
    fields = form.fields()
    resolver = Resolver(user, project)
    rows: list[Row] = []
    wanted: dict[str, set[str]] = {}
    places: dict[str, Place | str] = {}
    for g in given:
        row = Row(g.key, g.entity, g.revision)
        rows.append(row)
        for name, text in g.cells.items():
            at = f"values/{name}"
            f = fields.get(name)
            if f is None:
                row.problems.append(Problem(at, f"{_label(type_name)} has no field `{name}`."))
                continue
            why = cells.not_mappable(f)
            if why is not None:
                row.problems.append(Problem(at, f"{_field_label(f, name)}: {why}"))
                continue
            column = Column(0, name, unit=input_unit(f), time_zone=time_zone).under(locale)
            try:
                value = cells.convert(f, column, text)
            except ValueError as error:
                said = text or ""
                said = said if len(said) <= 60 else said[:57] + "…"
                kind = "ambiguous" if isinstance(error, cells.Ambiguous) else "invalid"
                row.problems.append(Problem(at, f"“{said}” {error}", kind))
                continue
            row.values[name] = value
            for found in value if isinstance(value, list) else [value]:
                if isinstance(found, Reference):
                    wanted.setdefault(name, set()).add(found.text)
        if g.location is not None and g.location.strip():
            if g.entity is not None:
                row.problems.append(Problem(LOCATION, "Move a record from its page or in Storage."))
            elif (why := placeable(type_name)) is not None:
                row.problems.append(Problem(LOCATION, why))
            else:
                text = g.location.strip()
                if text not in places:
                    places[text] = _place(user, project, text)
                place = places[text]
                if isinstance(place, str):
                    row.problems.append(Problem(LOCATION, f"“{text}” {place}"))
                else:
                    row.location = place
        if g.amount is not None and g.amount.strip():
            unit = amount_unit(type_name)
            if g.entity is not None:
                row.problems.append(
                    Problem(
                        AMOUNT,
                        "changes through its ledger: draw, transfer or adjust it on its page.",
                    )
                )
            elif unit is None:
                row.problems.append(Problem(AMOUNT, f"{_label(type_name)} records have no amount."))
            else:
                from iki_server.amounts import services as amounts

                column = Column(0).under(locale)
                text = g.amount.strip()
                try:
                    read_amount = cells.quantity(column, text, unit)
                    # As a create reads it first: more than zero, in a unit, kept whole.
                    amounts.Given.of(read_amount)
                except amounts.Wrong as wrong:
                    said = " ".join(m for ms in wrong.errors.values() for m in ms)
                    row.problems.append(Problem(AMOUNT, f"“{text}” {said}"))
                except ValueError as error:
                    kind = "ambiguous" if isinstance(error, cells.Ambiguous) else "invalid"
                    row.problems.append(Problem(AMOUNT, f"“{text}” {error}", kind))
                else:
                    row.amount = read_amount
    named = {name: resolver.find(fields[name], texts) for name, texts in wanted.items()}
    for row in rows:
        for name in list(row.values):
            if name not in named:
                continue
            try:
                row.values[name] = resolved(row.values[name], named[name])
            except ValueError as error:
                del row.values[name]
                row.problems.append(Problem(f"values/{name}", str(error)))
    return rows


def _place(user: User, project: Project, text: str) -> Place | str:
    """The container ``text`` names, with a position after it (``BOX-000002 A1``), or why
    it names none: by its ID, its code (a barcode or HRID, as a scan finds it) or its name,
    among the project's containers."""
    tried: list[tuple[str, str | None]] = [(text, None)]
    for separator in (" ", "/", ",", ":"):
        head, found, tail = text.rpartition(separator)
        if found and head.strip() and tail.strip():
            tried.append((head.strip().rstrip("/,:").strip(), tail.strip()))
    for container_text, position in tried:
        named = _containers(user, project, container_text)
        if len(named) == 1:
            return Place(named[0], position)
        if len(named) > 1:
            return f"names {len(named)} containers here: give its ID instead."
    return (
        f"names no box or other container in {project.name}, by its ID, code or name: "
        "write it as BOX-000002 A1."
    )


def _containers(user: User, project: Project, text: str) -> list[uuid.UUID]:
    from iki_server.storage import services as storage

    types = set(storage.container_types())
    here = access.in_project(project).filter(type__in=types)
    if cells.is_id(text):
        return list(here.filter(pk=text).values_list("pk", flat=True))
    coded = {e.pk for e in storage.find(user, text) if e.type in types}
    if coded:
        return sorted(here.filter(pk__in=coded).values_list("pk", flat=True)) or sorted(coded)
    named: set[uuid.UUID] = set()
    for type_name in types:
        if "name" not in shape(type_name).fields():
            continue
        model = shape(type_name).model
        named.update(
            model._default_manager.filter(pk__in=here.values("pk"), type=type_name)
            .alias(folded=Lower("name"))
            .filter(folded=text.casefold())
            .values_list("pk", flat=True)
        )
    return sorted(named)


# --- Checking and saving ---------------------------------------------------------------------


def check(
    user: User,
    project: Project,
    type_name: str,
    rows: Sequence[Row],
    *,
    describe: Callable[[list[Outcome]], None] | None = None,
) -> list[Outcome]:
    """What saving ``rows`` would do, row by row, with each one's problems: found by making
    the changes, as :func:`save` would, and undoing them. ``describe`` reads the outcomes
    while their changes are still there."""
    if len(rows) > MAX_CHECK_ROWS:
        raise Invalid({"rows": [f"has more than {MAX_CHECK_ROWS} rows: check them in parts."]})
    outcomes: list[Outcome] = []
    try:
        with transaction.atomic(), hrids.provisional():
            outcomes = _apply(
                user,
                project,
                type_name,
                rows,
                reason="Checking a sheet's changes.",
                hold_reason="Checking a sheet's changes.",
                operation=uuid.uuid4(),
                checking=True,
            )
            if describe is not None:
                describe(outcomes)
            raise _RollBack
    except _RollBack:
        pass
    for outcome in outcomes:
        # What it would make was made only to be undone.
        if outcome.kind in ("created", "registered", "added"):
            outcome.entity = None
    return outcomes


def save(
    user: User,
    project: Project,
    type_name: str,
    rows: Sequence[Row],
    *,
    reason: str,
    hold_reason: str = "",
    locale: Locale | None = None,
) -> list[Outcome]:
    """Makes every row's changes, each edit a revision with ``reason``, all of them or none.
    Raises :class:`Stale` if a record changed since the revision its row names, and
    :class:`Failed` with every row's problems if any has one."""
    if not reason.strip():
        raise Invalid({"reason": ["is required: say why these records change."]})
    if len(rows) > MAX_ROWS:
        raise Invalid({"rows": [f"has more than {MAX_ROWS} rows: save them in parts."]})
    if not rows:
        raise Invalid({"rows": ["names nothing to save."]})
    if problems := [r for r in rows if r.problems]:
        raise Failed([Outcome(r.key, "failed", problems=list(r.problems)) for r in problems])
    operation = uuid.uuid4()
    with transaction.atomic():
        ids = [r.entity for r in rows if r.entity is not None]
        locked = {
            e.pk: e
            for e in Entity.objects.select_for_update(of=("self",))
            .filter(pk__in=ids)
            .order_by("pk")
        }
        stale = [
            (r.key, locked[r.entity])
            for r in rows
            if r.entity is not None
            and r.entity in locked
            and r.revision is not None
            and locked[r.entity].revision != r.revision
        ]
        if stale:
            raise Stale(stale)
        outcomes = _apply(
            user,
            project,
            type_name,
            rows,
            reason=reason.strip(),
            hold_reason=hold_reason.strip(),
            operation=operation,
            checking=False,
        )
        if any(o.problems for o in outcomes):
            raise Failed(outcomes)
        written = [o for o in outcomes if o.entity is not None and o.kind != "unchanged"]
        if not written:
            raise Failed(
                [
                    Outcome(o.key, "failed", problems=[Problem("", "Nothing in it changes.")])
                    for o in outcomes
                ]
            )
        audit.record(
            "records.sheet_saved",
            actor=user,
            project=project,
            data={
                "type": type_name,
                "operation": str(operation),
                "reason": reason.strip(),
                "locale": (locale or Locale()).as_json(),
                "edited": [
                    {"hrid": o.entity.hrid, "revision": o.entity.revision, "fields": o.changed}
                    for o in written
                    if o.entity is not None and o.kind == "edited"
                ],
                "created": [
                    {"hrid": o.entity.hrid, "fields": o.changed}
                    for o in written
                    if o.entity is not None and o.kind in ("created", "registered", "added")
                ],
            },
        )
    return outcomes


def _apply(
    user: User,
    project: Project,
    type_name: str,
    rows: Sequence[Row],
    *,
    reason: str,
    hold_reason: str,
    operation: uuid.UUID,
    checking: bool,
) -> list[Outcome]:
    """Makes each row's changes, in the transaction it's called in, each in a savepoint of
    its own, so one row's problem leaves the others' to be found: every record's edits,
    then the new records in their rows' order (their HRIDs in it), then where they go."""
    form = sheet_type(type_name)
    fields = form.fields()
    of_type = subtypes(type_name)
    ids = [r.entity for r in rows if r.entity is not None]
    records = {
        e.pk: e
        for e in access.in_project(project)
        .filter(pk__in=ids, registry__isnull=True)
        .select_related("owner_project")
    }
    outcomes = [Outcome(r.key, "failed", problems=list(r.problems)) for r in rows]
    seen: set[uuid.UUID] = set()
    group = operation.hex
    for row, outcome in zip(rows, outcomes, strict=True):
        if row.entity is None:
            continue
        record = records.get(row.entity)
        if record is None:
            outcome.problems.append(Problem("", f"It isn't a record in {project.name}."))
            continue
        outcome.entity = record
        if record.pk in seen:
            outcome.problems.append(Problem("", f"{record.hrid} is in the sheet twice."))
            continue
        seen.add(record.pk)
        if record.type not in of_type:
            outcome.problems.append(
                Problem("", f"{record.hrid} is a {_label(record.type)}, not a {_label(type_name)}.")
            )
            continue
        if row.revision is not None and record.revision != row.revision:
            outcome.problems.append(_stale(record, row.revision))
            continue
        if not access.can_edit(user, record):
            owner = record.owner_project.name if record.owner_project else "its project"
            outcome.problems.append(
                Problem("", f"Only Editors in its owner project, {owner}, can change it.")
            )
            continue
        if not row.values:
            outcome.kind = "unchanged" if not outcome.problems else "failed"
            continue
        failed = {p.at for p in outcome.problems}
        values = {n: v for n, v in row.values.items() if f"values/{n}" not in failed}
        if not values:
            continue
        try:
            with transaction.atomic():
                saved = entities.update(
                    user,
                    record,
                    values,
                    expected_revision=record.revision if row.revision is None else row.revision,
                    reason=reason,
                    hold_reason=hold_reason,
                    group=group,
                )
        except entities.Stale as stale:
            outcome.problems.append(_stale(record, row.revision or stale.current))
            continue
        except (Invalid, Refused, PermissionDenied) as error:
            outcome.problems.extend(_problems(error, fields))
            continue
        outcome.entity = Entity.objects.get(pk=record.pk)
        outcome.held = list(saved.held or [])
        outcome.changed = list(saved.revision.changed_fields) if saved.revision is not None else []
        if not outcome.problems:
            outcome.kind = "edited" if saved.revision is not None else "unchanged"
    new = [(r, o) for r, o in zip(rows, outcomes, strict=True) if r.entity is None]
    if new and form.entity_type.abstract:
        for _, outcome in new:
            outcome.problems.append(
                Problem("", f"{_label(type_name)} is abstract: add rows of one of its kinds.")
            )
        return outcomes
    for row, outcome in new:
        if not row.values and row.location is None and row.amount is None and not row.problems:
            outcome.kind = "blank"
            continue
        failed = {p.at for p in outcome.problems}
        values = {
            n: v
            for n, v in row.values.items()
            if f"values/{n}" not in failed and v is not None and v != []
        }
        try:
            with transaction.atomic():
                saved = entities.create(
                    user,
                    type_name,
                    project,
                    values,
                    reason=reason,
                    hold_reason=hold_reason,
                    amount=row.amount if AMOUNT not in failed else None,
                    amount_reason=reason,
                )
                if saved.reused:
                    raise Refused(
                        f"{project.name} has this {_label(type_name).lower()} already, as "
                        f"{Entity.objects.get(pk=saved.entity.pk).hrid}: change its row instead."
                    )
        except (Invalid, Refused, PermissionDenied) as error:
            outcome.problems.extend(_problems(error, fields))
            continue
        outcome.entity = Entity.objects.get(pk=saved.entity.pk)
        outcome.held = list(saved.held or [])
        outcome.changed = sorted(values)
        if not outcome.problems:
            identified = registry.identity_owner(form.entity_type) is not None
            outcome.kind = (
                "registered" if saved.registered else "added" if identified else "created"
            )
    _put_away(user, rows, outcomes, reason=reason, operation=operation, checking=checking)
    return outcomes


def _put_away(
    user: User,
    rows: Sequence[Row],
    outcomes: Sequence[Outcome],
    *,
    reason: str,
    operation: uuid.UUID,
    checking: bool,
) -> None:
    """Puts each new record where its row says, all in one move. A check plans them as a
    move does, each at its position, or a container's next free ones in its order. A save
    puts each at the position its check found (ADR 0207), never at another: it holds the
    containers while it does, and raises :class:`Stale` if another placement took one."""
    from iki_server.storage import services as storage

    placing = [
        (row, outcome)
        for row, outcome in zip(rows, outcomes, strict=True)
        if row.entity is None and row.location is not None and outcome.entity is not None
    ]
    if not placing:
        return
    named = {r.location.container for r, _ in placing if r.location is not None}
    containers = {e.pk: e for e in Entity.objects.filter(pk__in=named)}
    wanted: list[storage.Wanted] = []
    for row, outcome in placing:
        assert row.location is not None and outcome.entity is not None  # noqa: S101
        to = containers.get(row.location.container)
        if to is None:
            outcome.problems.append(Problem(LOCATION, "Its container isn't one you can read."))
            continue
        wanted.append(storage.Wanted(outcome.entity, to, row.location.position))
    by_record = {o.entity.pk: o for _, o in placing if o.entity is not None}
    if not wanted:
        return
    if not checking:
        # Nothing else goes in them until this is saved, or isn't.
        storage.lock({w.to.pk for w in wanted if w.to is not None})
    planned = storage.plan(user, wanted)
    if not checking:
        taken = [
            Taken(by_record[p.record.pk].key, w.to, w.position)
            for w, p in zip(wanted, planned, strict=True)
            if p.occupied and w.to is not None and w.position is not None
        ]
        if taken:
            raise Stale([], taken)
    for w, p in zip(wanted, planned, strict=True):
        outcome = by_record[p.record.pk]
        if not checking and w.position is None and p.shape is not None and not p.problems:
            # Only a check finds the next free one: a save takes the one it found.
            assert w.to is not None  # noqa: S101 - each new record goes in a container
            outcome.problems.append(
                Problem(
                    LOCATION,
                    f"{w.to.hrid} has positions: save the one the check gave this new "
                    f"{_label(p.record.type).lower()}.",
                )
            )
            continue
        if p.problems:
            new = f"This new {_label(p.record.type).lower()}"
            outcome.problems.extend(
                Problem(LOCATION, m.replace(p.record.hrid, new)) for m in p.problems
            )
        else:
            outcome.container, outcome.position = p.to, p.position
    if checking or any(p.problems for p in planned) or any(o.problems for _, o in placing):
        return
    exact = [storage.Wanted(p.record, p.to, p.position) for p in planned]
    try:
        with transaction.atomic():
            storage.move(user, exact, reason=reason, operation=operation)
    except storage.Wrong as error:
        for where, messages in error.errors.items():
            index = where.split("/")[1] if where.startswith("items/") else None
            targets = (
                [by_record[exact[int(index)].record.pk]]
                if index is not None and index.isdigit() and int(index) < len(exact)
                else [by_record[w.record.pk] for w in exact]
            )
            for outcome in targets:
                outcome.problems.append(Problem(LOCATION, " ".join(messages)))


def _stale(record: Entity, read: int) -> Problem:
    return Problem(
        "",
        f"{record.hrid} changed since you read it at revision {read}: it's at revision "
        f"{Entity.objects.values_list('revision', flat=True).get(pk=record.pk)} now. Bring in "
        "its latest values, then check your changes again.",
        "stale",
    )


def _problems(error: Exception, fields: Mapping[str, Field]) -> list[Problem]:
    """An edit's or a create's problems, each at the cell it's about, or the row's: a
    field's as its label begins it (``Purity must be at most 100.``), a plugin's rule's
    as it says it (ADR 0127)."""
    if not isinstance(error, Invalid):
        return [Problem("", str(error))]
    out: list[Problem] = []
    told: dict[str, list[str]] = {}
    for b in getattr(error, "broken", ()):
        for name in b.fields or ("",):
            told.setdefault(name, []).append(b.message)
            at = f"values/{name}" if name in fields else ""
            held = " Saved, it's held for review: say why to save it so." if b.held else ""
            out.append(Problem(at, f"{b.message}{held}"))
    for name, messages in error.errors.items():
        rest = [m for m in messages if m not in told.get(name, [])]
        if not rest:
            continue
        head = name.split("/", 1)[0]
        said = " ".join(rest)
        if head in fields:
            out.append(Problem(f"values/{head}", f"{_field_label(fields[head], head)} {said}"))
        elif head == AMOUNT:
            out.append(Problem(AMOUNT, f"Its amount {said}"))
        else:
            out.append(Problem("", said))
    return out or [Problem("", str(error))]


def _label(type_name: str) -> str:
    found = registry.types().get(type_name)
    return (found.label or found.name) if found is not None else type_name


def _field_label(f: Field, name: str) -> str:
    return f.label or name.replace("_", " ").capitalize()
