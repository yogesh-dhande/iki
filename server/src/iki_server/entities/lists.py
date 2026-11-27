"""Lab lists (ADR 0270): the dropdowns a lab curates itself.

A list is an entity type marked ``list`` in its manifest. Its entities are its
options, each a registry entry (ADR 0115) whose code is its identity and whose
label, description and place in the list are facts, as are the type's other
fields. A field that uses a list is an ordinary reference to it.

- **Reading.** Every project in a registry reads its lists' options, and its
  records refer to them, without holding a record of each
  (:mod:`.access`). A dropdown offers the active ones, in the list's order;
  archived and superseded ones stay on the records that name them.
- **Curating.** The registry's registrars curate every list in it, and each
  list's curators that one (:func:`permissions.can_curate_list`): they add
  options, relabel and describe them, reorder the list, archive and restore
  options, and supersede a wrong code with the right one. Each change but an
  addition gives a reason, and is a revision of the option and an audit event.
- **Starting options.** A plugin's manifest gives a list's first options. Every
  deploy adds those a registry hasn't got, by code (:func:`seed`): a new
  registry gets them all, an upgrade its new ones, at the end of the list. A
  registry's own options, and what its curators did to the plugin's, are never
  changed by it.
- **Moving references.** A superseded option's records keep naming it until a
  project's Editor moves them to its replacement (:func:`repoint`), as a
  project moves its record of a superseded entry (ADR 0115).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Count, Max, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Lower

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import entries, identity, registry, services
from iki_server.entities.models import Entity, Revision, RevisionKind
from iki_server.entities.values import (
    Invalid,
    Refs,
    Shape,
    is_list_table,
    problems_of,
    shape,
    to_db,
    to_json,
)
from iki_server.org import permissions
from iki_server.org.models import Project, Registry
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_spec.model import (
    LIST_CODE,
    LIST_DESCRIPTION,
    LIST_FIELDS,
    LIST_LABEL,
    LIST_MAX_OPTIONS,
    LIST_POSITION,
    EntityType,
)

__all__ = [
    "ORDER",
    "Option",
    "add",
    "archive",
    "as_options",
    "change",
    "codes_of",
    "find",
    "is_list",
    "list_type",
    "names_of",
    "option_rows",
    "options",
    "reorder",
    "repoint",
    "seed",
    "types",
    "usage",
]


@dataclass(frozen=True)
class Option:
    """One of a list's options, as its readers see it."""

    entry: Entity
    code: str
    label: str
    description: str
    position: int
    archived: bool
    #: The option that replaced it, if a curator superseded it.
    superseded_by: Entity | None
    #: Its values for the list's other fields, as JSON: a reference is the option's ID.
    values: dict[str, Any]

    @property
    def active(self) -> bool:
        """Whether a dropdown offers it: it's neither archived nor superseded."""
        return not self.archived and self.superseded_by is None


def types() -> dict[str, EntityType]:
    """Every installed list, by its type's qualified name."""
    return {name: t for name, t in registry.types().items() if t.is_list}


def is_list(type_name: str) -> bool:
    found = registry.types().get(type_name)
    return found is not None and found.is_list


def list_type(type_name: str) -> EntityType:
    """The list ``type_name`` names. Raises :class:`Refused` for another type."""
    found = registry.types().get(type_name)
    if found is None:
        raise Refused(f"There's no entity type {type_name}.")
    if not found.is_list:
        raise Refused(f"{type_name} isn't a lab list.")
    return found


def entries_in(registry_id: Any) -> QuerySet[Entity]:  # noqa: ANN401 - a UUID
    """The options of every list in a registry, as entities."""
    return Entity.objects.filter(registry_id=registry_id, type__in=list(types()))


#: What a list's options sort by, in its order: for :func:`option_rows`' paging.
ORDER = ("list_position", LIST_CODE, "pk")


