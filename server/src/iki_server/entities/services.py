"""Creating, editing and archiving entities: revisions, history and provenance (ADR 0035).

Every change goes through here, so its rules hold for the web, the API, MCP
and runs alike:

- **Revisions.** Each change is a numbered :class:`Revision`: who, how
  (``via``), when, why, and which fields. The new values of every table it
  touched go to that table's history, so any revision can be read back
  exactly.
- **Edit rules.** ``computed`` fields are set only by actions. ``immutable``
  fields can't change once set. Removed fields can't be set.
- **No lost updates.** An edit names the revision it was made against. If
  someone else changed the entity since, it's refused (:class:`Stale`)
  rather than silently overwriting their change.
- **Registries** (ADR 0115). An entity of a type with an identity is a
  project's record of a registry entry, which holds its identity and what's
  derived from it or curated about it, with its ID. Creating one registers
  the identity in the project's registry: a new entry, or the one there is,
  of which the project gets a record (or keeps the one it has: one record
  per identity per project). Whether an entry existed is all it tells: who
  registered it, when, and which projects hold it, it never does.
- **Registry values.** Derived values and facts are set while they're empty,
  by whoever holds the entry. After that a fact changes only by its
  registry's registrars (:mod:`iki_server.entities.entries`), and a derived
  value not at all: it's its identity's.
- **Archiving, not deleting** (ADR 0008). Archived entities keep their
  history, and can't change until restored.

Callers check permissions first (:mod:`iki_server.entities.access`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, connection, transaction
from django.db.models import Model
from django.db.models.functions import Now

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.log import agent_data, current_agent, current_oauth_client, current_via
from iki_server.entities import (
    access,
    assignments,
    identity,
    registry,
    rules,
    states,
    unique,
)
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.values import (
    FOREIGN_KEYS,
    Invalid,
    Part,
    Refs,
    Shape,
    read_value,
    shape,
    to_db,
)
from iki_server.entities.values import is_list_table as _is_list
from iki_server.files.models import FileUse
from iki_server.org.models import Project, registry_of
from iki_server.org.services import Refused
from iki_spec.model import EntityType, Field, names_people

__all__ = [
    "Invalid",
    "Rows",
    "Saved",
    "Stale",
    "changes",
    "create",
    "current_values",
    "current_values_of",
    "entry_at",
    "identity_lock",
    "link",
    "prepare",
    "provenance",
    "set_archived",
    "unlink",
    "update",
    "values_at",
]


#: Why a state field isn't set with a record's other values.
STATE_SET_ALONE = (
    "is a state field: it's set on its own, by someone with one of its states' lab roles "
    "(ADR 0125). New records start in its initial state."
)


class Stale(Exception):  # noqa: N818 - reads as "the edit was stale"
    """The entity changed since the revision the edit was made against."""

    def __init__(self, current: int) -> None:
        super().__init__(f"The entity changed since: it's at revision {current} now.")
        self.current = current


@dataclass
class Saved:
    entity: Model
    #: The revision the change made to the entity, or None when nothing of its own changed
    #: (or the project's record was returned as it was).
    revision: Revision | None = None
    #: The project had a record of this identity already, and it was returned as it was.
    reused: bool = False
    #: A new registry entry was made for it (ADR 0115); otherwise it had one already.
    registered: bool = False
    #: The revision the change made to its registry entry, if it made one.
    entry_revision: Revision | None = None


def specific(entity: Entity) -> Model:
    """The entity as its type's model: a record's, or a registry entry's."""
    if entity.registry_id is not None:
        model = registry.entry_model(entity.type)
        assert model is not None  # noqa: S101 - only types with an identity have entries
        return model._default_manager.get(pk=entity.pk)
    return registry.model(entity.type)._default_manager.get(pk=entity.pk)


_specific = specific


def owner_of(entity: Entity) -> Project:
    """A record's project. Raises for a registry entry, which has none (ADR 0115)."""
    if entity.owner_project_id is None:
        raise Refused(f"{entity.hrid} is a registry entry: it belongs to no project.")
    return Project.objects.select_related("team", "registry").get(pk=entity.owner_project_id)


# --- Where values live ---------------------------------------------------------------------------


class Rows:
    """Where each of an entity's tables keeps its values: the record (its own type's
    tables), its registry entry (ADR 0115), and their extension rows."""

    def __init__(self, form: Shape, record: Model | None, entry: Model | None) -> None:
        self.form = form
        self.record = record
        self.entry = entry
        self.extensions: dict[type[Model], Model] = {}
        for part in form.parts:
            owner = self.owner(part)
            if part.extension and owner is not None:
                row = part.model._default_manager.filter(entity_id=owner.pk).first()
                if row is not None:
                    self.extensions[part.model] = row

    @classmethod
    def of(cls, entity: Model) -> Rows:
        """The rows of a record, with its entry's, or of an entry alone."""
        form = shape(entity.type)  # type: ignore[attr-defined]
        if entity.registry_id is not None:  # type: ignore[attr-defined]
            return cls(form, None, entity)
        entry_id = entity.entry_id  # type: ignore[attr-defined]
        entry = None
        if entry_id is not None and form.entry_model is not None:
            entry = form.entry_model._default_manager.get(pk=entry_id)
        return cls(form, entity, entry)

    def owner(self, part: Part) -> Model | None:
        return self.entry if part.entry else self.record

    def holder(self, part: Part) -> Model | None:
        if part.extension:
            return self.extensions.get(part.model)
        return self.owner(part)

    def value(self, part: Part, f: Field) -> Any:  # noqa: ANN401 - a model value
        holder = self.holder(part)
        if holder is None:
            return [] if _is_list(f) else None
        return read_value(holder, f, part)

    def get(self, name: str) -> Any:  # noqa: ANN401 - a model value
        part = self.form.part_of(name)
        return self.value(part, part.fields[name])

    def ensure(self, part: Part) -> Model:
        """The row to write the part's values to, an extension's made if there's none."""
        if not part.extension:
            owner = self.owner(part)
            assert owner is not None  # noqa: S101 - callers write only parts they have
            return owner
        row = self.extensions.get(part.model)
        if row is None:
            owner = self.owner(part)
            assert owner is not None  # noqa: S101
            row = part.model(entity_id=owner.pk)
            self.extensions[part.model] = row
        return row


