"""Receiving a request's material against what it expected (ADR 0180).

A request type names the references it sends that its service receives (``request:
receives``). A receiver in the service starts a receipt, whose lines are what's expected
that no earlier receipt received, and scans or ticks each as matched, missing, damaged or
rejected; a code that isn't expected is an extra line. Completing it takes custody of what
was received, puts it away, freezes the receipt, and tells the requester of what didn't
arrive as expected.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.core.ids import uuid7
from iki_server.entities import access, requests
from iki_server.entities.models import Entity
from iki_server.notifications import kinds
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.storage import observations, scan_identity
from iki_server.storage import services as storage
from iki_server.storage.models import Outcome, Place, Receipt, ReceiptLine, ReceiptStatus
from iki_server.storage.services import Wrong

#: What's received: in the receiver's custody, and put away if a position's given.
RECEIVED = (Outcome.MATCHED, Outcome.DAMAGED)
#: What's reported back to the requester.
DISCREPANCIES = (Outcome.MISSING, Outcome.EXTRA, Outcome.DAMAGED, Outcome.REJECTED)
#: What needs a note saying what was wrong.
NOTED = (Outcome.DAMAGED, Outcome.REJECTED, Outcome.EXTRA)
MAX_NOTE = 2000
MAX_SCAN_CODE = 200

DISCREPANCY = kinds.register(
    kinds.Kind(
        "storage.receipt_discrepancy",
        "What you sent didn't all arrive as expected",
        "A service receiving a request you made, or watch, found something missing, extra, "
        "damaged or rejected (ADR 0180).",
        group="Requests",
    )
)


def receives(entity: Entity) -> tuple[str, tuple[str, ...]] | None:
    """The field a request type's service receives, and its lab roles that receive it."""
    spec = requests.spec_of(entity.type)
    if spec is None or spec.receives is None:
        return None
    return spec.receives, spec.receives_by


def why_not_receiver(user: User, entity: Entity) -> str | None:
    """Why ``user`` may not receive ``entity``'s material: it isn't sent to a service they
    receive for. None if they may."""
    found = receives(entity)
    if found is None:
        return f"{entity.hrid} is a {entity.type}, whose material isn't received."
    request = requests.of(entity)
    if request is None:
        return f"{entity.hrid} isn't sent to a service yet."
    _, roles = found
    if not permissions.can(user, Action.PROJECT_EDIT, request.service):
        return f"Receiving {entity.hrid} needs Editor in {request.service.name}."
    if roles and permissions.lab_roles(user, request.service).isdisjoint(roles):
        named = " or ".join(lab_roles.label(r) for r in roles)
        return f"Receiving {entity.hrid} needs the {named} role in {request.service.name}."
    return None


def awaiting_put_away(user: User) -> QuerySet[Place]:
    """Accepted material the reader can see that is still held without a location."""
    # A later receipt can take custody of the same record again, even to reject it.
    last_received = ReceiptLine.objects.filter(
        entity_id=OuterRef("pk"),
        receipt__status=ReceiptStatus.COMPLETED,
        removed=False,
        outcome__in=(*RECEIVED, Outcome.REJECTED),
    ).order_by("-receipt__completed_at", "-pk")
    return (
        Place.objects.filter(
            entity__in=access.visible(user).values("pk"),
            container__isnull=True,
            custodian__isnull=False,
        )
        .alias(outcome=Subquery(last_received.values("outcome")[:1]))
        .filter(outcome__in=RECEIVED)
        .select_related("custodian")
        .order_by("custody_since", "pk")
    )


def _expected(entity: Entity) -> list[Entity]:
    """What the request expects that no completed receipt received."""
    from iki_server.entities.services import current_values_of

    found = receives(entity)
    assert found is not None  # noqa: S101
    name, _ = found
    value = current_values_of([entity])[entity.pk].get(name)
    ids = [str(v) for v in (value if isinstance(value, list) else [value]) if v]
    received = set(
        ReceiptLine.objects.filter(
            receipt__request=entity,
            receipt__status=ReceiptStatus.COMPLETED,
            outcome__in=[*RECEIVED, Outcome.REJECTED],
        ).values_list("entity_id", flat=True)
    )
    by_id = {str(e.pk): e for e in Entity.objects.filter(pk__in=ids)}
    out: list[Entity] = []
    for i in ids:
        e = by_id.get(i)
        if e is not None and e.pk not in received and e not in out:
            out.append(e)
    return out


