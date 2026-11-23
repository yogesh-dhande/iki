"""Receiving and counting observations keep independent, append-only chains of evidence."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from api_client import Api
from django.db import DatabaseError, IntegrityError, transaction
from org_world import build_world
from test_audit import as_app_role
from test_requests import Lab
from test_storage import box, lab, sent_with_tubes

from iki_server.audit import log
from iki_server.audit.models import AuditEvent
from iki_server.storage import observations
from iki_server.storage.models import (
    ObservationEvent,
    ObservationKind,
    Receipt,
    ReceiptLine,
    Reconciliation,
)

pytestmark = pytest.mark.django_db

__all__ = ["as_app_role", "lab"]


def test_observations_link_only_the_same_parent_line_and_kind(lab: Lab) -> None:
    request, _ = sent_with_tubes(lab, 1)
    first, second = lab.users["tech"], lab.users["core_qa"]
    storage_box = box(lab.api("tech"), lab.core)
    receipt = Receipt.objects.create(project=lab.core, request_id=request["id"], started_by=first)
    count = Reconciliation.objects.create(container_id=storage_box["id"], started_by=second)
    working = ReceiptLine.objects.create(receipt=receipt, expected=False, code="fixture-code")
    line, other = working.pk, uuid.uuid4()
    command = uuid.uuid4()
    before = {
        "code": "fixture-code",
        "note": "original observation",
        "entity_id": storage_box["id"],
    }
    revised = {**before, "note": "corrected observation"}

    def save(
        parent: Receipt | Reconciliation,
        line_id: uuid.UUID | None,
        kind: ObservationKind,
        revision: int,
    ) -> ObservationEvent:
        return observations.record(
            parent,
            line_id=line_id,
            kind=kind,
            before=before,
            after=revised,
            user=second,
            reason="Read the original worksheet again",
            revision=revision,
            operation=command,
        )

    with transaction.atomic():
        receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
        count = Reconciliation.objects.select_for_update().get(pk=count.pk)
        initial = observations.record(
            receipt,
            line_id=line,
            kind=ObservationKind.OUTCOME,
            before=None,
            after=before,
            user=first,
            reason="",
            revision=2,
            operation=uuid.uuid4(),
        )
        elsewhere = save(count, line, ObservationKind.SCAN, 2)
        other_line = save(receipt, other, ObservationKind.OUTCOME, 3)
        handoff = save(receipt, None, ObservationKind.HANDOFF, 3)
        status = save(receipt, None, ObservationKind.STATUS, 3)
        corrected = save(receipt, line, ObservationKind.OUTCOME, 3)
        next_status = save(receipt, None, ObservationKind.STATUS, 4)
        # Two same-kind changes to the same line in one parent command remain individually ordered.
        repeated = save(count, line, ObservationKind.SCAN, 2)
        third = save(count, line, ObservationKind.SCAN, 2)
        working.delete()

    assert (
        initial.previous_id is None
        and initial.actor_id == first.pk
        and initial.created_at is not None
    )
    assert [row.previous_id for row in (elsewhere, other_line, handoff, status)] == [None] * 4
    assert (corrected.previous_id, next_status.previous_id) == (initial.pk, status.pk)
    assert (repeated.previous_id, third.previous_id) == (elsewhere.pk, repeated.pk)
    corrected.refresh_from_db()
    assert (corrected.before, corrected.after, corrected.actor_id) == (before, revised, second.pk)
    assert corrected.receipt_id == receipt.pk and corrected.reconciliation_id is None
    assert corrected.line_id == line and not ReceiptLine.objects.filter(pk=line).exists()
    event = AuditEvent.objects.get(
        action="storage.observation_recorded", data__event=str(corrected.pk)
    )
    assert (event.target_id, event.project_id, event.actor_id) == (
        str(receipt.pk),
        lab.core.pk,
        second.pk,
    )
    assert event.data == {
        "event": str(corrected.pk),
        "previous": str(initial.pk),
        "receipt": str(receipt.pk),
        "line": str(line),
        "operation": str(command),
        "kind": ObservationKind.OUTCOME,
        "revision": 3,
        "reason": corrected.reason,
    }
    counted = AuditEvent.objects.get(action=event.action, data__event=str(elsewhere.pk))
    assert counted.target_id == counted.data["reconciliation"] == str(count.pk)
    assert counted.project_id == lab.core.pk
    assert (
        ObservationEvent.objects.count() == AuditEvent.objects.filter(action=event.action).count()
    )


def test_audit_failure_leaves_no_unpaired_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world()
    user = world.users["editor"]
    storage_box = box(Api(world.browser("editor")), world.project)
    count = Reconciliation.objects.create(container_id=storage_box["id"], started_by=user)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(log, "record", unavailable)
    with transaction.atomic():
        locked = Reconciliation.objects.select_for_update().get(pk=count.pk)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            observations.record(
                locked,
                line_id=None,
                kind=ObservationKind.STATUS,
                before={"status": "open"},
                after={"status": "cancelled"},
                user=user,
                reason="Count restarted",
                revision=2,
                operation=uuid.uuid4(),
            )
        assert not ObservationEvent.objects.exists()


def test_observation_constraints_and_immutable_history(as_app_role: Callable[[str], None]) -> None:
    world = build_world()
    user = world.users["editor"]
    storage_box = box(Api(world.browser("editor")), world.project)
    receipt = Receipt.objects.create(
        project=world.project, request_id=storage_box["id"], started_by=user
    )
    count = Reconciliation.objects.create(container_id=storage_box["id"], started_by=user)
    valid: dict[str, Any] = {
        "receipt": receipt,
        "actor": user,
        "kind": ObservationKind.OUTCOME,
        "line_id": uuid.uuid4(),
        "before": None,
        "after": {"outcome": "matched"},
        "revision": 1,
        "operation": uuid.uuid4(),
    }
    event = ObservationEvent.objects.create(**valid)
    earlier_receipt = Receipt.objects.create(
        project=world.project, request_id=storage_box["id"], started_by=user, status="cancelled"
    )
    handoff = ObservationEvent.objects.create(
        **(valid | {"kind": ObservationKind.HANDOFF, "line_id": None})
    )
    invalid: list[dict[str, Any]] = [
        {"receipt": None},
        {"reconciliation": count},
        {"revision": 0},
        {"kind": "invalid"},
        {"before": [1]},
        {"after": "invalid"},
        {"previous": event, "receipt": earlier_receipt},
        {"previous": event, "line_id": uuid.uuid4()},
        {"previous": handoff, "line_id": None, "kind": ObservationKind.STATUS},
    ]
    for changes in invalid:
        with pytest.raises(IntegrityError), transaction.atomic():
            ObservationEvent.objects.create(**(valid | changes))
    for statement in [
        "UPDATE iki_storage_observation_event SET reason = ''",
        "DELETE FROM iki_storage_observation_event",
        "TRUNCATE iki_storage_observation_event",
    ]:
        with pytest.raises(DatabaseError, match="permission denied"):
            as_app_role(statement)
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        ObservationEvent.objects.filter(pk=event.pk).update(reason="changed")
    assert ObservationEvent.objects.get(pk=event.pk).reason == ""
    as_app_role(
        "INSERT INTO iki_storage_observation_event"
        " (id, receipt_id, actor_id, kind, operation, revision, reason)"
        " SELECT gen_random_uuid(), receipt_id, actor_id, 'status', gen_random_uuid(), 2, ''"
        " FROM iki_storage_observation_event WHERE kind = 'outcome'"
    )
    assert ObservationEvent.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_recording_requires_the_parent_transaction() -> None:
    from django.db.transaction import TransactionManagementError

    world = build_world()
    user = world.users["editor"]
    storage_box = box(Api(world.browser("editor")), world.project)
    count = Reconciliation.objects.create(container_id=storage_box["id"], started_by=user)
    with pytest.raises(TransactionManagementError, match="parent transaction"):
        observations.record(
            count,
            line_id=None,
            kind=ObservationKind.STATUS,
            before=None,
            after={"status": "open"},
            user=user,
            reason="",
            revision=1,
            operation=uuid.uuid4(),
        )
    assert not ObservationEvent.objects.exists()


def test_only_the_workflows_observations_can_name_a_line() -> None:
    world = build_world()
    user = world.users["editor"]
    storage_box = box(Api(world.browser("editor")), world.project)
    receipt = Receipt.objects.create(
        project=world.project, request_id=storage_box["id"], started_by=user
    )
    count = Reconciliation.objects.create(container_id=storage_box["id"], started_by=user)
    line = uuid.uuid4()
    invalid_scopes: list[tuple[Receipt | Reconciliation, ObservationKind, uuid.UUID | None]] = [
        (receipt, ObservationKind.OUTCOME, None),
        (receipt, ObservationKind.HANDOFF, line),
        (receipt, ObservationKind.STATUS, line),
        (receipt, ObservationKind.RESOLUTION, line),
        (receipt, ObservationKind.SCAN, line),
        (count, ObservationKind.SCAN, None),
        (count, ObservationKind.RESOLUTION, None),
        (count, ObservationKind.STATUS, line),
        (count, ObservationKind.OUTCOME, line),
        (count, ObservationKind.HANDOFF, None),
    ]
    for parent, kind, line_id in invalid_scopes:
        with pytest.raises(IntegrityError, match="iki_observation_scope"), transaction.atomic():
            parent.observations.create(
                actor=user,
                kind=kind,
                line_id=line_id,
                before=None,
                after={"read": True},
                revision=1,
                operation=uuid.uuid4(),
            )