# --- Creating -----------------------------------------------------------------------------------


def create(
    user: User,
    type_name: str,
    project: Project,
    values: Mapping[str, Any],
    *,
    reason: str = "",
    by_run: bool = False,
) -> Saved:
    """A new entity of ``type_name`` in ``project``. For a type with an identity, the
    project's record of its registry entry: a new one, or the one the project has, as it is.

    ``by_run``: an action run is writing its results (ADR 0044), for ``user``. It may set
    computed fields, and its revision is marked ``via="run"``.
    """
    form, given = prepare(
        user, type_name, project, values, access.refs_for(user, project), by_run=by_run
    )
    with transaction.atomic(), _unique_values():
        if not form.has_entries:
            return _new_record(user, form, project, given, reason, by_run)
        entry = _find_entry(form, project, given)
        if entry is not None:
            existing = Entity.objects.filter(owner_project=project, entry=entry).first()
            if existing is not None:
                return Saved(_specific(existing), reused=True)
            named = {n: given[n] for n in values if n in form.entry_fields()}
            entry_revision = fill_entry(
                user, form, entry, named, project=project, reason=reason, by_run=by_run
            )
            saved = _new_record(user, form, project, given, reason, by_run, entry=entry)
            saved.entry_revision = entry_revision
            return saved
        entry, entry_revision = _new_entry(user, form, project, given, reason, by_run)
        saved = _new_record(
            user, form, project, given, reason, by_run, entry=entry, registered=entry_revision
        )
        saved.registered = True
        saved.entry_revision = entry_revision
        return saved


def _new_record(
    user: User,
    form: Shape,
    project: Project,
    given: Mapping[str, Any],
    reason: str,
    by_run: bool,
    *,
    entry: Entity | None = None,
    registered: Revision | None = None,
) -> Saved:
    """The project's record: its own fields' values, and for an identity, its entry."""
    instance = form.model(owner_project=project, created_by=user)
    if entry is not None:
        instance.entry_id = entry.pk
        instance.hrid = entry.hrid
    rows = Rows(form, instance, None)
    mine = [p for p in form.parts if not p.entry]
    for part in mine:
        if not part.extension:
            for name, f in part.fields.items():
                if name in given and not _is_list(f):
                    _set(instance, f, given[name])
    own = {n: v for n, v in given.items() if not form.part_of(n).entry}
    clashes = unique.taken(project, form, own)
    if clashes:
        raise Invalid(clashes)
    instance.save()
    touched = [p for p in mine if not p.extension]
    for part in mine:
        if part.extension and any(own.get(n) is not None for n in part.fields):
            row = rows.ensure(part)
            for name, f in part.fields.items():
                if name in own and not _is_list(f):
                    _set(row, f, own[name])
            row.save()
            touched.append(part)
        for name, f in part.fields.items():
            if _is_list(f) and own.get(name):
                _write_list(part, f, instance.pk, own[name])
    changed = sorted(n for n, v in own.items() if v is not None)
    revision = _revise(
        instance,
        RevisionKind.CREATED,
        changed,
        user,
        reason,
        via="run" if by_run else None,
        registered=registered,
    )
    _write_history(touched, rows, revision.number)
    _use_files(form.fields(), instance.pk, own, revision.number)
    ProjectLink.objects.create(entity_id=instance.pk, project=project, linked_by=user)
    assignments.changed(instance, revision, assignments.diff(instance.type, {}, own), user)
    return Saved(_specific(instance), revision)


def _find_entry(form: Shape, project: Project, given: Mapping[str, Any]) -> Entity | None:
    """The entry in the project's registry with this identity, if there's one, holding the
    identity's lock until the transaction ends: two people registering the same identity at
    once make one entry, the second waiting, then finding it."""
    digest = identity_digest(form, given)
    identity_lock(form.entity_type, registry_of(project), shared=True)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"iki_entry:{registry_of(project)}:{digest}"],
        )
    entry = Entity.objects.filter(registry_id=registry_of(project), identity_hash=digest).first()
    if entry is None:
        return None
    if entry.superseded_by_id is not None:
        raise Refused(superseded(entry))
    if entry.type != form.entity_type.qualified_name:
        raise Invalid(
            {
                "type": [
                    f"This is registered already, as a {entry.type} ({entry.hrid}): register it "
                    "as one."
                ]
            }
        )
    return entry