def option_rows(
    registry_id: Any,  # noqa: ANN401 - a UUID
    type_name: str,
    *,
    archived: bool = False,
    ids: Iterable[Any] | None = None,
    q: str | None = None,
) -> QuerySet[Any]:
    """A list's options in a registry, as rows to sort by :data:`ORDER`: the active ones, or
    with ``archived`` every one, archived and superseded ones too. ``ids``: only these;
    ``q``: only those whose label or code has it in it, in any case. :func:`as_options`
    reads them."""
    list_type(type_name)
    model = shape(type_name).entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    found = model._default_manager.filter(registry_id=registry_id, type=type_name).filter(
        purged_at__isnull=True
    )
    if ids is not None:
        found = found.filter(pk__in=list(ids))
    if not archived:
        found = found.filter(archived_at__isnull=True, superseded_by__isnull=True)
    if q and (text := q.strip()):
        found = found.filter(
            Q(**{f"{LIST_LABEL}__icontains": text}) | Q(**{f"{LIST_CODE}__icontains": text})
        )
    return found.select_related("superseded_by").annotate(
        list_position=Coalesce(LIST_POSITION, Value(0))
    )


def as_options(type_name: str, rows: Sequence[Any]) -> list[Option]:
    """Rows of :func:`option_rows`, as options."""
    form = shape(type_name)
    return [_option(form, row, values) for row, values in _with_values(form, rows)]


def options(
    registry_id: Any,  # noqa: ANN401 - a UUID
    type_name: str,
    *,
    archived: bool = False,
    ids: Iterable[Any] | None = None,
) -> list[Option]:
    """A list's options in a registry, in the list's order, every one: as
    :func:`option_rows` finds them."""
    rows = option_rows(registry_id, type_name, archived=archived, ids=ids)
    return as_options(type_name, list(rows.order_by(*ORDER)))


def _with_values(form: Shape, rows: Sequence[Any]) -> list[tuple[Any, dict[str, Any]]]:
    current = services.current_values_of(list(rows)) if rows else {}
    fields = form.entry_fields()
    return [
        (
            row,
            {
                n: to_json(fields[n], v)
                for n, v in current.get(row.pk, {}).items()
                if n not in LIST_FIELDS and n in fields
            },
        )
        for row in rows
    ]


def _option(form: Shape, row: Any, values: dict[str, Any]) -> Option:  # noqa: ANN401 - an entry model
    return Option(
        entry=row,
        code=getattr(row, LIST_CODE),
        label=getattr(row, LIST_LABEL) or "",
        description=getattr(row, LIST_DESCRIPTION) or "",
        position=getattr(row, LIST_POSITION) or 0,
        archived=row.archived_at is not None,
        superseded_by=row.superseded_by,
        values=values,
    )


def find(registry_id: Any, type_name: str, code: str) -> Option | None:  # noqa: ANN401 - a UUID
    """The option of a list in a registry with this code, archived or not."""
    model = shape(list_type(type_name).qualified_name).entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    row = (
        model._default_manager.filter(registry_id=registry_id, type=type_name, **{LIST_CODE: code})
        .values_list("pk", flat=True)
        .first()
    )
    if row is None:
        return None
    found = options(registry_id, type_name, archived=True, ids=[row])
    return found[0] if found else None


def option_of(entry: Entity) -> Option:
    """An option, from its entry."""
    found = options(entry.registry_id, entry.type, archived=True, ids=[entry.pk])
    if not found:
        raise Refused(f"{entry.hrid} isn't a lab list's option.")
    return found[0]


def usage(registry_id: Any, type_name: str) -> dict[Any, int]:  # noqa: ANN401 - a UUID
    """How many records refer to each of a list's options, by its ID, in every project:
    what a curator weighs archiving one by. Never which records, nor whose."""
    from iki_server.graph.models import Edge, EdgeKind

    ids = entries_in(registry_id).filter(type=type_name).values("pk")
    rows = (
        Edge.objects.filter(on__in=ids, kind=EdgeKind.REFERENCE, node__purged_at__isnull=True)
        .values("on")
        .annotate(n=Count("node", distinct=True))
    )
    return {row["on"]: row["n"] for row in rows}


# --- Curating ------------------------------------------------------------------------------------


