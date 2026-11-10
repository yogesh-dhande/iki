"""Changes to org roles, teams, projects and memberships.

Callers check permissions first (:mod:`iki_server.org.permissions`); these
functions keep the invariants that hold no matter who asks:

- The organization always keeps at least one active Org Admin, because
  otherwise only cluster access (``setup_link --recover``) could fix it.
- Nobody deactivates themselves.
- A project keeps at least one Admin, so its members can manage it. (Org
  Admins can still recover a project whose Admins have all left.)
- Inactive people can't be given new roles or memberships.

Every change is logged as a structured event. The append-only audit log
(next release) records the same events.
"""

from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
    TeamMembership,
)

logger = structlog.get_logger("iki.org")


class Refused(Exception):  # noqa: N818 - reads as "the change was refused"
    """A change that would break an invariant. The message explains it to people."""


def _ids(**objects: Any) -> dict[str, str]:  # noqa: ANN401
    return {f"{name}_id": str(obj.pk) for name, obj in objects.items() if obj is not None}


def _require_active(user: User) -> None:
    if not user.is_active:
        raise Refused(f"{user.email} is deactivated.")


# --- Org roles and people ------------------------------------------------------


def _other_active_org_admins(user: User) -> bool:
    return (
        OrgRoleAssignment.objects.filter(role=OrgRole.ORG_ADMIN, user__is_active=True)
        .exclude(user=user)
        .exists()
    )


def _lock_org_admins() -> None:
    """Serializes changes that could leave no active Org Admin."""
    list(
        OrgRoleAssignment.objects.select_for_update()
        .filter(role=OrgRole.ORG_ADMIN)
        .values_list("pk", flat=True)
    )


def set_org_roles(user: User, roles: set[OrgRole], *, by: User | None) -> None:
    """Gives the user exactly these org roles."""
    with transaction.atomic():
        _lock_org_admins()
        current = {
            OrgRole(r)
            for r in OrgRoleAssignment.objects.filter(user=user).values_list("role", flat=True)
        }
        added, removed = roles - current, current - roles
        if added:
            _require_active(user)
        if OrgRole.ORG_ADMIN in removed and not _other_active_org_admins(user):
            raise Refused("Iki needs at least one Org Admin. Make someone else an Org Admin first.")
        OrgRoleAssignment.objects.filter(user=user, role__in=removed).delete()
        OrgRoleAssignment.objects.bulk_create(
            OrgRoleAssignment(user=user, role=role, granted_by=by) for role in sorted(added)
        )
    for role in sorted(added):
        logger.info("org_role_granted", role=str(role), **_ids(user=user, by=by))
    for role in sorted(removed):
        logger.info("org_role_revoked", role=str(role), **_ids(user=user, by=by))


def grant_org_role(user: User, role: OrgRole, *, by: User | None) -> None:
    _require_active(user)
    _, created = OrgRoleAssignment.objects.get_or_create(
        user=user, role=role, defaults={"granted_by": by}
    )
    if created:
        logger.info("org_role_granted", role=str(role), **_ids(user=user, by=by))


def set_active(user: User, active: bool, *, by: User) -> None:
    """Deactivates or reactivates someone. Deactivation ends their sessions at once."""
    if user.is_active == active:
        return
    with transaction.atomic():
        if not active:
            if user.pk == by.pk:
                raise Refused("You can't deactivate yourself.")
            _lock_org_admins()
            is_org_admin = OrgRoleAssignment.objects.filter(
                user=user, role=OrgRole.ORG_ADMIN
            ).exists()
            if is_org_admin and not _other_active_org_admins(user):
                raise Refused("Iki needs at least one active Org Admin.")
        user.is_active = active
        user.save(update_fields=["is_active"])
    logger.info("user_activated" if active else "user_deactivated", **_ids(user=user, by=by))


# --- Teams ---------------------------------------------------------------------


def create_team(name: str, description: str, *, by: User) -> Team:
    try:
        with transaction.atomic():
            team = Team.objects.create(name=name, description=description)
    except IntegrityError as error:
        raise Refused(f"There's already a team called “{name}”.") from error
    logger.info("team_created", **_ids(team=team, by=by))
    return team