def superseded(entry: Entity) -> str:
    """Why a superseded entry can't be registered again, naming what replaced it."""
    from iki_server.entities.entries import supersession

    found = supersession(entry)
    replacement = found.replacement.hrid if found is not None else "another entry"
    why = f" ({found.reason})" if found is not None and found.reason else ""
    return (
        f"{entry.hrid} was superseded by {replacement}{why}: register that instead, or ask "
        "the registry's registrars."
    )


def identity_digest(form: Shape, given: Mapping[str, Any]) -> str:
    """The identity hash of an entity with these values, as the database stores them."""
    instance = _entry_instance(form, given)
    digest = identity.hash_for(instance, form.entity_type)
    assert digest is not None  # noqa: S101 - only types with an identity have entries
    return digest


def _entry_instance(form: Shape, given: Mapping[str, Any]) -> Model:
    model = form.entry_model
    assert model is not None  # noqa: S101
    instance = model(type=form.entity_type.qualified_name)
    for part in form.parts:
        if part.entry and not part.extension:
            for name, f in part.fields.items():
                if name in given and not _is_list(f):
                    _set(instance, f, given[name])
    return instance


def _new_entry(
    user: User,
    form: Shape,
    project: Project,
    given: Mapping[str, Any],
    reason: str,
    by_run: bool,
    *,
    pk: Any = None,  # noqa: ANN401 - a UUID
    hrid: str = "",
) -> tuple[Entity, Revision]:
    """A new registry entry in the project's registry, with its values from ``given``. An
    import keeps the ID and HRID it had where it's from, when they're free here."""
    instance = _entry_instance(form, given)
    if pk is not None:
        # Its root table's ID: each table below takes it from there.
        instance.id = pk  # type: ignore[attr-defined]
    if hrid:
        instance.hrid = hrid  # type: ignore[attr-defined]
    instance.registry_id = registry_of(project)  # type: ignore[attr-defined]
    instance.created_by = user  # type: ignore[attr-defined]
    instance.identity_hash = identity.hash_for(instance, form.entity_type)  # type: ignore[attr-defined]
    fields = form.entry_fields()
    mine = {n: v for n, v in given.items() if n in fields}
    clashes = unique.taken(project, form, mine)
    if clashes:
        raise Invalid(clashes)
    instance.save()
    rows = Rows(form, None, instance)
    touched = [p for p in form.parts if p.entry and not p.extension]
    for part in form.parts:
        if not part.entry:
            continue
        if part.extension and any(mine.get(n) is not None for n in part.fields):
            row = rows.ensure(part)
            for name, f in part.fields.items():
                if name in mine and not _is_list(f):
                    _set(row, f, mine[name])
            row.save()
            touched.append(part)
        for name, f in part.fields.items():
            if _is_list(f) and mine.get(name):
                _write_list(part, f, instance.pk, mine[name])
    changed = sorted(n for n, v in mine.items() if v is not None)
    revision = _revise(
        instance,
        RevisionKind.CREATED,
        changed,
        user,
        reason,
        via="run" if by_run else None,
        project=project,
    )
    _write_history(touched, rows, revision.number)
    _use_files(fields, instance.pk, mine, revision.number)
    return Entity.objects.get(pk=instance.pk), revision


def fill_entry(
    user: User,
    form: Shape,
    entry: Entity,
    values: Mapping[str, Any],
    *,
    project: Project,
    reason: str = "",
    by_run: bool = False,
    explicit: bool = False,
) -> Revision | None:
    """Sets those of an entry's values that are empty from ``values`` (as the database
    stores them), as a holder of it may. One it has already is kept: a fact with another
    value is refused, without saying what it has, as only its registrars change it; a
    derived value is its identity's, so another is ignored, unless ``explicit`` (a person
    changing it, not registering it again), when it's refused too."""
    if not values:
        return None
    locked = Entity.objects.select_for_update().get(pk=entry.pk)
    rows = Rows(form, None, _specific(locked))
    errors: dict[str, list[str]] = {}
    new: dict[str, Any] = {}
    for name, value in values.items():
        part = form.part_of(name)
        f = part.fields[name]
        current = rows.value(part, f)
        empty = current is None or current == []
        if value == current or (empty and (value is None or value == [])):
            continue
        if empty:
            new[name] = value
        elif value is None or value == []:
            if explicit:
                errors[name] = [settled(locked)]
        elif f.registry == "fact":
            errors[name] = [settled(locked)]
        elif explicit or f.registry is None:
            errors[name] = [
                f"is {locked.hrid}'s registry entry's, as its identity gives it: it can't change."
            ]
    if not errors and new:
        errors = unique.taken(project, form, new, entry=locked.pk)
    if errors:
        raise Invalid(errors)
    if not new:
        return None
    touched = _write(form, rows, new, locked.pk)
    revision = _revise(
        locked,
        RevisionKind.EDITED,
        sorted(new),
        user,
        reason,
        via="run" if by_run else None,
        project=project,
    )
    _write_history(touched, rows, revision.number)
    _use_files(form.fields(), locked.pk, new, revision.number)
    return revision


def settled(entry: Entity) -> str:
    """Why a holder can't change a fact the entry has."""
    return (
        f"is a registry fact {entry.hrid} has already: only its registry's registrars change "
        "it once it's set."
    )