def start(user: User, entity: Entity) -> Receipt:
    """A receipt for ``entity``'s material: the one open already, or a new one, whose lines
    are what it expects that no earlier receipt received."""
    refused = why_not_receiver(user, entity)
    if refused is not None:
        raise PermissionError(refused)
    request = requests.of(entity)
    assert request is not None  # noqa: S101
    with transaction.atomic():
        Entity.objects.select_for_update(no_key=True).filter(pk=entity.pk).first()
        open_now = (
            Receipt.objects.select_for_update(of=("self",))
            .filter(request=entity, status=ReceiptStatus.OPEN)
            .first()
        )
        if open_now is not None:
            return open_now
        expected = _expected(entity)
        if not expected:
            raise Wrong({"": [f"Everything {entity.hrid} expects has been received."]})
        receipt = Receipt.objects.create(request=entity, project=request.service, started_by=user)
        codes = storage.codes_of(expected)
        ReceiptLine.objects.bulk_create(
            ReceiptLine(receipt=receipt, entity=e, code=codes[e.pk], expected=True)
            for e in expected
        )
        log.record("request.receipt_started", actor=user, target=receipt, project=request.service)
        return receipt


class LinesChanged(Exception):  # noqa: N818 - reads as "the lines changed"
    """The receipt or a line changed since the revision a change named (ADR 0289):
    ``current`` is the revision of what the change addressed, and ``lines`` the lines that
    changed, each with its revision now."""

    def __init__(self, message: str, lines: Iterable[tuple[UUID, int]], *, current: int) -> None:
        super().__init__(message)
        self.current = current
        self.lines = [{"line_id": str(pk), "revision": rev} for pk, rev in lines]


class RevisionRequired(Exception):  # noqa: N818 - reads as "a revision is required"
    """A change replaces what someone recorded without naming the revision it replaces."""


def _open(user: User, receipt: Receipt, plan_revision: int | None = None) -> Receipt:
    locked = (
        Receipt.objects.select_for_update(of=("self",)).select_related("request").get(pk=receipt.pk)
    )
    refused = why_not_receiver(user, locked.request)
    if refused is not None:
        raise PermissionError(refused)
    if locked.status != ReceiptStatus.OPEN:
        raise Wrong({"": [f"This receipt is {locked.get_status_display().lower()}."]})
    if plan_revision is not None and locked.plan_revision != plan_revision:
        # Handoff events have no line. A removed extra retains its line and revision.
        changed = locked.observations.filter(revision__gt=plan_revision, line_id__isnull=False)
        versions = (
            locked.lines.filter(pk__in=changed.values("line_id"))
            .order_by("pk")
            .values_list("pk", "line_revision")
        )
        raise LinesChanged(
            "The receipt changed. Reload it and review its current observations.",
            versions,
            current=locked.plan_revision,
        )
    return locked


def _advance(receipt: Receipt) -> None:
    receipt.plan_revision += 1
    receipt.save(update_fields=["plan_revision"])


def _reason(text: str, *, required: bool) -> str:
    if len(text) > MAX_NOTE:
        raise Wrong({"reason": [f"is longer than {MAX_NOTE} characters."]})
    if required and not text.strip():
        raise Wrong({"reason": ["is required: say why this saved observation is changing."]})
    return text.strip()


def _line_state(line: ReceiptLine) -> dict[str, Any]:
    return {
        **({"entity_id": str(line.entity_id)} if line.entity_id else {}),
        "code": line.code,
        "revision": line.line_revision,
        "expected": line.expected,
        "outcome": line.outcome,
        "note": line.note,
        "removed": line.removed,
        "checked_by": str(line.checked_by_id) if line.checked_by_id else None,
        "checked_at": line.checked_at.isoformat() if line.checked_at else None,
    }


