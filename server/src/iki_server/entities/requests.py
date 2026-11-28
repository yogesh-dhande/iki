"""Requests between projects (ADR 0132): a project asks another's service, such as a core
facility, for work, and gets results back.

Core holds only the intake between projects. A request's workflow (its states, who sets
them, what follows what) is its type's, through state fields (ADR 0125), and later
guards (step 58).

- **Intake.** A service project accepts the request types its :class:`Intake` names,
  from the projects it names or from every project in its registry. Its Admins set it.
- **A request** is a record of a request type (``request:`` in its manifest), owned by
  the project that asks. Submitting it (:func:`submit`) sends it to one service, for
  good: it's linked into the service's project, with the records its ``sends`` fields
  name (its samples). That's the service's queue.
- **Two sides.** The service may change only the fields its type names in
  ``service_fields``, and set only those state fields, with the lab roles its own people
  hold in its project; every other field is the asking project's. :func:`sides` says
  which a person writes as, every write path checks it (:func:`refused_fields`), and so
  does the database: a trigger on the request's tables (:func:`install_guards`) refuses
  a change to the other side's columns, whatever wrote it.
- **Results** are records of a result type (``result:``), made in the service's project,
  which owns them. When the service sets one's state to a released state, it's linked
  back into the asking project (:func:`released`), and stays there. Until then it's the
  service's alone: no other project may use it, by a link, a reference or a run.
- **Nothing else crosses.** What a request linked into a project is readable there, and
  can't be passed on to a third project, nor its files referred to anew
  (:func:`iki_server.entities.access.shareable_into`, ``shareable_files``).
  Requests stay within a registry, and a project that doesn't share its entities (ADR
  0067) can't send them out, nor release results back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cache
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Now
from psycopg import sql

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.log import current_agent
from iki_server.entities import registry
from iki_server.entities.models import (
    Entity,
    Intake,
    IntakeSource,
    ProjectLink,
    Request,
    Revision,
)
from iki_server.entities.values import Invalid, shape
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_spec.model import RequestSpec, ResultSpec, WorkItemSpec, amount_of, stored_in_list_table

__all__ = [
    "ASKER",
    "SERVICE",
    "accepts",
    "field_project",
    "install_guards",
    "of",
    "refused_fields",
    "released",
    "result_spec",
    "run_sides",
    "send",
    "services_for",
    "set_intake",
    "sides",
    "spec_of",
    "submit",
    "writing",
]

#: The two sides of a request: the project that asked, and the service it was sent to.
ASKER, SERVICE = "asker", "service"
#: Set for a transaction by whoever writes a sent request's values: which side's columns
#: the write may change (``asker``, ``service`` or ``both``). Unset is the asking side's.
SIDE_SETTING = "iki.request_side"
#: The guards' triggers' names start with this; ``sync_plugins`` drops those not needed.
TRIGGER_PREFIX = "iki_request__"


# --- Types ---------------------------------------------------------------------------------------


@cache
def spec_of(type_name: str) -> RequestSpec | None:
    """What makes ``type_name``'s records requests: the ``request`` of it or an ancestor,
    with the service fields each subtype on the way down adds (ADR 0232). None if they
    aren't."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    found: RequestSpec | None = None
    for t in [*reversed(registry.ancestors(entity_type)), entity_type]:
        if t.service.request is None:
            continue
        if found is None:
            found = t.service.request
        else:
            added = tuple(
                n for n in t.service.request.service_fields if n not in found.service_fields
            )
            found = replace(found, service_fields=(*found.service_fields, *added))
    return found


@cache
def result_spec(type_name: str) -> ResultSpec | None:
    """What makes ``type_name``'s records a request's results, its own or an ancestor's."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    for t in [entity_type, *registry.ancestors(entity_type)]:
        if t.service.result is not None:
            return t.service.result
    return None


@cache
def work_item_spec(type_name: str) -> WorkItemSpec | None:
    """What makes this type an internal work item, including an inherited declaration."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    return next(
        (
            t.service.work_item
            for t in [entity_type, *registry.ancestors(entity_type)]
            if t.service.work_item
        ),
        None,
    )


def work_item_types() -> list[str]:
    """Every installed type whose records are work items, including subtypes."""
    return sorted(n for n in registry.types() if work_item_spec(n) is not None)


def request_types() -> list[str]:
    """Every installed type whose records are requests, its subtypes included."""
    return sorted(n for n in registry.types() if spec_of(n) is not None)


def result_types() -> list[str]:
    """Every installed type whose records are a request's results, its subtypes included."""
    return sorted(n for n in registry.types() if result_spec(n) is not None)


def service_fields(type_name: str) -> frozenset[str]:
    """The fields of a request type the service writes. Empty for other types."""
    spec = spec_of(type_name)
    return frozenset(spec.service_fields) if spec is not None else frozenset()