@contextmanager
def _unique_values() -> Iterator[None]:
    """Two people writing the same unique value at once: the second is told, as if it had
    been there all along (ADR 0082)."""
    try:
        yield
    except IntegrityError as error:
        if not unique.is_clash(error):
            raise
        raise Invalid(
            {"unique": ["Another entity has just been given one of these unique values."]}
        ) from None


def prepare(
    user: User,
    type_name: str,
    project: Project,
    values: Mapping[str, Any],
    refs: Refs,
    *,
    by_run: bool = False,
) -> tuple[Shape, dict[str, Any]]:
    """A new entity's values as the database stores them, defaults filled in, checked
    against its type's fields and rules. Raises :class:`Invalid` with what's wrong.

    Bulk imports (ADR 0056) check each record with this, as :func:`create` does.
    """
    if type_name not in registry.types() or type_name == "iki/core.Entity":
        raise Invalid({"type": [f"There's no entity type {type_name}."]})
    form = shape(type_name)
    entity_type = form.entity_type
    if entity_type.abstract:
        raise Invalid({"type": [f"{type_name} is abstract: create one of its subtypes."]})
    why = frozen(form)
    if why:
        raise Invalid({"type": [why]})
    fields = form.fields()
    kept = _kept(form)
    errors: dict[str, list[str]] = {}
    given: dict[str, Any] = {}
    for name, value in values.items():
        f = fields.get(name)
        if f is None:
            errors[name] = [f"{type_name} has no field `{name}`."]
        elif name in kept:
            errors[name] = [kept[name]]
        elif f.computed and not by_run:
            errors[name] = ["is computed: only actions set it."]
        elif f.type == "state":
            errors[name] = [STATE_SET_ALONE]
        else:
            try:
                given[name] = to_db(f, value, refs)
            except ValueError as error:
                errors[name] = [str(error)]
    for name, f in fields.items():
        if name in kept:
            continue
        if f.type == "state" and not f.removed:
            given[name] = f.initial  # Where new records start (ADR 0125).
        elif name not in values and f.has_default and not f.computed:
            given[name] = to_db(f, f.default, refs)
        if f.required and given.get(name) is None and name not in errors:
            errors[name] = ["is required."]
    if not errors:
        errors = rules.check(user, entity_type, project, given.get)
    if errors:
        raise Invalid(errors)
    return form, given


def identity_lock(entity_type: EntityType, registry_id: Any, *, shared: bool) -> None:  # noqa: ANN401 - a UUID
    """Serializes registering a type's identities in a registry with a bulk import
    registering them (ADR 0056): each create shares the lock, and an import holds it alone
    while it writes. Locks the type its identity belongs to, as subtypes share entries."""
    owner = registry.identity_owner(entity_type)
    if owner is None:
        return
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {function}(hashtextextended(%s, 0))",
            [f"iki_identity:{registry_id}:{owner.qualified_name}"],
        )


# --- Editing ------------------------------------------------------------------------------------


def update(
    user: User,
    entity: Entity,
    values: Mapping[str, Any],
    *,
    expected_revision: int,
    reason: str = "",
    by_run: bool = False,
    group: str = "",
) -> Saved:
    """Changes some of an entity's fields. Fields not named keep their values. Its registry
    entry's are set only while they're empty (see :func:`fill_entry`).

    ``by_run``: as for :func:`create`. ``group``: a bulk change it's part of, which the audit
    records of its assignments name (ADR 0122).
    """
    with transaction.atomic(), _unique_values():
        locked = Entity.objects.select_for_update().get(pk=entity.pk)
        if locked.registry_id is not None:
            raise Refused(
                f"{locked.hrid} is a registry entry: change a project's record of it, or ask "
                "its registrars to change its facts."
            )
        if locked.revision != expected_revision:
            raise Stale(locked.revision)
        if locked.purged_at is not None:
            raise Refused(purged(locked))
        if locked.archived_at is not None:
            raise Refused("The entity is archived: restore it before changing it.")
        why = states.locked_why(locked)
        if why:
            raise Refused(why)
        form = shape(locked.type)
        why = frozen(form)
        if why:
            raise Refused(why)
        kept = _kept(form)
        fields = form.fields()
        owner = registry.identity_owner(registry.types()[locked.type])
        identifying = set(owner.identity) if owner is not None else set()
        instance = _specific(locked)
        rows = Rows.of(instance)
        project = owner_of(locked)
        refs = access.refs_for(user, project)
        errors: dict[str, list[str]] = {}
        new: dict[str, Any] = {}
        for_entry: dict[str, Any] = {}
        for name, value in values.items():
            f = fields.get(name)
            if f is None:
                errors[name] = [f"{locked.type} has no field `{name}`."]
                continue
            if name in kept:
                errors[name] = [kept[name]]
                continue
            if f.computed and not by_run:
                errors[name] = ["is computed: only actions set it."]
                continue
            if f.type == "state":
                errors[name] = [STATE_SET_ALONE]
                continue
            part = form.part_of(name)
            current = rows.value(part, f)
            if name in identifying:
                try:
                    converted = to_db(f, value, refs)
                except ValueError as error:
                    errors[name] = [str(error)]
                    continue
                if converted == current:
                    continue
                errors[name] = (
                    [
                        "is part of what makes it this one, so it can't be added later: "
                        "register another with it instead."
                    ]
                    if current is None
                    else ["can't change once it's set."]
                )
                continue
            try:
                # Whoever it names already may stay named, though they couldn't be named
                # anew (ADR 0122): an edit of something else keeps them.
                with refs.namer.keeping(_named_in(f, current)):
                    converted = to_db(f, value, refs)
            except ValueError as error:
                errors[name] = [str(error)]
                continue
            if converted == current:
                continue
            if part.entry:
                for_entry[name] = converted
            elif f.immutable and current is not None:
                errors[name] = ["can't change once it's set."]
            elif f.required and converted is None:
                errors[name] = ["is required."]
            else:
                new[name] = converted
        if not errors and new:
            errors = unique.taken(project, form, new, entity=locked.pk)
        if not errors and (new or for_entry):
            changing = {**new, **for_entry}
            errors = rules.check(
                user,
                form.entity_type,
                project,
                lambda n: changing[n] if n in changing else rows.get(n),
                changed=changing,
            )
        if errors:
            raise Invalid(errors)
        entry_revision = None
        if for_entry:
            assert rows.entry is not None  # noqa: S101 - entry fields are an entry's
            entry_revision = fill_entry(
                user,
                form,
                Entity.objects.get(pk=rows.entry.pk),
                for_entry,
                project=project,
                reason=reason,
                by_run=by_run,
                explicit=not by_run,
            )
        if not new:
            return Saved(_specific(locked), entry_revision=entry_revision)
        before = {n: rows.get(n) for n in new if names_people(fields[n])}
        touched = _write(form, rows, new, instance.pk)
        revision = _revise(
            locked, RevisionKind.EDITED, sorted(new), user, reason, via="run" if by_run else None
        )
        _write_history(touched, rows, revision.number)
        _use_files(fields, locked.pk, new, revision.number)
        assignments.changed(
            locked, revision, assignments.diff(locked.type, before, new), user, group=group
        )
    return Saved(_specific(locked), revision, entry_revision=entry_revision)


