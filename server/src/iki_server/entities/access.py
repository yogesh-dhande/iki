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
nothing (ADR 0115). What a request linked into a project (ADR 0132) is read
there, and never passed on from there, nor its files referred to anew; and a
request's result awaiting release is its service's alone.

A **registry entry** (ADR 0115) belongs to no project: it's readable by whoever
can read a record of it, and nobody else, so its ID alone grants nothing. The
entities above (``visible`` and the rest) are records; :func:`visible_entries`
are entries.

A **lab list's option** (ADR 0270) is a registry entry every project in its
registry reads, and its records refer to, without a record of it: whoever can
read a project reads its registry's lists.
"""

from collections.abc import Callable, Iterable
from typing import Any, cast
from uuid import UUID

from django.db.models import Exists, F, OuterRef, Q, QuerySet

from iki_server.entities import registry
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.people import Namer
from iki_server.entities.values import Refs, shape, subtypes
from iki_server.files.models import File, FileUse
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectMembership
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
    return readable_in(readable_projects(user).values("pk"))


def readable_in(projects: QuerySet[Any]) -> QuerySet[Entity]:
    """Records and registry entries readable through ``projects``: the one definition of
    it, which the graph's walks use too (:mod:`iki_server.graph.queries`)."""
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    return Entity.objects.filter(
        Q(Exists(links), _views_they_read(Entity, projects)) | _has_record_in(projects),
        purged_at__isnull=True,
    )


def readable_reference_ids(user: Any, ids: Iterable[UUID]) -> set[UUID]:  # noqa: ANN401 - a caller
    """The named records, registry entries and tombstones the reader can identify."""
    named = set(ids)
    return _reference_ids_in(readable_projects(user).values("pk"), named) if named else set()


def project_reference_ids(project: Project, ids: set[UUID]) -> set[UUID]:
    """The named records, entries and tombstones readable through this project."""
    return (
        _reference_ids_in(Project.objects.filter(pk=project.pk).values("pk"), ids) if ids else set()
    )


def _reference_ids_in(projects: QuerySet[Any], named: set[UUID]) -> set[UUID]:
    visible = set(readable_in(projects).filter(pk__in=named).values_list("pk", flat=True))
    if remaining := named - visible:
        visible.update(
            _tombstones_in(projects).filter(pk__in=remaining).values_list("pk", flat=True)
        )
    return visible


def all_references_readable(user: Any, ids: Iterable[UUID]) -> bool:  # noqa: ANN401 - a caller
    """Whether the reader can identify every record, entry or tombstone in an exact copy."""
    named = set(ids)
    return not named or readable_reference_ids(user, named) == named


def _has_record_in(projects: QuerySet[Any]) -> Q:
    """Of entries, those with a record in (or shared with) one of ``projects``, and the
    options of the lab lists of their registries (ADR 0270)."""
    links = ProjectLink.objects.filter(entity=OuterRef("pk"), project__in=projects)
    records = Entity.objects.filter(entry=OuterRef("pk"), purged_at__isnull=True).filter(
        Exists(links)
    )
    registries = Project.objects.filter(pk__in=projects).values("registry")
    return Q(registry__isnull=False) & (Q(Exists(records)) | _listed(Q(registry__in=registries)))


def _listed(where: Q) -> Q:
    """Lab lists' options (ADR 0270), where ``where`` says: none, without lists."""
    lists = list_types()
    return Q(type__in=sorted(lists)) & where if lists else Q(pk__in=[])


def list_types() -> frozenset[str]:
    """The installed lab lists' types (ADR 0270)."""
    return frozenset(name for name, t in registry.types().items() if t.is_list)


def held_entries(project: Project) -> QuerySet[Entity]:
    """The registry entries `project` holds: it has a record of each, its own or one shared
    with it (ADR 0115), and its registry's lists' options (ADR 0270). Its entities'
    references may name them."""
    here = ProjectLink.objects.filter(entity=OuterRef("pk"), project=project)
    records = Entity.objects.filter(entry=OuterRef("pk"), purged_at__isnull=True).filter(
        Exists(here)
    )
    return Entity.objects.filter(registry__isnull=False).filter(
        Q(Exists(records)) | _listed(Q(registry_id=project.registry_id))
    )


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
    return _tombstones_in(readable_projects(user).values("pk"))


def _tombstones_in(projects: QuerySet[Any]) -> QuerySet[Entity]:
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


def _their_own_way(user: Any) -> QuerySet[ProjectLink]:  # noqa: ANN401
    """Of an entity's links, those the user reads it through that no request made: what
    a request linked into a project is read there, and not passed on (ADR 0132)."""
    return ProjectLink.objects.filter(
        entity=OuterRef("pk"),
        project__in=readable_projects(user).values("pk"),
        via_request__isnull=True,
    )