def _record_line(
    user: User,
    receipt: Receipt,
    line: ReceiptLine,
    before: dict[str, Any] | None,
    reason: str,
    operation: UUID,
    *,
    scan: dict[str, str | bool] | None = None,
) -> None:
    after = _line_state(line)
    if scan is not None:
        after["scan"] = scan
    if before is not None and before["outcome"] is not None:
        # Evidence belongs to the original observation. Its amendment shows it in
        # "before"; the new manual observation makes no claim to have scanned it.
        earlier_scan = (
            receipt.observations.filter(line_id=line.pk, kind="outcome")
            .order_by("-revision", "-id")
            .values_list("after__scan", flat=True)
            .first()
        )
        if earlier_scan is not None:
            before["scan"] = earlier_scan
    observations.record(
        receipt,
        line_id=line.pk,
        kind="outcome",
        before=before,
        after=after,
        user=user,
        reason=reason,
        revision=receipt.plan_revision,
        operation=operation,
    )


def _checked(
    user: User,
    receipt: Receipt,
    line: ReceiptLine,
    outcome: str | None,
    note: str,
    reason: str,
    operation: UUID,
    *,
    scan: dict[str, str | bool] | None = None,
) -> None:
    before = None if line._state.adding else _line_state(line)
    # Editing the note alone does not change who observed this outcome. The history
    # entry names its own author independently.
    if before is None or line.outcome != outcome:
        line.checked_by = user if outcome is not None else None
        line.checked_at = timezone.now() if outcome is not None else None
    line.outcome, line.note = outcome, note
    if before is not None:
        line.line_revision += 1
    line.save()
    _record_line(user, receipt, line, before, reason, operation, scan=scan)


@dataclass(frozen=True)
class CheckedLine:
    """One receipt line and the parent revision returned to the operator."""

    line: ReceiptLine
    found: str
    receipt: Receipt


def scan(
    user: User,
    receipt: Receipt,
    code: str,
    *,
    mode: scan_identity.ScanMode = "label",
    choice: UUID | None = None,
) -> CheckedLine:
    """Tick an identified line or record an unexpected label during receiving."""
    if len(code) > MAX_SCAN_CODE:
        raise Wrong({"code": [f"is longer than {MAX_SCAN_CODE} characters."]})
    text = code.strip()
    if not text:
        raise Wrong({"code": ["is empty: scan a tube's code."]})
    with transaction.atomic():
        receipt = _open(user, receipt)
        lines = list(receipt.lines.order_by("id"))
        linked = {line.entity_id: line for line in lines if line.entity_id is not None}
        index = scan_identity.ScanIndex(user, [text], scope_ids=linked.keys())
        if scan_identity.is_explicit(text, mode):
            match = index.resolve(text, mode=mode, choice=choice)
        else:
            match = index.resolve(
                text,
                mode=mode,
                choice=choice,
                candidates=linked.keys(),
                protected=bool(linked.keys() - index.readable_ids),
            )
        if match.status == "ambiguous":
            raise index.ambiguity(match)
        line = linked.get(match.record.pk) if match.record is not None else None
        if line is None and match.status == "none":
            line = next(
                (item for item in lines if item.entity_id is None and item.code == text), None
            )
        scope = "receipt" if line is not None else "inventory"
        if line is not None:
            if line.removed:
                return CheckedLine(line, "removed", receipt)
            if line.outcome is not None:
                return CheckedLine(line, "again", receipt)
            found = "matched" if line.expected else "extra"
        else:
            if match.status == "none":
                match = index.resolve(text, mode=mode, choice=choice)
            found = "ambiguous_extra" if match.status == "ambiguous" else "extra"
            line = ReceiptLine(receipt=receipt, entity=match.record, code=text, expected=False)
        _advance(receipt)
        _checked(
            user,
            receipt,
            line,
            Outcome.MATCHED if line.expected else Outcome.EXTRA,
            line.note,
            "",
            uuid7(),
            scan={
                "code": code,
                "identity_basis": index.evidence_basis(code, mode, match),
                "scope": scope,
                "chosen": choice is not None,
            },
        )
        return CheckedLine(line, found, receipt)


def _line(receipt: Receipt, line_id: UUID | str) -> ReceiptLine:
    line = receipt.lines.filter(pk=line_id).first()
    if line is None:
        raise Wrong({"line": ["isn't one of this receipt's."]})
    return line


def _reviewed_line(line: ReceiptLine, line_revision: int | None) -> None:
    if line_revision is None:
        raise RevisionRequired(
            "Send the line's ETag as If-Match: review it before correcting what was recorded."
        )
    if line_revision != line.line_revision:
        raise LinesChanged(
            "This receipt line changed. Reload it and review its current observation.",
            [(line.pk, line.line_revision)],
            current=line.line_revision,
        )


