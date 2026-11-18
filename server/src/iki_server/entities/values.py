"""An entity type's fields at run time: where each is stored, and checking and converting values.

A type's fields are its own, its parents' (multi-table inheritance, so one
model instance holds them all) and those other plugins add (extension
tables, one row per entity once it has a value there). A type with an
identity keeps its identity, derived and fact fields in its registry entries'
tables (ADR 0115), which a record reads through the entry it names. Values arrive as JSON
and are checked against the field's rules before they reach the database,
whose CHECK constraints hold them to the same rules again.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, cast

from django.apps import apps
from django.db import models
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from iki_server.core.sizes import human_size
from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_spec import naming, units
from iki_spec.model import EntityType, Field, split_entry
from iki_spec.validate import value_problems


class Invalid(Exception):  # noqa: N818 - reads as "the values are invalid"
    """Values that don't fit their fields: field name → what's wrong, as sentences."""

    def __init__(self, errors: Mapping[str, list[str]], *, broken: Iterable[Any] = ()) -> None:
        self.errors = dict(errors)
        #: The plugins' rules among them (ADR 0127), as ``checking.Broken``: which rule each
        #: message is, and the fields it points at.
        self.broken = list(broken)
        super().__init__("; ".join(f"{k}: {' '.join(v)}" for k, v in self.errors.items()))


@dataclass(frozen=True)
class Part:
    """One table an entity's fields live in: a type in its chain, or an extension."""

    plugin: str
    model: type[models.Model]
    history: type[models.Model]
    #: Every field the table has had, removed ones included.
    fields: dict[str, Field]
    extension: bool
    #: The table by what it holds: the type's qualified name, such as ``iki/dna.Sequence``, or
    #: for an extension, its plugin and the type it extends, ``acme/lims:iki/dna.DNASequence``.
    #: A registry entry's table adds ``#entry``.
    name: str = ""
    #: It holds registry entries' fields (ADR 0115), keyed by the entry, not the record.
    entry: bool = False

    def list_model(self, name: str) -> type[models.Model]:
        return apps.get_model(
            naming.app_label(self.plugin), naming.list_model(self.model.__name__, name)
        )


@dataclass(frozen=True)
class Shape:
    entity_type: EntityType
    parts: tuple[Part, ...]

    @property
    def model(self) -> type[Entity]:
        """The type's generated model of records: a subclass of :class:`Entity`, through its
        parents'."""
        return cast("type[Entity]", registry.model(self.entity_type.qualified_name))

    @property
    def entry_model(self) -> type[Entity] | None:
        """The model of its registry entries, for a type with an identity (ADR 0115)."""
        return cast("type[Entity] | None", registry.entry_model(self.entity_type.qualified_name))

    @property
    def has_entries(self) -> bool:
        return any(p.entry for p in self.parts)

    def fields(self) -> dict[str, Field]:
        """The fields entities of this type have now, parents' first."""
        return {n: f for part in self.parts for n, f in part.fields.items() if not f.removed}

    def entry_fields(self) -> dict[str, Field]:
        """Those of :meth:`fields` its registry entries hold."""
        return {n: f for p in self.parts if p.entry for n, f in p.fields.items() if not f.removed}

    def part_of(self, name: str) -> Part:
        return next(p for p in self.parts if name in p.fields)


@cache
def shape(qualified: str) -> Shape:
    entity_type = registry.types()[qualified]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    parts = []
    entries = []
    for t in chain:
        record, entry = split_entry(t.fields, t.identity)
        label = naming.app_label(t.plugin)
        parts.append(
            Part(
                plugin=t.plugin,
                model=registry.model(t.qualified_name),
                history=apps.get_model(label, naming.history_model(t.name)),
                fields=record,
                extension=False,
                name=t.qualified_name,
            )
        )
        if t.identity:
            model = apps.get_model(label, naming.entry_model(t.name))
            entries.append(
                Part(
                    plugin=t.plugin,
                    model=model,
                    history=apps.get_model(label, naming.history_model(model.__name__)),
                    fields=entry,
                    extension=False,
                    name=f"{t.qualified_name}#entry",
                    entry=True,
                )
            )
    names = {t.qualified_name for t in chain}
    for e in registry.extensions():
        if e.extends_type not in names:
            continue
        label = naming.app_label(e.plugin)
        record, entry = split_entry(e.fields)
        name = naming.extension_model(e.extends_type)
        if record:
            model = apps.get_model(label, name)
            parts.append(
                Part(
                    plugin=e.plugin,
                    model=model,
                    history=apps.get_model(label, naming.history_model(model.__name__)),
                    fields=record,
                    extension=True,
                    name=f"{e.plugin}:{e.extends_type}",
                )
            )
        if entry:
            model = apps.get_model(label, naming.entry_model(name))
            entries.append(
                Part(
                    plugin=e.plugin,
                    model=model,
                    history=apps.get_model(label, naming.history_model(model.__name__)),
                    fields=entry,
                    extension=True,
                    name=f"{e.plugin}:{e.extends_type}#entry",
                    entry=True,
                )
            )
    return Shape(entity_type, tuple(parts + entries))


