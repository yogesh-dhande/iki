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
- **Registering as another type** (ADR 0165). Types under one identity share
  entries (a protein complex and an antibody of the same chains are one), so an
  entry registered as the wrong one is superseded by a new entry of its
  identity as the right one, which it hands its identity to: registering that
  identity finds the new entry from then on. Or by an entry of another
  identity of that kind, as any mistaken entry is.
- **Moving.** A holder's Editor moves the project's record from the
  superseded entry to its replacement: the record keeps its data, history
  and runs, and the project's own references to the old entry are re-pointed
  to the new one, each a revision with the reason. A record can't change its
  type, so a move to an entry of another type makes the project's record of
  it, with the values the two types' records share, and archives the old one,
  which keeps its history, signatures and runs.
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
from iki_server.core.ids import uuid7
from iki_server.entities import (
    access,
    checking,
    identity,
    registry,
    requests,
    services,
    states,
    unique,
)
from iki_server.entities.models import (
    Entity,
    ProjectLink,
    Revision,
    RevisionKind,
    UniqueValue,
)
from iki_server.entities.values import (
    Invalid,
    Shape,
    ids_in,
    problems_of,
    referable,
    row_columns,
    shape,
    to_db,
    to_json,
)
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectRole, Registry
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_spec.model import LIST_LABEL, LIST_POSITION

__all__ = [
    "Supersession",
    "add_record",
    "announce",
    "carried",
    "curate",
    "holders",
    "move",
    "registrable_as",
    "registrar_entries",
    "supersede",
    "supersede_as",
    "supersession",
    "to_give",
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
    identity and derived values are the identity's, and never change. A lab list's option
    (ADR 0270) is its list's curators' too: its required values can't be cleared, its
    place changes by reordering the list, and its label names no other option of the list,
    under the list's lock (:mod:`.lists`)."""
    from iki_server.entities import lists

    _registrar(user, entry)
    listed = _is_list(entry.type)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: a fact's change is every holder's."]})
    with transaction.atomic(), services._unique_values():
        if listed:
            lists.lock(entry.registry_id, entry.type)
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
            if f.immutable:
                errors[name] = [services.fixed(locked)]
                continue
            if listed and name == LIST_POSITION:
                errors[name] = ["is its place in the list: reorder the list to move it."]
                continue
            try:
                converted = to_db(f, value, refs)
            except ValueError as error:
                errors.update(problems_of(name, error))
                continue
            if listed and f.required and converted is None:
                errors[name] = ["is required."]
                continue
            if converted != rows.get(name):
                new[name] = converted
        if not errors and new:
            errors = _taken_in_registry(form, locked, new)
        if not errors and listed and isinstance(new.get(LIST_LABEL), str):
            errors = lists.names_taken(
                form, locked.registry_id, label=new[LIST_LABEL], besides=locked.pk
            )
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
            "list.option_changed" if listed else "registry.facts_changed",
            actor=user,
            target=locked,
            data={
                "registry": str(locked.registry_id),
                **({"list": locked.type} if listed else {}),
                "reason": reason.strip(),
                "changes": {
                    n: {"before": before[n], "after": to_json(fields[n], v)} for n, v in new.items()
                },
            },
        )
    return revision


def _registrar(user: User, entry: Entity) -> Registry:
    """The entry's registry, if the user curates the entry: one of its registrars, or for a
    lab list's option, one of the list's curators (ADR 0270)."""
    if entry.registry_id is None:
        raise Refused(f"{entry.hrid} is a project's record, not a registry entry.")
    found = Registry.objects.get(pk=entry.registry_id)
    if _is_list(entry.type):
        if not permissions.can_curate_list(user, found, entry.type):
            label = registry.types()[entry.type].label_plural
            raise PermissionDenied(
                f"Only {found.name}'s registrars, and the curators of {label}, curate its options."
            )
        return found
    if not permissions.can(user, Action.REGISTRY_CURATE, found):
        raise PermissionDenied(f"Only {found.name}'s registrars curate its entries.")
    return found