def is_service_field(type_name: str, name: str) -> bool:
    return name in service_fields(type_name)


def side_of(type_name: str, name: str) -> str:
    """Whose ``name`` is, on a request: the service's, or the asking project's."""
    return SERVICE if is_service_field(type_name, name) else ASKER


# --- A request's sides --------------------------------------------------------------------------


def of(entity: Entity) -> Request | None:
    """The request a record is, once it's been sent; None for one not sent, or not a request."""
    if spec_of(entity.type) is None:
        return None
    return Request.objects.select_related("service", "service__team").filter(entity=entity).first()


def services_of(entities: Iterable[Entity]) -> dict[Any, Any]:
    """The service each sent request among ``entities`` was sent to, by ID."""
    wanted = [e.pk for e in entities if spec_of(e.type) is not None]
    if not wanted:
        return {}
    return dict(Request.objects.filter(entity_id__in=wanted).values_list("entity_id", "service_id"))


def service_projects_of(entities: Iterable[Entity]) -> dict[Any, Project]:
    """The service each sent request among ``entities`` was sent to."""
    wanted = [e.pk for e in entities if spec_of(e.type) is not None]
    if not wanted:
        return {}
    return {
        r.entity_id: r.service
        for r in Request.objects.select_related("service__team").filter(entity_id__in=wanted)
    }


def field_project(entity: Entity, name: str, request: Request | None = None) -> Project | None:
    """The project whose people may set the record's field ``name``, and be named in it:
    for a request's service field, the service it was sent to (None while it isn't);
    otherwise the record's own."""
    if is_service_field(entity.type, name):
        found = request if request is not None else of(entity)
        return found.service if found is not None else None
    return entity.owner_project


def sides(
    user: Any,  # noqa: ANN401 - a User, or AnonymousUser
    entity: Entity,
    request: Request | None,
    *,
    acting_in: Project | None = None,
) -> frozenset[str]:
    """The sides of a request ``user`` may change it as: the asking project's, as its
    Editor, and the service's, as an Editor there. ``acting_in``: only as that project
    (a run in it writes as it)."""
    from iki_server.entities.access import edit_action

    action = edit_action(entity.type, by_run=acting_in is not None)
    out = set()
    owner = entity.owner_project
    if (
        owner is not None
        and (acting_in is None or acting_in.pk == owner.pk)
        and permissions.can(user, action, owner)
    ):
        out.add(ASKER)
    if (
        request is not None
        and (acting_in is None or acting_in.pk == request.service_id)
        and permissions.can(user, action, request.service)
    ):
        out.add(SERVICE)
    return frozenset(out)


def refused_fields(
    entity: Entity, names: Iterable[str], allowed: frozenset[str], request: Request | None
) -> dict[str, list[str]]:
    """Why the fields of ``names`` that aren't ``allowed`` sides' can't be changed on a
    request. Empty for a record of another type."""
    if spec_of(entity.type) is None:
        return {}
    owner = entity.owner_project
    out: dict[str, list[str]] = {}
    for name in names:
        if is_service_field(entity.type, name):
            if request is None:
                out[name] = [
                    "is the service's: the project the request is sent to sets it, once it's "
                    "submitted."
                ]
            elif SERVICE not in allowed:
                out[name] = [
                    f"is {request.service.name}'s: the project serving the request changes it."
                ]
        elif ASKER not in allowed:
            asking = owner.name if owner is not None else "the asking project"
            out[name] = [f"is {asking}'s: the project that sent the request changes it."]
    return out


@contextmanager
def writing(allowed: frozenset[str]) -> Iterator[None]:
    """Inside, the database lets a sent request's write change ``allowed`` sides' columns,
    and only those: its guards refuse any other."""
    side = "both" if allowed == {ASKER, SERVICE} else SERVICE if allowed == {SERVICE} else ASKER
    if side == ASKER:
        yield
        return
    # In a savepoint: a write that fails rolls it back, and the setting with it, so nothing
    # runs in a transaction the failure aborted.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, true)", [SIDE_SETTING, side])
        yield
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, '', true)", [SIDE_SETTING])


# --- Intake -------------------------------------------------------------------------------------


def accepted_by(type_name: str) -> list[str]:
    """The names an intake can accept ``type_name``'s records by: it, and its ancestors."""
    entity_type = registry.types()[type_name]
    return [type_name, *(t.qualified_name for t in registry.ancestors(entity_type))]


