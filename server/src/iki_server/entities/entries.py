"""Registry entries (ADR 0115): curating them, superseding them, and moving records.

A registry entry is an identity in a registry, with its ID, what's derived
from it, and its facts. Projects hold records of it (:mod:`.services`); this
is what's done to the entry itself, and to the records that name one:

- **Curating facts.** A holder sets a fact while it's empty. After that only
  the registry's registrars change it, with a reason, audited
  (``registry.facts_changed``) and told to the holders: who, when and why.
- **Superseding.** A registrar replaces a mistaken entry (CPD-000123, drawn
  with the wrong stereochemistry) with another of the same type in the same
  registry (CPD-000456), with a reason. It can't be undone. The superseded
  entry is kept, marked, and can't be registered again; the people who
  added each holding project's record, and those projects' Admins, are told.
- **Moving.** A holder's Editor moves the project's record from the
  superseded entry to its replacement: the record keeps its data, history
  and runs, and the project's own references to the old entry are re-pointed
  to the new one, each a revision with the reason.
- **Adding a record** of an entry the project doesn't hold, by its ID: as
  registering its identity there.

What a registrar sees of an entry is the entry: its values, its history, and
how many projects hold it, never which, nor their records.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import access, registry, services, unique
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.values import Invalid, shape, to_db, to_json
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Registry
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

__all__ = [
    "Supersession",
    "add_record",
    "announce",
    "curate",
    "holders",
    "move",
    "registrar_entries",
    "supersede",
    "supersession",
]


@dataclass(frozen=True)
class Supersession:
    replacement: Entity
    reason: str
    at: dt.datetime
    by: User | None


def supersession(entry: Entity) -> Supersession | None:
    """What replaced a superseded entry, and why."""
    if entry.superseded_by_id is None or entry.superseded_at is None:
        return None
    revision = (
        Revision.objects.filter(entity_id=entry.pk, kind=RevisionKind.SUPERSEDED)
        .select_related("user")
        .order_by("-number")
        .first()
    )
    return Supersession(
        replacement=Entity.objects.get(pk=entry.superseded_by_id),
        reason=revision.reason if revision is not None else "",
        at=entry.superseded_at,
        by=revision.user if revision is not None else None,
    )


def holders(entry: Entity) -> QuerySet[Project]:
    """The projects with a record of the entry, their own (not one shared with them)."""
    records = Entity.objects.filter(entry=entry, owner_project=OuterRef("pk"))
    return Project.objects.filter(Exists(records))


def registrar_entries(registry_: Registry) -> QuerySet[Entity]:
    """Every entry in the registry, as its registrars see them: with how many projects hold
    each (``holders``), and nothing of their records."""
    return (
        Entity.objects.filter(registry=registry_)
        .annotate(holders=Count("records", filter=Q(records__purged_at__isnull=True)))
        .order_by("hrid")
    )


# --- Facts --------------------------------------------------------------------------------------


def curate(
    user: User,
    entry: Entity,
    values: Mapping[str, Any],
    *,
    reason: str,
    expected_revision: int,
) -> Revision | None:
    """A registrar's change to an entry's facts: set, changed or cleared, with why. Their
    identity and derived values are the identity's, and never change."""
    _registrar(user, entry)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: a fact's change is every holder's."]})
    with transaction.atomic(), services._unique_values():
        locked = Entity.objects.select_for_update().get(pk=entry.pk)
        if locked.revision != expected_revision:
            raise services.Stale(locked.revision)
        if locked.superseded_by_id is not None:
            raise Refused(f"{locked.hrid} was superseded: change the entry that replaced it.")
        if locked.purged_at is not None:
            raise Refused(services.purged(locked))
        form = shape(locked.type)
        why = services.frozen(form)
        if why:
            raise Refused(why)
        kept = services._kept(form)
        fields = form.entry_fields()
        rows = services.Rows(form, None, services.specific(locked))
        refs = _registry_refs(locked)
        errors: dict[str, list[str]] = {}
        new: dict[str, Any] = {}
        for name, value in values.items():
            f = fields.get(name)
            if f is None:
                errors[name] = [f"{locked.type}'s entries have no field `{name}`."]
                continue
            if name in kept:
                errors[name] = [kept[name]]
                continue
            if f.registry != "fact":
                errors[name] = [
                    "is its identity's, which registrars don't change: supersede the entry "
                    "with the right one instead."
                ]
                continue
            try:
                converted = to_db(f, value, refs)
            except ValueError as error:
                errors[name] = [str(error)]
                continue
            if converted != rows.get(name):
                new[name] = converted
        if not errors and new:
            errors = _taken_in_registry(form, locked, new)
        if errors:
            raise Invalid(errors)
        if not new:
            return None
        before = {n: to_json(fields[n], rows.get(n)) for n in new}
        touched = services._write(form, rows, new, locked.pk)
        revision = services._revise(locked, RevisionKind.CURATED, sorted(new), user, reason)
        services._write_history(touched, rows, revision.number)
        services._use_files(fields, locked.pk, new, revision.number)
        log.record(
            "registry.facts_changed",
            actor=user,
            target=locked,
            data={
                "registry": str(locked.registry_id),
                "reason": reason.strip(),
                "changes": {
                    n: {"before": before[n], "after": to_json(fields[n], v)} for n, v in new.items()
                },
            },
        )
    return revision


