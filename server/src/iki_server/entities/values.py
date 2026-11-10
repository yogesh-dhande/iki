"""An entity type's fields at run time: where each is stored, and checking and converting values.

A type's fields are its own, its parents' (multi-table inheritance, so one
model instance holds them all) and those other plugins add (extension
tables, one row per entity once it has a value there). Values arrive as JSON
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

from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_spec import naming, units
from iki_spec.model import EntityType, Field
from iki_spec.validate import value_problems


class Invalid(Exception):  # noqa: N818 - reads as "the values are invalid"
    """Values that don't fit their fields: field name → what's wrong, as sentences."""

    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        self.errors = dict(errors)
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

    def list_model(self, name: str) -> type[models.Model]:
        return apps.get_model(self.plugin, naming.list_model(self.model.__name__, name))


@dataclass(frozen=True)
class Shape:
    entity_type: EntityType
    parts: tuple[Part, ...]

    @property
    def model(self) -> type[Entity]:
        """The type's generated model: a subclass of :class:`Entity`, through its parents'."""
        return cast("type[Entity]", registry.model(self.entity_type.qualified_name))

    def fields(self) -> dict[str, Field]:
        """The fields entities of this type have now, parents' first."""
        return {n: f for part in self.parts for n, f in part.fields.items() if not f.removed}

    def part_of(self, name: str) -> Part:
        return next(p for p in self.parts if name in p.fields)


@cache
def shape(qualified: str) -> Shape:
    entity_type = registry.types()[qualified]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    parts = [
        Part(
            plugin=t.plugin,
            model=registry.model(t.qualified_name),
            history=apps.get_model(t.plugin, naming.history_model(t.name)),
            fields=t.fields,
            extension=False,
        )
        for t in chain
    ]
    names = {t.qualified_name for t in chain}
    for e in registry.extensions():
        if e.extends_type in names:
            model = apps.get_model(e.plugin, naming.extension_model(e.extends_type))
            parts.append(
                Part(
                    plugin=e.plugin,
                    model=model,
                    history=apps.get_model(e.plugin, naming.history_model(model.__name__)),
                    fields=e.fields,
                    extension=True,
                )
            )
    return Shape(entity_type, tuple(parts))


def is_list_table(f: Field) -> bool:
    return f.type == "list" and f.items is not None and f.items.type in ("ref", "file")


def subtypes(qualified: str) -> set[str]:
    """The type and every type that extends it, at any depth."""
    if qualified == "core.Entity":
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


class Refs:
    """Finds referenced entities for a caller, so a reference never reveals one they can't read."""

    def __init__(self, readable: Any) -> None:  # noqa: ANN401 - a QuerySet of entities
        self.readable = readable

    def types(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        return dict(self.readable.filter(pk__in=list(ids)).values_list("pk", "type"))


def to_db(f: Field, value: Any, refs: Refs) -> Any:  # noqa: ANN401 - JSON in, a model value out
    """``value`` from JSON as the database stores it. Raises ``ValueError`` with what's wrong."""
    if value is None:
        return None
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        problems = value_problems(f, value) if isinstance(value, list) else ["must be a list."]
        if not isinstance(value, list):
            raise ValueError(" ".join(problems))
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
    if f.type == "file":
        raise ValueError("is a file: attach files through the files API.")
    if f.type == "json":
        if f.schema is not None:
            errors = sorted(Draft202012Validator(f.schema).iter_errors(value), key=str)
            if errors:
                raise ValueError(f"doesn't fit its schema: {errors[0].message}.")
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


def _ref(f: Field, value: Any, refs: Refs) -> uuid.UUID:  # noqa: ANN401 - JSON
    try:
        target = uuid.UUID(str(value))
    except ValueError:
        raise ValueError("must be an entity's ID.") from None
    assert f.to is not None  # noqa: S101 - refs always name a type
    found = refs.types([target]).get(target)
    if found is None:
        raise ValueError("refers to no entity you can read.")
    if found not in subtypes(f.to):
        raise ValueError(f"must refer to a {f.to}, not a {found}.")
    return target


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
    if f.type in ("ref", "file"):
        return str(value)
    return value


def read_value(instance: models.Model, f: Field, part: Part) -> Any:  # noqa: ANN401 - a model value
    """A field's current value, from the model instance (or extension row) that holds it."""
    if is_list_table(f):
        rows = part.list_model(f.name)._default_manager.filter(entity_id=instance.pk)
        return list(rows.order_by("position").values_list("item_id", flat=True))
    if f.type in ("ref", "file"):
        return getattr(instance, f"{f.name}_id")
    return getattr(instance, f.name)


def history_value(row: models.Model, f: Field) -> Any:  # noqa: ANN401 - a model value
    return getattr(row, f.name)