def _curator(user: User, registry_: Registry, type_name: str) -> EntityType:
    entity_type = list_type(type_name)
    if not permissions.can_curate_list(user, registry_, type_name):
        raise PermissionDenied(
            f"Only {registry_.name}'s registrars, and the curators of {entity_type.label_plural}, "
            "curate its options."
        )
    why = services.frozen(shape(type_name))
    if why:
        raise Refused(why)
    return entity_type


def _lock(registry_id: Any, type_name: str) -> None:  # noqa: ANN401 - a UUID
    """Serializes changes to a list's order and options in a registry."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"iki_list:{registry_id}:{type_name}"],
        )


def _refs(registry_id: Any) -> Refs:  # noqa: ANN401 - a UUID
    """What an option's values may refer to: the registry's lists' options."""
    return Refs(
        Entity.objects.none(), None, entries=entries_in(registry_id), project="the registry"
    )


def label_taken(
    form: Shape,
    registry_id: Any,  # noqa: ANN401 - a UUID
    label: str,
    *,
    besides: Any = None,  # noqa: ANN401 - a UUID
) -> str | None:
    """Which option of the list has this label already, ignoring case: a dropdown lists
    options by label, so the list has each once."""
    model = form.entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    found = (
        model._default_manager.filter(registry_id=registry_id, type=form.entity_type.qualified_name)
        .annotate(lowered=Lower(LIST_LABEL))
        .filter(lowered=label.lower())
    )
    if besides is not None:
        found = found.exclude(pk=besides)
    return found.values_list(LIST_CODE, flat=True).first()


def _values(
    form: Shape,
    registry_id: Any,  # noqa: ANN401 - a UUID
    given: Mapping[str, Any],
    *,
    adding: bool,
) -> dict[str, Any]:
    """An option's values as the database stores them, checked against its list's fields.
    Its place is the list's to give: it's added last, and reordering moves it."""
    fields = form.entry_fields()
    refs = _refs(registry_id)
    errors: dict[str, list[str]] = {}
    out: dict[str, Any] = {}
    for name, value in given.items():
        f = fields.get(name)
        if f is None:
            errors[name] = [f"{form.entity_type.label_plural} have no field `{name}`."]
            continue
        if name == LIST_POSITION:
            errors[name] = ["is its place in the list: reorder the list to move it."]
            continue
        if name == LIST_CODE and not adding:
            errors[name] = [
                "is the option's identity, which never changes: supersede the option with one "
                "of the right code instead."
            ]
            continue
        if f.immutable and not adding:
            errors[name] = ["is fixed once the option is added: supersede it instead."]
            continue
        if isinstance(value, str) and f.type in ("string", "text"):
            value = value.strip() or None
        try:
            out[name] = to_db(f, value, refs)
        except ValueError as error:
            errors.update(problems_of(name, error))
    for name, f in fields.items():
        if name == LIST_POSITION or f.removed or not f.required:
            continue
        if (adding and out.get(name) is None) or (name in out and out[name] is None):
            errors.setdefault(name, ["is required."])
    if errors:
        raise Invalid(errors)
    return out


def add(
    user: User,
    registry_: Registry,
    type_name: str,
    values: Mapping[str, Any],
    *,
    reason: str = "",
) -> Option:
    """A new option at the end of a list, from its values as JSON."""
    entity_type = _curator(user, registry_, type_name)
    form = shape(type_name)
    given = _values(form, registry_.pk, values, adding=True)
    with transaction.atomic():
        _lock(registry_.pk, type_name)
        entry = _new_option(user, form, registry_, given, reason=reason, via=None)
        log.record(
            "list.option_added",
            actor=user,
            target=entry,
            data={
                "registry": str(registry_.pk),
                "list": entity_type.qualified_name,
                "code": given[LIST_CODE],
                "label": given[LIST_LABEL],
                "reason": reason.strip(),
            },
        )
    return option_of(entry)