def services_for(asking: Project, type_name: str | None = None) -> QuerySet[Project]:
    """The projects that take requests (of ``type_name``, or any) from ``asking``: in its
    registry, not archived, and naming it or taking requests from the whole registry."""
    named = IntakeSource.objects.filter(intake_id=OuterRef("pk"), project=asking)
    intakes = Intake.objects.filter(project_id=OuterRef("pk")).filter(
        Q(from_registry=True) | Exists(named)
    )
    if type_name is not None:
        intakes = intakes.filter(types__overlap=accepted_by(type_name))
    else:
        intakes = intakes.exclude(types=[])
    return (
        Project.objects.select_related("team", "registry")
        .filter(registry=asking.registry, archived_at__isnull=True)
        .exclude(pk=asking.pk)
        .filter(Exists(intakes))
    )


def accepts(service: Project, asking: Project, type_name: str) -> bool:
    return services_for(asking, type_name).filter(pk=service.pk).exists()


@dataclass(frozen=True)
class IntakeSettings:
    types: list[str]
    from_registry: bool
    sources: list[Project]


def intake_of(project: Project) -> IntakeSettings:
    found = Intake.objects.filter(project=project).first()
    if found is None:
        return IntakeSettings([], False, [])
    sources = list(
        Project.objects.select_related("team")
        .filter(pk__in=found.sources.values("project_id"))
        .order_by("name")
    )
    return IntakeSettings(sorted(found.types), found.from_registry, sources)


def set_intake(
    project: Project,
    *,
    types: Sequence[str],
    from_registry: bool,
    sources: Sequence[Project],
    by: User,
) -> IntakeSettings:
    """What ``project`` accepts as a service: the request types, and who from. Audited as
    ``project.intake_changed``. Raises :class:`Invalid` for a type that isn't a request
    type, or a project it can't take requests from."""
    errors: dict[str, list[str]] = {}
    for index, name in enumerate(types):
        if name not in registry.types() or spec_of(name) is None:
            errors[f"types/{index}"] = [f"`{name}` isn't a request type."]
    for index, source in enumerate(sources):
        if source.pk == project.pk:
            errors[f"projects/{index}"] = ["is this project: it doesn't send requests to itself."]
        elif source.registry_id != project.registry_id:
            errors[f"projects/{index}"] = [
                f"is in another registry from {project.name}, and requests stay within one."
            ]
    if errors:
        raise Invalid(errors)
    with transaction.atomic():
        # Locked before it's read, its first time too: what changed is read under the lock,
        # so two Admins' changes are each audited against the one before it.
        Project.objects.select_for_update().filter(pk=project.pk).first()
        before = intake_of(project)
        intake, _ = Intake.objects.get_or_create(project=project)
        intake.types = sorted(set(types))
        intake.from_registry = from_registry
        intake.updated_by = by
        intake.updated_at = Now()
        intake.save()
        intake.sources.exclude(project__in=[s.pk for s in sources]).delete()
        for source in sources:
            IntakeSource.objects.get_or_create(intake=intake, project=source)
        after = intake_of(project)
        changes: dict[str, list[Any]] = {
            "types": [before.types, after.types],
            "from_registry": [before.from_registry, after.from_registry],
            "projects": [
                [{"id": str(p.pk), "name": p.name} for p in before.sources],
                [{"id": str(p.pk), "name": p.name} for p in after.sources],
            ],
        }
        changed = {k: v for k, v in changes.items() if v[0] != v[1]}
        if changed:
            log.record(
                "project.intake_changed", actor=by, target=project, project=project, data=changed
            )
    return after


# --- Submitting ---------------------------------------------------------------------------------


class NoSuchService(LookupError):  # noqa: N818 - reads as "there's no such service"
    """The project named isn't a service the request can go to, or doesn't exist: which,
    the caller isn't told."""


def not_a_service(asking: Project, type_label: str) -> str:
    """What's said of a project that isn't a service ``asking`` may send these to: the same
    whether it exists or not."""
    return f"No such service, or it doesn’t take {type_label} from {asking.name}."


def sent_records(entity: Entity, values: Mapping[str, Any]) -> list[uuid.UUID]:
    """The records a request's ``sends`` fields name, in order, each once: for a reference
    to a registry entry, the asking project's record of it (ADR 0115)."""
    spec = spec_of(entity.type)
    if spec is None:
        return []
    named: list[Any] = []
    for name in spec.sends:
        value = values.get(name)
        if isinstance(value, list):
            named.extend(value)
        elif value is not None:
            named.append(value)
    ids = list(dict.fromkeys(uuid.UUID(str(v)) for v in named))
    if not ids:
        return []
    entries = set(
        Entity.objects.filter(pk__in=ids, registry__isnull=False).values_list("pk", flat=True)
    )
    if not entries:
        return ids
    records: dict[Any, Any] = {}
    here = ProjectLink.objects.filter(entity=OuterRef("pk"), project=entity.owner_project)
    for record, entry, owner in (
        Entity.objects.filter(entry_id__in=entries, purged_at__isnull=True)
        .filter(Exists(here))
        .values_list("pk", "entry_id", "owner_project_id")
    ):
        # Its own record, else one shared with it.
        if entry not in records or owner == entity.owner_project_id:
            records[entry] = record
    return [records[i] if i in entries else i for i in ids if i not in entries or i in records]


