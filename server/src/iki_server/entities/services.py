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
- **Plugins' rules** (ADR 0127). Every save is checked against its type's rules
  (:mod:`iki_server.entities.checking`): one that breaks a rule is refused, or for a
  rule that holds, kept only with a reason and held for review
  (:mod:`iki_server.entities.holds`). The database holds ``unique`` and ``no_overlap``
  rules itself (:mod:`iki_server.entities.constraints`).
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
from django.db.models import Model, Q
from django.db.models.functions import Now

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.log import agent_data, current_agent, current_oauth_client, current_via
from iki_server.core.ids import uuid7
from iki_server.entities import (
    access,
    assignments,
    checking,
    constraints,
    holds,
    identity,
    registry,
    requests,
    rules,
    states,
    unique,
)
from iki_server.entities.history import FieldNames
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.people import Namer
from iki_server.entities.values import (
    FOREIGN_KEYS,
    Invalid,
    Part,
    Refs,
    RowProblems,
    Shape,
    history_value,
    problems_of,
    read_value,
    row_columns,
    shape,
    to_history,
)
from iki_server.entities.values import is_list_table as _is_list
from iki_server.files.models import FileUse
from iki_server.notifications import declared
from iki_server.org.models import Project, registry_of
from iki_server.org.services import Refused
from iki_server.setup import starter_flows
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


class ClearReasonRequired(Invalid):
    """An edit clearing a marked Boolean needs a reason."""


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
    #: The rules it broke that hold it for review, with the reason it was saved anyway
    #: (ADR 0127).
    held: list[str] | None = None


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
    hold_reason: str = "",
    amount: Any = None,  # noqa: ANN401 - JSON: a quantity
    amount_reason: str | None = None,
    fills: frozenset[str] = frozenset(),
) -> Saved:
    """A new entity of ``type_name`` in ``project``. For a type with an identity, the
    project's record of its registry entry: a new one, or the one the project has, as it is.

    ``by_run``: an action run is writing its results (ADR 0044), for ``user``. It may set
    computed fields, and its revision is marked ``via="run"``. ``hold_reason``: why to save
    it though it breaks rules that hold records for review (ADR 0127). Without one, such a
    save is refused with :class:`checking.Held`. ``amount``: how much there is of it, for a
    type with one (ADR 0139). A receipt's ``amount_reason`` explains its ledger movement.
    ``fills``: fields of ``values`` that only fill an
    existing entry's empty ones: where it has a value, it keeps it and this isn't refused,
    as a fact that differs otherwise is (ADR 0140).
    """
    from iki_server.amounts import services as amounts

    if amount is not None and amounts.spec(type_name) is None:
        raise amounts.Wrong({"amount": [f"{type_name} has no amount."]})
    given_amount = amounts.Given.of(amount) if amount is not None else None
    form, given, holding = prepare(
        user,
        type_name,
        project,
        values,
        access.refs_for(user, project),
        by_run=by_run,
        hold_reason=hold_reason,
        defer_result_check=True,
    )
    with transaction.atomic(), _unique_values():
        if errors := requests.check_result(project, type_name, given, lock=True):
            raise Invalid(errors)
        if refused := starter_flows.refused_create(project, [type_name]):
            raise Invalid({"type": [refused]})
        saved = _create(user, form, project, values, given, reason, by_run, fills)
        if holding and not saved.reused and saved.revision is not None:
            entry_number = (
                saved.entry_revision.number if saved.registered and saved.entry_revision else None
            )
            _hold(saved, holding, user, hold_reason, saved.revision.number, entry_number)
        if given_amount is not None and not saved.reused:
            record = Entity.objects.get(pk=saved.entity.pk)
            amounts.record(
                user,
                record,
                given_amount,
                reason=reason if amount_reason is None else amount_reason,
            )
        return saved