def _named_in(f: Field, current: Any) -> set[Any]:  # noqa: ANN401 - a model value
    """Whom a field naming people names now: they may stay named."""
    if not names_people(f) or current is None:
        return set()
    return set(current) if isinstance(current, list) else {current}


def _write(form: Shape, rows: Rows, new: Mapping[str, Any], owner_id: Any) -> list[Part]:  # noqa: ANN401 - a UUID
    """Writes ``new`` values to the tables of ``rows`` that hold them; returns those it did."""
    touched: list[Part] = []
    saves: list[Model] = []
    for part in form.parts:
        mine = {n: v for n, v in new.items() if n in part.fields}
        if not mine:
            continue
        touched.append(part)
        target = rows.ensure(part)
        owner = rows.owner(part)
        key = owner.pk if owner is not None else owner_id
        for name, value in mine.items():
            f = part.fields[name]
            if _is_list(f):
                part.list_model(name)._default_manager.filter(entity_id=key).delete()
                if value:
                    _write_list(part, f, key, value)
            else:
                _set(target, f, value)
        if all(target is not s for s in saves):
            saves.append(target)
    for target in saves:
        target.save()
    return touched


def set_archived(
    user: User,
    entity: Entity,
    archived: bool,
    *,
    reason: str = "",
    expected_revision: int | None = None,
) -> Saved:
    """Archives or restores an entity. Archived entities keep their history and can't change.
    A registry entry isn't archived: its registrars supersede it (ADR 0115)."""
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=entity.pk)
        if locked.registry_id is not None:
            raise Refused(f"{locked.hrid} is a registry entry: archive a project's record of it.")
        if expected_revision is not None and locked.revision != expected_revision:
            raise Stale(locked.revision)
        if locked.purged_at is not None:
            raise Refused(purged(locked))
        if (locked.archived_at is not None) == archived:
            return Saved(_specific(locked))
        why = states.locked_why(locked, doing="be archived or restored")
        if why:
            raise Refused(why)
        why = frozen(shape(locked.type))
        if why:
            raise Refused(why)
        locked.archived_at = dt.datetime.now(dt.UTC) if archived else None
        locked.save(update_fields=["archived_at"])
        kind = RevisionKind.ARCHIVED if archived else RevisionKind.RESTORED
        revision = _revise(locked, kind, [], user, reason)
    return Saved(_specific(locked), revision)


def link(user: User, entity: Entity, project: Project) -> bool:
    """Adds the entity to another project, so its members can read it. True if it's new there.

    It changes who can read the entity, so it's audited. Raises :class:`Refused` if the
    entity's own project doesn't allow sharing it (ADR 0067), or is in another registry
    (ADR 0115)."""
    if entity.purged_at is not None:
        raise Refused(purged(entity))
    if entity.registry_id is not None:
        raise Refused(
            f"{entity.hrid} is a registry entry: share a project's record of it, or register "
            "it in the project."
        )
    why = frozen(shape(entity.type))
    if why:
        raise Refused(why)
    with transaction.atomic():
        if not access.shareable_into(user, project).filter(pk=entity.pk).exists():
            owner = owner_of(entity)
            if owner.registry_id != registry_of(project):
                raise Refused(other_registry(entity, owner, project))
            raise Refused(not_shared(entity, owner))
        _, created = ProjectLink.objects.get_or_create(
            entity=entity, project=project, defaults={"linked_by": user}
        )
        if created:
            log.record("entity.linked", actor=user, target=entity, project=project)
    return created


def purged(entity: Entity) -> str:
    return f"{entity.hrid} was purged: only its ID, HRID and type are left (ADR 0092)."