def _new_option(
    user: User | None,
    form: Shape,
    registry_: Registry,
    given: Mapping[str, Any],
    *,
    reason: str,
    via: str | None,
) -> Entity:
    """The option, written last in its list's order. The caller holds the list's lock."""
    type_name = form.entity_type.qualified_name
    model = form.entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    code = given[LIST_CODE]
    existing = (
        model._default_manager.filter(registry_id=registry_.pk, type=type_name, **{LIST_CODE: code})
        .values_list("hrid", "archived_at", "superseded_by")
        .first()
    )
    if existing is not None:
        hrid, archived_at, superseded_by = existing
        why = (
            "archived: restore it instead"
            if archived_at is not None
            else "superseded, and its code can't be added again"
            if superseded_by is not None
            else "in the list already"
        )
        raise Invalid({LIST_CODE: [f"is {hrid}'s code, which is {why}."]})
    taken = label_taken(form, registry_.pk, given[LIST_LABEL])
    if taken is not None:
        raise Invalid({LIST_LABEL: [f"is `{taken}`'s label already: the list has each once."]})
    count = model._default_manager.filter(registry_id=registry_.pk, type=type_name).count()
    if count >= LIST_MAX_OPTIONS:
        raise Refused(f"A list has {LIST_MAX_OPTIONS:,} options at most.")
    last = model._default_manager.filter(registry_id=registry_.pk, type=type_name).aggregate(
        last=Max(LIST_POSITION)
    )["last"]
    values = {**given, LIST_POSITION: (last or 0) + 1}
    instance = services._entry_instance(form, values)
    instance.registry_id = registry_.pk  # type: ignore[attr-defined]
    instance.created_by = user  # type: ignore[attr-defined]
    instance.identity_hash = identity.hash_for(  # type: ignore[attr-defined]
        instance, form.entity_type, services._lists(form, values)
    )
    instance.save()
    rows = services.Rows(form, None, instance)
    fields = form.entry_fields()
    touched = [p for p in form.parts if p.entry and not p.extension]
    for part in form.parts:
        if not part.entry:
            continue
        if part.extension and any(values.get(n) is not None for n in part.fields):
            row = rows.ensure(part)
            for name, f in part.fields.items():
                if name in values and not is_list_table(f):
                    services._set(row, f, values[name])
            row.save()
            touched.append(part)
        for name, f in part.fields.items():
            if is_list_table(f) and values.get(name):
                services._write_list(part, f, instance.pk, values[name])
    changed = sorted(n for n, v in values.items() if v is not None and n in fields)
    revision = services._revise(instance, RevisionKind.CREATED, changed, user, reason, via=via)
    services._write_history(touched, rows, revision.number)
    return Entity.objects.get(pk=instance.pk)


def change(
    user: User,
    entry: Entity,
    values: Mapping[str, Any],
    *,
    reason: str,
    expected_revision: int,
) -> Revision | None:
    """A curator's correction of an option: its label, description or other values, with
    why. Records naming it keep naming it, so they show what it's called now; their history
    says what it was called when."""
    registry_, entity_type = _option_registry(entry)
    _curator(user, registry_, entity_type.qualified_name)
    form = shape(entity_type.qualified_name)
    given = _values(form, registry_.pk, values, adding=False)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: every project in the registry sees the change."]})
    with transaction.atomic():
        _lock(registry_.pk, entity_type.qualified_name)
        return entries.curate(
            user,
            entry,
            {n: to_json(form.entry_fields()[n], v) for n, v in given.items()},
            reason=reason,
            expected_revision=expected_revision,
        )


@dataclass(frozen=True)
class StaleOption:
    code: str
    expected_revision: int | None
    current_revision: int | None


class StaleOrder(Exception):  # noqa: N818 - reads as "the order was stale"
    """The list changed since the curator read it: options changed, came or went."""

    def __init__(self, options: Sequence[StaleOption]) -> None:
        super().__init__(
            "The list changed since you read it: "
            + ", ".join(f"`{o.code}`" for o in options)
            + ". Read it again, then reorder it."
        )
        self.options = list(options)


