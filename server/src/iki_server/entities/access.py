"""Who can read and change entities: the permission service, applied to projects' entities.

- **Reading** an entity takes a role in any project it belongs to (its owner
  project, or one it was linked into). Org Admins get no more than that.
- **Changing** it takes Editor in its owner project, which must not be
  archived.

Entities people can't read behave as if they don't exist (404).
"""

from typing import Any

from django.db.models import Exists, OuterRef, QuerySet

from iki_server.entities.models import Entity, ProjectLink
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action


def readable_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401 - User or AnonymousUser
    """Projects whose data the user can read (:attr:`Action.PROJECT_READ`)."""
    return permissions.visible_projects(user).filter(my_role__isnull=False)  # type: ignore[misc]


def visible(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """The entities the user can read."""
    links = ProjectLink.objects.filter(
        entity=OuterRef("pk"), project__in=readable_projects(user).values("pk")
    )
    return Entity.objects.filter(Exists(links))


def can_read(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    return visible(user).filter(pk=entity.pk).exists()


def can_edit(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    return permissions.can(user, Action.PROJECT_EDIT, owner)