def _registrar(user: User, entry: Entity) -> Registry:
    if entry.registry_id is None:
        raise Refused(f"{entry.hrid} is a project's record, not a registry entry.")
    found = Registry.objects.get(pk=entry.registry_id)
    if not permissions.can(user, Action.REGISTRY_CURATE, found):
        raise PermissionDenied(f"Only {found.name}'s registrars curate its entries.")
    return found


def _registry_refs(entry: Entity) -> Any:  # noqa: ANN401 - Refs
    """What a registrar's facts may refer to: the registry's own entries."""
    from iki_server.entities.values import Refs

    assert entry.registry_id is not None  # noqa: S101 - it's an entry
    entries = Entity.objects.filter(registry_id=entry.registry_id)
    return Refs(Entity.objects.none(), None, entries=entries, project="the registry")


def _taken_in_registry(form: Any, entry: Entity, new: Mapping[str, Any]) -> dict[str, list[str]]:  # noqa: ANN401 - a Shape
    """A registrar sees every entry: each fact another has already, with which."""
    from iki_server.entities.models import UniqueValue

    errors: dict[str, list[str]] = {}
    registry_id = entry.registry_id
    assert registry_id is not None  # noqa: S101 - it's an entry
    for part in form.parts:
        for name, f in part.fields.items():
            value = new.get(name)
            if not part.entry or not f.unique or value is None:
                continue
            found = (
                UniqueValue.objects.filter(
                    registry_id=registry_id,
                    key=unique.key(part.model, name),
                    value=str(value),
                )
                .exclude(entity_id=entry.pk)
                .values_list("entity__hrid", flat=True)
                .first()
            )
            if found is not None:
                errors[name] = [f"is {found}'s already: no two entries in the registry share it."]
    return errors


# --- Superseding --------------------------------------------------------------------------------


def supersede(user: User, entry: Entity, replacement: Entity, *, reason: str) -> Revision:
    """Replaces a mistaken entry with another, for good, and tells its holders."""
    found = _registrar(user, entry)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: its holders are told."]})
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=entry.pk)
        other = Entity.objects.select_for_update().get(pk=replacement.pk)
        if locked.superseded_by_id is not None:
            raise Refused(f"{locked.hrid} was superseded already.")
        if other.pk == locked.pk:
            raise Invalid({"replacement": ["An entry can't replace itself."]})
        if other.registry_id != locked.registry_id:
            raise Invalid({"replacement": [f"must be an entry in {found.name}."]})
        if other.type != locked.type:
            raise Invalid({"replacement": [f"must be a {locked.type}, as {locked.hrid} is."]})
        if other.superseded_by_id is not None:
            raise Invalid({"replacement": [f"{other.hrid} was superseded itself."]})
        if services.frozen(shape(locked.type)) is not None:
            raise Refused(services.frozen(shape(locked.type)) or "")
        locked.superseded_by = other
        locked.superseded_at = dt.datetime.now(dt.UTC)
        locked.save(update_fields=["superseded_by", "superseded_at"])
        revision = services._revise(locked, RevisionKind.SUPERSEDED, [], user, reason)
        log.record(
            "entry.superseded",
            actor=user,
            target=locked,
            data={
                "registry": str(found.pk),
                "replacement": {"id": str(other.pk), "hrid": other.hrid},
                "reason": reason.strip(),
            },
        )
        _tell_holders(user, locked, other, reason.strip())
    return revision