def submit(
    user: User,
    entity: Entity,
    service: Project,
    *,
    expected_revision: int,
    reason: str = "",
) -> Request:
    """Sends a request to ``service``: linked into its project, with the records it sends,
    as its asking project's Editor. Raises :class:`PermissionDenied` when ``user`` may not,
    :class:`Refused` when it can't go, and the services' ``Stale`` for an old revision."""
    from iki_server.entities import access, services

    with transaction.atomic():
        locked = (
            Entity.objects.select_for_update(of=("self",))
            .select_related("owner_project", "owner_project__team")
            .get(pk=entity.pk)
        )
        entity_type = registry.types().get(locked.type)
        if locked.registry_id is not None or entity_type is None or spec_of(locked.type) is None:
            raise Refused(f"{locked.hrid} isn't a request: only a request type's records are sent.")
        asking = locked.owner_project
        assert asking is not None  # noqa: S101 - a record has a project
        if not permissions.can(user, Action.PROJECT_EDIT, asking):
            raise PermissionDenied(f"Sending it needs you as an Editor in {asking.name}.")
        if locked.revision != expected_revision:
            raise services.Stale(locked.revision)
        if locked.purged_at is not None:
            raise Refused(services.purged(locked))
        if locked.archived_at is not None:
            raise Refused(f"{locked.hrid} is archived: restore it before sending it.")
        why = services.frozen(shape(locked.type))
        if why:
            raise Refused(why)
        sent = Request.objects.select_related("service").filter(entity=locked).first()
        if sent is not None:
            raise Refused(
                f"{locked.hrid} was sent to {sent.service.name} already: a request goes to one "
                "service, for good."
            )
        label = entity_type.label_plural or entity_type.label
        if not accepts(service, asking, locked.type):
            raise NoSuchService(not_a_service(asking, label))
        if not asking.allow_sharing:
            raise Refused(
                f"{asking.name} doesn’t allow sharing its entities with other projects, so its "
                "requests can’t be sent out (ADR 0067)."
            )
        values = services.current_values(services.specific(locked))
        records = sent_records(locked, values)
        may = set(
            access.shareable_into(user, service).filter(pk__in=records).values_list("pk", flat=True)
        )
        refused = [r for r in records if r not in may]
        if refused:
            raise Refused(_unsendable(user, refused, service))
        request = Request.objects.create(
            entity=locked,
            service=service,
            submitted_by=user,
            agent=current_agent(),
            revision=locked.revision,
        )
        _link(user, locked, service, locked)
        for record in records:
            _link(user, Entity.objects.get(pk=record), service, locked)
        data = {
            "service": {"id": str(service.pk), "name": service.name},
            "asking_project": {"id": str(asking.pk), "name": asking.name},
            "revision": locked.revision,
            "sent": [str(r) for r in records],
            "reason": reason.strip(),
        }
        log.record("request.submitted", actor=user, target=locked, project=asking, data=data)
        log.record("request.received", actor=user, target=locked, project=service, data=data)
        # Linked into the service first, so its people can read what they're told of.
        from iki_server.notifications import declared

        declared.sent(locked, service, user, request.agent)
        # And the service's own automation hears of it, in its project (ADR 0232).
        from iki_server.runs import triggers

        triggers.received(locked, service, locked.revision)
    return request


def _unsendable(user: User, refused: list[uuid.UUID], service: Project) -> str:
    from iki_server.entities import access, services

    readable = {
        e.pk: e for e in access.visible(user).select_related("owner_project").filter(pk__in=refused)
    }
    first = readable.get(refused[0])
    if first is None:
        return "It names a record you can’t read any more: take it out of the request first."
    return f"{services.unshareable(user, first, service)} Take it out of the request first."


def _link(user: User, entity: Entity, project: Project, request: Entity) -> None:
    """Links ``entity`` into ``project`` for ``request``, audited. One already there stays
    as it was."""
    _, created = ProjectLink.objects.get_or_create(
        entity=entity, project=project, defaults={"linked_by": user, "via_request": request}
    )
    if created:
        log.record(
            "entity.linked",
            actor=user,
            target=entity,
            project=project,
            data={"request": str(request.pk), "request_hrid": request.hrid},
        )