def reorder(
    user: User,
    registry_: Registry,
    type_name: str,
    codes: Sequence[str],
    *,
    expected_revisions: Mapping[str, int],
    reason: str,
) -> list[Revision]:
    """Puts a list's options in this order: every option's code, archived and superseded
    ones too, as a curator's list shows them. Each option that moves gets a revision.
    ``expected_revisions`` is each option's revision as the curator read it (ADR 0289):
    an option changed, added or removed since raises :class:`StaleOrder`, so a second
    curator's order can't silently undo the first's."""
    entity_type = _curator(user, registry_, type_name)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: every project's dropdowns follow the new order."]})
    form = shape(type_name)
    model = form.entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    with transaction.atomic():
        _lock(registry_.pk, type_name)
        rows = list(
            model._default_manager.select_for_update()
            .filter(registry_id=registry_.pk, type=type_name)
            .order_by(LIST_POSITION, LIST_CODE)
        )
        current = [getattr(r, LIST_CODE) for r in rows]
        if len(set(codes)) != len(codes):
            raise Invalid({"codes": ["names an option twice."]})
        now = dict(Entity.objects.filter(pk__in=[r.pk for r in rows]).values_list("pk", "revision"))
        revisions_now = {getattr(r, LIST_CODE): now[r.pk] for r in rows}
        changed = [
            StaleOption(code, expected_revisions.get(code), revisions_now.get(code))
            for code in sorted(set(revisions_now) | set(expected_revisions))
            if expected_revisions.get(code) != revisions_now.get(code)
        ]
        if changed:
            raise StaleOrder(changed)
        if set(codes) != set(current):
            missing = sorted(set(current) - set(codes))
            unknown = sorted(set(codes) - set(current))
            said = []
            if missing:
                said.append("leaves out " + ", ".join(f"`{c}`" for c in missing))
            if unknown:
                said.append("names " + ", ".join(f"`{c}`" for c in unknown) + ", not in the list")
            raise Invalid(
                {
                    "codes": [
                        " and ".join(said).capitalize()
                        + ": give every option of the list, in its new order. If someone has "
                        "just changed the list, start again from it."
                    ]
                }
            )
        by_code = {getattr(r, LIST_CODE): r for r in rows}
        revisions: list[Revision] = []
        for position, code in enumerate(codes, start=1):
            row = by_code[code]
            if getattr(row, LIST_POSITION) == position:
                continue
            locked = Entity.objects.select_for_update().get(pk=row.pk)
            if locked.purged_at is not None:
                raise Refused(services.purged(locked))
            stored = services.Rows(form, None, row)
            touched = services._write(form, stored, {LIST_POSITION: position}, row.pk)
            revision = services._revise(locked, RevisionKind.CURATED, [LIST_POSITION], user, reason)
            services._write_history(touched, stored, revision.number)
            revisions.append(revision)
        if revisions:
            log.record(
                "list.reordered",
                actor=user,
                target=registry_,
                data={
                    "registry": str(registry_.pk),
                    "list": entity_type.qualified_name,
                    "changes": {"order": {"before": current, "after": list(codes)}},
                    "reason": reason.strip(),
                },
            )
    return revisions


def archive(
    user: User, entry: Entity, *, archived: bool = True, expected_revision: int, reason: str
) -> Revision | None:
    """Archives an option, so no dropdown offers it, or restores it, from the revision the
    curator read. Records that name it keep it: it's valid on them, in search, filters,
    exports and the API. Raises :class:`services.Stale`."""
    registry_, entity_type = _option_registry(entry)
    _curator(user, registry_, entity_type.qualified_name)
    if not reason.strip():
        raise Invalid({"reason": ["Say why: every project in the registry sees the change."]})
    with transaction.atomic():
        _lock(registry_.pk, entity_type.qualified_name)
        locked = Entity.objects.select_for_update().get(pk=entry.pk)
        if locked.purged_at is not None:
            raise Refused(services.purged(locked))
        if locked.revision != expected_revision:
            raise services.Stale(locked.revision)
        if (locked.archived_at is not None) == archived:
            return None
        if not archived and locked.superseded_by_id is not None:
            raise Refused(f"{locked.hrid} was superseded: its replacement is offered instead.")
        locked.archived_at = dt.datetime.now(dt.UTC) if archived else None
        locked.save(update_fields=["archived_at"])
        kind = RevisionKind.ARCHIVED if archived else RevisionKind.RESTORED
        revision = services._revise(locked, kind, [], user, reason)
        option = option_of(locked)
        log.record(
            "list.option_archived" if archived else "list.option_restored",
            actor=user,
            target=locked,
            data={
                "registry": str(registry_.pk),
                "list": entity_type.qualified_name,
                "code": option.code,
                "label": option.label,
                "reason": reason.strip(),
            },
        )
    return revision