#: Kinds of field stored as a foreign key, in a column named ``<field>_id``: an entity,
#: a file, a person (ADR 0122).
FOREIGN_KEYS = ("ref", "file", "person")


def is_list_table(f: Field) -> bool:
    return f.type == "list" and f.items is not None and f.items.type in FOREIGN_KEYS


def subtypes(qualified: str) -> set[str]:
    """The type and every type that extends it, at any depth."""
    if qualified == "iki/core.Entity":
        return set(registry.types())
    every = registry.types()
    out = {qualified}
    grew = True
    while grew:
        more = {name for name, t in every.items() if t.extends in out and name not in out}
        out |= more
        grew = bool(more)
    return out


# --- JSON → database ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What a reference names, as it's stored: a registry entry for a type with an identity
    (ADR 0115), else the entity itself."""

    id: uuid.UUID
    type: str


class Refs:
    """Finds what values refer to, so a reference never reveals what the caller can't read.

    A reference to an entity with an identity holds its registry entry (ADR 0115), named
    by the entry's ID or by a record of it, and only one the project holds: it has a
    record of it, its own or one shared with it. ``project`` names it when one isn't.
    """

    def __init__(
        self,
        readable: Any,  # noqa: ANN401 - QuerySets
        files: Any = None,  # noqa: ANN401
        *,
        held: Any = None,  # noqa: ANN401
        held_files: Any = None,  # noqa: ANN401
        entries: Any = None,  # noqa: ANN401
        readable_entries: Any = None,  # noqa: ANN401
        project: str = "the project",
        namer: Any = None,  # noqa: ANN401 - people.Namer
    ) -> None:
        self.readable = readable
        self.files = files
        #: What the caller can read but these values may not refer to, because its project
        #: doesn't allow sharing (ADR 0067): a reference to one says so.
        self.held = held
        self.held_files = held_files
        #: The registry entries the project holds, and those the caller can read.
        self.entries = entries
        self.readable_entries = readable_entries
        self.project = project
        #: Finds the people values name, in the project whose entities they are (ADR 0122).
        self.namer = namer
        #: Already looked up: what to store, why it can't be referred to, or None for
        #: nothing the caller can read.
        self._found: dict[uuid.UUID, Target | str | None] = {}

    def look_up(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Target | str | None]:
        wanted = set(ids)
        unknown = [i for i in wanted if i not in self._found]
        if unknown:
            self._look_up(unknown)
        return {i: self._found[i] for i in wanted}

    def _look_up(self, unknown: list[uuid.UUID]) -> None:
        records = {
            pk: (type_name, entry, hrid)
            for pk, type_name, entry, hrid in self.readable.filter(pk__in=unknown).values_list(
                "pk", "type", "entry_id", "hrid"
            )
        }
        entry_ids = (set(unknown) - records.keys()) | {
            entry for _, entry, _ in records.values() if entry is not None
        }
        held: dict[uuid.UUID, str] = {}
        known: dict[uuid.UUID, str] = {}
        if entry_ids and self.entries is not None:
            held = dict(self.entries.filter(pk__in=entry_ids).values_list("pk", "type"))
        if entry_ids - held.keys() and self.readable_entries is not None:
            known = dict(
                self.readable_entries.filter(pk__in=entry_ids - held.keys()).values_list(
                    "pk", "hrid"
                )
            )
        for i in unknown:
            found: Target | str | None = None
            if i in records:
                type_name, entry, hrid = records[i]
                if entry is None:
                    found = Target(i, type_name)
                elif entry in held:
                    found = Target(entry, type_name)
                else:
                    found = self._not_held(hrid)
            elif i in held:
                found = Target(i, held[i])
            elif i in known:
                found = self._not_held(known[i])
            self._found[i] = found

    def _not_held(self, hrid: str) -> str:
        return (
            f"refers to {hrid}, which isn't in {self.project}: add it to the project first, "
            "by registering it there."
        )

    def types(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        """The types of those of ``ids`` the values may refer to."""
        return {i: t.type for i, t in self.look_up(ids).items() if isinstance(t, Target)}

    def file(self, file_id: uuid.UUID) -> tuple[str, int] | None:
        """A readable file's media type and size."""
        if self.files is None:
            return None
        found = self.files.filter(pk=file_id).values_list("media_type", "blob__size").first()
        return None if found is None else (found[0], found[1])

    def holder(self, entity_id: uuid.UUID) -> str | None:
        """The project keeping a readable entity to itself, if it's why it can't be used."""
        if self.held is None:
            return None
        name: str | None = (
            self.held.filter(pk=entity_id).values_list("owner_project__name", flat=True).first()
        )
        return name

    def file_holder(self, file_id: uuid.UUID) -> str | None:
        if self.held_files is None:
            return None
        name: str | None = (
            self.held_files.filter(pk=file_id).values_list("project__name", flat=True).first()
        )
        return name