def unreleased() -> Q | None:
    """Of entities, a request's results its service hasn't released (ADR 0132): until it
    does, they're its alone. None if no type is a request's results."""
    from iki_server.entities import requests

    types = requests.result_types()
    if not types:
        return None
    # Released, it's in the request's own project, for the request.
    released = ProjectLink.objects.filter(
        entity=OuterRef("pk"),
        via_request__isnull=False,
        project=F("via_request__owner_project"),
    )
    return Q(Q(type__in=types), ~Exists(released))


def shareable_into(user: Any, project: Project) -> QuerySet[Entity]:  # noqa: ANN401
    """The entities the user can read that `project` may use (ADR 0067): its own, those
    already in it, and those whose project allows sharing and is in its registry (ADR
    0115), which the user reads other than through a request, and which aren't results
    awaiting release (ADR 0132). A work item is usable only in its service project."""
    from iki_server.entities import requests

    here = ProjectLink.objects.filter(entity=OuterRef("pk"), project=project)
    leaving = Q(
        Exists(_their_own_way(user)),
        owner_project__allow_sharing=True,
        owner_project__registry_id=project.registry_id,
    )
    held_back = unreleased()
    if held_back is not None:
        leaving &= ~held_back
    shared = (leaving | Exists(here)) & ~Q(type__in=requests.work_item_types())
    return visible(user).filter(Q(owner_project=project) | shared)


def why_held(user: Any, entity: Entity, project: Project) -> str:  # noqa: ANN401
    """Why ``project`` may not use an entity the user reads, though they read it: one of
    ``work_item``, ``unreleased``, ``registry``, ``request`` or ``unshared``."""
    from iki_server.entities import requests

    if entity.owner_project_id != project.pk and requests.work_item_spec(entity.type):
        return "work_item"
    held_back = unreleased()
    if held_back is not None and Entity.objects.filter(held_back, pk=entity.pk).exists():
        return "unreleased"
    owner = entity.owner_project
    if owner is not None and owner.registry_id != project.registry_id:
        return "registry"
    if not visible(user).filter(Exists(_their_own_way(user)), pk=entity.pk).exists():
        return "request"
    return "unshared"


def shareable_files(user: Any, project: Project) -> QuerySet[File]:  # noqa: ANN401
    """The files the user can read that `project`'s entities may refer to: its own, those
    its entities use, and those from projects that allow sharing, in its registry, which
    the user reads other than through a request. A file that came with a request (ADR
    0132) is read with the records it came on, and nothing else refers to it anew."""
    kept_here = ProjectLink.objects.filter(
        entity=OuterRef("entity_id"), project=project, via_request__isnull=True
    )
    used_here = FileUse.objects.filter(file=OuterRef("pk"), entity__purged_at__isnull=True).filter(
        Exists(kept_here)
    )
    return readable_files(user).filter(
        Q(project=project)
        | Exists(used_here)
        | Q(
            _files_their_own_way(user),
            project__allow_sharing=True,
            project__registry_id=project.registry_id,
        )
    )


def _files_their_own_way(user: Any) -> Q:  # noqa: ANN401
    """Of files, those the user reads through their project, or an entity they read other
    than through a request."""
    theirs = visible(user).filter(Exists(_their_own_way(user)))
    uses = FileUse.objects.filter(file=OuterRef("pk"), entity__in=theirs.values("pk"))
    return Q(project__in=readable_projects(user).values("pk")) | Q(Exists(uses))


def why_held_file(user: Any, file: File, project: Project) -> str:  # noqa: ANN401
    """Why ``project``'s entities may not refer to a file the user reads: ``registry``,
    ``request`` or ``unshared``."""
    if file.project.registry_id != project.registry_id:
        return "registry"
    if not File.objects.filter(_files_their_own_way(user), pk=file.pk).exists():
        return "request"
    return "unshared"


def refs_for(user: Any, project: Project) -> Refs:  # noqa: ANN401
    """What values of `project`'s entities may refer to: what the user can read, and the
    project may use. A reference to something the user reads but the project may not use
    says why."""
    return Refs(
        shareable_into(user, project),
        shareable_files(user, project),
        held=held_entity(user, project),
        held_file=held_file(user, project),
        entries=held_entries(project),
        readable_entries=visible_entries(user),
        project=project.name,
        namer=Namer(project),
    )


def held_entity(user: Any, project: Project) -> Callable[[Any], str | None]:  # noqa: ANN401
    """For :class:`Refs`: why a reference from ``project`` can't name an entity the user
    reads, or None if they don't read it."""
    return lambda pk: _held_entity(user, pk, project)