def update_team(team: Team, name: str, description: str, *, by: User) -> None:
    team.name, team.description = name, description
    try:
        with transaction.atomic():
            team.save(update_fields=["name", "description"])
    except IntegrityError as error:
        raise Refused(f"There's already a team called “{name}”.") from error
    logger.info("team_updated", **_ids(team=team, by=by))


def set_team_archived(team: Team, archived: bool, *, by: User) -> None:
    team.archived_at = timezone.now() if archived else None
    team.save(update_fields=["archived_at"])
    logger.info("team_archived" if archived else "team_restored", **_ids(team=team, by=by))


def add_team_member(team: Team, user: User, *, by: User) -> None:
    _require_active(user)
    _, created = TeamMembership.objects.get_or_create(
        team=team, user=user, defaults={"added_by": by}
    )
    if created:
        logger.info("team_member_added", **_ids(team=team, user=user, by=by))


def remove_team_member(team: Team, user: User, *, by: User) -> None:
    deleted, _ = TeamMembership.objects.filter(team=team, user=user).delete()
    if deleted:
        logger.info("team_member_removed", **_ids(team=team, user=user, by=by))


# --- Projects ------------------------------------------------------------------


def create_project(team: Team, name: str, description: str, *, by: User) -> Project:
    """Creates a project in the team. Its creator becomes its first Admin."""
    try:
        with transaction.atomic():
            project = Project.objects.create(
                team=team, name=name, description=description, created_by=by
            )
            ProjectMembership.objects.create(
                project=project, user=by, role=ProjectRole.ADMIN, added_by=by
            )
    except IntegrityError as error:
        raise Refused(f"{team.name} already has a project called “{name}”.") from error
    logger.info("project_created", **_ids(project=project, team=team, by=by))
    return project


def update_project(project: Project, name: str, description: str, *, by: User) -> None:
    project.name, project.description = name, description
    try:
        with transaction.atomic():
            project.save(update_fields=["name", "description"])
    except IntegrityError as error:
        raise Refused(f"{project.team.name} already has a project called “{name}”.") from error
    logger.info("project_updated", **_ids(project=project, by=by))


def set_project_archived(project: Project, archived: bool, *, by: User) -> None:
    project.archived_at = timezone.now() if archived else None
    project.save(update_fields=["archived_at"])
    event = "project_archived" if archived else "project_restored"
    logger.info(event, **_ids(project=project, by=by))


def _other_admins(project: Project, membership: ProjectMembership) -> bool:
    """Whether someone active other than this grant's holder(s) stays an Admin."""
    return (
        ProjectMembership.objects.filter(project=project, role=ProjectRole.ADMIN)
        .exclude(pk=membership.pk)
        .filter(Q(user__is_active=True) | Q(team__memberships__user__is_active=True))
        .exists()
    )


def _lock(project: Project) -> None:
    Project.objects.select_for_update().filter(pk=project.pk).first()


def grant_project_role(
    project: Project,
    role: ProjectRole,
    *,
    user: User | None = None,
    team: Team | None = None,
    by: User,
) -> ProjectMembership:
    """Gives a person or a team a role in the project, replacing any earlier grant."""
    if (user is None) == (team is None):
        raise TypeError("grant to exactly one of user or team")
    if user is not None:
        _require_active(user)
    with transaction.atomic():
        _lock(project)
        membership = ProjectMembership.objects.filter(project=project, user=user, team=team).first()
        if membership is None:
            membership = ProjectMembership.objects.create(
                project=project, user=user, team=team, role=role, added_by=by
            )
        elif membership.role != role:
            if membership.role == ProjectRole.ADMIN and not _other_admins(project, membership):
                raise Refused("A project needs at least one Admin. Make someone else Admin first.")
            membership.role = role
            membership.save(update_fields=["role"])
    logger.info(
        "project_role_granted",
        role=ProjectRole(role).label,
        **_ids(project=project, user=user, team=team, by=by),
    )
    return membership


def remove_project_membership(membership: ProjectMembership, *, by: User) -> None:
    project = membership.project
    with transaction.atomic():
        _lock(project)
        if membership.role == ProjectRole.ADMIN and not _other_admins(project, membership):
            raise Refused("A project needs at least one Admin. Make someone else Admin first.")
        membership.delete()
    logger.info(
        "project_member_removed",
        **_ids(project=project, user=membership.user, team=membership.team, by=by),
    )