def to_db(f: Field, value: Any, refs: Refs) -> Any:  # noqa: ANN401 - JSON in, a model value out
    """``value`` from JSON as the database stores it. Raises ``ValueError`` with what's wrong."""
    if value is None:
        return None
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        problems = value_problems(f, value) if isinstance(value, list) else ["must be a list."]
        if not isinstance(value, list):
            raise ValueError(" ".join(problems))
        if f.items.type == "ref":
            # One query for a whole list of references, not one per item.
            refs.types(_uuids(value))
        if f.items.type == "person" and refs.namer is not None and refs.namer.project is not None:
            refs.namer.look_up(f.items.role, [v for v in value if isinstance(v, str)])
        out = []
        for index, item in enumerate(value):
            if item is None:
                raise ValueError(f"item {index + 1} can't be empty.")
            try:
                out.append(to_db(f.items, item, refs))
            except ValueError as error:
                raise ValueError(f"item {index + 1} {error}") from None
        own = [p for p in problems if not p.startswith("item ")]
        if own:
            raise ValueError(" ".join(own))
        if f.items.type == "person" and len(set(out)) != len(out):
            raise ValueError("names someone twice.")
        return out
    if f.type == "quantity":
        value = _quantity(f, value)
    elif f.type == "float" and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if (
        f.type in ("int", "float", "quantity")
        and isinstance(value, float)
        and not math.isfinite(value)
    ):
        raise ValueError("must be a finite number.")
    if f.type == "datetime":
        return _datetime(value)
    if f.type == "ref":
        return _ref(f, value, refs)
    if f.type == "person":
        if refs.namer is None:
            raise ValueError("can't name anyone here.")
        return refs.namer.person(f, value)
    if f.type == "file":
        return _file(f, value, refs)
    if f.type == "json":
        if f.schema is not None:
            mismatch = best_match(Draft202012Validator(f.schema).iter_errors(value))
            if mismatch is not None:
                where = ".".join(str(p) for p in mismatch.absolute_path)
                at = f" at `{where}`" if where else ""
                raise ValueError(f"doesn't fit its schema{at}: {mismatch.message}.")
        return value
    problems = value_problems(f, value)
    if problems:
        raise ValueError(" ".join(problems))
    if f.type == "date":
        return dt.date.fromisoformat(value)
    return value


def _quantity(f: Field, value: Any) -> float:  # noqa: ANN401 - JSON
    assert f.unit is not None  # noqa: S101 - quantities always have a unit
    if isinstance(value, dict):
        if set(value) != {"value", "unit"}:
            raise ValueError('must be a number, or {"value": …, "unit": …}.')
        number, unit = value["value"], value["unit"]
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise ValueError("needs a number as its value.")
        if not isinstance(unit, str):
            raise ValueError("needs a UCUM unit, such as `mg`.")
        try:
            value = units.convert(float(number), unit, f.unit)
        except units.UnitError as error:
            raise ValueError(f"has a unit Iki can't use here: {error}") from None
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f'must be a number in {f.unit}, or {{"value": …, "unit": …}}.')
    number = float(value)
    problems = value_problems(f, number)
    if problems:
        # "must be at least 0." → "must be at least 0 g."
        raise ValueError(" ".join(f"{p.removesuffix('.')} {f.unit}." for p in problems))
    return number


