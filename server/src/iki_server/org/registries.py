"""Registries (ADR 0115): adding and changing them, their roles, and projects' choice.

The default registry is every team's, and new projects are in it unless
they're given another. Org Admins add more, for a CRO's clients or a partner
program, each used by the teams they name, and grant each one's roles
(ADR 0292): its registrars, and its lab lists' curators. A project's registry
is chosen when it's made; an Org Admin may change it until the project has
entities, as they're registered in it.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import Project, Registry, RegistryRole, Team, registry_of
from iki_server.org.services import Refused

__all__ = [
    "create_registry",
    "for_team",
    "grant_role",
    "remove_role",
    "set_project_registry",
    "update_registry",
]


def for_team(team: Team) -> QuerySet[Registry]:
    """The registries a team's projects may be in: every team's, and those naming it."""
    Registry.default()
    return Registry.objects.filter(Q(all_teams=True) | Q(teams=team)).distinct()


def check_for_team(registry: Registry, team: Team) -> None:
    if not for_team(team).filter(pk=registry.pk).exists():
        raise Refused(
            f"{team.name}'s projects can't be in {registry.name}: an Org Admin chooses which "
            "teams use it.",
            field="registry",
        )


def create_registry(
    name: str,
    description: str,
    *,
    all_teams: bool,
    teams: Iterable[Team],
    by: User,
) -> Registry:
    chosen = list(teams)
    try:
        with transaction.atomic():
            Registry.default()
            registry = Registry.objects.create(
                name=name, description=description, all_teams=all_teams, created_by=by
            )
            registry.teams.set(chosen)
            audit.record(
                "registry.created",
                actor=by,
                target=registry,
                data={
                    "name": name,
                    "all_teams": all_teams,
                    "teams": sorted(t.name for t in chosen),
                },
            )
    except IntegrityError as error:
        raise Refused(f"There's a registry called “{name}” already.", field="name") from error
    return registry


def update_registry(
    registry: Registry,
    *,
    by: User,
    name: str | None = None,
    description: str | None = None,
    all_teams: bool | None = None,
    teams: Iterable[Team] | None = None,
) -> Registry:
    """Renames, redescribes, or changes which teams use a registry. A team whose projects
    are in it already keeps them there: it may no longer put new ones in it."""
    changes: dict[str, list[object]] = {}
    with transaction.atomic():
        locked = Registry.objects.select_for_update().get(pk=registry.pk)
        if name is not None and name != locked.name:
            changes["name"] = [locked.name, name]
            locked.name = name
        if description is not None and description != locked.description:
            changes["description"] = [locked.description, description]
            locked.description = description
        if all_teams is not None and all_teams != locked.all_teams:
            if locked.is_default and not all_teams:
                raise Refused(
                    "The default registry is every team's: new projects are in it unless "
                    "they're given another.",
                    field="all_teams",
                )
            changes["all_teams"] = [locked.all_teams, all_teams]
            locked.all_teams = all_teams
        try:
            locked.save()
        except IntegrityError as error:
            raise Refused(f"There's a registry called “{name}” already.", field="name") from error
        if teams is not None:
            chosen = list(teams)
            before = sorted(t.name for t in locked.teams.all())
            after = sorted(t.name for t in chosen)
            if before != after:
                locked.teams.set(chosen)
                changes["teams"] = [before, after]
        if changes:
            audit.record("registry.updated", actor=by, target=locked, data=changes)
    return locked


def grant_role(
    registry: Registry,
    role: RegistryRole.Role,
    user: User,
    *,
    by: User,
    list_type: str | None = None,
) -> tuple[RegistryRole, bool]:
    """Gives someone a role in the registry (ADR 0292): a registrar's, over the whole
    registry, or a curator's of one of its lab lists, which ``list_type`` names. The grant,
    and True if they hadn't it."""
    if role in RegistryRole.LIST_ROLES:
        from iki_server.entities import lists

        if not list_type:
            raise Refused("Say which lab list they curate.", field="list")
        try:
            list_type = lists.list_type(list_type).qualified_name
        except Refused as refusal:
            raise Refused(str(refusal), field="list") from None
    elif list_type:
        raise Refused("A registrar curates the whole registry, not one list.", field="list")
    else:
        list_type = None
    if not user.is_active:
        raise Refused(f"{user.email} is deactivated.", field="user")
    if user.is_service_account:
        # A registry role's changes are a person's, with their reason (ADR 0115).
        raise Refused(
            f"A {RegistryRole.Role(role).label.lower()} is a person: a service account can't "
            "be one.",
            field="user",
        )
    with transaction.atomic():
        grant, created = RegistryRole.objects.get_or_create(
            registry=registry,
            user=user,
            role=role,
            list_type=list_type,
            defaults={"granted_by": by},
        )
        if created:
            audit.record(
                "registry.role_granted",
                actor=by,
                target=registry,
                data=_role_data(grant, user),
            )
    return grant, created


def remove_role(grant: RegistryRole, *, by: User) -> bool:
    """Takes a registry role away. True if it was still held. Its list needn't be
    installed: a curator of a removed plugin's list can still be removed."""
    with transaction.atomic():
        removed, _ = RegistryRole.objects.filter(pk=grant.pk).delete()
        if removed:
            audit.record(
                "registry.role_removed",
                actor=by,
                target=grant.registry,
                data=_role_data(grant, grant.user),
            )
    return bool(removed)


def _role_data(grant: RegistryRole, user: User) -> dict[str, object]:
    """What a registry role's audit event says: the role, its list (or null, for the whole
    registry) and whose it is."""
    return {
        "role": grant.role,
        "list": grant.list_type,
        "user": str(user.pk),
        "email": user.email,
    }


def set_project_registry(project: Project, registry: Registry, *, by: User) -> None:
    """Moves a project with no entities to another registry its team may use."""
    from iki_server.entities.models import Entity

    with transaction.atomic():
        locked = (
            Project.objects.select_for_update(of=("self",))
            .select_related("team")
            .get(pk=project.pk)
        )
        if locked.registry_id == registry.pk:
            return
        if Entity.objects.filter(owner_project=locked).exists():
            raise Refused(
                f"{locked.name} has entities, registered in its registry: its registry can't "
                "change now.",
                field="registry",
            )
        check_for_team(registry, locked.team)
        before = Registry.objects.get(pk=registry_of(locked))
        locked.registry = registry
        locked.save(update_fields=["registry"])
        audit.record(
            "project.registry_changed",
            actor=by,
            target=locked,
            project=locked,
            data={"registry": [before.name, registry.name]},
        )
    project.registry = registry
