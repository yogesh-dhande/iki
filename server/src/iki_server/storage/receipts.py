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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import access, requests
from iki_server.entities.models import Entity
from iki_server.notifications import kinds
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
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
        open_now = Receipt.objects.filter(request=entity, status=ReceiptStatus.OPEN).first()
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
        return receipt


def _open(user: User, receipt: Receipt) -> Receipt:
    locked = (
        Receipt.objects.select_for_update(of=("self",)).select_related("request").get(pk=receipt.pk)
    )
    if locked.status != ReceiptStatus.OPEN:
        raise Wrong({"": [f"This receipt is {locked.get_status_display().lower()}."]})
    refused = why_not_receiver(user, locked.request)
    if refused is not None:
        raise PermissionError(refused)
    return locked


@dataclass(frozen=True)
class Scanned:
    line: ReceiptLine
    #: ``matched``: an expected line ticked; ``again``: one already checked; ``extra``: a
    #: line added for a code that isn't expected.
    found: str


def scan(user: User, receipt: Receipt, code: str) -> Scanned:
    """Ticks the expected line ``code`` names as matched, or adds an extra line for it."""
    text = code.strip()
    if not text:
        raise Wrong({"code": ["is empty: scan a tube's code."]})
    with transaction.atomic():
        receipt = _open(user, receipt)
        lines = list(receipt.lines.select_related("entity"))
        for line in lines:
            if line.entity is not None and storage.confirms(line.entity, text):
                if line.outcome is not None:
                    return Scanned(line, "again")
                _checked(user, line, Outcome.MATCHED if line.expected else Outcome.EXTRA, "")
                return Scanned(line, "matched")
        for line in lines:
            if not line.expected and line.code == text:
                return Scanned(line, "again")
        found = [e for e in storage.find(user, text) if storage.spec(e.type) is not None]
        known = found[0] if len(found) == 1 else None
        line = ReceiptLine(
            receipt=receipt, entity=known, code=text, expected=False, outcome=Outcome.EXTRA
        )
        line.checked_by, line.checked_at = user, timezone.now()
        line.save()
        return Scanned(line, "extra")


def _checked(user: User, line: ReceiptLine, outcome: str | None, note: str) -> None:
    line.outcome = outcome
    line.note = note.strip()
    line.checked_by = user if outcome is not None else None
    line.checked_at = timezone.now() if outcome is not None else None
    line.save(update_fields=["outcome", "note", "checked_by", "checked_at"])


def check(
    user: User, receipt: Receipt, line_id: UUID | str, outcome: str | None, note: str = ""
) -> ReceiptLine:
    """Sets an expected line's outcome, or a line's note; None clears an expected line."""
    if len(note) > MAX_NOTE:
        raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
    with transaction.atomic():
        receipt = _open(user, receipt)
        line = receipt.lines.filter(pk=line_id).first()
        if line is None:
            raise Wrong({"line": ["isn't one of this receipt's."]})
        if not line.expected:
            if outcome not in (None, Outcome.EXTRA):
                raise Wrong({"outcome": ["is extra, for what wasn't expected."]})
            _checked(user, line, Outcome.EXTRA, note)
            return line
        if outcome is not None and outcome not in (
            Outcome.MATCHED,
            Outcome.MISSING,
            Outcome.DAMAGED,
            Outcome.REJECTED,
        ):
            raise Wrong({"outcome": ["is matched, missing, damaged or rejected."]})
        if outcome in NOTED and not note.strip():
            raise Wrong({"note": [f"is required: say why it's {outcome}."]})
        _checked(user, line, outcome, note)
        return line


def drop(user: User, receipt: Receipt, line_id: UUID | str) -> None:
    """Takes away an extra line scanned by mistake."""
    with transaction.atomic():
        receipt = _open(user, receipt)
        line = receipt.lines.filter(pk=line_id).first()
        if line is None:
            raise Wrong({"line": ["isn't one of this receipt's."]})
        if line.expected:
            raise Wrong({"line": ["is expected: mark it missing instead."]})
        line.delete()


def describe(
    user: User, receipt: Receipt, *, handed_over_by: str | None = None, note: str | None = None
) -> Receipt:
    with transaction.atomic():
        receipt = _open(user, receipt)
        if handed_over_by is not None:
            receipt.handed_over_by = handed_over_by.strip()[:200]
        if note is not None:
            if len(note) > MAX_NOTE:
                raise Wrong({"note": [f"is longer than {MAX_NOTE} characters."]})
            receipt.note = note.strip()
        receipt.save(update_fields=["handed_over_by", "note"])
        return receipt


@dataclass(frozen=True)
class PutAway:
    line: Any  # a line's ID
    to: Entity
    position: str | None = None


def complete(
    user: User, receipt: Receipt, put_away: Sequence[PutAway] = (), *, missing_rest: bool = False
) -> Receipt:
    """Completes a receipt: custody of what was received, each put away where it's given,
    as one change; the requester told of what didn't arrive as expected."""
    with transaction.atomic():
        receipt = _open(user, receipt)
        lines = list(receipt.lines.select_related("entity").order_by("-expected", "id"))
        wrong: dict[str, list[str]] = {}
        if not receipt.handed_over_by:
            wrong["handed_over_by"] = [
                "is required: say who handed it over, such as a courier and its waybill."
            ]
        for line in lines:
            if line.outcome is None and missing_rest:
                _checked(user, line, Outcome.MISSING, line.note)
            if line.outcome is None:
                wrong[f"lines/{line.pk}"] = ["hasn't been checked: scan it, or mark it missing."]
            elif line.outcome in NOTED and not line.note:
                wrong[f"lines/{line.pk}"] = [f"needs a note: say why it's {line.outcome}."]
        by_id = {str(line.pk): line for line in lines}
        wanted: list[storage.Wanted] = []
        putting: list[ReceiptLine] = []
        for index, p in enumerate(put_away):
            chosen = by_id.get(str(p.line))
            if chosen is None:
                wrong[f"put_away/{index}"] = ["isn't one of this receipt's lines."]
            elif chosen.outcome not in RECEIVED or chosen.entity is None:
                wrong[f"put_away/{index}"] = ["is put away only once it's matched or damaged."]
            elif chosen in putting:
                wrong[f"put_away/{index}"] = ["is put away twice."]
            else:
                putting.append(chosen)
                wanted.append(storage.Wanted(chosen.entity, p.to, p.position))
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
                storage.move(user, wanted, reason=reason, operation=receipt.operation)
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
        _recorded(user, receipt, lines)
        _tell(user, receipt, [line for line in lines if line.outcome in DISCREPANCIES])
        return receipt


def cancel(user: User, receipt: Receipt) -> Receipt:
    """Abandons an open receipt: nothing was received with it."""
    with transaction.atomic():
        receipt = _open(user, receipt)
        receipt.status = ReceiptStatus.CANCELLED
        receipt.completed_at = timezone.now()
        receipt.save(update_fields=["status", "completed_at"])
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
    # What, not why: notes stay in Iki, with the receipt, for whoever can read it.
    details = "\n".join(
        f"{line.entity.hrid if line.entity is not None else line.code}: {line.outcome}"
        for line in wrong
    )
    notifications.notify(
        people,
        DISCREPANCY,
        title=f"{receipt.project.name} received {request.hrid}: {said}",
        body=details,
        path=f"/entities/{request.pk}",
        project=request.owner_project,
        actor=user,
        about=[request],
    )
