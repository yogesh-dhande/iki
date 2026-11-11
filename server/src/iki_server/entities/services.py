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
- **Duplicates** (entities with the same identity), by the type's
  ``on_duplicate``:
  - ``reuse`` (the default) returns the existing entity and links it into
    the project.
  - ``warn`` refuses (:class:`Duplicate`) unless the caller says to go ahead.
  - ``allow`` creates another.
  Only entities the caller can read count, so a duplicate never reveals one
  they can't.
- **Archiving, not deleting** (ADR 0008). Archived entities keep their
  history, and can't change until restored.

Callers check permissions first (:mod:`iki_server.entities.access`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction
from django.db.models import Model
from django.db.models.functions import Now

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.audit.log import current_via
from iki_server.entities import access, identity, registry, rules
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.values import Invalid, Part, Refs, Shape, read_value, shape, to_db
from iki_server.entities.values import is_list_table as _is_list
from iki_server.files.models import FileUse
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_spec.model import Field

__all__ = [
    "Duplicate",
    "Invalid",
    "Saved",
    "Stale",
    "changes",
    "create",
    "current_values",
    "current_values_of",
    "link",
    "provenance",
    "set_archived",
    "unlink",
    "update",
    "values_at",
]


class Stale(Exception):  # noqa: N818 - reads as "the edit was stale"
    """The entity changed since the revision the edit was made against."""

    def __init__(self, current: int) -> None:
        super().__init__(f"The entity changed since: it's at revision {current} now.")
        self.current = current


class Duplicate(Exception):  # noqa: N818 - reads as "it's a duplicate"
    """Entities with the same identity exist, that the caller can read."""

    def __init__(self, existing: list[Entity]) -> None:
        names = ", ".join(e.hrid for e in existing)
        super().__init__(f"The same entity already exists: {names}.")
        self.existing = existing


@dataclass
class Saved:
    entity: Model
    #: The revision the change made, or None when nothing changed (or an entity was reused).
    revision: Revision | None = None
    #: The entity existed already, and was returned (and linked) instead of a new one.
    reused: bool = False
    linked: bool = False


def _specific(entity: Entity) -> Model:
    return registry.model(entity.type)._default_manager.get(pk=entity.pk)


# --- Creating -----------------------------------------------------------------------------------


def create(
    user: User,
    type_name: str,
    project: Project,
    values: Mapping[str, Any],
    *,
    reason: str = "",
    allow_duplicate: bool = False,
    by_run: bool = False,
) -> Saved:
    """A new entity of ``type_name`` in ``project``, or the existing one it duplicates.

    ``by_run``: an action run is writing its results (ADR 0044), for ``user``. It may set
    computed fields, and its revision is marked ``via="run"``.
    """
    if type_name not in registry.types() or type_name == "core.Entity":
        raise Invalid({"type": [f"There's no entity type {type_name}."]})
    form = shape(type_name)
    entity_type = form.entity_type
    if entity_type.abstract:
        raise Invalid({"type": [f"{type_name} is abstract: create one of its subtypes."]})
    fields = form.fields()
    refs = Refs(access.visible(user), access.readable_files(user))
    errors: dict[str, list[str]] = {}
    given: dict[str, Any] = {}
    for name, value in values.items():
        f = fields.get(name)
        if f is None:
            errors[name] = [f"{type_name} has no field `{name}`."]
        elif f.computed and not by_run:
            errors[name] = ["is computed: only actions set it."]
        else:
            try:
                given[name] = to_db(f, value, refs)
            except ValueError as error:
                errors[name] = [str(error)]
    for name, f in fields.items():
        if name not in values and f.has_default and not f.computed:
            given[name] = to_db(f, f.default, refs)
        if f.required and given.get(name) is None and name not in errors:
            errors[name] = ["is required."]
    if not errors:
        errors = rules.check(user, entity_type, given.get)
    if errors:
        raise Invalid(errors)

    with transaction.atomic():
        instance = form.model(owner_project=project, created_by=user)
        chain = [p for p in form.parts if not p.extension]
        for part in chain:
            for name, f in part.fields.items():
                if name in given and not _is_list(f):
                    _set(instance, f, given[name])
        instance.identity_hash = identity.hash_for(instance, entity_type)
        policy = registry.duplicate_policy(entity_type) or "allow"
        found = _duplicate(user, instance, policy, allow_duplicate)
        if found is not None:
            return _reuse(user, found, project)
        instance.save()
        touched = list(chain)
        for part in form.parts:
            if part.extension and any(given.get(n) is not None for n in part.fields):
                row = part.model(entity_id=instance.pk)
                for name, f in part.fields.items():
                    if name in given and not _is_list(f):
                        _set(row, f, given[name])
                row.save()
                touched.append(part)
            for name, f in part.fields.items():
                if _is_list(f) and given.get(name):
                    _write_list(part, f, instance.pk, given[name])
        changed = sorted(n for n, v in given.items() if v is not None)
        revision = _revise(
            instance, RevisionKind.CREATED, changed, user, reason, via="run" if by_run else None
        )
        _write_history(form, touched, instance, revision.number)
        _use_files(fields, instance.pk, given, revision.number)
        ProjectLink.objects.create(entity_id=instance.pk, project=project, linked_by=user)
    return Saved(_specific(instance), revision)


def _duplicate(user: User, instance: Model, policy: str, allow: bool) -> Entity | None:
    """The existing entity to reuse, if any. Raises :class:`Duplicate` for ``warn``."""
    digest = instance.identity_hash  # type: ignore[attr-defined]
    if digest is None or policy == "allow":
        return None
    with connection.cursor() as cursor:
        # Two people creating the same entity at once: the second waits, then finds it.
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [digest])
    existing = list(
        access.visible(user)
        .filter(identity_hash=digest, archived_at__isnull=True)
        .order_by("created_at", "pk")
    )
    if not existing:
        return None
    if policy == "warn" and not allow:
        raise Duplicate(existing)
    return existing[0] if policy == "reuse" else None


