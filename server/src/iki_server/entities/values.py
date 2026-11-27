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
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, cast

from django.apps import apps
from django.db import models
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from iki_server.api.problems import Problem
from iki_server.core.sizes import human_size
from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.files.scan_state import status_from_decision
from iki_server.storage.codes import RESERVED_VENDOR_MESSAGE, has_iki_prefix
from iki_spec import naming, units
from iki_spec.model import EntityType, Field, as_refined, split_entry
from iki_spec.validate import value_problems


class Invalid(Exception):  # noqa: N818 - reads as "the values are invalid"
    """Values that don't fit their fields: field name → what's wrong, as sentences. A row's
    field of a list of rows (ADR 0137) is keyed by its path, ``components/1/part``."""

    def __init__(self, errors: Mapping[str, list[str]], *, broken: Iterable[Any] = ()) -> None:
        self.errors = dict(errors)
        #: The plugins' rules among them (ADR 0127), as ``checking.Broken``: which rule each
        #: message is, and the fields it points at.
        self.broken = list(broken)
        super().__init__("; ".join(f"{k}: {' '.join(v)}" for k, v in self.errors.items()))


class RowProblems(ValueError):  # noqa: N818 - reads as "the rows have problems"
    """What's wrong with a list of rows (ADR 0137), each where it is: ``""`` for the list,
    ``"1/part"`` for the second row's ``part``."""

    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        self.errors = dict(errors)
        super().__init__(
            " ".join(
                " ".join(v) if not k else f"row {_row_number(k)} {' '.join(v)}"
                for k, v in self.errors.items()
            )
        )


def _row_number(key: str) -> str:
    index, _, cell = key.partition("/")
    return f"{int(index) + 1}" + (f" `{cell}`" if cell else "")


def problems_of(name: str, error: ValueError) -> dict[str, list[str]]:
    """``error`` about field ``name`` as :class:`Invalid`'s errors: one entry, or for a list
    of rows, one per row's field that's wrong."""
    if isinstance(error, RowProblems):
        return {f"{name}/{k}" if k else name: v for k, v in error.errors.items()}
    return {name: [str(error)]}


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
    vendor_barcode: str | None = None

    def field_value(self, field: Field, value: Any, refs: Refs) -> Any:  # noqa: ANN401 - JSON
        """Convert an entity field under its concrete type's storage declaration."""
        if self.is_reserved_vendor_code(field.name, value):
            raise ValueError(RESERVED_VENDOR_MESSAGE)
        return to_db(field, value, refs)

    def is_reserved_vendor_code(self, name: str, value: object) -> bool:
        return name == self.vendor_barcode and isinstance(value, str) and has_iki_prefix(value)

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

    def fields(self, *, removed: bool = False) -> dict[str, Field]:
        """The fields entities of this type have now, parents' first; with ``removed``, and
        those they had, which their history keeps."""
        return {
            n: f for part in self.parts for n, f in part.fields.items() if removed or not f.removed
        }

    def entry_fields(self, *, removed: bool = False) -> dict[str, Field]:
        """Those of :meth:`fields` its registry entries hold."""
        return {
            n: f
            for p in self.parts
            if p.entry
            for n, f in p.fields.items()
            if removed or not f.removed
        }

    def part_of(self, name: str) -> Part:
        return next(p for p in self.parts if name in p.fields)


@cache
def shape(qualified: str) -> Shape:
    entity_type = registry.types()[qualified]
    lineage = [entity_type, *registry.ancestors(entity_type)]
    chain = list(reversed(lineage))
    parts = []
    entries = []
    registered = False
    for t in chain:
        # A list of rows a type below refines has the roles it gives (ADR 0164), and a
        # reference the types it gives (ADR 0235).
        fields = {n: as_refined(lineage, n, f) for n, f in t.fields.items()}
        record, entry = split_entry(fields, t.identity)
        registered = registered or bool(t.identity)
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
        if registered:
            # Each type under an identity has a table of its entries (ADR 0165).
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
    storage = next((t.storage for t in lineage if t.storage is not None), None)
    return Shape(entity_type, tuple(parts + entries), storage.barcode if storage else None)