def _tell_holders(user: User, entry: Entity, replacement: Entity, reason: str) -> None:
    """Each holding project's Admins, and whoever added its record, by notification."""
    from iki_server.notifications import kinds
    from iki_server.notifications.services import notify

    records = Entity.objects.filter(entry=entry, purged_at__isnull=True).select_related(
        "owner_project__team", "created_by"
    )
    for record in records:
        project = record.owner_project
        assert project is not None  # noqa: S101 - records have projects
        admins = permissions.members_with(project, ProjectRole.ADMIN)
        people = {u.pk: u for u in admins}
        if record.created_by is not None:
            people.setdefault(record.created_by.pk, record.created_by)
        notify(
            people.values(),
            kinds.ENTRY_SUPERSEDED,
            title=f"{entry.hrid} was superseded by {replacement.hrid}",
            body=f"{reason}\n\nMove {project.name}'s record to {replacement.hrid} to keep "
            "its data with the right entry.",
            path=f"/entities/{record.pk}",
            project=project,
            actor=user,
        )


# --- Moving -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Moved:
    record: Any
    revision: Revision
    #: The project's records whose references to the old entry now name the new one.
    repointed: list[Entity]


def move(user: User, record: Entity, *, reason: str = "") -> Moved:
    """Moves a project's record from its superseded entry to the one that replaced it."""
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=record.pk)
        if locked.entry_id is None:
            raise Refused(f"{locked.hrid} isn't a record of a registry entry.")
        old = Entity.objects.get(pk=locked.entry_id)
        if old.superseded_by_id is None:
            raise Refused(f"{old.hrid} wasn't superseded: there's nothing to move it to.")
        if locked.archived_at is not None:
            raise Refused("The record is archived: restore it before moving it.")
        why = services.frozen(shape(locked.type))
        if why:
            raise Refused(why)
        new = Entity.objects.get(pk=old.superseded_by_id)
        project = services.owner_of(locked)
        if Entity.objects.filter(owner_project=project, entry=new).exists():
            raise Refused(
                f"{project.name} has a record of {new.hrid} already: archive this one, or "
                "move its data there by hand."
            )
        said = reason.strip() or f"{old.hrid} was superseded by {new.hrid}."
        Entity.objects.filter(pk=locked.pk).update(entry=new, hrid=new.hrid)
        revision = services._revise(locked, RevisionKind.MOVED, [], user, said, moved_from=old.pk)
        repointed = _repoint(user, project, old, new, said)
        log.record(
            "entity.moved",
            actor=user,
            target=locked,
            project=project,
            data={
                "from": {"id": str(old.pk), "hrid": old.hrid},
                "to": {"id": str(new.pk), "hrid": new.hrid},
                "repointed": [e.hrid for e in repointed],
                "reason": said,
            },
        )
    return Moved(services.specific(Entity.objects.get(pk=locked.pk)), revision, repointed)


def _repoint(user: User, project: Project, old: Entity, new: Entity, reason: str) -> list[Entity]:
    """The project's own records that refer to ``old``, referring to ``new`` instead, each
    a revision: immutable references too, as the entry they named was a mistake. Archived
    records keep theirs, as do those of a detached plugin's types."""
    done: list[Entity] = []
    own = Entity.objects.filter(
        owner_project=project, archived_at__isnull=True, purged_at__isnull=True
    )
    for qualified in sorted(registry.types()):
        entity_type = registry.types()[qualified]
        if entity_type.abstract or qualified == "iki/core.Entity":
            continue
        form = shape(qualified)
        if services.frozen(form) is not None:
            continue
        for part in form.parts:
            if part.entry:
                continue
            for name, f in part.fields.items():
                if f.removed:
                    continue
                kind = f.items.type if f.type == "list" and f.items is not None else f.type
                if kind != "ref":
                    continue
                ids = _referring(part, f, name, old.pk) & set(
                    own.filter(type=qualified).values_list("pk", flat=True)
                )
                for pk in sorted(ids, key=str):
                    _repoint_one(user, form, part, f, name, pk, old.pk, new.pk, reason)
                    done.append(Entity.objects.get(pk=pk))
    return sorted({e.pk: e for e in done}.values(), key=lambda e: e.hrid)