def _datetime(value: Any) -> dt.datetime:  # noqa: ANN401 - JSON
    if not isinstance(value, str):
        raise ValueError("must be a date and time, such as 2026-03-01T09:30:00Z.")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a date and time, such as 2026-03-01T09:30:00Z.") from None
    if parsed.tzinfo is None:
        raise ValueError("needs a time zone, such as Z or +01:00.")
    return parsed


def _uuids(values: Iterable[Any]) -> list[uuid.UUID]:
    out = []
    for value in values:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


def _ref(f: Field, value: Any, refs: Refs) -> uuid.UUID:  # noqa: ANN401 - JSON
    try:
        target = uuid.UUID(str(value))
    except ValueError:
        raise ValueError("must be an entity's ID.") from None
    assert f.to is not None  # noqa: S101 - refs always name a type
    found = refs.look_up([target])[target]
    if isinstance(found, str):
        raise ValueError(found)
    if found is None:
        holder = refs.holder(target)
        if holder is not None:
            raise ValueError(
                f"refers to an entity in {holder}, which doesn't allow sharing its entities "
                "with other projects."
            )
        raise ValueError("refers to no entity you can read.")
    if found.type not in subtypes(f.to):
        raise ValueError(f"must refer to a {f.to}, not a {found.type}.")
    return found.id


def _file(f: Field, value: Any, refs: Refs) -> uuid.UUID:  # noqa: ANN401 - JSON
    try:
        target = uuid.UUID(str(value))
    except ValueError:
        raise ValueError("must be a file's ID: upload the file first.") from None
    found = refs.file(target)
    if found is None:
        holder = refs.file_holder(target)
        if holder is not None:
            raise ValueError(
                f"refers to a file in {holder}, which doesn't allow sharing with other projects."
            )
        raise ValueError("refers to no file you can read.")
    media_type, size = found
    if f.media_types and not media_type_allowed(media_type, f.media_types):
        raise ValueError(f"must be {_either(f.media_types)}, and this file is {media_type}.")
    if f.max_size is not None and size > f.max_size:
        raise ValueError(
            f"can be at most {human_size(f.max_size)}, and this file is {human_size(size)}."
        )
    return target


def media_type_allowed(media_type: str, allowed: Iterable[str]) -> bool:
    """Whether ``media_type`` is one of ``allowed``, where ``image/*`` allows any image."""
    kind = media_type.split("/", 1)[0]
    return any(a == media_type or a == f"{kind}/*" for a in allowed)


def _either(choices: tuple[str, ...]) -> str:
    if len(choices) == 1:
        return choices[0]
    return ", ".join(choices[:-1]) + f" or {choices[-1]}"


# --- Database → JSON ----------------------------------------------------------------------------


def to_json(f: Field, value: Any) -> Any:  # noqa: ANN401 - a model value in, JSON out
    if value is None:
        return None
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        return [to_json(f.items, item) for item in value]
    if f.type == "datetime":
        utc = value.astimezone(dt.UTC)
        return utc.isoformat().replace("+00:00", "Z")
    if f.type == "date":
        return value.isoformat()
    if f.type in FOREIGN_KEYS:
        return str(value)
    return value


def read_value(instance: models.Model, f: Field, part: Part) -> Any:  # noqa: ANN401 - a model value
    """A field's current value, from the model instance (or extension row) that holds it."""
    if is_list_table(f):
        rows = part.list_model(f.name)._default_manager.filter(entity_id=instance.pk)
        return list(rows.order_by("position").values_list("item_id", flat=True))
    if f.type in FOREIGN_KEYS:
        return getattr(instance, f"{f.name}_id")
    return getattr(instance, f.name)


def history_value(row: models.Model, f: Field) -> Any:  # noqa: ANN401 - a model value
    return getattr(row, f.name)