def send(user: User, locked: Entity, values: Mapping[str, Any]) -> None:
    """After the asking project changes a sent request's values, sends what it names now
    and didn't before. Raises :class:`Invalid` for a record that can't go."""
    from iki_server.entities import access

    request = of(locked)
    if request is None:
        return
    records = sent_records(locked, values)
    there = set(
        ProjectLink.objects.filter(entity_id__in=records, project=request.service).values_list(
            "entity_id", flat=True
        )
    )
    new = [r for r in records if r not in there]
    if not new:
        return
    may = set(
        access.shareable_into(user, request.service).filter(pk__in=new).values_list("pk", flat=True)
    )
    refused = [r for r in new if r not in may]
    if refused:
        spec = spec_of(locked.type)
        assert spec is not None  # noqa: S101 - a request
        raise Invalid({spec.sends[0]: [_unsendable(user, refused, request.service)]})
    for record in new:
        _link(user, Entity.objects.get(pk=record), request.service, locked)


MATERIAL_REFUSAL = (
    "Choose a sample or tube on this request, or another requested material. "
    "You can also choose a recorded aliquot of that material or a sample drawn from either."
)


def materials_of(request: Entity, revision: int | None = None) -> set[uuid.UUID]:
    """The physical records commissioned by the requester, excluding references sent for context."""
    from iki_server.entities import services

    spec = spec_of(request.type)
    if spec is None or not spec.materials:
        return set()
    values = services.values_at(request, revision or request.revision)
    materials: set[uuid.UUID] = set()
    for name in spec.materials:
        value = values.get(name)
        if isinstance(value, list):
            materials.update(value)
        elif value is not None:
            materials.add(value)
    # Identity references can name a registry entry shared by several physical records.
    # Only a reference that actually identifies one project record establishes coverage.
    return set(
        Entity.objects.filter(pk__in=materials, registry__isnull=True).values_list("pk", flat=True)
    )


def covers(materials: set[uuid.UUID], material: uuid.UUID | None) -> bool:
    """Whether a result covers at least one of the commissioned materials."""
    return next(_covered_subjects(materials, material), None) is not None


def covered_subjects(entity: Entity, values: Mapping[str, Any]) -> list[str]:
    """Commissioned subjects this result covers, including through sample draws and aliquots."""
    result = result_spec(entity.type)
    if result is None or not result.material:
        return []
    material = values.get(result.material)
    reference = values.get(result.of)
    if material is None or reference is None:
        return []
    request = (
        Request.objects.select_related("entity")
        .filter(entity_id=reference, service_id=entity.owner_project_id)
        .first()
    )
    if request is None:
        return []
    return [str(subject) for subject in _covered_subjects(materials_of(request.entity), material)]


def _covered_subjects(materials: set[uuid.UUID], material: uuid.UUID | None) -> Iterator[uuid.UUID]:
    """The commissioned records reached through a sample's draw and its material's aliquot chain."""
    from iki_server.amounts.models import Movement, MovementKind
    from iki_server.entities.values import subtypes

    samples = subtypes("iki/samples.Sample")
    seen: set[uuid.UUID] = set()
    pending = [material] if materials and material is not None else []
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        if child in materials:
            yield child
        record = Entity.objects.filter(pk=child, registry__isnull=True).first()
        if record is not None and record.type in samples:
            # The draw debits entity_id. A tube's contents may also appear on it as source_id.
            pending.extend(
                Movement.objects.filter(used_in_id=child, kind=MovementKind.DRAWN).values_list(
                    "entity_id", flat=True
                )
            )
            continue
        parent = (
            Movement.objects.filter(entity_id=child, kind=MovementKind.ALIQUOT)
            .values_list("counterpart_id", flat=True)
            .first()
        )
        if parent is not None:
            pending.append(parent)
        elif record is not None:
            pending.extend(_declared_parents(record))


def _declared_parents(child: Entity) -> list[uuid.UUID]:
    """A registered aliquot's source, as declared by the source type's amount."""
    from iki_server.entities import services

    if child.type not in registry.types():
        return []
    kind = registry.types()[child.type]
    lineage = {t.qualified_name for t in [kind, *registry.ancestors(kind)]}
    # The declaration lives on the source type and names a field of the aliquot type.
    fields = shape(child.type).fields()
    names = {
        spec.aliquots_of
        for t in registry.types().values()
        if (spec := t.amount)
        and spec.aliquots in lineage
        and spec.aliquots_of
        and spec.aliquots_of in fields
        and fields[spec.aliquots_of].immutable
    }
    if not names:
        return []
    values = services.current_values_of([child])[child.pk]
    parents = Entity.objects.filter(pk__in=[values.get(n) for n in names], registry__isnull=True)
    return [
        parent.pk
        for parent in parents
        if (spec := amount_of(parent.type, registry.types()))
        and spec.aliquots in lineage
        and spec.aliquots_of in names
        and values.get(spec.aliquots_of) == parent.pk
    ]