def check(
    user: User,
    receipt: Receipt,
    line_id: UUID | str,
    outcome: str | None,
    note: str | None = None,
    *,
    line_revision: int | None = None,
    reason: str = "",
) -> CheckedLine:
    """Record the first outcome or change the reviewed version of this line."""
    if note is not None and len(note) > MAX_NOTE:
        raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
    with transaction.atomic():
        receipt = _open(user, receipt)
        line = _line(receipt, line_id)
        note = line.note if note is None else note.strip()
        clearing_note = bool(line.note) and not note
        if line_revision is not None or outcome is None or clearing_note:
            _reviewed_line(line, line_revision)
        elif not line.removed:
            # An unreviewed first check may carry a reason. If another operator
            # has already checked the line, a blind check cannot replace theirs.
            if line.outcome is not None and (
                not reason.strip() or (line.outcome, line.note) == (outcome, note)
            ):
                return CheckedLine(line, "again", receipt)
            if line.outcome is not None or (line.note and line.note != note):
                _reviewed_line(line, None)
        if line.removed:
            raise Wrong({"line": ["was removed. Restore the extra before changing it."]})
        if not line.expected:
            if outcome not in (None, Outcome.EXTRA):
                raise Wrong({"outcome": ["is extra, for what wasn't expected."]})
            outcome = Outcome.EXTRA
        elif outcome is not None and outcome not in (
            Outcome.MATCHED,
            Outcome.MISSING,
            Outcome.DAMAGED,
            Outcome.REJECTED,
        ):
            raise Wrong({"outcome": ["is matched, missing, damaged or rejected."]})
        if (line.outcome, line.note) == (outcome, note):
            return CheckedLine(line, "again", receipt)
        if line.expected and outcome in NOTED and not note:
            raise Wrong({"note": [f"is required: say why it's {outcome}."]})
        changing_outcome = line.outcome is not None and line.outcome != outcome
        changing_note = bool(line.note) and line.note != note
        reason = _reason(reason, required=changing_outcome or changing_note)
        _advance(receipt)
        _checked(user, receipt, line, outcome, note, reason, uuid7())
        return CheckedLine(line, "recorded", receipt)


def set_removed(
    user: User,
    receipt: Receipt,
    line_id: UUID | str,
    *,
    removed: bool,
    line_revision: int,
    reason: str,
) -> Receipt:
    """Retire or restore one extra while keeping its observation and stable line identity."""
    with transaction.atomic():
        receipt = _open(user, receipt)
        line = _line(receipt, line_id)
        _reviewed_line(line, line_revision)
        if line.expected:
            raise Wrong({"line": ["is expected and cannot be removed or restored."]})
        reason = _reason(reason, required=True)
        if line.removed == removed:
            return receipt
        before = _line_state(line)
        line.removed = removed
        line.line_revision += 1
        line.save(update_fields=["removed", "line_revision"])
        _advance(receipt)
        _record_line(user, receipt, line, before, reason, uuid7())
        return receipt


def describe(
    user: User,
    receipt: Receipt,
    *,
    handed_over_by: str | None = None,
    note: str | None = None,
    plan_revision: int,
    reason: str = "",
) -> Receipt:
    with transaction.atomic():
        receipt = _open(user, receipt, plan_revision)
        before = {"handed_over_by": receipt.handed_over_by, "note": receipt.note}
        if handed_over_by is not None:
            receipt.handed_over_by = handed_over_by.strip()[:200]
        if note is not None:
            if len(note) > MAX_NOTE:
                raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
            receipt.note = note.strip()
        after = {"handed_over_by": receipt.handed_over_by, "note": receipt.note}
        if before == after:
            return receipt
        reason = _reason(
            reason, required=any(old and old != after[key] for key, old in before.items())
        )
        receipt.save(update_fields=["handed_over_by", "note"])
        _advance(receipt)
        observations.record(
            receipt,
            line_id=None,
            kind="handoff",
            before=before,
            after=after,
            user=user,
            reason=reason,
            revision=receipt.plan_revision,
            operation=uuid7(),
        )
        return receipt


@dataclass(frozen=True)
class PutAway:
    line: Any  # a line's ID
    to: Entity
    position: str | None = None