def _option_registry(entry: Entity) -> tuple[Registry, EntityType]:
    if entry.registry_id is None or not is_list(entry.type):
        raise Refused(f"{entry.hrid} isn't a lab list's option.")
    return Registry.objects.get(pk=entry.registry_id), registry.types()[entry.type]


# --- Moving a project's references ---------------------------------------------------------------


def referrers(entry: Entity) -> QuerySet[Project]:
    """The projects with records referring to an option, as their own: whom a
    supersession tells."""
    from iki_server.graph.models import Edge, EdgeKind

    nodes = Edge.objects.filter(on=entry, kind=EdgeKind.REFERENCE).values("node")
    owners = Entity.objects.filter(pk__in=nodes, purged_at__isnull=True).values("owner_project")
    return Project.objects.filter(pk__in=owners)


def repoint(user: User, project: Project, entry: Entity, *, reason: str) -> list[Entity]:
    """Moves a project's references from a superseded option to the one that replaced
    it, each a revision of the record with the reason, as moving a record of a superseded
    entry does (ADR 0115). Archived and locked records keep theirs."""
    said = reason.strip()
    if not said:
        raise Invalid({"reason": ["Say why the records are moving to the new option."]})
    if entry.registry_id is None or not is_list(entry.type):
        raise Refused(f"{entry.hrid} isn't a lab list's option.")
    if entry.registry_id != project.registry_id:
        raise Refused(f"{entry.hrid} is another registry's option.")
    permissions.require(user, Action.PROJECT_EDIT, project)
    with transaction.atomic():
        old = Entity.objects.select_for_update().get(pk=entry.pk)
        if old.superseded_by_id is None:
            raise Refused(f"{old.hrid} wasn't superseded: there's nothing to move to.")
        new = Entity.objects.get(pk=old.superseded_by_id)
        moved = entries._repoint(user, project, old, new, said)
        log.record(
            "list.references_moved",
            actor=user,
            target=old,
            project=project,
            data={
                "from": {"id": str(old.pk), "hrid": old.hrid},
                "to": {"id": str(new.pk), "hrid": new.hrid},
                "repointed": [e.hrid for e in moved],
                "reason": said,
            },
        )
    return moved


# --- Starting options ----------------------------------------------------------------------------


def seed(registries: Iterable[Registry] | None = None) -> list[str]:
    """Adds each list's starting options to each registry that hasn't got them, by code, at
    the end of its list: all of them to a new registry, and an upgrade's new ones to the
    others. What a registry's curators did to an option is never undone, nor an archived
    one restored. One line per list and registry that got options, as the sync reports."""
    # A registry made later is seeded as it's made (:mod:`iki_server.entities.apps`).
    chosen = list(registries) if registries is not None else list(Registry.objects.all())
    done: list[str] = []
    for type_name in _seeding_order():
        entity_type = registry.types()[type_name]
        if entity_type.list_spec is None or not entity_type.list_spec.options:
            continue
        if services.frozen(shape(type_name)) is not None:
            continue
        for found in chosen:
            added = _seed_one(entity_type, found)
            if added:
                done.append(f"{type_name} in {found.name}: {', '.join(added)}")
    return done


def _seeding_order() -> list[str]:
    """Lists whose options others' refer to first, so a reference finds its option."""
    every = types()
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for f in every[name].fields.values():
            targets = f.to if f.type == "ref" else f.items.to if f.items is not None else ()
            for target in targets:
                if target in every:
                    visit(target)
        order.append(name)

    for name in sorted(every):
        visit(name)
    return order