def check_work_item(
    project: Project, type_name: str, given: Mapping[str, Any]
) -> dict[str, list[str]]:
    """The work item belongs to the project the request was sent to."""
    spec = work_item_spec(type_name)
    if spec is None:
        return {}
    reference = given.get(spec.of)
    if (
        isinstance(reference, (str, uuid.UUID))
        and Request.objects.filter(entity_id=reference, service=project).exists()
    ):
        return {}
    return {spec.of: [f"isn't a request sent to {project.name}: work items are the service's."]}


def _material_matches(request: Entity, material: uuid.UUID | None) -> bool:
    subjects = materials_of(request)
    return covers(subjects, material) if material is not None else not subjects


def _nearness(request_type: str, result_type: str) -> int | None:
    """How near ``request_type`` the type that ``result_type``'s request reference refers to
    is: 0 for the request's own type, 1 for its parent, and so on. None if its results can't
    answer one."""
    spec = result_spec(result_type)
    if spec is None or registry.types()[result_type].abstract:
        return None
    targets = shape(result_type).fields()[spec.of].to
    entity_type = registry.types()[request_type]
    lineage = [request_type, *(t.qualified_name for t in registry.ancestors(entity_type))]
    return next((i for i, name in enumerate(lineage) if name in targets), None)


def _a(label: str) -> str:
    return ("an " if label[:1] in "AEIOU" else "a ") + label


def answered_by(request_type: str) -> list[str]:
    """The result types whose results answer requests of ``request_type`` (ADR 0235): of
    those whose request reference can refer to one, the ones referring to the type nearest
    it. A service's result type that refines its request reference to the service's own
    request type is so the one that answers it, and its parent's isn't."""
    near = {name: n for name in result_types() if (n := _nearness(request_type, name)) is not None}
    nearest = min(near.values(), default=None)
    return sorted(name for name, n in near.items() if n == nearest)


def result_rows(request: Entity) -> Iterator[tuple[str, ResultSpec, QuerySet[Any]]]:
    """Each concrete type of result that can answer this request and all its recorded results."""
    from iki_server.entities.values import subtypes

    for name in result_types():
        spec = result_spec(name)
        if spec is None or registry.types()[name].abstract:
            continue
        form = shape(name)
        if any(request.type in subtypes(t) for t in form.fields()[spec.of].to):
            yield (
                name,
                spec,
                form.model._default_manager.filter(type=name, **{f"{spec.of}_id": request.pk}),
            )


def delivery_refusal(request: Entity) -> str | None:
    """Every result has been returned as its type requires, covering each expected subject."""
    from iki_server.entities import services
    from iki_server.entities.values import to_json
    from iki_spec import titles

    owner = request.owner_project_id
    if owner is None:
        return "Only a project request can be delivered."
    missing = materials_of(request)
    any_result = False
    pending = False
    for name, spec, rows in result_rows(request):
        release = shape(name).fields()[spec.released_field]
        signed_states = {
            state for state in spec.released_states if release.states[state].signatures
        }
        links = ProjectLink.objects.filter(
            entity_id=OuterRef("pk"), project_id=owner, via_request=request
        )
        # A plain release needs its recorded transition. A signed release also needs the
        # signatures on that transition, even when a previous revision was signed.
        last_transition = Revision.objects.filter(
            entity_id=OuterRef("pk"), state_field=spec.released_field
        ).order_by("-number")
        signatures = Subquery(last_transition.values("signatures")[:1])
        material_field = [spec.material] if spec.material else []
        for state, returned, evidence, *material in rows.values_list(
            spec.released_field, Exists(links), signatures, *material_field
        ).iterator():
            any_result = True
            if (
                state not in spec.released_states
                or not returned
                or evidence is None
                or (state in signed_states and not evidence)
            ):
                pending = True
            elif missing and material:
                missing.difference_update(_covered_subjects(missing, material[0]))
    if missing:
        records = list(Entity.objects.filter(pk__in=missing).order_by("hrid", "pk"))
        values = services.current_values_of(records)
        labels = []
        for record in records:
            fields_of_type = shape(record.type).fields()
            shown = {
                k: to_json(fields_of_type[k], v)
                for k, v in values[record.pk].items()
                if k in fields_of_type
            }
            template = registry.display_title(registry.types()[record.type])
            labels.append(titles.render(template, shown, record.hrid, fields_of_type))
        return " ".join(f"{label} has no returned result yet." for label in labels)
    if pending:
        return "Every recorded deliverable must be returned before completing this request."
    if not any_result:
        return "At least one deliverable must be returned before completing this request."
    return None