def frozen(form: Shape) -> str | None:
    """Why entities of ``form``'s type can't change, if they can't: their plugin is
    detached (ADR 0091). Taking one out of a project still can."""
    detached = registry.detached()
    plugin = next((p.plugin for p in form.parts if not p.extension and p.plugin in detached), None)
    if plugin is None:
        return None
    return (
        f"{form.entity_type.qualified_name} is from the {plugin} plugin, which is detached: "
        "its entities can be read but not changed."
    )


def _kept(form: Shape) -> dict[str, str]:
    """The fields an extension from a detached plugin adds, which keep their values, with why."""
    detached = registry.detached()
    return {
        name: f"is from the {part.plugin} plugin, which is detached: it can't change."
        for part in form.parts
        if part.extension and part.plugin in detached
        for name in part.fields
    }


def not_shared(entity: Entity, owner: Project) -> str:
    return (
        f"{entity.hrid} can't be shared outside {owner.name}: its project doesn't allow "
        "sharing its entities with other projects."
    )


def other_registry(entity: Entity, owner: Project, project: Project) -> str:
    return (
        f"{entity.hrid} can't be shared with {project.name}: it's in another registry from "
        f"{owner.name}, and registries share nothing (ADR 0115)."
    )


def unlink(user: User, entity: Entity, project: Project) -> bool:
    """Takes the entity out of a project. True if it was there."""
    if project.pk == entity.owner_project_id:
        raise Refused("An entity always belongs to its owner project.")
    with transaction.atomic():
        removed, _ = ProjectLink.objects.filter(entity=entity, project=project).delete()
        if removed:
            log.record("entity.unlinked", actor=user, target=entity, project=project)
    return bool(removed)


# --- Reading ------------------------------------------------------------------------------------


def current_values(entity: Model) -> dict[str, Any]:
    """Every current field's value, as the database has it (see ``values.to_json``): a
    record's own and its entry's; an entry's own alone."""
    rows = Rows.of(entity)
    form = rows.form
    return {
        name: rows.value(form.part_of(name), f)
        for name, f in form.fields().items()
        if entity.registry_id is None or form.part_of(name).entry  # type: ignore[attr-defined]
    }


def current_values_of(entities: Sequence[Entity]) -> dict[Any, dict[str, Any]]:
    """:func:`current_values` for many entities, in a few queries per type, not per entity."""
    by_type: dict[str, list[Entity]] = defaultdict(list)
    for entity in entities:
        by_type[entity.type].append(entity)
    out: dict[Any, dict[str, Any]] = {}
    for type_name, of_type in by_type.items():
        form = shape(type_name)
        records = [e.pk for e in of_type if e.registry_id is None]
        entries = {e.pk for e in of_type if e.registry_id is not None}
        entries |= {e.entry_id for e in of_type if e.entry_id is not None}
        holders: dict[str, dict[Any, Model]] = {}
        lists: dict[str, dict[Any, list[Any]]] = {}
        entry_model = form.entry_model
        if records:
            holders["record"] = {
                o.pk: o for o in form.model._default_manager.filter(pk__in=records)
            }
        if entries and entry_model is not None:
            holders["entry"] = {
                o.pk: o for o in entry_model._default_manager.filter(pk__in=entries)
            }
        for part in form.parts:
            ids = entries if part.entry else records
            if not ids:
                continue
            if part.extension:
                holders[part.name] = {
                    r.entity_id: r  # type: ignore[attr-defined]
                    for r in part.model._default_manager.filter(entity_id__in=ids)
                }
            for name, f in part.fields.items():
                if _is_list(f) and not f.removed:
                    items: dict[Any, list[Any]] = defaultdict(list)
                    rows = part.list_model(name)._default_manager.filter(entity_id__in=ids)
                    for entity_id, item in rows.order_by("entity_id", "position").values_list(
                        "entity_id", "item_id"
                    ):
                        items[entity_id].append(item)
                    lists[name] = items
        for entity in of_type:
            values: dict[str, Any] = {}
            is_entry = entity.registry_id is not None
            for name, f in form.fields().items():
                part = form.part_of(name)
                if is_entry and not part.entry:
                    continue
                key = (entity.pk if is_entry else entity.entry_id) if part.entry else entity.pk
                if name in lists:
                    values[name] = lists[name].get(key, [])
                    continue
                table = part.name if part.extension else ("entry" if part.entry else "record")
                holder = holders.get(table, {}).get(key)
                if holder is None:
                    values[name] = None
                elif f.type in FOREIGN_KEYS:
                    values[name] = getattr(holder, f"{name}_id")
                else:
                    values[name] = getattr(holder, name)
            out[entity.pk] = values
    return out


def entry_at(
    entity: Entity,
    number: int,
    entry_revision: int | None = None,
) -> tuple[Any, int] | None:
    """For a record of a registry entry, at its revision ``number``: the entry it named
    then (one it moved from, ADR 0115), and the entry's revision: ``entry_revision``, as a
    run pins it, or else the entry's latest while that revision of the record was its
    latest (now, for its latest), as the entry changes without the record changing."""
    if entity.registry_id is not None:
        return (entity.pk, number)
    moved = (
        entity.revisions.filter(kind=RevisionKind.MOVED, number__gt=number)
        .order_by("number")
        .values_list("moved_from_id", flat=True)
        .first()
    )
    entry_id = moved or entity.entry_id
    if entry_id is None:
        return None
    if entry_revision is not None:
        return (entry_id, entry_revision)
    after = entity.revisions.filter(number=number + 1).values_list("created_at", flat=True).first()
    then = Revision.objects.filter(entity_id=entry_id)
    if after is not None:
        then = then.filter(created_at__lt=after)
    return (entry_id, then.order_by("-number").values_list("number", flat=True).first() or 0)