def _create(
    user: User,
    form: Shape,
    project: Project,
    values: Mapping[str, Any],
    given: Mapping[str, Any],
    reason: str,
    by_run: bool,
    fills: frozenset[str] = frozenset(),
) -> Saved:
    if not form.has_entries:
        return _new_record(user, form, project, given, reason, by_run)
    entry = _find_entry(form, project, given)
    if entry is not None:
        # What's given for the entry fills what it hasn't got, and a fact that differs is
        # refused, whether or not the project has a record of it already: a second
        # registration never drops what it says about the identity unnoticed. Its identity
        # is the entry's, however it's spelled (ADR 0138): what it gives along a sequence
        # spelled otherwise is placed along the entry's.
        own = identity.identity_of(form.entity_type)
        stored = Rows(form, None, _specific(entry))
        kept = {n for n in fills if stored.get(n) is not None and stored.get(n) != []}
        given = {n: v for n, v in given.items() if n not in kept}
        named = {
            n: given[n]
            for n in values
            if n in form.entry_fields() and n not in own and n not in kept
        }
        entry_revision = fill_entry(
            user, form, entry, named, project=project, reason=reason, by_run=by_run
        )
        existing = Entity.objects.filter(owner_project=project, entry=entry).first()
        if existing is not None:
            return Saved(_specific(existing), reused=True, entry_revision=entry_revision)
        stored = Rows(form, None, _specific(entry))
        spelled = identity.spelled(form.entity_type, given, {n: stored.get(n) for n in own})
        given = identity.placed(form.entity_type, given, spelled, stored.get)
        saved = _new_record(
            user,
            form,
            project,
            given,
            reason,
            by_run,
            entry=entry,
            submitted=spelled or None,
        )
        saved.entry_revision = entry_revision
        return saved
    entry, entry_revision = _new_entry(user, form, project, given, reason, by_run)
    saved = _new_record(
        user, form, project, given, reason, by_run, entry=entry, registered=entry_revision
    )
    saved.registered = True
    saved.entry_revision = entry_revision
    return saved


def _hold(
    saved: Saved,
    holding: Sequence[checking.Broken],
    user: User,
    reason: str,
    revision: int,
    entry_revision: int | None,
) -> None:
    entity = Entity.objects.select_related("owner_project").get(pk=saved.entity.pk)
    holds.hold(
        entity, holding, user=user, reason=reason, revision=revision, entry_revision=entry_revision
    )
    saved.held = [b.rule.key for b in holding]


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
    submitted: dict[str, Any] | None = None,
) -> Saved:
    """The project's record: its own fields' values, and for an identity, its entry.
    ``submitted``: its identity's values as given, where they're spelled other than the
    entry's (ADR 0138)."""
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
        submitted=submitted,
    )
    _write_history(touched, rows, revision.number)
    _use_files(form.fields(), instance.pk, own, revision.number)
    ProjectLink.objects.create(entity_id=instance.pk, project=project, linked_by=user)
    assignments.changed(instance, revision, assignments.diff(instance.type, {}, own), user)
    declared.created(instance, revision, user)
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
    digest = identity.hash_for(instance, form.entity_type, _lists(form, given))
    assert digest is not None  # noqa: S101 - only types with an identity have entries
    return digest


def _lists(form: Shape, given: Mapping[str, Any]) -> dict[str, Any]:
    """The entry's lists' values, which its identity may read (ADR 0138)."""
    return {
        name: given.get(name) or []
        for part in form.parts
        if part.entry and not part.extension
        for name, f in part.fields.items()
        if _is_list(f)
    }


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
    instance.identity_hash = identity.hash_for(  # type: ignore[attr-defined]
        instance, form.entity_type, _lists(form, given)
    )
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
    current = {name: rows.get(name) for name in values}
    new, refused = entry_changes(form, current, values, explicit=explicit)
    errors = {
        name: [refusal(locked, why, form.part_of(name).fields[name])]
        for name, why in refused.items()
    }
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