def _held_entity(user: Any, pk: Any, project: Project) -> str | None:  # noqa: ANN401
    entity = visible(user).select_related("owner_project").filter(pk=pk).first()
    if entity is None:
        return None
    owner = entity.owner_project
    name = owner.name if owner is not None else "another project"
    match why_held(user, entity, project):
        case "work_item":
            return f"refers to {entity.hrid}, a work item that stays in {name}."
        case "unreleased":
            return (
                f"refers to {entity.hrid}, a request's result its service hasn't released: "
                "until it does, it's the service's alone."
            )
        case "registry":
            return f"refers to an entity in {name}, in another registry: registries share nothing."
        case "request":
            return (
                f"refers to {entity.hrid}, which you read through a request between projects: "
                "it's read there, and not referred to from another."
            )
        case _:
            return (
                f"refers to an entity in {name}, which doesn't allow sharing its entities "
                "with other projects."
            )


def held_file(user: Any, project: Project) -> Callable[[Any], str | None]:  # noqa: ANN401
    """For :class:`Refs`: why a reference from ``project`` can't name a file the user
    reads, or None if they don't read it."""
    return lambda pk: _held_file(user, pk, project)


def _held_file(user: Any, pk: Any, project: Project) -> str | None:  # noqa: ANN401
    file = readable_files(user).select_related("project").filter(pk=pk).first()
    if file is None:
        return None
    match why_held_file(user, file, project):
        case "registry":
            return (
                f"refers to a file in {file.project.name}, in another registry: registries "
                "share nothing."
            )
        case "request":
            return (
                "refers to a file you read through a request between projects: it's read "
                "with the records it came on, and nothing else refers to it."
            )
        case _:
            return (
                f"refers to a file in {file.project.name}, which doesn't allow sharing with "
                "other projects."
            )


def readers(entity: Entity, users: Iterable[Any]) -> set[Any]:
    """Of ``users``, the IDs of those who can read ``entity``, as :func:`can_read` says, in
    the same few queries however many there are: whom a notification tells (ADR 0129).

    Someone reads a record through a role in a project it's linked into, by their own grant
    or a team's; an entry, through a record of it. A view (ADR 0110) also needs every
    entity it shows readable, and someone whose token limits what they read is held to it:
    those are asked one by one, which notifications rarely need."""
    people = [u for u in users if getattr(u, "is_active", False)]
    if not people or entity.purged_at is not None:
        return set()
    one_by_one = [u for u in people if permissions.token_limits(u) is not None]
    together = {u.pk for u in people if permissions.token_limits(u) is None}
    found = {u.pk for u in one_by_one if can_read(u, entity)}
    if VIEW in registry.types() and entity.type in subtypes(VIEW):
        return found | {u.pk for u in people if u.pk in together and can_read(u, entity)}
    if entity.registry_id is not None and entity.type in list_types():
        # A lab list's option: every project in its registry reads it (ADR 0270).
        where = Q(project__registry_id=entity.registry_id)
    elif entity.registry_id is not None:
        links = ProjectLink.objects.filter(entity__entry=entity, entity__purged_at__isnull=True)
        where = Q(project__in=links.values("project"))
    else:
        where = Q(project__in=ProjectLink.objects.filter(entity=entity).values("project"))
    grants = (
        ProjectMembership.objects.filter(where)
        .filter(Q(user__in=together) | Q(team__memberships__user__in=together))
        .values_list("user", "team__memberships__user")
        .order_by()
        .distinct()
    )
    return found | ({pk for row in grants for pk in row} & together)


def can_read(user: Any, entity: Entity) -> bool:  # noqa: ANN401
    if entity.registry_id is not None:
        return visible_entries(user).filter(pk=entity.pk).exists()
    return visible(user).filter(pk=entity.pk).exists()


def edit_action(type_name: str, *, by_run: bool = False) -> Action:
    """The permission for authoring this type, including a subtype of App or View."""
    if registry.is_a(type_name, "iki/core.App") or registry.is_a(type_name, VIEW):
        return Action.RUN_EDIT_APP if by_run else Action.APP_EDIT
    return Action.PROJECT_EDIT


def can_edit(user: Any, entity: Entity, *, acting_in: Project | None = None) -> bool:  # noqa: ANN401
    """Whether the user may change a record: Editor in its project. Nobody edits a registry
    entry so: its facts are its registrars' (ADR 0115). A request sent to a service (ADR
    0132) is changed by either side, each in its own fields: ``acting_in``, as that
    project's alone (a run in it)."""
    if entity.owner_project_id is None:
        return False
    from iki_server.entities import requests

    if requests.spec_of(entity.type) is not None:
        return bool(requests.sides(user, entity, requests.of(entity), acting_in=acting_in))
    owner = Project.objects.select_related("team").get(pk=entity.owner_project_id)
    return permissions.can(user, edit_action(entity.type, by_run=acting_in is not None), owner)