#: Kinds of field stored as a foreign key, in a column named ``<field>_id``: an entity,
#: a file, a person (ADR 0122).
FOREIGN_KEYS = ("ref", "file", "person")


def list_model(plugin: str, owner: type[models.Model], name: str) -> type[models.Model]:
    """The model of the items of ``owner``'s list ``name``, from ``plugin``'s app."""
    return apps.get_model(naming.app_label(plugin), naming.list_model(owner.__name__, name))


def is_list_table(f: Field) -> bool:
    """Whether a list has a table of its own: of references, files or people, an item per
    row; or of rows (ADR 0137), a column per row's field."""
    return f.type == "list" and f.items is not None and f.items.type in (*FOREIGN_KEYS, "row")


def row_columns(f: Field) -> dict[str, str]:
    """A list of rows' fields, each with the column of its table holding it."""
    assert f.items is not None  # noqa: S101 - lists have items
    return {n: f"{n}_id" if cell.type in FOREIGN_KEYS else n for n, cell in f.items.fields.items()}


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
        held: Callable[[uuid.UUID], str | None] | None = None,
        held_file: Callable[[uuid.UUID], str | None] | None = None,
        entries: Any = None,  # noqa: ANN401
        readable_entries: Any = None,  # noqa: ANN401
        project: str = "the project",
        namer: Any = None,  # noqa: ANN401 - people.Namer
        scanning: bool = False,
    ) -> None:
        self.readable = readable
        self.files = files
        #: Files still being scanned may be named: a run's params, as it waits for their
        #: scans (ADR 0234). A record's value never may be (ADR 0216).
        self.scanning = scanning
        #: Why these values may not refer to an entity, or a file, the caller can read
        #: (ADR 0067, 0132), or None for one they can't: a reference to one says so.
        self.held = held
        self.held_file = held_file
        #: The registry entries the project holds, and those the caller can read.
        self.entries = entries
        self.readable_entries = readable_entries
        self.project = project
        #: Finds the people values name, in the project whose entities they are (ADR 0122).
        self.namer = namer
        #: Already looked up: what to store, why it can't be referred to, or None for
        #: nothing the caller can read.
        self._found: dict[uuid.UUID, Target | str | None] = {}

    def expect(self, stand_in: uuid.UUID, type_name: str) -> None:
        """Takes ``stand_in`` as an entity of ``type_name`` not written yet: a create a bulk
        import's lines refer to (ADR 0140), which it replaces once that's written."""
        self._found[stand_in] = Target(stand_in, type_name)

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
        """A readable and usable file's media type and size."""
        if self.files is None:
            return None
        found = self.files.filter(pk=file_id).select_related("blob__scan_result__release").first()
        if found is None:
            return None
        scan = status_from_decision(getattr(found.blob, "scan_result", None))
        if not (self.scanning and scan.state == "pending"):
            try:
                scan.require_usable()
            except Problem as error:
                raise ValueError(error.detail) from error
        return found.media_type, found.blob.size

    def why_held(self, entity_id: uuid.UUID) -> str | None:
        """Why a readable entity can't be referred to, if it's why it can't be."""
        return None if self.held is None else self.held(entity_id)

    def why_held_file(self, file_id: uuid.UUID) -> str | None:
        return None if self.held_file is None else self.held_file(file_id)


def to_db(f: Field, value: Any, refs: Refs) -> Any:  # noqa: ANN401 - JSON in, a model value out
    """``value`` from JSON as the database stores it. Raises ``ValueError`` with what's wrong."""
    if value is None:
        return None
    if f.is_rows:
        return _rows(f, value, refs)
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