def _placements(
    lines: Sequence[ReceiptLine],
    put_away: Sequence[PutAway],
    wrong: dict[str, list[str]],
) -> tuple[list[ReceiptLine], list[storage.Wanted], set[UUID]]:
    by_id = {str(line.pk): line for line in lines}
    undecided = {
        line.pk for line in lines if line.outcome in RECEIVED and line.entity_id is not None
    }
    wanted: list[storage.Wanted] = []
    putting: list[ReceiptLine] = []
    for index, p in enumerate(put_away):
        chosen = by_id.get(str(p.line))
        if chosen is None:
            wrong[f"put_away/{index}"] = ["isn't one of this receipt's lines."]
        elif chosen.outcome not in RECEIVED or chosen.entity is None:
            wrong[f"put_away/{index}"] = ["is put away only once it's matched or damaged."]
        elif chosen.pk not in undecided:
            wrong[f"put_away/{index}"] = ["is put away twice."]
        else:
            undecided.remove(chosen.pk)
            putting.append(chosen)
            wanted.append(storage.Wanted(chosen.entity, p.to, p.position))
    return putting, wanted, undecided


@transaction.atomic
def preview_put_away(
    user: User, receipt: Receipt, put_away: Sequence[PutAway], *, plan_revision: int
) -> list[storage.Planned]:
    """Plan from the custody the receipt will take, without changing it or the box."""
    receipt = _open(user, receipt, plan_revision)
    lines = list(receipt.lines.filter(removed=False).select_related("entity"))
    wrong: dict[str, list[str]] = {}
    _, wanted, _ = _placements(lines, put_away, wrong)
    if wrong:
        raise Wrong(wrong)
    taken = frozenset(
        line.entity_id
        for line in lines
        if line.entity_id and line.outcome in (*RECEIVED, Outcome.REJECTED)
    )
    return storage.plan(user, wanted, taken_into_custody=taken)


def complete(
    user: User,
    receipt: Receipt,
    put_away: Sequence[PutAway] = (),
    *,
    plan_revision: int,
    plan: str | None = None,
    keep_in_custody: Sequence[UUID] = (),
    missing_rest: bool = False,
) -> Receipt:
    """Put away or explicitly keep each accepted record, and report discrepancies atomically."""
    with transaction.atomic():
        receipt = _open(user, receipt, plan_revision)
        lines = list(
            receipt.lines.filter(removed=False).select_related("entity").order_by("-expected", "id")
        )
        _advance(receipt)
        wrong: dict[str, list[str]] = {}
        if not receipt.handed_over_by:
            wrong["handed_over_by"] = [
                "is required: say who handed it over, such as a courier and its waybill."
            ]
        for line in lines:
            if line.outcome is None and missing_rest:
                _checked(user, receipt, line, Outcome.MISSING, line.note, "", receipt.operation)
            if line.outcome is None:
                wrong[f"lines/{line.pk}"] = ["hasn't been checked: scan it, or mark it missing."]
            elif line.outcome in NOTED and not line.note:
                wrong[f"lines/{line.pk}"] = [f"needs a note: say why it's {line.outcome}."]
        putting, wanted, undecided = _placements(lines, put_away, wrong)
        for index, line_id in enumerate(keep_in_custody):
            if line_id in undecided:
                undecided.remove(line_id)
            else:
                wrong[f"keep_in_custody/{index}"] = [
                    "must name a matched or damaged line of this receipt exactly once."
                ]
        for line_id in sorted(undecided):
            wrong[f"lines/{line_id}"] = [
                "choose where to put it away or explicitly keep it in custody."
            ]
        if wrong:
            raise Wrong(wrong)
        request = receipt.request
        reason = f"Received with {request.hrid}."
        for line in lines:
            if line.outcome in (*RECEIVED, Outcome.REJECTED) and line.entity is not None:
                storage.took_custody(
                    user,
                    line.entity,
                    handed_over_by=receipt.handed_over_by,
                    reason=reason + (f" {line.note}" if line.note else ""),
                    operation=receipt.operation,
                )
        if wanted:
            try:
                storage.move(
                    user,
                    wanted,
                    reason=reason,
                    operation=receipt.operation,
                    reviewed=plan,
                    require_position_review=True,
                )
            except Wrong as error:
                raise Wrong(
                    {
                        f"put_away/{k.split('/', 1)[1]}" if k.startswith("items/") else k: v
                        for k, v in error.errors.items()
                    }
                ) from None
            places = {
                p.pk: p for p in Place.objects.filter(pk__in=[line.entity_id for line in putting])
            }
            for line in putting:
                place = places[line.entity_id]
                line.put_in_id, line.put_row, line.put_column = (
                    place.container_id,
                    place.row,
                    place.column,
                )
                line.save(update_fields=["put_in", "put_row", "put_column"])
        receipt.status = ReceiptStatus.COMPLETED
        receipt.received_by = user
        receipt.completed_at = timezone.now()
        receipt.save(update_fields=["status", "received_by", "completed_at"])
        observations.record(
            receipt,
            line_id=None,
            kind="status",
            before={"status": "open", "note": receipt.note},
            after={"status": "completed", "note": receipt.note},
            user=user,
            reason="",
            revision=receipt.plan_revision,
            operation=receipt.operation,
        )
        _recorded(user, receipt, lines)
        _tell(user, receipt, [line for line in lines if line.outcome in DISCREPANCIES])
        return receipt