def _referring(part: Any, f: Any, name: str, target: Any) -> set[Any]:  # noqa: ANN401
    if f.type == "list":
        rows = part.list_model(name)._default_manager.filter(item_id=target)
        return set(rows.values_list("entity_id", flat=True))
    return set(
        part.model._default_manager.filter(**{f"{name}_id": target}).values_list(
            "pk" if not part.extension else "entity_id", flat=True
        )
    )


def _repoint_one(
    user: User,
    form: Any,  # noqa: ANN401 - a Shape
    part: Any,  # noqa: ANN401 - a Part
    f: Any,  # noqa: ANN401 - a Field
    name: str,
    pk: Any,  # noqa: ANN401
    old: Any,  # noqa: ANN401
    new: Any,  # noqa: ANN401
    reason: str,
) -> None:
    locked = Entity.objects.select_for_update().get(pk=pk)
    rows = services.Rows.of(services.specific(locked))
    current = rows.value(part, f)
    value = [new if item == old else item for item in current] if f.type == "list" else new
    touched = services._write(form, rows, {name: value}, locked.pk)
    revision = services._revise(locked, RevisionKind.EDITED, [name], user, reason)
    services._write_history(touched, rows, revision.number)


# --- Adding a record ----------------------------------------------------------------------------


def add_record(
    user: User,
    entry: Entity,
    project: Project,
    values: Mapping[str, Any] | None = None,
    *,
    reason: str = "",
) -> services.Saved:
    """The project's record of an entry it doesn't hold, by the entry's ID: as registering
    its identity there. The user must read the entry through a project that may share
    with this one (ADR 0067), in the same registry."""
    if entry.registry_id is None:
        raise Refused(f"{entry.hrid} is a project's record, not a registry entry.")
    if entry.registry_id != project.registry_id:
        raise Refused(
            f"{entry.hrid} is in another registry from {project.name}, and registries share "
            "nothing (ADR 0115)."
        )
    usable = access.shareable_into(user, project).filter(entry=entry)
    if not usable.exists():
        raise Refused(
            f"You can't add {entry.hrid} to {project.name}: you read it only in projects that "
            "don't allow sharing with other projects."
        )
    form = shape(entry.type)
    owner = registry.identity_owner(form.entity_type)
    assert owner is not None  # noqa: S101 - entries have an identity
    current = services.current_values(services.specific(entry))
    fields = form.fields()
    identity = {n: to_json(fields[n], current.get(n)) for n in owner.identity}
    given = {**(values or {}), **{n: v for n, v in identity.items() if v is not None}}
    return services.create(user, entry.type, project, given, reason=reason)


# --- Telling holders ----------------------------------------------------------------------------


def announce(entry_id: Any, revision: Revision, user: User | None) -> None:  # noqa: ANN401 - a UUID
    """Tells the holding projects' webhooks, and the organization's once, of a change to an
    entry: ids only, and who made it only for a registrar's change."""
    from iki_server.webhooks import events
    from iki_server.webhooks.services import emit_to

    entry = Entity.objects.get(pk=entry_id)
    event = "entry.superseded" if revision.kind == RevisionKind.SUPERSEDED else "entry.updated"
    public = revision.kind in (RevisionKind.CURATED, RevisionKind.SUPERSEDED)
    data: dict[str, Any] = {
        "entry": events.entry_data(entry),
        "revision": {
            "number": revision.number,
            "kind": revision.kind,
            "changed_fields": revision.changed_fields,
            "reason": revision.reason if public else "",
        },
        "actor": events.actor_data(user) if public else None,
    }
    if entry.superseded_by_id is not None:
        replacement = Entity.objects.get(pk=entry.superseded_by_id)
        data["replacement"] = {"id": str(replacement.pk), "hrid": replacement.hrid}
    emit_to(event, data, projects=list(holders(entry)))


def held_through(user: Any, entry: Entity) -> QuerySet[Project]:  # noqa: ANN401
    """The projects the user reads the entry through: those with a record of it, their own
    or shared with them, that the user can read."""
    projects = access.readable_projects(user)
    records = Entity.objects.filter(entry=entry, purged_at__isnull=True)
    links = ProjectLink.objects.filter(project=OuterRef("pk"), entity__in=records)
    return projects.filter(Exists(links))


def is_member(user: Any, project: Project) -> bool:  # noqa: ANN401
    return ProjectMembership.objects.filter(project=project, user=user).exists()