def _rows(f: Field, value: Any, refs: Refs) -> list[dict[str, Any]]:  # noqa: ANN401 - JSON
    """A list of rows (ADR 0137), each an object of its fields' values, as stored: each
    field checked as a type's is, then each role's count and what its rows refer to.
    Raises :class:`RowProblems` with each problem where it is."""
    row = f.items
    assert row is not None  # noqa: S101 - lists always have items
    if not isinstance(value, list):
        raise RowProblems({"": ["must be a list of rows."]})
    errors: dict[str, list[str]] = {}
    own: list[str] = []
    if f.min_items is not None and len(value) < f.min_items:
        own.append(f"needs at least {f.min_items} row{'s' if f.min_items != 1 else ''}.")
    if f.max_items is not None and len(value) > f.max_items:
        own.append(f"can have at most {f.max_items} row{'s' if f.max_items != 1 else ''}.")
    given = [item for item in value if isinstance(item, dict)]
    references = [n for n, cell in row.fields.items() if cell.type == "ref"]
    # One query for every row's references, not one per row.
    refs.types(_uuids(item.get(n) for item in given for n in references))
    if refs.namer is not None and refs.namer.project is not None:
        for n, cell in row.fields.items():
            if cell.type == "person":
                found = [item.get(n) for item in given]
                refs.namer.look_up(cell.role, [v for v in found if isinstance(v, str)])
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors[str(index)] = ["must be an object of the row's fields."]
            continue
        stored: dict[str, Any] = {}
        for name in item:
            if name not in row.fields:
                errors[f"{index}/{name}"] = [
                    f"isn't one of the row's fields: {', '.join(row.fields)}."
                ]
        for name, cell in row.fields.items():
            cell_value = item.get(name)
            if cell_value is None and cell.has_default:
                cell_value = cell.default
            if cell_value is None:
                if cell.required:
                    errors[f"{index}/{name}"] = ["is required."]
                stored[name] = None
                continue
            try:
                stored[name] = to_db(cell, cell_value, refs)
            except ValueError as error:
                errors[f"{index}/{name}"] = [str(error)]
        out.append(stored)
    if row.by is not None and not errors:
        _roles(row, value, out, refs, own, errors)
    if own:
        errors = {"": own, **errors}
    if errors:
        raise RowProblems(errors)
    return out


def _roles(
    row: Field,
    given: list[Any],
    rows: list[dict[str, Any]],
    refs: Refs,
    own: list[str],
    errors: dict[str, list[str]],
) -> None:
    """Each role's count of rows, and what its rows' references refer to (ADR 0137)."""
    assert row.by is not None  # noqa: S101 - roles are by a field
    counts: dict[str, int] = {}
    for stored in rows:
        counts[stored[row.by]] = counts.get(stored[row.by], 0) + 1
    for name, role in row.roles.items():
        n = counts.get(name, 0)
        if role.min is not None and n < role.min:
            own.append(
                f"needs at least {role.min} {name} row{'s' if role.min != 1 else ''}, and has {n}."
            )
        if role.max is not None and n > role.max:
            own.append(
                f"can't have a {name} row."
                if role.max == 0
                else f"can have at most {role.max} {name} row{'s' if role.max != 1 else ''}, "
                f"and has {n}."
            )
    for index, stored in enumerate(rows):
        taken = row.roles.get(stored[row.by])
        if taken is None:
            if row.closed:
                # Its roles are every role it may have (ADR 0164).
                errors[f"{index}/{row.by}"] = [
                    f"isn't one of its roles: {', '.join(row.roles) or 'it has none'}."
                ]
            continue
        for cell, types in taken.to.items():
            named = given[index].get(cell)
            if named is None:
                continue
            found = refs.types(_uuids([named]))
            kind = next(iter(found.values()), None)
            allowed: set[str] = set()
            for t in types:
                allowed |= subtypes(t)
            if kind is not None and kind not in allowed:
                errors[f"{index}/{cell}"] = [
                    f"must refer to {_one_of(types)} in a {taken.name} row, not a {kind}."
                ]


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
    assert f.to  # noqa: S101 - refs always name a type
    found = refs.look_up([target])[target]
    if isinstance(found, str):
        raise ValueError(found)
    if found is None:
        raise ValueError(refs.why_held(target) or "refers to no entity you can read.")
    if found.type not in referable(f):
        raise ValueError(f"must refer to {_one_of(f.to)}, not a {found.type}.")
    return found.id