def check_result(
    project: Project, type_name: str, given: Mapping[str, Any], *, lock: bool = False
) -> dict[str, list[str]]:
    """A new result of a request must be made by the service the request was sent to."""
    spec = result_spec(type_name)
    if spec is None or given.get(spec.of) is None:
        return {}
    sent = Request.objects.filter(entity_id=given[spec.of]).select_related("entity")
    if lock:
        sent = sent.select_for_update(of=("entity",))
    found = sent.first()
    if found is not None and found.service_id == project.pk:
        from iki_server.entities import states

        request = spec_of(found.entity.type)
        if (
            request
            and request.completed_field
            and states.values_of(found.entity).get(request.completed_field)
            in request.completed_states
        ):
            return {spec.of: ["This request has already been delivered."]}
        answering = answered_by(found.entity.type)
        if type_name not in answering:
            types = registry.types()
            asked = _a(types[found.entity.type].label)
            labels = " or ".join(_a(types[name].label) for name in answering)
            return {spec.of: [f"is {asked}: record {labels} for it."]}
        if spec.material and not _material_matches(found.entity, given.get(spec.material)):
            return {spec.material: [MATERIAL_REFUSAL]}
        return {}
    return {
        spec.of: [
            f"isn't a request sent to {project.name}: a request's results are made by the "
            "service it was sent to."
        ]
    }


# --- Results ------------------------------------------------------------------------------------


def released(user: User | None, locked: Entity, field: str, state: str, revision: Revision) -> None:
    """A result the service just set to a released state, in ``revision``, goes back to the
    asking project: linked into it for its request, audited, for good, and its people told
    as the request's type says (ADR 0142). Raises :class:`Refused` if the service doesn't
    share its entities (ADR 0067)."""
    from iki_server.entities import services

    spec = result_spec(locked.type)
    if spec is None or field != spec.released_field or state not in spec.released_states:
        return
    values = services.current_values(services.specific(locked))
    value = values.get(spec.of)
    if value is None:
        return
    request = (
        Request.objects.select_related("entity__owner_project", "service")
        .select_for_update(of=("entity",))
        .filter(entity_id=value, service_id=locked.owner_project_id)
        .first()
    )
    if request is None:
        return
    asking = request.entity.owner_project
    assert asking is not None  # noqa: S101 - a request is a record
    if spec.material and not _material_matches(request.entity, values.get(spec.material)):
        raise Refused(MATERIAL_REFUSAL)
    there = ProjectLink.objects.select_for_update().filter(entity=locked, project=asking).first()
    if there is not None and there.via_request_id == request.entity_id:
        return  # Released already: a release is told once.
    if not request.service.allow_sharing:
        raise Refused(
            f"{request.service.name} doesn’t allow sharing its entities with other projects, "
            f"so {locked.hrid} can’t go back to {asking.name} (ADR 0067)."
        )
    data = {"request": str(request.entity_id), "request_hrid": request.entity.hrid}
    if there is None:
        ProjectLink.objects.create(
            entity=locked, project=asking, linked_by=user, via_request=request.entity
        )
        log.record("entity.linked", actor=user, target=locked, project=asking, data=data)
    else:
        # Already there some other way: released now, it stays as released results must.
        there.via_request = request.entity
        there.save(update_fields=["via_request"])
    reviewed_revision = None
    signed_revision = None
    if revision.signatures:
        from iki_server.signatures.models import Signature
        from iki_server.signatures.services import parsed

        signed = Signature.objects.get(pk=revision.signatures[-1])
        signed_revision = signed.revision
        if reviewed := parsed(signed).get("reviewed"):
            reviewed_revision = reviewed["revision"]
            Entity.objects.filter(pk=locked.pk).update(released_review_revision=reviewed_revision)
    log.record(
        "request.result_released",
        actor=user,
        target=locked,
        project=asking,
        data={
            **data,
            "service": {"id": str(request.service_id), "name": request.service.name},
            "reviewed_revision": reviewed_revision,
            "signed_revision": signed_revision,
            "released_revision": revision.number,
        },
    )
    from iki_server.notifications import declared

    declared.released(request.entity, locked, revision, user)


def why_kept(link: ProjectLink) -> str | None:
    """Why a link a request made can't be taken out, or None if it can: the request in its
    service, what it still sends, and the results released back."""
    if link.via_request_id is None:
        return None
    if link.entity_id == link.via_request_id:
        return (
            "It was sent there as a request: it stays, as the service’s record of what it was "
            "asked. Its type’s states say when it’s withdrawn or declined."
        )
    request = Request.objects.filter(entity_id=link.via_request_id).select_related("entity").first()
    if request is None:
        return None
    if link.project_id != request.service_id:
        return f"It was released for {request.entity.hrid}: it stays with the request’s results."
    from iki_server.entities import services

    values = services.current_values(services.specific(request.entity))
    if link.entity_id in sent_records(request.entity, values):
        return (
            f"It goes with {request.entity.hrid}: take it out of the request first, and then "
            "out of the service’s project."
        )
    return None