def _is_list(type_name: str) -> bool:
    found = registry.types().get(type_name)
    return found is not None and found.is_list


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
    """Replaces a mistaken entry with another, for good, and tells its holders. A lab list's
    option (ADR 0270) is replaced, under the list's lock, by one its dropdowns offer."""
    from iki_server.entities import lists

    found = _registrar(user, entry)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: its holders are told."]})
    listed = _is_list(entry.type)
    with transaction.atomic():
        if listed:
            lists.lock(entry.registry_id, entry.type)
        locked = Entity.objects.select_for_update().get(pk=entry.pk)
        other = Entity.objects.select_for_update().get(pk=replacement.pk)
        if locked.superseded_by_id is not None:
            raise Refused(f"{locked.hrid} was superseded already.")
        if other.pk == locked.pk:
            raise Invalid({"replacement": ["An entry can't replace itself."]})
        if other.registry_id != locked.registry_id:
            raise Invalid({"replacement": [f"must be an entry in {found.name}."]})
        if unlike := _unlike(locked, other.type):
            raise Invalid({"replacement": [unlike]})
        if other.superseded_by_id is not None:
            raise Invalid({"replacement": [f"{other.hrid} was superseded itself."]})
        if listed and other.archived_at is not None:
            raise Invalid(
                {
                    "replacement": [
                        f"`{lists.option_of(other).code}` is archived: restore it first, so "
                        "dropdowns offer it instead."
                    ]
                }
            )
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
        if listed:
            _tell_referrers(user, locked, other, reason.strip())
        else:
            _tell_holders(user, locked, other, reason.strip())
    return revision


def _unlike(entry: Entity, type_name: str) -> str | None:
    """Why an entry of ``type_name`` can't replace ``entry``, or None if it can: one of a
    type with its identity (ADR 0165), as a protein complex's entry may be an antibody's.
    A lab list's option is replaced by another option of its list."""
    if type_name == entry.type:
        return None
    every = registry.types()
    owner = registry.identity_owner(every[entry.type])
    other = every.get(type_name)
    if not _is_list(entry.type) and owner is not None and other is not None:
        theirs = registry.identity_owner(other)
        if theirs is not None and theirs.qualified_name == owner.qualified_name:
            return None
    kind = entry.type if _is_list(entry.type) or owner is None else owner.qualified_name
    return f"must be a {kind}, as {entry.hrid} is."


def registrable_as(entry: Entity) -> list[str]:
    """The other types ``entry``'s identity can be registered as (ADR 0165), which a
    registrar may supersede it with a new entry of: those under the type its identity is,
    with entities of their own, that can take new ones."""
    every = registry.types()
    owner = registry.identity_owner(every[entry.type])
    if owner is None or _is_list(entry.type):
        return []
    return sorted(
        name
        for name, t in every.items()
        if name != entry.type
        and not t.abstract
        and not t.is_list
        and (theirs := registry.identity_owner(t)) is not None
        and theirs.qualified_name == owner.qualified_name
        and services.frozen(shape(name)) is None
    )


def to_give(entry_type: str, type_name: str) -> list[str]:
    """The registry fields a registrar gives a new entry of an ``entry_type``'s identity
    as ``type_name`` (ADR 0165): those the superseded entry's values don't fill as they
    are, outside the tables the two types share, or refined by the new type (its parts'
    roles, ADR 0164). Not those only actions compute."""
    was, form = shape(entry_type), shape(type_name)
    theirs = {p.name: p for p in was.parts if p.entry}
    kept = services._kept(form)
    return [
        name
        for part in form.parts
        if part.entry
        for name, f in part.fields.items()
        if not f.removed
        and not f.computed
        and name not in kept
        and (part.name not in theirs or theirs[part.name].fields.get(name) != f)
    ]


