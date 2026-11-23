"""Counting a container: what's scanned in each position against what's recorded (ADR 0180).

A reconciliation notes what's recorded in each of a container's positions when it starts.
Each position scanned says what's there, or that it's empty, in any order, or from a rack
scanner's file. A record is then matched, missing, unexpected (scanned here, recorded
elsewhere or nowhere) or in the wrong position of this container. Each that isn't matched
is resolved, and completing it applies every resolution as one change.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.org.services import Refused
from iki_server.storage import observations, scan_identity
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
    lines = list(
        reconciliation.lines.select_related(
            "entity", "current_observation__actor", "reviewed_observation__actor", "resolved_by"
        ).order_by("id")
    )
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
    # Refresh the caller's instance under the lock. A mutation can serialize it under
    # that same transaction without reading a later revision back from the database.
    reconciliation.refresh_from_db(
        from_queryset=Reconciliation.objects.select_for_update(of=("self",)).select_related(
            "container", "started_by", "completed_by", "cancelled_by"
        )
    )
    if not storage.keeps(user, reconciliation.container):
        raise PermissionError(
            f"Reconciling {reconciliation.container.hrid} needs Editor in its project."
        )
    if plan_revision is not None and reconciliation.plan_revision != plan_revision:
        raise Refused("The count changed. Reload it and review its current findings and decisions.")
    if reconciliation.status != ReceiptStatus.OPEN:
        raise Wrong(
            {"": [f"This reconciliation is {reconciliation.get_status_display().lower()}."]}
        )
    return reconciliation


def _advance_plan(reconciliation: Reconciliation) -> None:
    """A successful edit to the open, locked count needs a fresh review."""
    reconciliation.plan_revision += 1
    reconciliation.save(update_fields=["plan_revision"])


@dataclass(frozen=True)
class Scan:
    position: str
    #: What's there: a code, or None for empty.
    code: str | None
    mode: scan_identity.ScanMode = "label"
    choice: UUID | None = None
    #: A new unlinked tube, or the UUID of the specific prior observation to move.
    unregistered: Literal["additional"] | UUID | None = None


def _reason(value: str) -> str:
    value = value.strip()
    if not value:
        raise Wrong(
            {"reason": ["Say why the previous count observation or decision is being changed."]}
        )
    if len(value) > MAX_NOTE:
        raise Wrong({"reason": [f"is longer than {MAX_NOTE} characters."]})
    return value


class LinesChanged(Refused):
    def __init__(self, lines: Sequence[ReconciliationLine]) -> None:
        self.lines = [
            {"line_id": str(line.pk), "line_revision": line.line_revision} for line in lines
        ]
        super().__init__("The count evidence changed. Review the affected lines before saving.")


class UnregisteredChoiceRequired(Refused):
    def __init__(
        self,
        index: int,
        slot: tuple[int, int],
        others: dict[tuple[int, int], ReconciliationLine],
        shape: Shape,
    ) -> None:
        self.index = index
        self.position = shape.label(*slot)
        self.observations = [
            {
                "line_id": str(line.pk),
                "line_revision": line.line_revision,
                "position": shape.label(*at),
            }
            for at, line in sorted(others.items(), key=lambda pair: shape.index(*pair[0]))
        ]
        super().__init__(
            "Is this an additional unregistered tube, or did a previously observed tube move?"
        )


@dataclass(frozen=True)
class PlannedScan:
    row: int
    column: int
    code: str
    line: ReconciliationLine
    identity_basis: scan_identity.IdentityBasis | None
    unregistered: Literal["additional"] | UUID | None


def _registered_line(
    count: Reconciliation,
    lines: list[ReconciliationLine],
    links: dict[UUID, Entity],
    occupied: ReconciliationLine | None,
    code: str,
    entity: Entity,
) -> ReconciliationLine:
    for line in lines:
        if line.entity_id == entity.pk or links.get(line.pk) == entity:
            return line
    # A registration can identify a prior unlinked observation at this same slot.
    if (
        occupied
        and occupied.entity_id is None
        and occupied.pk not in links
        and occupied.code == code
    ):
        links[occupied.pk] = entity
        return occupied
    return ReconciliationLine(reconciliation=count, entity=entity, code=code)


def _unregistered_line(
    count: Reconciliation,
    item: Scan,
    slot: tuple[int, int],
    occupied: ReconciliationLine | None,
    prior: dict[tuple[int, int], ReconciliationLine],
    together: set[tuple[int, int]],
    shape: Shape,
    index: int,
) -> ReconciliationLine:
    code = (item.code or "").strip()
    # The operator cannot move an observation that this rack still reads in its old slot.
    sources = {at: line for at, line in prior.items() if at != slot and at not in together}
    if isinstance(item.unregistered, UUID):
        for line in sources.values():
            if line.pk == item.unregistered:
                return line
        raise UnregisteredChoiceRequired(index, slot, sources, shape)
    if occupied and occupied.entity_id is None and occupied.code == code:
        if item.unregistered is not None:
            raise Wrong(
                {
                    f"scans/{index}/unregistered": [
                        "This slot already has that observation. Review it before rescanning."
                    ]
                }
            )
        return occupied
    if item.unregistered is None and sources:
        raise UnregisteredChoiceRequired(index, slot, sources, shape)
    if (
        item.unregistered is None
        and code
        and occupied
        and occupied.entity_id is None
        and occupied.code
    ):
        # A new reading of an unregistered tube at this slot corrects its own evidence.
        return occupied
    return ReconciliationLine(reconciliation=count, code=code)


def scan(
    user: User,
    reconciliation: Reconciliation,
    scans: Sequence[Scan],
    *,
    reviewed_lines: Sequence[tuple[UUID, int]] = (),
    reason: str = "",
) -> list[ReconciliationLine]:
    """Review only prior physical observations this command would replace."""
    if not scans:
        raise Wrong({"scans": ["names no position."]})
    with transaction.atomic():
        reconciliation = _open(user, reconciliation)
        shape = shape_of(reconciliation)
        lines = list(reconciliation.lines.select_related("current_observation"))
        entity_ids = {line.entity_id for line in lines if line.entity_id is not None}
        identities = scan_identity.ScanIndex(
            user, [s.code or "" for s in scans], scope_ids=entity_ids
        )
        concealed = entity_ids - identities.readable_ids
        expected = {
            (line.expected_row, line.expected_column): line
            for line in lines
            if line.expected_row is not None and line.expected_column is not None
        }
        slots = observed_slots(lines)
        prior: dict[str, dict[tuple[int, int], ReconciliationLine]] = defaultdict(dict)
        for slot, observed in slots.items():
            if observed.entity_id is None and observed.code:
                prior[observed.code][slot] = observed
        readings = []
        together: dict[str, set[tuple[int, int]]] = defaultdict(set)
        outgoing = set()
        for index, item in enumerate(scans):
            try:
                slot = shape.parse(item.position)
            except NotAPosition as error:
                raise Wrong({f"scans/{index}/position": [str(error)]}) from None
            readings.append((item, slot))
            # Two simultaneously occupied positions are independent evidence. A prior
            # line reread at its own slot in this file cannot also have moved elsewhere.
            if item.unregistered is None:
                together[(item.code or "").strip()].add(slot)
            elif isinstance(item.unregistered, UUID):
                outgoing.add(item.unregistered)
        # A missing finding depends on the actual observation at the expected slot.
        old_basis = _finding_basis(lines)
        required: dict[UUID, ReconciliationLine] = {}
        selected: dict[UUID, tuple[int, int]] = {}
        positions: dict[tuple[int, int], PlannedScan] = {}
        planned: list[PlannedScan] = []
        links: dict[UUID, Entity] = {}
        correction = False
        for index, (item, slot) in enumerate(readings):
            raw_code = item.code or ""
            code = raw_code.strip()
            occupied = slots.get(slot)
            reserved = expected.get(slot)
            # A checked-out tube reserves its old slot, but isn't expected in the box.
            occupant = (
                reserved.entity_id if reserved is not None and not reserved.checked_out else None
            )
            protected = any(
                x is not None and x.entity_id in concealed for x in (reserved, occupied)
            )
            match = identities.resolve(
                code,
                mode=item.mode,
                choice=item.choice,
                occupant=occupant,
                protected=protected,
                has_concealed=bool(concealed),
            )
            if match.status == "ambiguous":
                raise identities.ambiguity(match, index=index)
            prior_reading = positions.get(slot)
            at = prior_reading.line if prior_reading is not None else occupied
            if prior_reading is None and at is not None and at.pk in outgoing:
                # A named move is taking that line out; a reading here is of its replacement.
                at = None
            if item.unregistered is not None and (not code or match.record is not None):
                raise Wrong(
                    {
                        f"scans/{index}/unregistered": [
                            "Only applies to an unregistered label. Review the scan again."
                        ]
                    }
                )
            if match.record is not None:
                line = _registered_line(reconciliation, lines, links, at, code, match.record)
            else:
                line = _unregistered_line(
                    reconciliation, item, slot, at, prior[code], together[code], shape, index
                )
            previous_slot = selected.get(line.pk)
            if previous_slot is not None and previous_slot != slot:
                raise Wrong(
                    {
                        f"scans/{index}/code": [
                            f"The same tube was submitted at {shape.label(*previous_slot)} "
                            f"and {shape.label(*slot)}."
                        ]
                    }
                )
            if prior_reading is not None and (
                prior_reading.line is not line
                or (match.record is None and prior_reading.code.strip() != code)
            ):
                raise Wrong(
                    {
                        f"scans/{index}/position": [
                            f"Conflicting observations for {shape.label(*slot)} in one scan file."
                        ]
                    }
                )
            if line._state.adding and previous_slot is None:
                lines.append(line)
            if occupied is not None:
                required[occupied.pk] = occupied
                correction |= occupied is not line or (
                    match.record is None and occupied.code != code
                )
            if line.found_row is not None:
                required[line.pk] = line
                correction |= occupied is not line
            if line.pk in links and not line._state.adding:
                required[line.pk] = line
                correction = True
            selected[line.pk] = slot
            entry = PlannedScan(
                *slot, raw_code if code else "", line, match.basis, item.unregistered
            )
            positions[slot] = entry
            planned.append(entry)
        reviewed = dict(reviewed_lines)
        if len(reviewed) != len(reviewed_lines):
            raise Wrong({"reviewed_lines": ["names the same reviewed line more than once."]})
        if reviewed != {pk: line.line_revision for pk, line in required.items()}:
            raise LinesChanged(list(required.values()))
        if correction:
            reason = _reason(reason)
        revision, operation = reconciliation.plan_revision + 1, uuid4()
        # Vacate the affected positions first. This preserves both identities in a swap
        # and respects the unique slot constraint regardless of the rack file's order.
        reconciliation.lines.filter(pk__in=required).update(found_row=None, found_column=None)
        for line in required.values():
            if line.pk in selected:
                continue
            before = _slot_snapshot(line, shape)
            remove = line.expected_row is None and line.resolution is None
            line.found_row = line.found_column = None
            line.scanned_at = line.scanned_by = line.current_observation = None
            line.line_revision += 1
            observations.record(
                reconciliation,
                line_id=line.pk,
                kind="scan",
                before=before,
                after=None if remove else _slot_snapshot(line, shape),
                user=user,
                reason=reason.strip(),
                revision=revision,
                operation=operation,
            )
            if remove:
                lines.remove(line)
                line.delete()
            else:
                line.save()
        for entry in planned:
            line = entry.line
            previous = None if line._state.adding else _slot_snapshot(line, shape)
            if not line._state.adding:
                line.line_revision += 1
            if line.pk in links:
                line.entity = links[line.pk]
            if line.entity_id is None:
                line.code = entry.code.strip()
            line.found_row, line.found_column = entry.row, entry.column
            line.scanned_by, line.scanned_at = user, timezone.now()
            after = _slot_snapshot(line, shape)
            after.pop("input_unknown", None)
            after.pop("unregistered_action", None)
            if entry.unregistered is not None:
                after["unregistered_action"] = (
                    "additional" if entry.unregistered == "additional" else "move"
                )
            after["code"] = entry.code or None
            after["identity_basis"] = entry.identity_basis
            line.current_observation = observations.record(
                reconciliation,
                line_id=line.pk,
                kind="scan",
                before=previous,
                after=after,
                user=user,
                reason=reason.strip(),
                revision=revision,
                operation=operation,
            )
            line.save()
        changes = _finding_basis(lines)
        dependent = [
            pk
            for pk, basis in changes.items()
            if pk not in selected and pk not in required and basis != old_basis.get(pk)
        ]
        if dependent:
            reconciliation.lines.filter(pk__in=dependent).update(
                line_revision=F("line_revision") + 1
            )
        _advance_plan(reconciliation)
        return [item.line for item in planned]


def _finding_basis(lines: Sequence[ReconciliationLine]) -> dict[UUID, tuple[str, UUID | None]]:
    slots = observed_slots(lines)
    scanned = set(slots)
    result = {}
    for line in lines:
        what = finding(line, scanned)
        result[line.pk] = (what, evidence_id(line, what, slots))
    return result


def _slot_snapshot(line: ReconciliationLine, shape: Shape) -> dict[str, Any]:
    # An expected barcode may differ from what the scanner actually sent. In older counts
    # only an unexpected unknown code and an explicitly empty slot identify the input.
    state: dict[str, Any] = {
        "line_revision": line.line_revision,
        "expected": storage.label(shape, line.expected_row, line.expected_column),
        "position": storage.label(shape, line.found_row, line.found_column),
        "observed": line.found_row is not None,
    }
    if line.entity_id:
        state["entity_id"] = str(line.entity_id)
    if line.found_row is not None:
        state["scanned_by"] = str(line.scanned_by_id) if line.scanned_by_id else None
        state["scanned_at"] = line.scanned_at.isoformat() if line.scanned_at else None
        current = line.current_observation
        if current is not None and current.after is not None:
            state["code"] = current.after["code"]
            for key in ("identity_basis", "unregistered_action"):
                if key in current.after:
                    state[key] = current.after[key]
        elif line.entity_id is None:
            state["code"] = line.code or None
        else:
            state["input_unknown"] = True
    return state


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


def observed_slots(
    lines: Sequence[ReconciliationLine],
) -> dict[tuple[int, int], ReconciliationLine]:
    return {
        (x.found_row, x.found_column): x
        for x in lines
        if x.found_row is not None and x.found_column is not None
    }


def evidence_line(
    line: ReconciliationLine, what: str, slots: dict[tuple[int, int], ReconciliationLine]
) -> ReconciliationLine | None:
    if (
        what == Finding.MISSING
        and line.expected_row is not None
        and line.expected_column is not None
    ):
        return slots.get((line.expected_row, line.expected_column))
    return line


def evidence_id(
    line: ReconciliationLine, what: str, slots: dict[tuple[int, int], ReconciliationLine]
) -> UUID | None:
    observed = evidence_line(line, what, slots)
    return observed.current_observation_id if observed is not None else None


def resolution_stale(
    line: ReconciliationLine, what: str, position: str | None, evidence: UUID | None
) -> bool:
    """Whether the saved action still applies to this finding and its particular slot."""
    if line.resolution is None:
        return False
    return (
        line.resolution not in _allowed(line, what)
        or position is None
        or line.resolved_finding != what
        or position != line.resolved_position
        or line.resolved_at is None
        or line.resolved_by_id is None
        or evidence is None
        or evidence != line.reviewed_observation_id
        or (line.resolution == Resolution.KEPT and not line.note.strip())
    )


def _decision_snapshot(line: ReconciliationLine) -> dict[str, Any]:
    state: dict[str, Any] = {
        "line_revision": line.line_revision,
        "finding": line.resolved_finding,
        "position": line.resolved_position,
        "resolution": line.resolution,
        "note": line.note,
        "resolved_by": str(line.resolved_by_id) if line.resolved_by_id else None,
        "resolved_at": line.resolved_at.isoformat() if line.resolved_at else None,
        "observation": str(line.reviewed_observation_id) if line.reviewed_observation_id else None,
    }
    if line.entity_id:
        state["entity_id"] = str(line.entity_id)
    return state


def resolve(
    user: User,
    reconciliation: Reconciliation,
    line_id: UUID | str,
    resolution: str | None,
    note: str = "",
    *,
    line_revision: int,
    reason: str = "",
) -> ReconciliationLine | None:
    """Applies the decision to the physical observation the operator reviewed."""
    note = note.strip()
    if len(note) > MAX_NOTE:
        raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
    with transaction.atomic():
        reconciliation = _open(user, reconciliation)
        lines = list(reconciliation.lines.select_related("entity"))
        line = next((x for x in lines if str(x.pk) == str(line_id)), None)
        if line is None:
            raise LinesChanged([])
        if line.line_revision != line_revision:
            raise LinesChanged([line])
        found = finding(line, scanned_positions(lines))
        position = None
        evidence = None
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
            evidence = evidence_id(line, found, observed_slots(lines))
            if position is None or evidence is None:
                raise Wrong(
                    {
                        "resolution": [
                            "Rescan this position to record the evidence for this decision."
                        ]
                    }
                )
            if resolution == Resolution.KEPT and not note:
                raise Wrong({"note": ["is required: say why it's left as recorded."]})
        elif note:
            raise Wrong({"note": ["Use a correction reason when clearing a decision."]})
        if (
            line.resolution == resolution
            and line.note == note
            and not resolution_stale(line, found, position, evidence)
        ):
            return line
        before = _decision_snapshot(line) if line.resolution or line.note else None
        if before:
            reason = _reason(reason)
        line.line_revision += 1
        line.resolved_finding = found if resolution else None
        line.resolution = resolution
        line.resolved_position = position
        line.note = note
        line.resolved_by = user if resolution is not None else None
        line.resolved_at = timezone.now() if resolution is not None else None
        line.reviewed_observation_id = evidence
        observations.record(
            reconciliation,
            line_id=line.pk,
            kind="resolution",
            before=before,
            after=_decision_snapshot(line) if resolution is not None else None,
            user=user,
            reason=reason.strip(),
            revision=reconciliation.plan_revision + 1,
            operation=uuid4(),
        )
        _advance_plan(reconciliation)
        if resolution is None and found == NO_LONGER_FOUND:
            line.delete()
            return None
        line.save(
            update_fields=[
                "line_revision",
                "resolution",
                "resolved_position",
                "resolved_finding",
                "note",
                "resolved_by",
                "resolved_at",
                "reviewed_observation",
            ]
        )
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
        # Starting a count holds the container while returning its open count. Take
        # these two locks in the same order when applying its storage movements.
        Entity.objects.select_for_update(no_key=True).filter(pk=reconciliation.container_id).first()
        reconciliation = _open(user, reconciliation, plan_revision=plan_revision)
        container = reconciliation.container
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
        slots = observed_slots([line for line, _ in found])
        wrong: dict[str, list[str]] = {}
        wanted: list[storage.Wanted] = []
        decisions: Counter[str] = Counter()
        assumed_missing: list[ReconciliationLine] = []
        for line, what in found:
            position = decision_position(line, what, shape)
            if resolution_stale(line, what, position, evidence_id(line, what, slots)):
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
                line.line_revision += 1
                line.resolved_finding = UNCHECKED
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
            for line in assumed_missing:
                observations.record(
                    reconciliation,
                    line_id=line.pk,
                    kind="resolution",
                    before=None,
                    after={**_decision_snapshot(line), "assumed_missing": True},
                    user=user,
                    reason="",
                    revision=plan_revision,
                    operation=reconciliation.operation,
                )
            ReconciliationLine.objects.bulk_update(
                assumed_missing,
                [
                    "resolution",
                    "resolved_by",
                    "resolved_at",
                    "resolved_position",
                    "resolved_finding",
                    "line_revision",
                ],
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
        observations.record(
            reconciliation,
            line_id=None,
            kind="status",
            before={"status": ReceiptStatus.OPEN},
            after={"status": ReceiptStatus.COMPLETED, "note": reconciliation.note},
            user=user,
            reason="",
            revision=plan_revision,
            operation=reconciliation.operation,
        )
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


def cancel(
    user: User, reconciliation: Reconciliation, *, plan_revision: int, reason: str
) -> Reconciliation:
    with transaction.atomic():
        reconciliation = _open(user, reconciliation, plan_revision=plan_revision)
        reason = _reason(reason)
        reconciliation.status = ReceiptStatus.CANCELLED
        reconciliation.cancelled_by = user
        reconciliation.cancel_reason = reason
        reconciliation.completed_at = timezone.now()
        _advance_plan(reconciliation)
        reconciliation.save(
            update_fields=["status", "completed_at", "cancelled_by", "cancel_reason"]
        )
        observations.record(
            reconciliation,
            line_id=None,
            kind="status",
            before={"status": ReceiptStatus.OPEN},
            after={
                "status": ReceiptStatus.CANCELLED,
                "cancelled_by": str(user.pk),
                "cancelled_at": reconciliation.completed_at.isoformat(),
            },
            user=user,
            reason=reason,
            revision=reconciliation.plan_revision,
            operation=uuid4(),
        )
        return reconciliation