def entry_changes(
    form: Shape,
    current: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    explicit: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """What ``values`` change of an entry whose values are ``current``, both as the database
    stores them (see :func:`fill_entry`): the values it sets, where the entry's are empty,
    and those it can't, by why: ``settled`` (a fact the entry has another value for) or
    ``identity`` (a value its identity gives it)."""
    new: dict[str, Any] = {}
    refused: dict[str, str] = {}
    for name, value in values.items():
        f = form.part_of(name).fields[name]
        has = current.get(name)
        empty = has is None or has == []
        if value == has or (empty and (value is None or value == [])):
            continue
        if empty:
            new[name] = value
        elif value is None or value == []:
            if explicit:
                refused[name] = "settled"
        elif f.registry == "fact":
            refused[name] = "settled"
        elif explicit or f.registry is None:
            refused[name] = "identity"
    return new, refused


def refusal(entry: Entity, why: str, f: Field | None = None) -> str:
    """Why a holder can't set an entry's value, as :func:`entry_changes` says."""
    if why == "settled":
        return settled(entry, f)
    return f"is {entry.hrid}'s registry entry's, as its identity gives it: it can't change."


def settled(entry: Entity, f: Field | None = None) -> str:
    """Why a holder can't change a fact the entry has."""
    if f is not None and f.immutable:
        return fixed(entry)
    return (
        f"is a registry fact {entry.hrid} has already: only its registry's registrars change "
        "it once it's set."
    )


def fixed(entry: Entity) -> str:
    """Why no one changes a fixed fact (ADR 0143): what's registered against it means it."""
    return (
        f"is fixed in {entry.hrid}'s registry entry, as what's registered against it reads "
        "it: no one changes it. If it's wrong, a registrar supersedes the entry with the "
        "right one."
    )


@contextmanager
def _unique_values() -> Iterator[None]:
    """Two people writing the same unique value at once: the second is told, as if it had
    been there all along (ADR 0082)."""
    try:
        yield
    except IntegrityError as error:
        found, is_rule = constraints.clash(error)
        if is_rule:
            # A plugin's `unique` or `no_overlap` rule (ADR 0127), as two saves at once
            # broke it.
            label = found.rule.label if found is not None else "a rule"
            names = found.rule.fields or found.rule.reads if found is not None else ()
            message = (
                f"Another record has just been saved that this one would break “{label}” with."
            )
            raise Invalid({n: [message] for n in names or ("",)}) from None
        if not unique.is_clash(error):
            raise
        raise Invalid(
            {"unique": ["Another entity has just been given one of these unique values."]}
        ) from None


def _observation_ids(
    form: Shape,
    f: Field,
    rows: Any,  # noqa: ANN401 - stored field value
    current: Sequence[dict[str, Any]] | None = None,
) -> Any:  # noqa: ANN401 - stored values
    """Assign IDs to new Experiment observations and keep the IDs of retained rows."""
    if (
        rows is None
        or f.name != "observations"
        or form.part_of(f.name).name != "iki/experiments.Experiment"
        or f.items is None
        or "observation_id" not in f.items.fields
    ):
        return rows

    name = "observation_id"
    existing = {row[name]: row for row in current or ()}
    used: set[str] = set()
    for index, row in enumerate(rows):
        row_id = row[name]
        if row_id is None:
            continue
        if row_id in used:
            raise RowProblems({f"{index}/{name}": ["names the same observation twice."]})
        if current is not None and row_id not in existing:
            raise RowProblems({f"{index}/{name}": ["isn't a current observation on this record."]})
        used.add(row_id)

    def content(row: dict[str, Any]) -> tuple[Any, Any]:
        return row["observed_at"], row["note"]

    # Older clients can keep an unchanged list as it is. For a changed list, incomplete
    # requests can preserve a unique content match, but cannot identify identical rows.
    if not used and current is not None and list(map(content, rows)) == list(map(content, current)):
        return list(current)
    matches: dict[tuple[Any, Any], list[str]] = defaultdict(list)
    if not used:
        for row_id, row in existing.items():
            matches[content(row)].append(row_id)
    for index, row in enumerate(rows):
        if row[name] is not None:
            continue
        candidates = matches[content(row)]
        if len(candidates) > 1:
            raise RowProblems({f"{index}/{name}": ["is required to identify this duplicate."]})
        row[name] = candidates.pop() if candidates else str(uuid7())
    return rows


def prepare(
    user: User,
    type_name: str,
    project: Project,
    values: Mapping[str, Any],
    refs: Refs,
    *,
    by_run: bool = False,
    hold_reason: str = "",
    check_rules: bool = True,
    defer_result_check: bool = False,
) -> tuple[Shape, dict[str, Any], list[checking.Broken]]:
    """A new entity's values as the database stores them, defaults filled in, checked
    against its type's fields and rules, with the plugins' rules it breaks that hold it
    for review (ADR 0127), saved anyway for ``hold_reason``. Raises :class:`Invalid` with
    what's wrong, or :class:`checking.Held` when it breaks rules that hold and there's no
    reason.

    Bulk imports (ADR 0056) check each record with this, as :func:`create` does, but
    without the plugins' rules (``check_rules=False``): they check a chunk's at once.
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
    ledger_field = registry.draws_received(type_name)
    for name, value in values.items():
        f = fields.get(name)
        if f is None:
            errors[name] = [f"{type_name} has no field `{name}`."]
        elif name in kept:
            errors[name] = [kept[name]]
        elif name == ledger_field:
            errors[name] = ["is recorded by the amounts ledger when material is drawn into it."]
        elif f.computed and not by_run:
            errors[name] = ["is computed: only actions set it."]
        elif f.type == "state":
            errors[name] = [STATE_SET_ALONE]
        elif requests.is_service_field(type_name, name):
            errors[name] = [
                "is the service's: the project the request is sent to sets it, once it's submitted."
            ]
        else:
            try:
                given[name] = _observation_ids(form, f, form.field_value(f, value, refs))
            except ValueError as error:
                errors.update(problems_of(name, error))
    for name, f in fields.items():
        if name in kept:
            continue
        if f.type == "state" and not f.removed:
            given[name] = f.initial  # Where new records start (ADR 0125).
        elif name not in values and f.has_default and not f.computed:
            try:
                given[name] = form.field_value(f, f.default, refs)
            except ValueError as error:
                errors.update(problems_of(name, error))
        if f.required and given.get(name) is None and not _wrong(errors, name):
            errors[name] = ["is required."]
    if not errors:
        from iki_server.amounts import stock

        errors = stock.check_item(type_name, project, given)
    if not errors:
        errors = requests.check_work_item(project, type_name, given)
    if not errors and not defer_result_check:
        errors = requests.check_result(project, type_name, given)
    if not errors:
        errors = rules.check(user, entity_type, project, given.get)
    if errors:
        raise Invalid(errors)
    if not check_rules:
        return form, given, []
    own = {n: v for n, v in given.items() if not form.part_of(n).entry}
    found = [
        *checking.broken(type_name, [checking.Saving(given.get)])[0],
        *checking.apart(project.pk, form, own),
    ]
    holding = checking.raise_broken(found, hold_reason=hold_reason)
    return form, given, holding


def _wrong(errors: Mapping[str, list[str]], name: str) -> bool:
    """Whether ``errors`` say what's wrong with a field, or with one of its rows' (ADR 0137)."""
    return name in errors or any(k.startswith(f"{name}/") for k in errors)


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
    hold_reason: str = "",
    acting_in: Project | None = None,
    taking: bool = False,
    allow_correction: bool = False,
    _recording_draw: bool = False,
    _recheck_note: str = "",
) -> Saved:
    """Changes some of an entity's fields. Fields not named keep their values. Its registry
    entry's are set only while they're empty (see :func:`fill_entry`).

    ``by_run`` and ``hold_reason``: as for :func:`create`. ``group``: a bulk change it's
    part of, which the audit records of its assignments name (ADR 0122). The plugins'
    rules checked are those reading a field it changes (ADR 0127). A request sent to a
    service (ADR 0132) is changed by two sides, each only in its own fields: ``user``
    writes as the sides they're an Editor of, or as ``acting_in``'s alone (a run in that
    project). ``taking``: someone takes the record in an assignment field (ADR 0122), as
    its lab role lets them. A direct edit can opt into a declared reasoned return
    with ``allow_correction``. Other operations keep every state lock.
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
        returns: dict[str, str] = {}
        if (
            allow_correction
            and not by_run
            and locked.locked_by
            and locked.released_review_revision is None
        ):
            returns = states.on_edit(states.state_fields(locked.type), states.values_of(locked))
        if not returns or set(locked.locked_by) != returns.keys():
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
        checked_entry_revision = None
        if locked.entry_id is not None and any(
            rule.hold for rule in checking.checks_of(locked.type)
        ):
            # A held observation names the entry revision read when the rule was checked.
            checked_entry_revision = (
                Entity.objects.select_for_update(no_key=True)
                .values_list("revision", flat=True)
                .get(pk=locked.entry_id)
            )
        instance = _specific(locked)
        rows = Rows.of(instance)
        project = owner_of(locked)
        refs = access.refs_for(user, project)
        asked = requests.spec_of(locked.type) is not None
        request = requests.of(locked) if asked else None
        allowed = (
            None
            if not asked
            else frozenset(requests.side_of(locked.type, n) for n in values)
            if taking
            else requests.sides(user, locked, request, acting_in=acting_in)
        )
        errors: dict[str, list[str]] = (
            requests.refused_fields(locked, [n for n in values if n in fields], allowed, request)
            if allowed is not None
            else {}
        )
        # The service's fields name its people, in its project (ADR 0132, 0122).
        service_refs = (
            Refs(Entity.objects.none(), project=request.service.name, namer=Namer(request.service))
            if request is not None
            else refs
        )
        new: dict[str, Any] = {}
        for_entry: dict[str, Any] = {}
        ledger_field = registry.draws_received(locked.type)
        for name, value in values.items():
            f = fields.get(name)
            if f is None:
                errors[name] = [f"{locked.type} has no field `{name}`."]
                continue
            if name in errors:
                continue
            if name in kept:
                errors[name] = [kept[name]]
                continue
            if name == ledger_field and not _recording_draw:
                errors[name] = ["is recorded by the amounts ledger when material is drawn into it."]
                continue
            if f.computed and not by_run and name not in (ledger_field, _recheck_note):
                errors[name] = ["is computed: only actions set it."]
                continue
            if f.type == "state":
                errors[name] = [STATE_SET_ALONE]
                continue
            part = form.part_of(name)
            current = rows.value(part, f)
            if name in identifying:
                try:
                    converted = form.field_value(f, value, refs)
                except ValueError as error:
                    errors.update(problems_of(name, error))
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
            using = service_refs if requests.is_service_field(locked.type, name) else refs
            try:
                # Whoever it names already may stay named, though they couldn't be named
                # anew (ADR 0122): an edit of something else keeps them.
                with using.namer.keeping(_named_in(f, current)):
                    converted = _observation_ids(
                        form, f, form.field_value(f, value, using), current
                    )
            except ValueError as error:
                errors.update(problems_of(name, error))
                continue
            # A list stored as item rows reads back as [] when cleared, including
            # when the client sends null. Compare what will actually be stored.
            if converted is None and not f.required and _is_list(f):
                converted = []
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
        if errors:
            raise Invalid(errors)
        if not reason.strip() and any(
            fields[name].reason_on_clear and rows.get(name) is True for name in new
        ):
            raise ClearReasonRequired({"reason": ["Give a reason for clearing this flag."]})
        from iki_server.runs import triggers

        rechecks = triggers.rechecks(locked.type, set(new)) if new else []
        for spec, outputs in rechecks:
            for name in outputs:
                value = "Needs re-verification" if name == spec.recheck else None
                if rows.get(name) != value or name in new:
                    new[name] = value
        changing = {**new, **for_entry}
        if changing:
            if returns:
                states.return_for_edit(user, locked, returns, reason)
                # _write saves the typed row, including its state and entity revision.
                # Its in-memory value must include the returns recorded above.
                locked.refresh_from_db()
                instance = _specific(locked)
                rows = Rows.of(instance)
                changing.update(returns)
            errors = rules.check(
                user,
                form.entity_type,
                project,
                lambda n: changing[n] if n in changing else rows.get(n),
                changed=changing,
            )
        if errors:
            raise Invalid(errors)
        holding: list[checking.Broken] = []
        found: list[checking.Broken] = []
        changed: frozenset[str] = frozenset()
        if changing:
            current = lambda n: changing[n] if n in changing else rows.get(n)  # noqa: E731
            changed = frozenset(changing)
            found = [
                *checking.broken(
                    locked.type,
                    [checking.Saving(current, changed, id=locked.pk, hrid=locked.hrid)],
                )[0],
                *checking.apart(
                    project.pk,
                    form,
                    {n: current(n) for n in fields if not form.part_of(n).entry},
                    entity=locked.pk,
                    changed=changed,
                ),
            ]
            holding = checking.raise_broken(found, hold_reason=hold_reason)
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
        held_entry_revision = entry_revision.number if entry_revision else checked_entry_revision
        if not new:
            saved = Saved(_specific(locked), entry_revision=entry_revision)
            checking.kept(locked, changed, found)
            if holding:
                # Its registry entry's values held it: by the record as it is.
                _hold(saved, holding, user, hold_reason, locked.revision, held_entry_revision)
            return saved
        was = {n: rows.get(n) for n in new}
        before = {n: v for n, v in was.items() if names_people(fields[n])}
        with requests.writing(allowed or frozenset()):
            touched = _write(form, rows, new, instance.pk)
        revision = _revise(
            locked, RevisionKind.EDITED, sorted(new), user, reason, via="run" if by_run else None
        )
        _write_history(touched, rows, revision.number)
        if request is not None:
            # What it sends now goes to the service with it.
            requests.send(user, locked, current_values(_specific(locked)))
        _use_files(fields, locked.pk, new, revision.number)
        assignments.changed(
            locked, revision, assignments.diff(locked.type, before, new), user, group=group
        )
        saved = Saved(_specific(locked), revision, entry_revision=entry_revision)
        checking.kept(locked, changed, found)
        if holding:
            _hold(saved, holding, user, hold_reason, revision.number, held_entry_revision)
        declared.changed(locked, revision, {n: (was.get(n), v) for n, v in new.items()}, user)
        # A failed launch may append a diagnostic. The action's effect still names its own edit.
        updated = saved
        for spec, _ in rechecks:
            problem = triggers.recheck_edit(user, locked, revision, spec)
            if problem:
                with log.using(log.Context(via="trigger")):
                    saved = update(
                        user,
                        locked,
                        {spec.recheck: f"Needs re-verification. {problem}"[:10000]},
                        _recheck_note=spec.recheck,
                        expected_revision=saved.revision.number
                        if saved.revision
                        else revision.number,
                        reason=revision.reason,
                    )
    return updated if by_run else saved


def _named_in(f: Field, current: Any) -> set[Any]:  # noqa: ANN401 - a model value
    """Whom a field naming people names now, or a list of rows' people (ADR 0137): they
    may stay named."""
    if f.is_rows and current:
        assert f.items is not None  # noqa: S101 - lists have items
        people = [n for n, cell in f.items.fields.items() if cell.type == "person"]
        return {row[n] for row in current for n in people if row.get(n) is not None}
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
    (ADR 0115); if the user reads it only through a request, or it's a request's result
    its service hasn't released (ADR 0132). Released, a result reaches the project that
    asked by its release alone."""
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
            raise Refused(unshareable(user, entity, project))
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


def unshareable(user: User, entity: Entity, project: Project) -> str:
    """Why ``project`` may not use an entity the user reads (:func:`access.why_held`)."""
    owner = owner_of(entity)
    match access.why_held(user, entity, project):
        case "work_item":
            return f"{entity.hrid} stays in {owner.name}: it's an internal work item."
        case "unreleased":
            return not_released(entity, project)
        case "registry":
            return other_registry(entity, owner, project)
        case "request":
            return came_with_a_request(entity, project)
        case _:
            return not_shared(entity, owner)


def not_released(entity: Entity, project: Project) -> str:
    return (
        f"{entity.hrid} can't be shared with {project.name}: it's a request's result its "
        "service hasn't released, and it's the service's alone until then. Releasing it "
        "shares it with the project that asked (ADR 0132)."
    )


def came_with_a_request(entity: Entity, project: Project) -> str:
    return (
        f"{entity.hrid} can't be shared with {project.name}: you read it through a request "
        "between projects, which shares it with that project alone (ADR 0132)."
    )


def other_registry(entity: Entity, owner: Project, project: Project) -> str:
    return (
        f"{entity.hrid} can't be shared with {project.name}: it's in another registry from "
        f"{owner.name}, and registries share nothing (ADR 0115)."
    )


def unlink(user: User, entity: Entity, project: Project) -> bool:
    """Takes the entity out of a project. True if it was there. What a request linked there
    stays while the request needs it (ADR 0132)."""
    if project.pk == entity.owner_project_id:
        raise Refused("An entity always belongs to its owner project.")
    with transaction.atomic():
        found = ProjectLink.objects.filter(entity=entity, project=project).first()
        why = requests.why_kept(found) if found is not None else None
        if why:
            raise Refused(why)
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
                    rows = rows.order_by("entity_id", "position")
                    if f.is_rows:
                        columns = row_columns(f)
                        for found in rows.values("entity_id", *columns.values()):
                            items[found["entity_id"]].append(
                                {n: found[c] for n, c in columns.items()}
                            )
                    else:
                        for entity_id, item in rows.values_list("entity_id", "item_id"):
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


def values_at(
    entity: Entity, number: int, entry_revision: int | None = None, *, removed: bool = False
) -> dict[str, Any]:
    """Every field's value as it was at revision ``number``, from the history tables: a
    record's own as of its revision, and its entry's as :func:`entry_at` says. With
    ``removed``, fields since removed too, as history kept them: null in revisions after."""
    values, _ = values_and_entry_at(entity, number, entry_revision, removed=removed)
    return values


def values_and_entry_at(
    entity: Entity, number: int, entry_revision: int | None = None, *, removed: bool = False
) -> tuple[dict[str, Any], tuple[uuid.UUID, int] | None]:
    """Read both the record values and the historical entry they were composed from."""
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
            if removed or not f.removed:
                out[name] = history_value(row, f) if row is not None else None
    return out, entry


def revision_values(entity: Entity, number: int, fields: Mapping[str, Field]) -> dict[str, Any]:
    """Revision ``number``'s values of ``fields``, as reading it back gives them: every field
    the type has now, and each field since removed that held a value then (ADR 0035)."""
    return {
        n: v
        for n, v in values_at(entity, number, removed=True).items()
        if n in fields and (v is not None or not fields[n].removed)
    }


def changes(entity: Entity, revision: Revision) -> dict[str, tuple[Any, Any]]:
    """What a revision changed: field → (before, after), fields since removed included."""
    if not revision.changed_fields:
        return {}
    after = values_at(entity, revision.number, removed=True)
    before = values_at(entity, revision.number - 1, removed=True) if revision.number > 1 else {}
    return {
        name: (before.get(name), after.get(name))
        for name in FieldNames.for_type(entity.type, entry=entity.registry_id is not None).changed(
            revision
        )
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
    for i, revisions in enumerate(sources):
        names = FieldNames.for_type(entity.type, entry=i == 1 or entity.registry_id is not None)
        for revision in revisions:
            for name in names.changed(revision):
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


def disclosing(projects: Iterable[Any]) -> Q:
    """Which of a registry entry's revisions a reader of ``projects`` is told who made, as
    :func:`discloses` says of one: for filtering by who made them."""
    return Q(kind__in=(RevisionKind.CURATED, RevisionKind.SUPERSEDED)) | Q(
        project_id__in=list(projects)
    )


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
        elif f.is_rows:
            assert f.items is not None  # noqa: S101 - lists have items
            files = [n for n, cell in f.items.fields.items() if cell.type == "file"]
            found.update(row[n] for row in value for n in files if row.get(n) is not None)
    if found:
        FileUse.objects.bulk_create(
            [FileUse(file_id=i, entity_id=entity_id, revision=revision) for i in sorted(found)],
            ignore_conflicts=True,
        )


def _write_list(part: Part, f: Field, entity_id: Any, items: list[Any]) -> None:  # noqa: ANN401
    model = part.list_model(f.name)
    if f.is_rows:
        columns = row_columns(f)
        model._default_manager.bulk_create(
            model(entity_id=entity_id, position=index, **{c: row[n] for n, c in columns.items()})
            for index, row in enumerate(items)
        )
        return
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
    submitted: dict[str, Any] | None = None,
) -> Revision:
    """The entity's next revision. ``project``: for an entry's, the project it was made
    through; ``registered``: for a record's first, the entry's first, which it made."""
    current = Entity.objects.only("revision", "has_been_locked").get(pk=entity.pk)
    if kind == RevisionKind.EDITED and not reason.strip() and current.has_been_locked:
        raise states.CorrectionReason(
            {"reason": ["Give a reason for editing this record after its lock was lifted."]}
        )
    number = current.revision + 1
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
        submitted=submitted,
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
    declared.changed(entity, revision, {f.name: (old or None, new)}, user)
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
    triggers.source_changed(entity, revision)


def _state_data(entity: Entity, revision: Revision) -> dict[str, Any]:
    """For a state change, which field and between which states, with their labels."""
    if revision.kind != RevisionKind.STATE_CHANGED:
        return {}
    return {"state": state_change(entity.type, revision)}


def state_change(
    type_name: str, revision: Revision, names: FieldNames | None = None
) -> dict[str, Any]:
    """A state change's field and states, each by name and label."""
    field = (names or FieldNames.for_type(type_name)).current(
        revision.state_field, revision.history_seen
    )
    found = shape(type_name).fields().get(field) if type_name in registry.types() else None

    def state(name: str) -> dict[str, Any] | None:
        if not name:
            return None
        known = found.states.get(name) if found is not None else None
        return {"name": name, "label": known.label if known else name}

    return {
        "field": {
            "name": field,
            "label": found.label if found is not None else field,
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
    from iki_server.runs import triggers

    triggers.source_changed(Entity.objects.get(pk=entry_id), revision)


def _write_history(touched: list[Part], rows: Rows, number: int) -> None:
    """A history row for each table the revision touched, with all of that table's values."""
    for part in touched:
        owner = rows.owner(part)
        assert owner is not None  # noqa: S101
        data = {
            name: None if f.removed else to_history(f, rows.value(part, f))
            for name, f in part.fields.items()
        }
        part.history._default_manager.create(entity_id=owner.pk, revision=number, **data)