def supersede_as(
    user: User,
    entry: Entity,
    type_name: str,
    values: Mapping[str, Any] | None = None,
    *,
    reason: str,
) -> Entity:
    """Supersedes an entry registered as the wrong type with a new entry of its identity
    as ``type_name``, one under the same identity (ADR 0165), with a reason, for good, and
    tells its holders. The new entry has the superseded one's values for the fields the two
    types share, and ``values`` for the rest, or in place of them: its own facts, and its
    parts' roles as its type names them, say. Its identity is the same, and is the new
    entry's from now on: registering it finds the new entry. Returns the new entry."""
    found = _registrar(user, entry)
    said = reason.strip()
    if not said:
        raise Invalid({"reason": ["Say why: its holders are told."]})
    if entry.superseded_by_id is not None or entry.identity_hash is None:
        raise Refused(f"{entry.hrid} was superseded already.")
    with transaction.atomic(), services._unique_values():
        # The identity's lock first, then the entry's, as registering it takes them: a
        # registration of it waits, then finds the new entry.
        services.lock_identity(registry.types()[entry.type], entry.registry_id, entry.identity_hash)
        locked = Entity.objects.select_for_update().get(pk=entry.pk)
        if locked.superseded_by_id is not None:
            raise Refused(f"{locked.hrid} was superseded already.")
        if locked.purged_at is not None:
            raise Refused(services.purged(locked))
        why = services.frozen(shape(locked.type))
        if why:
            raise Refused(why)
        form = _registrable(locked, type_name)
        given, carried_unique = _registered_as(user, locked, form, values or {})
        if services.identity_digest(form, given) != locked.identity_hash:
            raise Invalid(
                {
                    name: [
                        f"must be {locked.hrid}'s: an entry registered as another type has "
                        "the same identity."
                    ]
                    for name in identity.identity_of(form.entity_type)
                }
            )
        errors = _taken_in_registry(form, locked, given)
        if errors:
            raise Invalid(errors)
        # Its identity, and the unique facts that come with what the types share, are the
        # new entry's from now on. Its values stay, as its history and page show them.
        new_id = uuid7()
        UniqueValue.objects.filter(entity_id=locked.pk, key__in=carried_unique).delete()
        Entity.objects.filter(pk=locked.pk).update(
            identity_hash=None, superseded_by_id=new_id, superseded_at=dt.datetime.now(dt.UTC)
        )
        new, _ = services.register_entry(
            user, form, locked.registry_id, given, said, pk=new_id, moved_from=locked.pk
        )
        services._revise(locked, RevisionKind.SUPERSEDED, [], user, said)
        log.record(
            "entry.superseded",
            actor=user,
            target=locked,
            data={
                "registry": str(found.pk),
                "replacement": {"id": str(new.pk), "hrid": new.hrid, "type": new.type},
                "registered": True,
                "reason": said,
            },
        )
        _tell_holders(user, locked, new, said)
    return new


def _registrable(entry: Entity, type_name: str) -> Shape:
    """The shape of the type ``entry`` is being registered as instead (ADR 0165)."""
    target = registry.types().get(type_name)
    if target is None or type_name == "iki/core.Entity":
        raise Invalid({"type": [f"There's no entity type {type_name}."]})
    if type_name == entry.type:
        raise Invalid({"type": [f"{entry.hrid} is a {type_name} already."]})
    # A lab list's options are its list's (ADR 0270): one is added, and superseded, on it.
    if target.is_list or registry.types()[entry.type].is_list:
        raise Invalid(
            {
                "type": [
                    "A lab list's option isn't registered as another type, nor another "
                    "type's entry as one: add or supersede it on its list."
                ]
            }
        )
    if unlike := _unlike(entry, type_name):
        raise Invalid({"type": [unlike]})
    if target.abstract:
        raise Invalid({"type": [f"{type_name} is abstract: choose one of its subtypes."]})
    form = shape(type_name)
    why = services.frozen(form)
    if why:
        raise Invalid({"type": [why]})
    return form