def _seed_one(entity_type: EntityType, registry_: Registry) -> list[str]:
    assert entity_type.list_spec is not None  # noqa: S101 - only lists seed
    type_name = entity_type.qualified_name
    form = shape(type_name)
    model = form.entry_model
    assert model is not None  # noqa: S101 - lists' options are entries
    with transaction.atomic():
        _lock(registry_.pk, type_name)
        have = set(
            model._default_manager.filter(registry_id=registry_.pk, type=type_name).values_list(
                LIST_CODE, flat=True
            )
        )
        wanted = [o for o in entity_type.list_spec.options if o.get(LIST_CODE) not in have]
        # By identity too, which is what's unique: an option whose entry row is gone (its
        # plugin's tables made again) is still the registry's.
        hashes = {
            str(o[LIST_CODE]): identity.hash_of({LIST_CODE: o[LIST_CODE]}, entity_type)
            for o in wanted
        }
        held = set(
            Entity.objects.filter(
                registry_id=registry_.pk, identity_hash__in=[h for h in hashes.values() if h]
            ).values_list("identity_hash", flat=True)
        )
        wanted = [o for o in wanted if hashes[str(o[LIST_CODE])] not in held]
        if not wanted:
            return []
        plugin = registry.plugins()[entity_type.plugin]
        version = plugin.version
        reason = f"A starting option of {plugin.name} {version}."
        refs = _refs(registry_.pk)
        fields = form.entry_fields()
        added: list[str] = []
        for option in wanted:
            values: dict[str, Any] = {}
            for name, value in option.items():
                f = fields.get(name)
                given = _by_id(f, value, registry_.pk) if f is not None else None
                if f is None or given is None or given == []:
                    continue
                # The plugin's own choice, though the lab archived the option it names since.
                item = f.items if f.type == "list" and f.items is not None else f
                kept = (given if isinstance(given, list) else [given]) if item.type == "ref" else []
                with refs.keeping(kept):
                    values[name] = to_db(f, given, refs)
            if label_taken(form, registry_.pk, values[LIST_LABEL]) is not None:
                # A curator's own option has the label: the starting one would read as it.
                continue
            _new_option(None, form, registry_, values, reason=reason, via="system")
            added.append(str(option[LIST_CODE]))
    return added


def _by_id(f: Any, value: Any, registry_id: Any) -> Any:  # noqa: ANN401 - JSON, a Field, a UUID
    """A starting option's value as the API takes it: a reference to another list's
    option, named by code, as that option's ID in the registry, or None if it hasn't got
    it."""
    item = f.items if f.type == "list" and f.items is not None else f
    if item.type != "ref" or not item.to:
        return value
    target = item.to[0]

    def one(code: Any) -> Any:  # noqa: ANN401 - JSON
        found = find(registry_id, target, str(code))
        return str(found.entry.pk) if found is not None else None

    # An option the registry hasn't got (a curator's own had its label) is left out.
    if isinstance(value, list):
        return [found for found in map(one, value) if found is not None]
    return one(value)


def names_of(ids: Iterable[Any]) -> dict[Any, tuple[str, str]]:
    """The code and label of those of ``ids`` that are lab lists' options, by ID: how a
    reference to one reads, and the code an export gives for it, which a relabel doesn't
    change."""
    wanted = list(ids)
    if not wanted:
        return {}
    grouped: dict[str, list[Any]] = {}
    for type_name, pk in Entity.objects.filter(pk__in=wanted, type__in=list(types())).values_list(
        "type", "pk"
    ):
        grouped.setdefault(type_name, []).append(pk)
    out: dict[Any, tuple[str, str]] = {}
    for type_name, pks in grouped.items():
        model = shape(type_name).entry_model
        assert model is not None  # noqa: S101 - lists' options are entries
        for pk, code, label in model._default_manager.filter(pk__in=pks).values_list(
            "pk", LIST_CODE, LIST_LABEL
        ):
            out[pk] = (code, label)
    return out


def codes_of(ids: Iterable[Any]) -> dict[Any, str]:
    """The codes of those of ``ids`` that are lab lists' options, by ID."""
    return {pk: code for pk, (code, _) in names_of(ids).items()}


def counted(registry_id: Any) -> dict[str, int]:  # noqa: ANN401 - a UUID
    """How many active options each list has in a registry."""
    rows = (
        entries_in(registry_id)
        .filter(Q(archived_at__isnull=True), Q(superseded_by__isnull=True))
        .values("type")
        .annotate(n=Count("pk"))
    )
    return {row["type"]: row["n"] for row in rows}
