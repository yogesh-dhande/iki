"""Changes to org roles, teams, projects and memberships.

Callers check permissions first (:mod:`iki_server.org.permissions`); these
functions keep the invariants that hold no matter who asks:

- The organization always keeps at least one active Org Admin, because
  otherwise only cluster access (``setup_link --recover``) could fix it.
- Nobody deactivates themselves.
- A project keeps at least one Admin, so its members can manage it. (Org
  Admins can still recover a project whose Admins have all left.)
- Inactive people can't be given new roles or memberships.
- Service accounts can't be Org Admins or team members (ADR 0027).

Every change is recorded in the audit log, in the same transaction.
"""

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
    TeamMembership,
)


class Refused(Exception):  # noqa: N818 - reads as "the change was refused"
    """A change that would break an invariant. The message explains it to people."""


def _require_active(user: User) -> None:
    if not user.is_active:
        raise Refused(f"{user.email} is deactivated.")


def _require_assignable(user: User, roles: set[OrgRole]) -> None:
    if user.is_service_account and OrgRole.ORG_ADMIN in roles:
        raise Refused("Service accounts can’t be Org Admins.")


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
            _require_assignable(user, added)
        if OrgRole.ORG_ADMIN in removed and not _other_active_org_admins(user):
            raise Refused("Iki needs at least one Org Admin. Make someone else an Org Admin first.")
        OrgRoleAssignment.objects.filter(user=user, role__in=removed).delete()
        OrgRoleAssignment.objects.bulk_create(
            OrgRoleAssignment(user=user, role=role, granted_by=by) for role in sorted(added)
        )
        for role in sorted(added):
            audit.record("org_role.granted", actor=by, target=user, data={"role": str(role)})
        for role in sorted(removed):
            audit.record("org_role.revoked", actor=by, target=user, data={"role": str(role)})


def grant_org_role(user: User, role: OrgRole, *, by: User | None) -> None:
    _require_active(user)
    _require_assignable(user, {role})
    with transaction.atomic():
        _, created = OrgRoleAssignment.objects.get_or_create(
            user=user, role=role, defaults={"granted_by": by}
        )
        if created:
            audit.record("org_role.granted", actor=by, target=user, data={"role": str(role)})


def set_active(user: User, active: bool, *, by: User) -> None:
    """Deactivates or reactivates someone. Deactivation ends their sessions at once.

    People provisioned over SCIM are activated and deactivated by the
    identity provider only, so the two never disagree (ADR 0026).
    """
    if user.is_active == active:
        return
    if user.is_provisioned:
        raise Refused(
            f"{user.email} is managed by your identity provider. "
            "Assign or unassign Iki for them there."
        )
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
        audit.record("user.activated" if active else "user.deactivated", actor=by, target=user)


def end_sessions(user: User, *, by: User) -> int:
    """Signs someone out everywhere, at once. They can sign in again."""
    from iki_server.accounts.models import UserSession

    with transaction.atomic():
        ended, _ = UserSession.objects.filter(user=user).delete()
        audit.record(
            "user.sessions_revoked",
            actor=by,
            target=user,
            data={"sessions": ended, "reason": "signed_out_by_admin"},
        )
    return ended


# --- Teams ---------------------------------------------------------------------


def linked_groups(team: Team) -> list[str]:
    """The identity provider groups that decide this team's members, if any."""
    from iki_server.scim.models import ScimGroup

    return list(ScimGroup.objects.filter(team=team).values_list("display_name", flat=True))


def _require_hand_managed(team: Team) -> None:
    groups = linked_groups(team)
    if groups:
        raise Refused(
            f"{team.name}’s members come from the identity provider group "
            f"{', '.join(groups)}. Change the group there."
        )


def create_team(name: str, description: str, *, by: User) -> Team:
    try:
        with transaction.atomic():
            team = Team.objects.create(name=name, description=description)
            audit.record("team.created", actor=by, target=team, team=team)
    except IntegrityError as error:
        raise Refused(f"There's already a team called “{name}”.") from error
    return team


def update_team(team: Team, name: str, description: str, *, by: User) -> None:
    before = {"name": team.name, "description": team.description}
    team.name, team.description = name, description
    try:
        with transaction.atomic():
            team.save(update_fields=["name", "description"])
            audit.record(
                "team.updated", actor=by, target=team, team=team, data=_changes(before, team)
            )
    except IntegrityError as error:
        raise Refused(f"There's already a team called “{name}”.") from error