def _registered_as(
    user: User, entry: Entity, form: Shape, values: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The new entry's values, as the database stores them: the superseded entry's for the
    fields their types share, checked as the new type has them (it may refine a list's
    roles, ADR 0164), and ``values``; defaults filled in, and checked against the new type's
    fields and the rules that read only its entries' fields. With the keys of the unique
    values it carries, which move with it."""
    was = shape(entry.type)
    stored = services.Rows(was, None, services.specific(entry))
    theirs = {p.name: p for p in was.parts if p.entry}
    fields = form.entry_fields()
    kept = services._kept(form)
    refs = _registry_refs(entry)
    errors: dict[str, list[str]] = {}
    given: dict[str, Any] = {}
    carried_unique: list[str] = []
    for part in form.parts:
        before = theirs.get(part.name) if part.entry else None
        if before is None:
            continue
        for name, f in part.fields.items():
            if f.removed:
                continue
            value = stored.value(before, before.fields[name])
            if f.unique and value is not None:
                carried_unique.append(unique.key(part.model, name))
            if name in values or value is None or value == []:
                continue
            if f == before.fields[name]:
                given[name] = value
                continue
            # Refined in the new type: what it was must be what the new type takes. What
            # it names already it may keep naming (ADR 0270).
            found = ids_in({name: before.fields[name]}, {name: value})
            try:
                with refs.keeping(found["ref"]):
                    given[name] = form.field_value(f, to_json(before.fields[name], value), refs)
            except ValueError as error:
                errors.update(problems_of(name, error))
    for name, value in values.items():
        wanted = fields.get(name)
        if wanted is None:
            errors[name] = [f"{form.entity_type.qualified_name}'s entries have no field `{name}`."]
        elif name in kept:
            errors[name] = [kept[name]]
        elif wanted.computed:
            errors[name] = ["is computed: only actions set it."]
        else:
            try:
                given[name] = form.field_value(wanted, value, refs)
            except ValueError as error:
                errors.update(problems_of(name, error))
    for name, f in fields.items():
        if name in given or name in values or name in kept or f.computed:
            continue
        if f.has_default:
            try:
                given[name] = form.field_value(f, f.default, refs)
            except ValueError as error:
                errors.update(problems_of(name, error))
        if f.required and given.get(name) is None and not services._wrong(errors, name):
            errors[name] = ["is required."]
    if not errors:
        for broken in checking.broken(
            form.entity_type.qualified_name,
            [checking.Saving(given.get)],
            reading=frozenset(fields),
        )[0]:
            for name in broken.fields or ("",):
                errors.setdefault(name, []).append(broken.message)
    if errors:
        raise Invalid(errors)
    return given, carried_unique


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
            about=[record],
        )


def _tell_referrers(user: User, option: Entity, replacement: Entity, reason: str) -> None:
    """For a lab list's option (ADR 0270): the Admins of each project whose records name it,
    who move them to the replacement (:func:`lists.repoint`)."""
    from iki_server.entities import lists
    from iki_server.notifications import kinds
    from iki_server.notifications.services import notify

    label = registry.types()[option.type].label
    # By their labels, as people pick them; the option's page says the rest.
    was, now = lists.option_of(option).label, lists.option_of(replacement).label
    for project in lists.referrers(option).select_related("team"):
        notify(
            permissions.members_with(project, ProjectRole.ADMIN),
            kinds.ENTRY_SUPERSEDED,
            title=f"{label} “{was}” was superseded by “{now}”",
            body=f"{reason}\n\nMove {project.name}'s records that name it to “{now}”, so "
            "they name the right option.",
            # Its page, where the project's Editors move them, with the project chosen.
            path=f"/entries/{option.pk}/?project={project.slug}",
            project=project,
            actor=user,
            about=[],
        )


# --- Moving -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Moved:
    record: Any
    revision: Revision
    #: The project's records whose references to the old entry now name the new one.
    repointed: list[Entity]


def move(
    user: User, record: Entity, *, reason: str, values: Mapping[str, Any] | None = None
) -> Moved:
    """Moves a project's record to its superseded entry's replacement, with a reason. To
    a replacement of another type (ADR 0165), ``values`` are the new record's own, beside
    those it carries."""
    said = reason.strip()
    if not said:
        raise Invalid({"reason": ["Say why the record is being moved."]})
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=record.pk)
        if locked.entry_id is None:
            raise Refused(f"{locked.hrid} isn't a record of a registry entry.")
        old = Entity.objects.get(pk=locked.entry_id)
        if old.superseded_by_id is None:
            raise Refused(f"{old.hrid} wasn't superseded: there's nothing to move it to.")
        if locked.archived_at is not None:
            raise Refused("The record is archived: restore it before moving it.")
        why = states.locked_why(locked, doing="be moved")
        if why:
            raise Refused(why)
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
        if new.type != locked.type:
            return _move_across(user, locked, old, new, project, said, values or {})
        if values:
            raise Invalid(
                {"values": [f"{new.hrid} is a {new.type}, as the record is: it keeps its values."]}
            )
        Entity.objects.filter(pk=locked.pk).update(entry=new, hrid=new.hrid)
        revision = services._revise(locked, RevisionKind.MOVED, [], user, said, moved_from=old.pk)
        repointed = _repoint(user, project, old, new, said)
        from iki_server.runs import triggers

        # What the project's records read through it is the replacement's now (ADR 0080).
        triggers.moved(locked)
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


def carried(record_type: str, replacement_type: str) -> tuple[str, ...]:
    """The fields a record's move to an entry of another type gives the new record (ADR
    0165): the record's own fields in the tables the two types' records share, those a
    person sets. Computed ones are worked out again, by the actions that compute them."""
    was, form = shape(record_type), shape(replacement_type)
    shared = {p.name for p in was.parts if not p.entry}
    kept = services._kept(form)
    ledger = registry.draws_received(replacement_type)
    return tuple(
        name
        for part in form.parts
        if not part.entry and part.name in shared
        for name, f in part.fields.items()
        if not f.removed
        and not f.computed
        and f.type != "state"
        and name not in kept
        and name != ledger
        and not requests.is_service_field(replacement_type, name)
    )


def _move_across(
    user: User,
    record: Entity,
    old: Entity,
    new: Entity,
    project: Project,
    reason: str,
    values: Mapping[str, Any],
) -> Moved:
    """A record's move to a replacement of another type (ADR 0165), which a record can't
    become: the project's record of the replacement, with the values the two types' records
    share and ``values``, and the old one archived, keeping its history, signatures and runs.
    The project's references to the old entry name the new one, as a move's do; one that
    can't (a lot of an antibody, say, of a replacement that isn't one) stops it."""
    form = shape(new.type)
    entry_fields = form.entry_fields()
    errors = {
        name: [f"is {new.hrid}'s registry entry's: the record has its own fields."]
        for name in values
        if name in entry_fields
    }
    if errors:
        raise Invalid(errors)
    stuck = _unreferable(project, old, new, besides=record.pk)
    if stuck:
        raise Refused(
            f"{'; '.join(stuck[:3])}{' and others' if len(stuck) > 3 else ''}: archive "
            f"or change {'it' if len(stuck) == 1 else 'them'} before moving this record to "
            f"{new.hrid}."
        )
    owner = registry.identity_owner(form.entity_type)
    assert owner is not None  # noqa: S101 - entries have an identity
    theirs = services.current_values(services.specific(new))
    given = {n: to_json(entry_fields[n], theirs.get(n)) for n in owner.identity}
    was = shape(record.type)
    mine = services.current_values(services.specific(record))
    taking = [n for n in carried(record.type, new.type) if n not in values]
    for name in taking:
        value = mine.get(name)
        if value is not None and value != []:
            given[name] = to_json(was.fields()[name], value)
    given.update(values)
    # What it names already it may keep naming: an archived option of a lab list, say.
    named = ids_in(was.fields(), {n: mine.get(n) for n in taking})
    services.set_archived(user, record, True, reason=reason)
    # The unique values it carries are the new record's from now on (ADR 0082): an ID in
    # another system it links, say.
    UniqueValue.objects.filter(
        entity_id=record.pk,
        key__in=[unique.key(was.part_of(n).model, n) for n in taking if was.fields()[n].unique],
    ).delete()
    saved = services.create(
        user, new.type, project, given, reason=reason, keeping=frozenset(named["ref"])
    )
    repointed = _repoint(user, project, old, new, reason)
    log.record(
        "entity.moved",
        actor=user,
        target=record,
        project=project,
        data={
            "from": {"id": str(old.pk), "hrid": old.hrid},
            "to": {"id": str(new.pk), "hrid": new.hrid},
            "record": {"id": str(saved.entity.pk), "type": new.type},
            "repointed": [e.hrid for e in repointed],
            "reason": reason,
        },
    )
    assert saved.revision is not None  # noqa: S101 - it's a new record
    return Moved(saved.entity, saved.revision, repointed)


def _unreferable(project: Project, old: Entity, new: Entity, *, besides: Any) -> list[str]:  # noqa: ANN401 - a UUID
    """The project's references to ``old`` that a move would re-point, but that can't name
    ``new``, as it's of a type they don't refer to: each as "LOT-000003's Antibody must be
    a iki/antibody.Antibody". Those a move leaves alone (archived, locked or detached
    records', as :func:`_repoint` does) aren't."""
    stuck: list[str] = []
    own = Entity.objects.filter(
        owner_project=project, archived_at__isnull=True, purged_at__isnull=True, locked_by=[]
    ).exclude(pk=besides)
    for qualified in sorted(registry.types()):
        entity_type = registry.types()[qualified]
        if entity_type.abstract or qualified == "iki/core.Entity":
            continue
        form = shape(qualified)
        if services.frozen(form) is not None:
            continue
        records = own.filter(type=qualified)
        for part in form.parts:
            if part.entry:
                continue
            for name, f in part.fields.items():
                if f.removed:
                    continue
                for cell, column, model in _ref_columns(part, f, name):
                    if new.type in referable(cell):
                        continue
                    key = "entity_id" if f.type == "list" or part.extension else "pk"
                    ids = set(
                        model._default_manager.filter(**{column: old.pk}).values_list(
                            key, flat=True
                        )
                    )
                    for hrid in records.filter(pk__in=ids).values_list("hrid", flat=True):
                        targets = " or ".join(f"a {t}" for t in cell.to)
                        stuck.append(f"{hrid}'s {f.label or name} must be {targets}")
    return sorted(set(stuck))


def _ref_columns(part: Any, f: Any, name: str) -> list[tuple[Any, str, Any]]:  # noqa: ANN401
    """Where a field's references are stored: (the field or row field, its column, its
    table's model), for a reference, a list of them, or a list of rows with them."""
    if f.is_rows:
        assert f.items is not None  # noqa: S101 - lists have items
        return [
            (f.items.fields[c], column, part.list_model(name))
            for c, column in row_columns(f).items()
            if f.items.fields[c].type == "ref"
        ]
    if f.type == "list":
        if f.items is None or f.items.type != "ref":
            return []
        return [(f.items, "item_id", part.list_model(name))]
    if f.type != "ref":
        return []
    return [(f, f"{name}_id", part.model)]


