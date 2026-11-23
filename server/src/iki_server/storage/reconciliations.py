"""Counting a container: what's scanned in each position against what's recorded (ADR 0180).

A reconciliation notes what's recorded in each of a container's positions when it starts.
Each position scanned says what's there, or that it's empty, in any order, or from a rack
scanner's file. A record is then matched, missing, unexpected (scanned here, recorded
elsewhere or nowhere) or in the wrong position of this container. Each that isn't matched
is resolved, and completing it applies every resolution as one change.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.org.services import Refused
from iki_server.storage import services as storage
from iki_server.storage.grids import NotAPosition, Shape
from iki_server.storage.models import (
    Finding,
    Place,
    ReceiptStatus,
    Reconciliation,
    ReconciliationLine,
    Resolution,
)
from iki_server.storage.services import Wrong

MAX_NOTE = 2000
#: What each finding may be resolved as.
RESOLUTIONS: dict[str, tuple[Resolution, ...]] = {
    Finding.WRONG_SLOT: (Resolution.MOVED, Resolution.KEPT),
    Finding.MISSING: (Resolution.REMOVED, Resolution.KEPT),
    Finding.UNEXPECTED: (Resolution.PLACED, Resolution.KEPT),
}
#: Not yet checked: a recorded position nobody has scanned.
UNCHECKED = "unchecked"
#: A position scanned empty.
EMPTY = "empty"
#: Checked out: not expected in its position, which is kept for it.
OUT = "checked_out"
#: An unexpected record's positive scan was replaced; its saved choice needs clearing.
NO_LONGER_FOUND = "no_longer_found"


def shape_of(reconciliation: Reconciliation) -> Shape:
    shape = storage.shapes([reconciliation.container_id]).get(reconciliation.container_id)
    assert shape is not None  # noqa: S101 - only containers with a grid are reconciled
    return shape


def finding(line: ReconciliationLine, scanned: set[tuple[int, int]]) -> str:
    """What the scan found of ``line``, given the positions scanned so far."""
    expected = (line.expected_row, line.expected_column) if line.expected_row else None
    found = (line.found_row, line.found_column) if line.found_row else None
    if line.entity_id is None and not line.code:
        return EMPTY
    if expected and found:
        return Finding.MATCHED if expected == found else Finding.WRONG_SLOT
    if found:
        return Finding.UNEXPECTED
    if not expected and line.resolution is not None:
        return NO_LONGER_FOUND
    if line.checked_out:
        return OUT
    if expected and expected in scanned:
        return Finding.MISSING
    return UNCHECKED


def scanned_positions(lines: Sequence[ReconciliationLine]) -> set[tuple[int, int]]:
    return {(x.found_row, x.found_column) for x in lines if x.found_row is not None}  # type: ignore[misc]


def findings(reconciliation: Reconciliation) -> list[tuple[ReconciliationLine, str]]:
    lines = list(reconciliation.lines.select_related("entity").order_by("id"))
    scanned = scanned_positions(lines)
    return [(x, finding(x, scanned)) for x in lines]


def start(user: User, container: Entity) -> Reconciliation:
    """The container's open reconciliation, or a new one noting what's recorded in it."""
    if not storage.keeps(user, container):
        raise PermissionError(f"Reconciling {container.hrid} needs Editor in its project.")
    with transaction.atomic():
        Entity.objects.select_for_update(no_key=True).filter(pk=container.pk).first()
        shape = (
            storage.ensure_container(container) if storage.is_container(container.type) else None
        )
        if shape is None:
            raise Wrong({"": [f"{container.hrid} has no positions to reconcile."]})
        found = Reconciliation.objects.filter(
            container=container, status=ReceiptStatus.OPEN
        ).first()
        if found is not None:
            return found
        reconciliation = Reconciliation.objects.create(container=container, started_by=user)
        places = list(Place.objects.filter(container=container, row__isnull=False))
        codes = storage.codes_of(list(Entity.objects.filter(pk__in=[p.pk for p in places])))
        ReconciliationLine.objects.bulk_create(
            ReconciliationLine(
                reconciliation=reconciliation,
                entity_id=p.pk,
                code=codes.get(p.pk, ""),
                checked_out=p.custodian_id is not None,
                expected_row=p.row,
                expected_column=p.column,
            )
            for p in places
        )
        return reconciliation


def _open(
    user: User, reconciliation: Reconciliation, *, plan_revision: int | None = None
) -> Reconciliation:
    locked = (
        Reconciliation.objects.select_for_update(of=("self",))
        .select_related("container")
        .get(pk=reconciliation.pk)
    )
    if not storage.keeps(user, locked.container):
        raise PermissionError(f"Reconciling {locked.container.hrid} needs Editor in its project.")
    if plan_revision is not None and locked.plan_revision != plan_revision:
        raise Refused("The count changed. Reload it and review its current findings and decisions.")
    if locked.status != ReceiptStatus.OPEN:
        raise Wrong({"": [f"This reconciliation is {locked.get_status_display().lower()}."]})
    return locked


def _advance_plan(reconciliation: Reconciliation) -> None:
    """A successful edit to the open, locked count needs a fresh review."""
    reconciliation.plan_revision += 1
    reconciliation.save(update_fields=["plan_revision"])


@dataclass(frozen=True)
class Scan:
    position: str
    #: What's there: a code, or None for empty.
    code: str | None


def scan(
    user: User, reconciliation: Reconciliation, scans: Sequence[Scan]
) -> list[ReconciliationLine]:
    """Records what's in each scanned position; a later scan of a position replaces an
    earlier one."""
    if not scans:
        raise Wrong({"scans": ["names no position."]})
    with transaction.atomic():
        reconciliation = _open(user, reconciliation)
        shape = shape_of(reconciliation)
        out: list[ReconciliationLine] = []
        for index, s in enumerate(scans):
            try:
                row, column = shape.parse(s.position)
            except NotAPosition as error:
                raise Wrong({f"scans/{index}/position": [str(error)]}) from None
            out.append(_scan_one(user, reconciliation, row, column, (s.code or "").strip()))
        _advance_plan(reconciliation)
        return out


def _scan_one(
    user: User, reconciliation: Reconciliation, row: int, column: int, code: str
) -> ReconciliationLine:
    lines = list(reconciliation.lines.select_related("entity"))
    now = timezone.now()
    # Whatever was scanned here before isn't, any more.
    gone: set[UUID] = set()
    for line in lines:
        if (line.found_row, line.found_column) == (row, column):
            if line.expected_row is not None or line.resolution is not None:
                line.found_row = line.found_column = None
                line.scanned_by, line.scanned_at = None, None
                line.save(update_fields=["found_row", "found_column", "scanned_by", "scanned_at"])
            else:
                gone.add(line.pk)
                line.delete()
    lines = [x for x in lines if x.pk not in gone]
    if not code:
        return ReconciliationLine.objects.create(
            reconciliation=reconciliation,
            found_row=row,
            found_column=column,
            scanned_by=user,
            scanned_at=now,
        )
    chosen = next(
        (x for x in lines if x.entity is not None and storage.confirms(x.entity, code)), None
    )
    if chosen is None:
        found = [e for e in storage.find(user, code) if storage.spec(e.type) is not None]
        known = found[0] if len(found) == 1 else None
        chosen = next((x for x in lines if known is not None and x.entity_id == known.pk), None)
        if chosen is None:
            chosen = next((x for x in lines if x.entity_id is None and x.code == code), None)
        if chosen is None:
            chosen = ReconciliationLine(reconciliation=reconciliation, entity=known, code=code)
    line = chosen
    line.found_row, line.found_column = row, column
    line.scanned_by, line.scanned_at = user, now
    # Keep the saved decision visible. Its author approved a particular observation;
    # read and complete both check whether that decision still applies.
    line.save()
    return line


def _allowed(line: ReconciliationLine, what: str) -> tuple[Resolution, ...]:
    choices = RESOLUTIONS.get(what, ())
    if choices and line.entity_id is None:
        return (Resolution.KEPT,)
    return choices


def decision_position(line: ReconciliationLine, what: str, shape: Shape) -> str | None:
    """The slot that was observed for this finding. Missing concerns its original slot."""
    if what == Finding.MISSING:
        return storage.label(shape, line.expected_row, line.expected_column)
    if what in (Finding.WRONG_SLOT, Finding.UNEXPECTED) and line.scanned_at and line.scanned_by_id:
        return storage.label(shape, line.found_row, line.found_column)
    return None


def resolution_stale(line: ReconciliationLine, what: str, position: str | None) -> bool:
    """Whether the saved action still applies to this finding and its particular slot."""
    if line.resolution is None:
        return False
    return (
        line.resolution not in _allowed(line, what)
        or position is None
        or position != line.resolved_position
        or line.resolved_at is None
        or line.resolved_by_id is None
        or (line.scanned_at is not None and line.scanned_at > line.resolved_at)
        or (line.resolution == Resolution.KEPT and not line.note.strip())
    )


def resolve(
    user: User,
    reconciliation: Reconciliation,
    line_id: UUID | str,
    resolution: str | None,
    note: str = "",
    *,
    plan_revision: int,
) -> ReconciliationLine | None:
    """Says what's done about a line that isn't matched."""
    if len(note) > MAX_NOTE:
        raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
    with transaction.atomic():
        reconciliation = _open(user, reconciliation, plan_revision=plan_revision)
        lines = list(reconciliation.lines.select_related("entity"))
        line = next((x for x in lines if str(x.pk) == str(line_id)), None)
        if line is None:
            raise Wrong({"line": ["isn't one of this reconciliation's."]})
        found = finding(line, scanned_positions(lines))
        if resolution is None and found == NO_LONGER_FOUND:
            # No recorded or observed slot remains. The operator has dismissed its old choice.
            line.delete()
            _advance_plan(reconciliation)
            return None
        position = None
        if resolution is not None:
            allowed = _allowed(line, found)
            if resolution not in allowed:
                raise Wrong(
                    {
                        "resolution": [
                            "isn't how this is resolved: "
                            + (", ".join(allowed) if allowed else "it's matched")
                            + "."
                        ]
                    }
                )
            position = decision_position(line, found, shape_of(reconciliation))
            if position is None:
                raise Wrong({"resolution": ["needs a current observation for this position."]})
            if resolution == Resolution.KEPT and not note.strip():
                raise Wrong({"note": ["is required: say why it's left as recorded."]})
        line.resolution = resolution
        line.resolved_position = position
        line.note = note.strip()
        line.resolved_by = user if resolution is not None else None
        line.resolved_at = timezone.now() if resolution is not None else None
        line.save(
            update_fields=["resolution", "resolved_position", "note", "resolved_by", "resolved_at"]
        )
        _advance_plan(reconciliation)
        return line


def complete(
    user: User,
    reconciliation: Reconciliation,
    *,
    plan_revision: int,
    missing_rest: bool = False,
    note: str = "",
) -> Reconciliation:
    """Applies every resolution as one change, refused if anything moved into or out of the
    container since it started."""
    with transaction.atomic():
        reconciliation = _open(user, reconciliation, plan_revision=plan_revision)
        container = reconciliation.container
        Entity.objects.select_for_update(no_key=True).filter(pk=container.pk).first()
        if storage.events_since(container, reconciliation.started_at):
            raise Wrong(
                {
                    "": [
                        f"Something moved into or out of {container.hrid} since this started: "
                        "start again, so what's recorded is current."
                    ]
                }
            )
        shape = shape_of(reconciliation)
        found = findings(reconciliation)
        wrong: dict[str, list[str]] = {}
        wanted: list[storage.Wanted] = []
        decisions: Counter[str] = Counter()
        assumed_missing: list[ReconciliationLine] = []
        for line, what in found:
            position = decision_position(line, what, shape)
            if resolution_stale(line, what, position):
                name = "This record"
                if line.entity is not None and access.can_read(user, line.entity):
                    name = line.entity.hrid
                elif line.entity_id is None and line.code:
                    name = line.code
                wrong[f"lines/{line.pk}"] = [
                    f"{name} is now {what}. Review the saved {line.resolution} decision "
                    "against the current scan."
                ]
                continue
            if what == UNCHECKED:
                if not missing_rest:
                    wrong[f"lines/{line.pk}"] = ["isn't scanned yet: scan its position."]
                    continue
                assumed_missing.append(line)
                line.resolution, line.resolved_by = Resolution.REMOVED, user
                line.resolved_at = timezone.now()
                line.resolved_position = storage.label(
                    shape, line.expected_row, line.expected_column
                )
            elif what in (Finding.MATCHED, EMPTY, OUT):
                continue
            if line.resolution is None:
                wrong[f"lines/{line.pk}"] = ["needs resolving."]
                continue
            decisions[line.resolution] += 1
            if line.resolution in (Resolution.MOVED, Resolution.PLACED):
                # These decisions must have a positive scan of this record at a concrete slot.
                # Never pass a null position to the storage planner, which allocates free slots.
                assert line.entity is not None and position is not None  # noqa: S101
                wanted.append(
                    storage.Wanted(
                        line.entity, container, position, evidence=f"Scanned at {position}."
                    )
                )
            elif line.resolution == Resolution.REMOVED:
                assert line.entity is not None  # noqa: S101
                slot = line.resolved_position
                evidence = (
                    f"{slot} was not scanned. Treated as missing and removed from storage."
                    if what == UNCHECKED
                    else f"The scan did not find this record at {slot}. Removed from storage."
                )
                wanted.append(storage.Wanted(line.entity, None, evidence=evidence))
        if wrong:
            raise Wrong(wrong)
        if assumed_missing:
            ReconciliationLine.objects.bulk_update(
                assumed_missing, ["resolution", "resolved_by", "resolved_at", "resolved_position"]
            )
        moved = 0
        if wanted:
            try:
                events = storage.move(
                    user,
                    wanted,
                    reason=f"Reconciled {container.hrid}.",
                    operation=reconciliation.operation,
                )
                moved = len(events)
            except Wrong as error:
                raise Wrong({"": [m for ms in error.errors.values() for m in ms]}) from None
        reconciliation.status = ReceiptStatus.COMPLETED
        reconciliation.completed_by = user
        reconciliation.completed_at = timezone.now()
        reconciliation.note = note.strip()[:MAX_NOTE]
        reconciliation.save(update_fields=["status", "completed_by", "completed_at", "note"])
        counts = Counter(what for _, what in found if what not in (EMPTY, OUT))
        log.record(
            "storage.reconciled",
            actor=user,
            target=container,
            project=container.owner_project,
            data={
                "reconciliation": str(reconciliation.pk),
                "plan_revision": reconciliation.plan_revision,
                "findings": {k: counts.get(k, 0) for k in [*Finding.values, UNCHECKED]},
                "resolutions": {choice: decisions[choice] for choice in Resolution.values},
                "assumed_missing": len(assumed_missing),
                "moved": moved,
                "operation": str(reconciliation.operation),
            },
        )
        return reconciliation


def cancel(user: User, reconciliation: Reconciliation) -> Reconciliation:
    with transaction.atomic():
        reconciliation = _open(user, reconciliation)
        reconciliation.status = ReceiptStatus.CANCELLED
        reconciliation.completed_at = timezone.now()
        reconciliation.save(update_fields=["status", "completed_at"])
        return reconciliation