def entry_revision(entity: Entity) -> int | None:
    """A record's entry's latest revision, as a run pins it with the record's; None for an
    entity with no entry."""
    if entity.entry_id is None:
        return None
    found: int | None = (
        Entity.objects.filter(pk=entity.entry_id).values_list("revision", flat=True).first()
    )
    return found


def values_at(entity: Entity, number: int, entry_revision: int | None = None) -> dict[str, Any]:
    """Every field's value as it was at revision ``number``, from the history tables: a
    record's own as of its revision, and its entry's as :func:`entry_at` says."""
    form = shape(entity.type)
    is_entry = entity.registry_id is not None
    entry = entry_at(entity, number, entry_revision) if form.has_entries else None
    out: dict[str, Any] = {}
    for part in form.parts:
        if is_entry and not part.entry:
            continue
        if part.entry and not is_entry:
            if entry is None:
                continue
            key, at = entry
        else:
            key, at = entity.pk, number
        row = (
            part.history._default_manager.filter(entity_id=key, revision__lte=at)
            .order_by("-revision")
            .first()
        )
        for name, f in part.fields.items():
            if not f.removed:
                out[name] = getattr(row, name) if row is not None else None
    return out


def changes(entity: Entity, revision: Revision) -> dict[str, tuple[Any, Any]]:
    """What a revision changed: field → (before, after)."""
    if not revision.changed_fields:
        return {}
    after = values_at(entity, revision.number)
    before = values_at(entity, revision.number - 1) if revision.number > 1 else {}
    return {
        name: (before.get(name), after.get(name))
        for name in revision.changed_fields
        if name in after
    }


#: The kinds of revision that set values.
SETTING = (
    RevisionKind.CREATED,
    RevisionKind.EDITED,
    RevisionKind.CURATED,
    RevisionKind.STATE_CHANGED,
)


def provenance(entity: Entity) -> dict[str, Revision]:
    """For each field with a value, the revision that set it: who or what, how, when. A
    record's entry's values come from the entry's revisions: see :func:`discloses` for
    what a reader is told of them."""
    out: dict[str, Revision] = {}
    sources: list[Iterable[Revision]] = [
        entity.revisions.filter(kind__in=SETTING).select_related("user").order_by("-number")
    ]
    if entity.entry_id is not None:
        sources.append(
            Revision.objects.filter(entity_id=entity.entry_id, kind__in=SETTING)
            .select_related("user")
            .order_by("-number")
        )
    for revisions in sources:
        for revision in revisions:
            for name in revision.changed_fields:
                out.setdefault(name, revision)
    return out


def discloses(revision: Revision, projects: Iterable[Any]) -> bool:
    """Whether a reader of ``projects`` is told who made a revision and when. A registry
    entry's revisions are told in full only to the project they were made through, and a
    registrar's to every holder (ADR 0115): others learn only that a value was set."""
    if revision.kind in (RevisionKind.CURATED, RevisionKind.SUPERSEDED):
        return True
    entity = Entity.objects.filter(pk=revision.entity_id).values_list("registry_id", flat=True)
    if entity.first() is None:
        return True
    return revision.project_id in set(projects)


# --- Writing ------------------------------------------------------------------------------------


def _set(instance: Model, f: Field, value: Any) -> None:  # noqa: ANN401 - a model value
    if f.type in FOREIGN_KEYS:
        setattr(instance, f"{f.name}_id", value)
    else:
        setattr(instance, f.name, value)


def _use_files(
    fields: Mapping[str, Field],
    entity_id: uuid.UUID,
    values: Mapping[str, Any],
    revision: int,
) -> None:
    """Records the files these values refer to, so the entity's readers can read them."""
    found: set[Any] = set()
    for name, value in values.items():
        f = fields[name]
        if value is None:
            continue
        if f.type == "file":
            found.add(value)
        elif f.type == "list" and f.items is not None and f.items.type == "file":
            found.update(value)
    if found:
        FileUse.objects.bulk_create(
            [FileUse(file_id=i, entity_id=entity_id, revision=revision) for i in sorted(found)],
            ignore_conflicts=True,
        )


def _write_list(part: Part, f: Field, entity_id: Any, items: list[Any]) -> None:  # noqa: ANN401
    model = part.list_model(f.name)
    model._default_manager.bulk_create(
        model(entity_id=entity_id, position=index, item_id=item) for index, item in enumerate(items)
    )