def _repoint(user: User, project: Project, old: Entity, new: Entity, reason: str) -> list[Entity]:
    """The project's own records that refer to ``old``, referring to ``new`` instead, each
    a revision with the reason: immutable references too, as the entry they named was a
    mistake. Each is saved as any edit is, so its rules and holds are checked, the checks
    that read what changed are cleared and run again, and whoever watches it is told.
    Archived records keep theirs, as do locked ones (ADR 0125) and a detached plugin's."""
    changes: dict[Any, dict[str, Any]] = {}
    own = Entity.objects.filter(
        owner_project=project, archived_at__isnull=True, purged_at__isnull=True, locked_by=[]
    )
    for qualified in sorted(registry.types()):
        entity_type = registry.types()[qualified]
        if entity_type.abstract or qualified == "iki/core.Entity":
            continue
        form = shape(qualified)
        if services.frozen(form) is not None:
            continue
        kept = services._kept(form)
        for part in form.parts:
            if part.entry:
                continue
            for name, f in part.fields.items():
                if f.removed or name in kept:
                    continue
                kind = f.items.type if f.type == "list" and f.items is not None else f.type
                if kind == "row":
                    assert f.items is not None  # noqa: S101 - lists have items
                    if not any(c.type == "ref" for c in f.items.fields.values()):
                        continue
                elif kind != "ref":
                    continue
                ids = _referring(part, f, name, old.pk) & set(
                    own.filter(type=qualified).values_list("pk", flat=True)
                )
                for pk in ids:
                    changes.setdefault(pk, {})[name] = f
    done = [
        _repoint_one(user, changes[pk], pk, old, new, reason) for pk in sorted(changes, key=str)
    ]
    return sorted(done, key=lambda e: e.hrid)