def set_team_archived(team: Team, archived: bool, *, by: User) -> None:
    with transaction.atomic():
        team.archived_at = timezone.now() if archived else None
        team.save(update_fields=["archived_at"])
        audit.record(
            "team.archived" if archived else "team.restored", actor=by, target=team, team=team
        )


def add_team_member(team: Team, user: User, *, by: User) -> None:
    _require_active(user)
    if user.is_service_account:
        raise Refused(
            "Service accounts don’t join teams. Give them a role in each project they need."
        )
    _require_hand_managed(team)
    with transaction.atomic():
        _, created = TeamMembership.objects.get_or_create(
            team=team, user=user, defaults={"added_by": by}
        )
        if created:
            audit.record("team.member_added", actor=by, target=user, team=team)


def remove_team_member(team: Team, user: User, *, by: User) -> None:
    _require_hand_managed(team)
    with transaction.atomic():
        deleted, _ = TeamMembership.objects.filter(team=team, user=user).delete()
        if deleted:
            audit.record("team.member_removed", actor=by, target=user, team=team)


# --- Projects ------------------------------------------------------------------


def _changes(before: dict[str, str], obj: Project | Team) -> dict[str, Any]:
    """The fields that changed, as {field: [old, new]}."""
    return {
        field: [old, getattr(obj, field)]
        for field, old in before.items()
        if getattr(obj, field) != old
    }


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
            audit.record("project.created", actor=by, target=project, project=project, team=team)
    except IntegrityError as error:
        raise Refused(f"{team.name} already has a project called “{name}”.") from error
    return project


def update_project(project: Project, name: str, description: str, *, by: User) -> None:
    before = {"name": project.name, "description": project.description}
    project.name, project.description = name, description
    try:
        with transaction.atomic():
            project.save(update_fields=["name", "description"])
            audit.record(
                "project.updated",
                actor=by,
                target=project,
                project=project,
                data=_changes(before, project),
            )
    except IntegrityError as error:
        raise Refused(f"{project.team.name} already has a project called “{name}”.") from error


def set_project_archived(project: Project, archived: bool, *, by: User) -> None:
    with transaction.atomic():
        project.archived_at = timezone.now() if archived else None
        project.save(update_fields=["archived_at"])
        action = "project.archived" if archived else "project.restored"
        audit.record(action, actor=by, target=project, project=project)


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
    """Gives a person or a team a role in the project, replacing any earlier grant.

    An Org Admin granting themselves a role (to recover a project, or to
    read its data) is recorded like any other grant, with ``self: true``.
    """
    if (user is None) == (team is None):
        raise TypeError("grant to exactly one of user or team")
    if user is not None:
        _require_active(user)
    with transaction.atomic():
        _lock(project)
        membership = ProjectMembership.objects.filter(project=project, user=user, team=team).first()
        previous = membership.role if membership else None
        if membership is None:
            membership = ProjectMembership.objects.create(
                project=project, user=user, team=team, role=role, added_by=by
            )
        elif membership.role != role:
            if membership.role == ProjectRole.ADMIN and not _other_admins(project, membership):
                raise Refused("A project needs at least one Admin. Make someone else Admin first.")
            membership.role = role
            membership.save(update_fields=["role"])
        else:
            return membership
        data: dict[str, Any] = {"role": ProjectRole(role).label}
        if previous is not None:
            data["previous_role"] = ProjectRole(previous).label
        if user is not None and user.pk == by.pk:
            data["self"] = True
        audit.record(
            "project.role_granted",
            actor=by,
            target=user or team,
            project=project,
            team=team,
            data=data,
        )
    return membership


def remove_project_membership(membership: ProjectMembership, *, by: User) -> None:
    project = membership.project
    with transaction.atomic():
        _lock(project)
        if membership.role == ProjectRole.ADMIN and not _other_admins(project, membership):
            raise Refused("A project needs at least one Admin. Make someone else Admin first.")
        membership.delete()
        audit.record(
            "project.member_removed",
            actor=by,
            target=membership.user or membership.team,
            project=project,
            team=membership.team,
            data={"role": ProjectRole(membership.role).label},
        )