def _revise(
    entity: Model,
    kind: str,
    changed: list[str],
    user: User | None,
    reason: str,
    via: str | None = None,
    *,
    project: Project | None = None,
    registered: Revision | None = None,
    moved_from: Any = None,  # noqa: ANN401 - a UUID
    state: tuple[str, str, str] | None = None,
    signatures: tuple[Any, ...] = (),
) -> Revision:
    """The entity's next revision. ``project``: for an entry's, the project it was made
    through; ``registered``: for a record's first, the entry's first, which it made."""
    number = Entity.objects.filter(pk=entity.pk).values_list("revision", flat=True).get() + 1
    revision = Revision.objects.create(
        entity_id=entity.pk,
        number=number,
        kind=kind,
        changed_fields=changed,
        user=user,
        via=via or current_via(),
        client=current_oauth_client(),
        agent=current_agent(),
        reason=reason.strip(),
        project=project,
        moved_from_id=moved_from,
        signatures=list(signatures),
        **(
            dict(zip(("state_field", "state_from", "state_to"), state, strict=True))
            if state
            else {}
        ),
    )
    Entity.objects.filter(pk=entity.pk).update(revision=number, updated_at=Now())
    if getattr(entity, "registry_id", None) is not None:
        _announce_entry(entity.pk, revision, user)
    else:
        _announce(entity.pk, revision, user, registered=registered)
    return revision


def state_changed(
    user: User,
    entity: Entity,
    f: Field,
    old: str,
    new: str,
    reason: str,
    *,
    signatures: tuple[Any, ...] = (),
) -> Revision:
    """Writes a record's state field's new state, as a revision of its own with its history
    row (see :func:`states.set_state`, which checks it and keeps the lock). Whoever the new
    state makes it the work of is told (ADR 0122). A signed state's revision carries the
    signatures that set it, in order (ADR 0126)."""
    form = shape(entity.type)
    instance = _specific(entity)
    rows = Rows.of(instance)
    touched = _write(form, rows, {f.name: new}, instance.pk)
    revision = _revise(
        entity,
        RevisionKind.STATE_CHANGED,
        [f.name],
        user,
        reason,
        state=(f.name, old, new),
        signatures=signatures,
    )
    _write_history(touched, rows, revision.number)
    assignments.became(entity, revision, f.name, old, new, user)
    return revision


_EVENTS = {
    RevisionKind.CREATED: "entity.created",
    RevisionKind.EDITED: "entity.updated",
    RevisionKind.ARCHIVED: "entity.archived",
    RevisionKind.RESTORED: "entity.restored",
    RevisionKind.MOVED: "entity.updated",
    RevisionKind.STATE_CHANGED: "entity.state_changed",
}


#: Inside :func:`quietly`: changes too many to tell of one by one.
_quiet: ContextVar[bool] = ContextVar("iki_entities_quiet", default=False)


@contextmanager
def quietly() -> Iterator[None]:
    """Changes made inside tell no webhook and fire no trigger, one by one: a bulk import
    (ADR 0056) tells of them all in one event, and fires nothing per entity."""
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


def _announce(
    entity_id: Any,  # noqa: ANN401 - a UUID
    revision: Revision,
    user: User | None,
    *,
    registered: Revision | None = None,
) -> None:
    """Tells the owner project's and the organization's webhooks, and fires its triggers,
    in the change's transaction."""
    if _quiet.get():
        return
    from iki_server.webhooks import events
    from iki_server.webhooks.services import emit

    entity = Entity.objects.select_related("owner_project__team").get(pk=entity_id)
    data = {
        "entity": events.entity_data(entity),
        "revision": {
            "number": revision.number,
            "kind": revision.kind,
            "changed_fields": revision.changed_fields,
            "via": revision.via,
            "reason": revision.reason,
        },
        **_state_data(entity, revision),
        "actor": events.actor_data(user),
        "agent": agent_data(revision.agent),
    }
    emit(_EVENTS[RevisionKind(revision.kind)], data, project=entity.owner_project)
    # And the project's triggers that it fires (ADR 0080).
    from iki_server.runs import triggers

    triggers.noticed(entity, revision, registered=registered)


def _state_data(entity: Entity, revision: Revision) -> dict[str, Any]:
    """For a state change, which field and between which states, with their labels."""
    if revision.kind != RevisionKind.STATE_CHANGED:
        return {}
    return {"state": state_change(entity.type, revision)}


def state_change(type_name: str, revision: Revision) -> dict[str, Any]:
    """A state change's field and states, each by name and label."""
    found = (
        shape(type_name).fields().get(revision.state_field)
        if type_name in registry.types()
        else None
    )

    def state(name: str) -> dict[str, Any] | None:
        if not name:
            return None
        known = found.states.get(name) if found is not None else None
        return {"name": name, "label": known.label if known else name}

    return {
        "field": {
            "name": revision.state_field,
            "label": found.label if found is not None else revision.state_field,
        },
        "from": state(revision.state_from),
        "to": state(revision.state_to),
    }


def _announce_entry(entry_id: Any, revision: Revision, user: User | None) -> None:  # noqa: ANN401 - a UUID
    """Tells the webhooks of the projects holding an entry that its values changed (ADR
    0115): which entry, which fields, and for a registrar's change, who. A holder's change
    is told without who made it, as the other holders don't learn that. Its registration
    is told as its record's creation, to the project that registered it."""
    if _quiet.get() or revision.kind == RevisionKind.CREATED:
        return
    from iki_server.entities import entries

    entries.announce(entry_id, revision, user)


def _write_history(touched: list[Part], rows: Rows, number: int) -> None:
    """A history row for each table the revision touched, with all of that table's values."""
    for part in touched:
        owner = rows.owner(part)
        assert owner is not None  # noqa: S101
        data = {name: None if f.removed else rows.value(part, f) for name, f in part.fields.items()}
        part.history._default_manager.create(entity_id=owner.pk, revision=number, **data)