def referable(f: Field) -> set[str]:
    """The types a reference may refer to (ADR 0136): its types, and their subtypes."""
    out: set[str] = set()
    for target in f.to:
        out |= subtypes(target)
    return out


def _one_of(targets: tuple[str, ...]) -> str:
    """ "a iki/protein.Protein", or "a Protein or a Compound" for several."""
    named = [f"a {t}" for t in targets]
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" or {named[-1]}"


def _file(f: Field, value: Any, refs: Refs) -> uuid.UUID:  # noqa: ANN401 - JSON
    try:
        target = uuid.UUID(str(value))
    except ValueError:
        raise ValueError("must be a file's ID: upload the file first.") from None
    found = refs.file(target)
    if found is None:
        raise ValueError(refs.why_held_file(target) or "refers to no file you can read.")
    media_type, size = found
    if f.media_types and not media_type_allowed(media_type, f.media_types):
        formats = registry.file_formats().formats
        labels = tuple(formats[t].label if t in formats else t for t in f.media_types)
        raise ValueError(f"must be {_either(labels)}, and this file is {media_type}.")
    if f.max_size is not None and size > f.max_size:
        raise ValueError(
            f"can be at most {human_size(f.max_size)}, and this file is {human_size(size)}."
        )
    return target


def media_type_allowed(media_type: str, allowed: Iterable[str]) -> bool:
    """Whether a file matches an accepted format ID, media type or wildcard family."""
    kind = media_type.split("/", 1)[0]
    return any(a == media_type or a == f"{kind}/*" for a in registry.file_formats().expand(allowed))


def _either(choices: tuple[str, ...]) -> str:
    if len(choices) == 1:
        return choices[0]
    return ", ".join(choices[:-1]) + f" or {choices[-1]}"


# --- Database → JSON ----------------------------------------------------------------------------


UNREADABLE_REFERENCE = "unreadable"


def ids_in(fields: Mapping[str, Field], values: Mapping[str, Any]) -> dict[str, set[Any]]:
    """The entity, file and person IDs these values refer to."""
    found: dict[str, set[Any]] = {"ref": set(), "file": set(), "person": set()}
    for name, value in values.items():
        f = fields.get(name)
        if f is None:
            continue
        if f.is_rows and f.items is not None and isinstance(value, list):
            # A list of rows' references, files and people (ADR 0137).
            for cell_name, cell in f.items.fields.items():
                if cell.type in found:
                    found[cell.type].update(
                        r[cell_name]
                        for r in value
                        if isinstance(r, dict) and isinstance(r.get(cell_name), str | uuid.UUID)
                    )
            continue
        kind = f.items.type if f.type == "list" and f.items is not None else f.type
        if kind in found and value is not None:
            items = value if isinstance(value, list) else [value]
            found[kind].update(i for i in items if isinstance(i, str | uuid.UUID))
    return found


def reference_ids(fields: Mapping[str, Field], values: Mapping[str, Any]) -> set[uuid.UUID]:
    """Record and entry IDs in typed values, including references inside lists and rows."""
    return set(_uuids(ids_in(fields, values)["ref"]))


def exact_reference_ids(fields: Mapping[str, Field], values: Mapping[str, Any]) -> set[uuid.UUID]:
    """Identify every typed reference in an immutable copy, using known field and cell shapes."""
    return {pk for _, pk in reference_positions(fields, values)}


def reference_positions(
    fields: Mapping[str, Field], values: Mapping[str, Any]
) -> Iterator[tuple[tuple[str | int, ...], uuid.UUID]]:
    """Typed reference positions within the values of a record, including row cells."""

    def collect(
        f: Field | None,
        value: Any,  # noqa: ANN401 - a stored field or a command's JSON
        at: tuple[str | int, ...],
    ) -> Iterator[tuple[tuple[str | int, ...], uuid.UUID]]:
        if f is None:
            raise ValueError("The copy needs the field types of its stored revision.")
        if value is None:
            return
        if f.type == "ref":
            # A command can name another create in the same output by its local key.
            if isinstance(value, dict) and value.keys() == {"$create"}:
                return
            yield at, uuid.UUID(str(value))
        elif f.type == "list" and f.items is not None:
            if not isinstance(value, list):
                raise ValueError("The copy needs a list for this field.")
            for index, item in enumerate(value):
                yield from collect(f.items, item, (*at, index))
        elif f.type == "row":
            if not isinstance(value, dict):
                raise ValueError("The copy needs named values for this row.")
            for name, item in value.items():
                yield from collect(f.fields.get(name), item, (*at, name))

    for name, value in values.items():
        yield from collect(fields.get(name), value, (name,))