def cancel(user: User, receipt: Receipt, *, plan_revision: int, reason: str) -> Receipt:
    """Archive the open receipt in full with its cancelling operator and reason."""
    with transaction.atomic():
        receipt = _open(user, receipt, plan_revision)
        reason = _reason(reason, required=True)
        receipt.status = ReceiptStatus.CANCELLED
        receipt.completed_at = timezone.now()
        receipt.cancelled_by = user
        receipt.cancel_reason = reason
        receipt.save(update_fields=["status", "completed_at", "cancelled_by", "cancel_reason"])
        _advance(receipt)
        observations.record(
            receipt,
            line_id=None,
            kind="status",
            before={"status": "open", "note": receipt.note},
            after={"status": "cancelled", "note": receipt.note},
            user=user,
            reason=reason,
            revision=receipt.plan_revision,
            operation=uuid7(),
        )
        return receipt


def tally(lines: Sequence[ReceiptLine]) -> dict[str, int]:
    found = Counter(line.outcome or "unchecked" for line in lines)
    return {k: found.get(k, 0) for k in [*Outcome.values, "unchecked"]}


def _recorded(user: User, receipt: Receipt, lines: Sequence[ReceiptLine]) -> None:
    request = receipt.request
    data = {
        "receipt": str(receipt.pk),
        "request": str(request.pk),
        "request_hrid": request.hrid,
        "service": {"id": str(receipt.project_id), "name": receipt.project.name},
        "handed_over_by": receipt.handed_over_by,
        "outcomes": tally(lines),
    }
    log.record(
        "request.receipt_completed",
        actor=user,
        target=request,
        project=receipt.project,
        data=data,
    )
    if request.owner_project_id != receipt.project_id:
        log.record(
            "request.receipt_completed",
            actor=user,
            target=request,
            project=request.owner_project,
            data=data,
        )


def _tell(user: User, receipt: Receipt, wrong: Sequence[ReceiptLine]) -> None:
    """Whoever submitted the request, and its watchers, hear of what didn't arrive as
    expected, each only while they can read the request."""
    if not wrong:
        return
    from iki_server.notifications import services as notifications
    from iki_server.notifications.models import Watch, WatchMode

    request = receipt.request
    sent = requests.of(request)
    who: set[Any] = set(
        Watch.objects.filter(entity=request, mode=WatchMode.WATCHING).values_list("user", flat=True)
    )
    if sent is not None and sent.submitted_by_id is not None:
        who.add(sent.submitted_by_id)
    muted = set(
        Watch.objects.filter(entity=request, mode=WatchMode.MUTED).values_list("user", flat=True)
    )
    people = [
        u
        for u in User.objects.filter(pk__in=who - muted - {user.pk}, is_active=True)
        if access.can_read(u, request)
    ]
    if not people:
        return
    counts = tally(wrong)
    said = ", ".join(f"{n} {k}" for k, n in counts.items() if n and k != "unchecked")
    notifications.notify(
        people,
        DISCREPANCY,
        title=f"{receipt.project.name} received {request.hrid}: {said}",
        path=f"/storage/receive/{request.pk}/",
        project=request.owner_project,
        actor=user,
        about=[request],
    )