def _reuse(user: User, existing: Entity, project: Project) -> Saved:
    linked = link(user, existing, project)
    return Saved(_specific(existing), reused=True, linked=linked)


# --- Editing ------------------------------------------------------------------------------------


def update(
    user: User,
    entity: Entity,
    values: Mapping[str, Any],
    *,
    expected_revision: int,
    reason: str = "",
    by_run: bool = False,
) -> Saved:
    """Changes some of an entity's fields. Fields not named keep their values.

    ``by_run``: as for :func:`create`.
    """
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=entity.pk)
        if locked.revision != expected_revision:
            raise Stale(locked.revision)
        if locked.archived_at is not None:
            raise Refused("The entity is archived: restore it before changing it.")
        form = shape(locked.type)
        fields = form.fields()
        instance = _specific(locked)
        rows = _extension_rows(form, instance.pk)
        refs = Refs(access.visible(user), access.readable_files(user))
        errors: dict[str, list[str]] = {}
        new: dict[str, Any] = {}
        for name, value in values.items():
            f = fields.get(name)
            if f is None:
                errors[name] = [f"{locked.type} has no field `{name}`."]
                continue
            if f.computed and not by_run:
                errors[name] = ["is computed: only actions set it."]
                continue
            try:
                converted = to_db(f, value, refs)
            except ValueError as error:
                errors[name] = [str(error)]
                continue
            part = form.part_of(name)
            current = _current(part, f, instance, rows)
            if converted == current:
                continue
            if f.immutable and current is not None:
                errors[name] = ["can't change once it's set."]
            elif f.required and converted is None:
                errors[name] = ["is required."]
            else:
                new[name] = converted
        if not errors and new:
            errors = rules.check(
                user,
                form.entity_type,
                lambda n: (
                    new[n] if n in new else _current(form.part_of(n), fields[n], instance, rows)
                ),
                changed=new,
            )
        if errors:
            raise Invalid(errors)
        if not new:
            return Saved(instance)
        touched: list[Part] = []
        for part in form.parts:
            mine = {n: v for n, v in new.items() if n in part.fields}
            if not mine:
                continue
            touched.append(part)
            target: Model = instance
            if part.extension:
                target = rows.get(part.model) or part.model(entity_id=instance.pk)
                rows[part.model] = target
            for name, value in mine.items():
                f = part.fields[name]
                if _is_list(f):
                    part.list_model(name)._default_manager.filter(entity_id=instance.pk).delete()
                    if value:
                        _write_list(part, f, instance.pk, value)
                else:
                    _set(target, f, value)
            if part.extension:
                target.save()
        # Identity fields are required and immutable (the spec checks), so the hash stays put.
        if any(not p.extension for p in touched):
            instance.save()
        revision = _revise(
            locked, RevisionKind.EDITED, sorted(new), user, reason, via="run" if by_run else None
        )
        _write_history(form, touched, instance, revision.number, rows=rows)
        _use_files(fields, locked.pk, new, revision.number)
    return Saved(_specific(locked), revision)


def set_archived(
    user: User,
    entity: Entity,
    archived: bool,
    *,
    reason: str = "",
    expected_revision: int | None = None,
) -> Saved:
    """Archives or restores an entity. Archived entities keep their history and can't change."""
    with transaction.atomic():
        locked = Entity.objects.select_for_update().get(pk=entity.pk)
        if expected_revision is not None and locked.revision != expected_revision:
            raise Stale(locked.revision)
        if (locked.archived_at is not None) == archived:
            return Saved(_specific(locked))
        locked.archived_at = dt.datetime.now(dt.UTC) if archived else None
        locked.save(update_fields=["archived_at"])
        kind = RevisionKind.ARCHIVED if archived else RevisionKind.RESTORED
        revision = _revise(locked, kind, [], user, reason)
    return Saved(_specific(locked), revision)