def redact_json_references(
    f: Field | None,
    value: Any,  # noqa: ANN401 - stored JSON
    readable_refs: Collection[uuid.UUID],
) -> Any:  # noqa: ANN401 - JSON out
    """Apply the same read boundary to values that are already JSON."""
    if value is None:
        return None
    if f is None:
        return UNREADABLE_REFERENCE
    if f.type == "ref":
        try:
            target = uuid.UUID(str(value))
        except ValueError:
            return UNREADABLE_REFERENCE
        return value if target in readable_refs else UNREADABLE_REFERENCE
    if f.is_rows and f.items is not None:
        return [
            {
                n: redact_json_references(f.items.fields.get(n), v, readable_refs)
                for n, v in row.items()
            }
            for row in value
        ]
    if f.type == "list" and f.items is not None and f.items.type == "ref":
        return [redact_json_references(f.items, item, readable_refs) for item in value]
    return value


def to_json(
    f: Field,
    value: Any,  # noqa: ANN401 - a model value in
    *,
    readable_refs: Collection[Any] | None = None,
) -> Any:  # noqa: ANN401 - JSON out
    """Serialize a value. Reader-facing callers supply the IDs available to that reader."""
    if value is None:
        return None
    if f.type == "ref" and readable_refs is not None and value not in readable_refs:
        return UNREADABLE_REFERENCE
    if f.is_rows:
        assert f.items is not None  # noqa: S101 - lists always have items
        cells = f.items.fields
        return [
            {n: to_json(cell, row.get(n), readable_refs=readable_refs) for n, cell in cells.items()}
            for row in value
        ]
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        return [to_json(f.items, item, readable_refs=readable_refs) for item in value]
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
    if f.is_rows:
        columns = row_columns(f)
        found = part.list_model(f.name)._default_manager.filter(entity_id=instance.pk)
        return [
            {n: r[c] for n, c in columns.items()}
            for r in found.order_by("position").values(*columns.values())
        ]
    if is_list_table(f):
        rows = part.list_model(f.name)._default_manager.filter(entity_id=instance.pk)
        return list(rows.order_by("position").values_list("item_id", flat=True))
    if f.type in FOREIGN_KEYS:
        return getattr(instance, f"{f.name}_id")
    return getattr(instance, f.name)


def history_value(row: models.Model, f: Field) -> Any:  # noqa: ANN401 - a model value
    """A field's value in a row of its history table, as :func:`read_value` gives it."""
    value = getattr(row, f.name)
    if f.is_rows and value is not None:
        return from_history(f, value)
    return value


def to_history(f: Field, value: Any) -> Any:  # noqa: ANN401 - a model value
    """A value as its history table keeps it: a list of rows as JSON (ADR 0137)."""
    return to_json(f, value) if f.is_rows else value


def from_history(f: Field, value: Any) -> Any:  # noqa: ANN401 - JSON
    """A list of rows from its history's JSON, as :func:`read_value` gives it."""
    assert f.items is not None  # noqa: S101 - lists have items
    cells = f.items.fields
    return [
        {n: _cell_from_json(cell, row.get(n)) for n, cell in cells.items() if n in row}
        | {n: None for n in cells if n not in row}
        for row in value
    ]


def _cell_from_json(f: Field, value: Any) -> Any:  # noqa: ANN401 - JSON
    if value is None:
        return None
    if f.type in FOREIGN_KEYS:
        return uuid.UUID(value)
    if f.type == "date":
        return dt.date.fromisoformat(value)
    if f.type == "datetime":
        return dt.datetime.fromisoformat(value)
    return value
