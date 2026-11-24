"""Immutable receiving and reconciliation observations."""

from typing import Any
from uuid import UUID

from django.db import connection, transaction

from iki_server.accounts.models import User
from iki_server.audit import log

from .models import ObservationEvent, Receipt, Reconciliation


def record(
    parent: Receipt | Reconciliation,
    *,
    line_id: UUID | None,
    kind: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    user: User,
    reason: str,
    revision: int,
    operation: UUID,
) -> ObservationEvent:
    """Append one observation and its audit reference in the caller's parent transaction.

    The caller holds the parent row lock, checks the reviewed revision, and supplies the
    resulting revision and a common operation ID for this command. On older material the
    working value can be known even when there is no prior observation event to point to.
    """
    if not connection.in_atomic_block:
        raise transaction.TransactionManagementError(
            "Recording an observation needs its parent transaction."
        )
    if isinstance(parent, Reconciliation):
        name = "reconciliation"
        project = parent.container.owner_project
        if project is None:
            raise ValueError("A reconciliation observation needs a container in a project.")
    else:
        name = "receipt"
        project = parent.project
    with transaction.atomic():
        previous = (
            parent.observations.filter(line_id=line_id, kind=kind)
            .order_by("-revision", "-id")
            .values_list("pk", flat=True)
            .first()
        )
        event = parent.observations.create(
            line_id=line_id,
            kind=kind,
            before=before,
            after=after,
            actor=user,
            previous_id=previous,
            reason=reason,
            revision=revision,
            operation=operation,
        )
        # Audit readers can have a different record scope. The event is the authoritative
        # snapshot; its read endpoint checks visibility before exposing codes or identities.
        log.record(
            "storage.observation_recorded",
            actor=user,
            target=parent,
            project=project,
            data={
                "event": str(event.pk),
                "previous": str(previous) if previous else None,
                name: str(parent.pk),
                "line": str(line_id) if line_id else None,
                "kind": kind,
                "revision": revision,
                "operation": str(operation),
                "reason": reason,
            },
        )
    return event
