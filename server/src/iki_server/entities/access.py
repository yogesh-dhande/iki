"""Who can read and change entities: the permission service, applied to projects' entities.

- **Reading** an entity takes a role in any project it belongs to (its owner
  project, or one it was linked into). Org Admins get no more than that.
- **Changing** it takes Editor in its owner project, which must not be
  archived.

Entities people can't read behave as if they don't exist (404).

A **view** (``iki/core.View``) is read only by people who can read every entity it
shows (ADR 0110): to anyone else it doesn't exist either, so a view never shows
or names what someone can't read. An entity purged with its plugin still counts
as readable, as its tombstone (ADR 0092).

**Files** are readable by whoever can read the project they were added to,
or an entity that refers to them, at any revision.

A run reads only what belongs to its project: entities linked into it, and
its files (ADR 0066).

**Sharing** an entity with another project (linking it in, or referring to it
from that project's entities) takes its own project's leave:
:func:`shareable_into` (ADR 0067). Projects in different registries share
nothing (ADR 0115).

A **registry entry** (ADR 0115) belongs to no project: it's readable by whoever
can read a record of it, and nobody else, so its ID alone grants nothing. The
entities above (``visible`` and the rest) are records; :func:`visible_entries`
are entries.
"""

from typing import Any, cast

from django.db.models import Exists, OuterRef, Q, QuerySet

from iki_server.entities import registry
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.people import Namer
from iki_server.entities.values import Refs, shape, subtypes
from iki_server.files.models import File, FileUse
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action


def readable_projects(user: Any) -> QuerySet[Project]:  # noqa: ANN401 - User or AnonymousUser
    """Projects whose data the user can read (:attr:`Action.PROJECT_READ`)."""
    projects = permissions.visible_projects(user)
    if projects.query.is_empty():
        return projects  # none, and so without ``my_role`` to filter by
    return projects.filter(my_role__isnull=False)  # type: ignore[misc]


