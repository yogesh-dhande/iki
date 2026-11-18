"""Background work for records: telling Admins whose records need reassigning (ADR 0122)."""

from __future__ import annotations

from typing import Any

import structlog
from django.db import models, transaction

from iki_server.audit.models import AuditEvent
from iki_server.tasks import task

logger = structlog.get_logger("iki.entities")

#: Changes after which someone may not be nameable where they're assigned work any more.
LEAVING = frozenset(
    {
        "user.deactivated",
        "user.deprovisioned",
        "project.member_removed",
        "project.lab_role_revoked",
        "team.member_removed",
    }
)


@task(max_attempts=3)
def check_people(user_ids: list[str]) -> int:
    """Tells Admins of the projects these people can't be assigned work in any more, but
    are, which records need reassigning. Runs after the change, so it reads it."""
    from iki_server.entities import assignments

    with transaction.atomic():
        told = assignments.check_people(user_ids)
    logger.info("assignments_checked", people=len(user_ids), projects_told=told)
    return told


def on_audit(event: AuditEvent, target: models.Model | None) -> None:
    """The audit log's listener: after someone leaves a project, loses a lab role or is
    deactivated, check whether their assignments need reassigning."""
    if event.action not in LEAVING:
        return
    ids = _people(target)
    if ids:
        check_people.enqueue(sorted(ids))


def _people(target: models.Model | None) -> set[str]:
    from iki_server.accounts.models import User
    from iki_server.org.models import ProjectMembership, Team

    if isinstance(target, User):
        return {str(target.pk)} if not target.is_service_account else set()
    if isinstance(target, ProjectMembership):
        target = target.user if target.user_id is not None else target.team
        return _people(target)
    if isinstance(target, Team):
        found: Any = target.memberships.values_list("user_id", flat=True)
        return {str(pk) for pk in found}
    return set()