# --- What the API shows -------------------------------------------------------------------------


def summaries(user: Any, entities: Sequence[Entity]) -> dict[Any, dict[str, Any]]:  # noqa: ANN401
    """For each request among ``entities``, what its page shows of its two sides: who it's
    from and to, when it was sent, whose each field is, and which side ``user`` writes as."""
    wanted = [e for e in entities if e.registry_id is None and spec_of(e.type) is not None]
    if not wanted:
        return {}
    sent = {
        r.entity_id: r
        for r in Request.objects.select_related("service", "submitted_by").filter(entity__in=wanted)
    }
    owners = {p.pk: p for p in Project.objects.filter(pk__in={e.owner_project_id for e in wanted})}
    out: dict[Any, dict[str, Any]] = {}
    for entity in wanted:
        spec = spec_of(entity.type)
        assert spec is not None  # noqa: S101 - wanted
        request = sent.get(entity.pk)
        owner = owners.get(entity.owner_project_id) if entity.owner_project_id else None
        mine = sides(user, entity, request) if owner is not None else frozenset()
        out[entity.pk] = {
            "asking_project": _project(owner),
            "service": _project(request.service) if request is not None else None,
            "submitted": request is not None,
            "submitted_at": request.submitted_at if request is not None else None,
            "submitted_by": request.submitted_by if request is not None else None,
            "service_fields": list(spec.service_fields),
            "sends": list(spec.sends),
            "yours": sorted(mine),
        }
    return out


def _project(project: Project | None) -> dict[str, Any] | None:
    if project is None:
        return None
    return {"id": project.pk, "name": project.name, "slug": project.slug}


def run_sides(entities: Sequence[Entity]) -> dict[Any, dict[str, Any]]:
    """For each request among ``entities``, its two sides as a run reads them (ADR 0232):
    the project that asked and the service it was sent to (None until it's sent), by ID,
    slug and name, which each side's people see of the other already (ADR 0132)."""
    wanted = [e for e in entities if e.registry_id is None and spec_of(e.type) is not None]
    if not wanted:
        return {}
    sent = dict(Request.objects.filter(entity__in=wanted).values_list("entity_id", "service_id"))
    projects = {
        p.pk: p
        for p in Project.objects.filter(
            pk__in={*sent.values(), *(e.owner_project_id for e in wanted)}
        )
    }

    def side(pk: Any) -> dict[str, Any] | None:  # noqa: ANN401 - a project's ID
        found = _project(projects.get(pk))
        return {**found, "id": str(found["id"])} if found is not None else None

    return {
        e.pk: {"asking_project": side(e.owner_project_id), "service": side(sent.get(e.pk))}
        for e in wanted
    }


# --- The database's guard -----------------------------------------------------------------------


@dataclass(frozen=True)
class Guarded:
    """A table holding a request type's values: whose columns are the service's."""

    table: str
    #: Its row's columns the service writes; the rest are the asking project's.
    service_columns: tuple[str, ...]
    #: For a list's table, one field's items: whose they are. "" for a table of fields.
    whole: str = ""


def guarded_tables() -> list[Guarded]:
    """Every table holding values of a request type's records."""
    out: dict[str, Guarded] = {}
    for name in request_types():
        if registry.types()[name].abstract:
            continue
        service = service_fields(name)
        for part in shape(name).parts:
            columns = tuple(
                sorted(
                    str(getattr(part.model._meta.get_field(n), "column", n))
                    for n, f in part.fields.items()
                    if n in service and not f.removed and not stored_in_list_table(f)
                )
            )
            table = part.model._meta.db_table
            if table not in out or columns:
                out[table] = Guarded(table, columns)
            for n, f in part.fields.items():
                if not f.removed and stored_in_list_table(f):
                    listed = part.list_model(n)._meta.db_table
                    out[listed] = Guarded(listed, (), SERVICE if n in service else ASKER)
    return sorted(out.values(), key=lambda g: g.table)


def install_guards(cursor: Any, wanted: Iterable[Guarded]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Puts the guard on each table, and takes it off tables that no longer need it."""
    wanted = list(wanted)
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    needed = {g.table: _trigger(g.table) for g in wanted}
    for table, name in cursor.fetchall():
        if needed.get(table) != name:
            cursor.execute(
                sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
            )
    for g in wanted:
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION iki_request_guard({columns}, {whole})"
            ).format(
                name=sql.Identifier(needed[g.table]),
                table=sql.Identifier(g.table),
                columns=sql.Literal("{" + ",".join(g.service_columns) + "}"),
                whole=sql.Literal(g.whole),
            )
        )
    return sorted(needed)


def _trigger(table: str) -> str:
    return (TRIGGER_PREFIX + table)[:63]