def visible(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """The entities the user can read: records, not registry entries."""
    return visible_of(user, Entity)


def visible_entries(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """The registry entries the user can read (ADR 0115): those with a record they can."""
    projects = readable_projects(user).values("pk")
    return Entity.objects.filter(registry__isnull=False).filter(_has_record_in(projects))


def readable(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """Records and registry entries the user can read: what a reference may show."""
    projects = readable_projects(user).values("pk")
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    return Entity.objects.filter(
        Q(Exists(links), _views_they_read(Entity, projects)) | _has_record_in(projects),
        purged_at__isnull=True,
    )


def _has_record_in(projects: QuerySet[Any]) -> Q:
    """Of entries, those with a record in (or shared with) one of ``projects``."""
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    records = Entity.objects.filter(entry=OuterRef("pk"), purged_at__isnull=True).filter(
        Exists(links)
    )
    return Q(registry__isnull=False) & Q(Exists(records))


def held_entries(project: Project) -> QuerySet[Entity]:
    """The registry entries `project` holds: it has a record of each, its own or one shared
    with it (ADR 0115). Its entities' references may name them."""
    here = ProjectLink.objects.filter(entity=OuterRef("pk"), project=project)
    records = Entity.objects.filter(entry=OuterRef("pk"), purged_at__isnull=True).filter(
        Exists(here)
    )
    return Entity.objects.filter(registry__isnull=False).filter(Exists(records))


def visible_of[E: Entity](user: Any, model: type[E]) -> QuerySet[E]:  # noqa: ANN401 - User or AnonymousUser
    """The entities of a type's model (its subtypes' included) the user can read. A purged
    entity's tombstone isn't one (ADR 0092): see :func:`tombstones`."""
    projects = readable_projects(user).values("pk")
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    return cast(
        "QuerySet[E]",
        model._default_manager.filter(
            Exists(links), _views_they_read(model, projects), purged_at__isnull=True
        ),
    )


#: The type whose entities show others (ADR 0086, ADR 0110).
VIEW = "iki/core.View"


def _views_they_read(model: type[Entity], projects: QuerySet[Any]) -> Q:
    """Of ``model``'s entities, the views only if every entity they show is linked into one
    of ``projects``, and everything else. Nothing for models no view can be."""
    if VIEW not in registry.types():
        return Q()
    view = registry.model(VIEW)
    if model is not Entity and not issubclass(model, view):
        return Q()
    items = shape(VIEW).part_of("targets").list_model("targets")
    linked = ProjectLink.objects.filter(entity=OuterRef("item_id"), project__in=projects)
    # A registry entry it shows is readable through a record of it (ADR 0115).
    record_links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    records = Entity.objects.filter(entry=OuterRef("item_id")).filter(Exists(record_links))
    hidden = items._default_manager.filter(entity_id=OuterRef("pk")).filter(
        ~Exists(linked), ~Exists(records)
    )
    return ~Q(type__in=subtypes(VIEW)) | Q(~Exists(hidden))


def traced(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """The entities the user can read, and purged ones they could: lineage goes through
    a tombstone, which it shows as purged (ADR 0092)."""
    projects = readable_projects(user).values("pk")
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    return Entity.objects.filter(Exists(links), _views_they_read(Entity, projects))


def tombstones(user: Any) -> QuerySet[Entity]:  # noqa: ANN401 - User or AnonymousUser
    """What's left of purged entities the user could read (ADR 0092): their HRIDs, types
    and when, but no values."""
    projects = readable_projects(user).values("pk")
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    # A purged registry entry (ADR 0115), through a purged record of it they could read.
    record_links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    records = Entity.objects.filter(entry=OuterRef("pk")).filter(Exists(record_links))
    return Entity.objects.filter(
        Q(Exists(links), _views_they_read(Entity, projects))
        | Q(registry__isnull=False, owner_project__isnull=True) & Q(Exists(records)),
        purged_at__isnull=False,
    )


def readable_files(user: Any) -> QuerySet[File]:  # noqa: ANN401 - User or AnonymousUser
    """The files the user can read: through their project, or an entity that uses them."""
    projects = readable_projects(user).values("pk")
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=visible(user).values("pk"))
    return File.objects.filter(Q(project__in=projects) | Exists(uses))


def in_project(project: Project) -> QuerySet[Entity]:
    """The entities that belong to `project`: its own, and those linked into it."""
    return Entity.objects.filter(
        Exists(ProjectLink.objects.filter(entity=OuterRef("pk"), project=project)),
        purged_at__isnull=True,
    )


def project_files(project: Project) -> QuerySet[File]:
    """The files that belong to `project`: added to it, or used by one of its entities."""
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=in_project(project).values("pk"))
    return File.objects.filter(Q(project=project) | Exists(uses))


def shareable_into(user: Any, project: Project) -> QuerySet[Entity]:  # noqa: ANN401
    """The entities the user can read that `project` may use (ADR 0067): its own, those
    already in it, and those whose project allows sharing and is in its registry (ADR
    0115). Others behave, for it, as if the user couldn't read them."""
    here = ProjectLink.objects.filter(entity=OuterRef("pk"), project=project)
    return visible(user).filter(
        Q(owner_project__allow_sharing=True, owner_project__registry_id=project.registry_id)
        | Q(owner_project=project)
        | Exists(here)
    )


def shareable_files(user: Any, project: Project) -> QuerySet[File]:  # noqa: ANN401
    """The files the user can read that `project`'s entities may refer to: its own, those
    its entities use, and those from projects that allow sharing."""
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=in_project(project).values("pk"))
    return readable_files(user).filter(
        Q(project__allow_sharing=True) | Q(project=project) | Exists(uses)
    )


def refs_for(user: Any, project: Project) -> Refs:  # noqa: ANN401
    """What values of `project`'s entities may refer to: what the user can read, and the
    project may use. A reference to something another project keeps to itself says so."""
    return Refs(
        shareable_into(user, project),
        shareable_files(user, project),
        held=visible(user),
        held_files=readable_files(user),
        entries=held_entries(project),
        readable_entries=visible_entries(user),
        project=project.name,
        namer=Namer(project),
    )


def can_read(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    if entity.registry_id is not None:
        return visible_entries(user).filter(pk=entity.pk).exists()
    return visible(user).filter(pk=entity.pk).exists()


def can_edit(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    """Whether the user may change a record: Editor in its project. Nobody edits a registry
    entry so: its facts are its registrars' (ADR 0115)."""
    if entity.owner_project_id is None:
        return False
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    return permissions.can(user, Action.PROJECT_EDIT, owner)