def link(user: User, entity: Entity, project: Project) -> bool:
    """Adds the entity to another project, so its members can read it. True if it's new there.

    It changes who can read the entity, so it's audited."""
    with transaction.atomic():
        _, created = ProjectLink.objects.get_or_create(
            entity=entity, project=project, defaults={"linked_by": user}
        )
        if created:
            log.record("entity.linked", actor=user, target=entity, project=project)
    return created


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
    """Every current field's value, as the database has it (see ``values.to_json``)."""
    form = shape(entity.type)  # type: ignore[attr-defined]
    rows = _extension_rows(form, entity.pk)
    return {
        name: _current(form.part_of(name), f, entity, rows) for name, f in form.fields().items()
    }


def current_values_of(entities: Sequence[Entity]) -> dict[Any, dict[str, Any]]:
    """:func:`current_values` for many entities, in a few queries per type, not per entity."""
    by_type: dict[str, list[Any]] = defaultdict(list)
    for entity in entities:
        by_type[entity.type].append(entity.pk)
    out: dict[Any, dict[str, Any]] = {}
    for type_name, ids in by_type.items():
        form = shape(type_name)
        holders: dict[type[Model], dict[Any, Model]] = {
            form.model: {o.pk: o for o in form.model._default_manager.filter(pk__in=ids)}
        }
        lists: dict[str, dict[Any, list[Any]]] = {}
        for part in form.parts:
            if part.extension:
                holders[part.model] = {
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
        for pk in ids:
            values: dict[str, Any] = {}
            for name, f in form.fields().items():
                part = form.part_of(name)
                if name in lists:
                    values[name] = lists[name].get(pk, [])
                    continue
                holder = holders[part.model if part.extension else form.model].get(pk)
                if holder is None:
                    values[name] = None
                elif f.type in ("ref", "file"):
                    values[name] = getattr(holder, f"{name}_id")
                else:
                    values[name] = getattr(holder, name)
            out[pk] = values
    return out


def values_at(entity: Entity, number: int) -> dict[str, Any]:
    """Every field's value as it was at revision ``number``, from the history tables."""
    form = shape(entity.type)
    out: dict[str, Any] = {}
    for part in form.parts:
        row = (
            part.history._default_manager.filter(entity_id=entity.pk, revision__lte=number)
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


def provenance(entity: Entity) -> dict[str, Revision]:
    """For each field with a value, the revision that set it: who or what, how, when."""
    out: dict[str, Revision] = {}
    revisions = entity.revisions.filter(
        kind__in=[RevisionKind.CREATED, RevisionKind.EDITED]
    ).select_related("user")
    for revision in revisions.order_by("-number"):
        for name in revision.changed_fields:
            out.setdefault(name, revision)
    return out


# --- Writing ------------------------------------------------------------------------------------


def _set(instance: Model, f: Field, value: Any) -> None:  # noqa: ANN401 - a model value
    if f.type in ("ref", "file"):
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


def _extension_rows(form: Shape, entity_id: Any) -> dict[type[Model], Model]:  # noqa: ANN401
    rows: dict[type[Model], Model] = {}
    for part in form.parts:
        if part.extension:
            row = part.model._default_manager.filter(entity_id=entity_id).first()
            if row is not None:
                rows[part.model] = row
    return rows


def _current(part: Part, f: Field, instance: Model, rows: Mapping[type[Model], Model]) -> Any:  # noqa: ANN401
    if part.extension:
        row = rows.get(part.model)
        if row is None:
            return None
        return read_value(row, f, part)
    return read_value(instance, f, part)


def _revise(
    entity: Model, kind: str, changed: list[str], user: User, reason: str, via: str | None = None
) -> Revision:
    number = Entity.objects.filter(pk=entity.pk).values_list("revision", flat=True).get() + 1
    revision = Revision.objects.create(
        entity_id=entity.pk,
        number=number,
        kind=kind,
        changed_fields=changed,
        user=user,
        via=via or current_via(),
        reason=reason.strip(),
    )
    Entity.objects.filter(pk=entity.pk).update(revision=number, updated_at=Now())
    _announce(entity.pk, revision, user)
    return revision


_EVENTS = {
    RevisionKind.CREATED: "entity.created",
    RevisionKind.EDITED: "entity.updated",
    RevisionKind.ARCHIVED: "entity.archived",
    RevisionKind.RESTORED: "entity.restored",
}


def _announce(entity_id: Any, revision: Revision, user: User) -> None:  # noqa: ANN401 - a UUID
    """Tells the owner project's and the organization's webhooks, in the change's transaction."""
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
        "actor": events.actor_data(user),
    }
    emit(_EVENTS[RevisionKind(revision.kind)], data, project=entity.owner_project)


def _write_history(
    form: Shape,
    touched: list[Part],
    instance: Model,
    number: int,
    *,
    rows: Mapping[type[Model], Model] | None = None,
) -> None:
    """A history row for each table the revision touched, with all of that table's values."""
    rows = rows if rows is not None else _extension_rows(form, instance.pk)
    for part in touched:
        data = {
            name: None if f.removed else _current(part, f, instance, rows)
            for name, f in part.fields.items()
        }
        part.history._default_manager.create(entity_id=instance.pk, revision=number, **data)
