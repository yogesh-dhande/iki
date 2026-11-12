"""Who can read and change entities: the permission service, applied to projects' entities.

- **Reading** an entity takes a role in any project it belongs to (its owner
  project, or one it was linked into). Org Admins get no more than that.
- **Changing** it takes Editor in its owner project, which must not be
  archived.

Entities people can't read behave as if they don't exist (404).

**Files** are readable by whoever can read the project they were added to,
or an entity that refers to them, at any revision.

A run reads only what belongs to its project: entities linked into it, and
its files (ADR 0066).
"""

from typing import Any, cast

from django.db.models import Exists, OuterRef, Q, QuerySet

from iki_server.entities.models import Entity, ProjectLink
from iki_server.files.models import File, FileUse
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action


def readable_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401 - User or AnonymousUser
    """Projects whose data the user can read (:attr:`Action.PROJECT_READ`)."""
    return permissions.visible_projects(user).filter(my_role__isnull=False)  # type: ignore[misc]


def visible(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """The entities the user can read."""
    return visible_of(user, Entity)


def visible_of[E: Entity](user: Any, model: type[E]) -> QuerySet[E]:  # noqa: ANN401 - User or AnonymousUser
    """The entities of a type's model (its subtypes' included) the user can read."""
    links = ProjectLink.objects.filter(
        entity=OuterRef("pk"), project__in=readable_projects(user).values("pk")
    )
    return cast("QuerySet[E]", model._default_manager.filter(Exists(links)))


def readable_files(user: Any) -> QuerySet[File]:  # noqa: ANN401 - User or AnonymousUser
    """The files the user can read: through their project, or an entity that uses them."""
    projects = readable_projects(user).values("pk")
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=visible(user).values("pk"))
    return File.objects.filter(Q(project__in=projects) | Exists(uses))


def in_project(project: Project) -> QuerySet[Entity]:
    """The entities that belong to `project`: its own, and those linked into it."""
    return Entity.objects.filter(
        Exists(ProjectLink.objects.filter(entity=OuterRef("pk"), project=project))
    )


def project_files(project: Project) -> QuerySet[File]:
    """The files that belong to `project`: added to it, or used by one of its entities."""
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=in_project(project).values("pk"))
    return File.objects.filter(Q(project=project) | Exists(uses))


def can_read(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    return visible(user).filter(pk=entity.pk).exists()


def can_edit(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    return permissions.can(user, Action.PROJECT_EDIT, owner)