def _referring(part: Any, f: Any, name: str, target: Any) -> set[Any]:  # noqa: ANN401
    if f.is_rows:
        found = Q()
        for cell, column in row_columns(f).items():
            if f.items.fields[cell].type == "ref":
                found |= Q(**{column: target})
        rows = part.list_model(name)._default_manager.filter(found)
        return set(rows.values_list("entity_id", flat=True))
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
    fields: Mapping[str, Any],
    pk: Any,  # noqa: ANN401 - a UUID
    old: Entity,
    new: Entity,
    reason: str,
) -> Entity:
    """One record's references to ``old`` naming ``new``, in one save through
    :func:`services.update`, as every save is: a rule it would break refuses the move, a
    rule that holds holds the record with the move's reason."""
    locked = Entity.objects.select_for_update().get(pk=pk)
    rows = services.Rows.of(services.specific(locked))
    was, now = str(old.pk), str(new.pk)
    values: dict[str, Any] = {}
    for name, f in fields.items():
        current = to_json(f, rows.get(name))
        if f.is_rows:
            refs = {n for n, cell in f.items.fields.items() if cell.type == "ref"}
            values[name] = [
                {n: now if n in refs and v == was else v for n, v in row.items()} for row in current
            ]
        elif f.type == "list":
            values[name] = [now if item == was else item for item in current]
        else:
            values[name] = now
    try:
        services.update(
            user,
            locked,
            values,
            expected_revision=locked.revision,
            reason=reason,
            hold_reason=reason,
            _repointing=frozenset(values),
        )
    except Invalid as error:
        said = " ".join(m for messages in error.errors.values() for m in messages)
        raise Refused(
            f"{locked.hrid} can't refer to {new.hrid} instead of {old.hrid}: {said}"
        ) from None
    from iki_server.runs import triggers

    # What it's worked out from is another record now (ADR 0080).
    triggers.repointed(locked, set(values))
    return Entity.objects.get(pk=pk)


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
