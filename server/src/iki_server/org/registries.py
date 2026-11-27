"""Registries (ADR 0115): adding and changing them, their registrars, and projects' choice.

The default registry is every team's, and new projects are in it unless
they're given another. Org Admins add more, for a CRO's clients or a partner
program, each used by the teams they name, and appoint each one's
registrars. A project's registry is chosen when it's made; an Org Admin may
change it until the project has entities, as they're registered in it.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.org.models import ListCurator, Project, Registrar, Registry, Team, registry_of
from iki_server.org.services import Refused

__all__ = [
    "appoint",
    "appoint_curator",
    "create_registry",
    "dismiss",
    "dismiss_curator",
    "for_team",
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


def appoint(registry: Registry, user: User, *, by: User) -> bool:
    """Makes someone one of the registry's registrars. True if they weren't."""
    if not user.is_active:
        raise Refused(f"{user.email} is deactivated.")
    if user.is_service_account:
        # A registrar's change is a person's, signed with their reason (ADR 0115).
        raise Refused("A registrar is a person: a service account can't be one.")
    with transaction.atomic():
        _, created = Registrar.objects.get_or_create(
            registry=registry, user=user, defaults={"appointed_by": by}
        )
        if created:
            audit.record(
                "registry.registrar_appointed",
                actor=by,
                target=registry,
                data={"user": str(user.pk), "email": user.email},
            )
    return created


def dismiss(registry: Registry, user: User, *, by: User) -> bool:
    """Takes someone's registrar role in the registry away. True if they had it."""
    with transaction.atomic():
        removed, _ = Registrar.objects.filter(registry=registry, user=user).delete()
        if removed:
            audit.record(
                "registry.registrar_removed",
                actor=by,
                target=registry,
                data={"user": str(user.pk), "email": user.email},
            )
    return bool(removed)


def appoint_curator(registry: Registry, list_type: str, user: User, *, by: User) -> bool:
    """Makes someone a curator of one of the registry's lab lists (ADR 0270). True if they
    weren't."""
    from iki_server.entities import lists

    entity_type = lists.list_type(list_type)
    if not user.is_active:
        raise Refused(f"{user.email} is deactivated.")
    if user.is_service_account:
        # A curator's change is a person's, with their reason, as a registrar's is.
        raise Refused("A list's curator is a person: a service account can't be one.")
    with transaction.atomic():
        _, created = ListCurator.objects.get_or_create(
            registry=registry, list_type=list_type, user=user, defaults={"appointed_by": by}
        )
        if created:
            audit.record(
                "registry.list_curator_appointed",
                actor=by,
                target=registry,
                data={
                    "list": entity_type.qualified_name,
                    "user": str(user.pk),
                    "email": user.email,
                },
            )
    return created


def dismiss_curator(registry: Registry, list_type: str, user: User, *, by: User) -> bool:
    """Takes someone's curation of one of the registry's lab lists away. True if they had
    it."""
    with transaction.atomic():
        removed, _ = ListCurator.objects.filter(
            registry=registry, list_type=list_type, user=user
        ).delete()
        if removed:
            audit.record(
                "registry.list_curator_removed",
                actor=by,
                target=registry,
                data={"list": list_type, "user": str(user.pk), "email": user.email},
            )
    return bool(removed)


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
