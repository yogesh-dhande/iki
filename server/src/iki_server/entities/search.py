"""Searching entities (ADR 0037): typed conditions on a type's fields, sorted by one.

A search names a type, and its conditions name that type's fields: its own,
its parents', and those extensions add. Entities of every subtype match too,
since each type's table holds its subtypes' rows. Without a type (or with
``core.Entity``), only the columns every entity has can be named: ``hrid``,
``type``, ``owner_project``, ``created_by``, ``created_at``, ``updated_at``
and ``archived_at``.

Conditions are JSON, so the same search can be sent, saved and shared::

    {
        "all": [
            {"field": "organism", "op": "eq", "value": "Homo sapiens"},
            {"field": "molecular_weight", "op": "gte", "value": {"value": 50, "unit": "kDa"}},
            {"not": {"field": "name", "op": "contains", "value": "fragment"}},
        ]
    }

Each kind of field takes the operators that make sense for it, and values
as it stores them: a quantity in any unit of its kind, a reference by ID or
HRID. Mistakes are reported with a JSON pointer to the condition.

A search can be limited to the members of a collection (``core.Collection``):
those it lists, or those its own search finds, run as the same caller.

Results come in pages, sorted by one field, then by ID. Pages are keyset
pages, so they stay fast and stable however deep they go. Searches run with
a time limit, so one search can't hold the database for long.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from django.contrib.postgres.search import SearchQuery, SearchVector, SearchVectorExact
from django.core import signing
from django.db import OperationalError, connection, transaction
from django.db.models import Exists, F, Model, OuterRef, Q, QuerySet

from iki_server.entities import access, registry
from iki_server.entities.models import Entity
from iki_server.entities.values import Shape, is_list_table, shape, subtypes
from iki_spec import naming, titles, units
from iki_spec.model import Field

#: Bounds on one search, so none is too costly to plan or run.
MAX_CONDITIONS = 50
MAX_DEPTH = 6
MAX_VALUES = 100
MAX_TEXT = 200
#: How long a search may run, in milliseconds.
TIME_LIMIT_MS = 15_000

EQUALITY = ("eq", "ne", "in")
ORDER = ("lt", "lte", "gt", "gte")

#: The operators each kind of field takes, besides ``is_set``, which all do.
OPERATORS: Mapping[str, tuple[str, ...]] = {
    "string": (*EQUALITY, "contains", "starts_with"),
    "text": ("contains", "matches"),
    "enum": EQUALITY,
    "int": (*EQUALITY, *ORDER),
    "float": (*EQUALITY, *ORDER),
    "quantity": (*EQUALITY, *ORDER),
    "date": (*EQUALITY, *ORDER),
    "datetime": (*EQUALITY, *ORDER),
    "bool": ("eq",),
    "ref": EQUALITY,
    "file": ("eq", "ne"),
    "json": (),
    "list": ("has",),
    # Columns every entity has.
    "user": EQUALITY,
    "project": EQUALITY,
    "type": EQUALITY,
}
ALL_OPERATORS = ("is_set", *dict.fromkeys(op for ops in OPERATORS.values() for op in ops))

#: Kinds a search can be sorted by.
SORTABLE = frozenset({"string", "enum", "int", "float", "quantity", "date", "datetime", "bool"})


class SearchError(Exception):
    """What's wrong with a search: ``(pointer, detail)`` pairs, pointers into its JSON."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{p}: {d}" for p, d in errors))
        self.errors = errors


class TooSlow(Exception):  # noqa: N818 - reads as "the search is too slow"
    """The search ran out of time."""


@dataclass(frozen=True)
class Column:
    """What a condition or sort names: how to reach it, and what kind of value it holds."""

    name: str
    kind: str
    path: str
    field: Field | None = None
    #: For lists of references or files: the table of their items.
    items: type[Model] | None = None


_BUILT_IN = {
    "hrid": Column("hrid", "string", "hrid"),
    "type": Column("type", "type", "type"),
    "owner_project": Column("owner_project", "project", "owner_project_id"),
    "created_by": Column("created_by", "user", "created_by_id"),
    "created_at": Column("created_at", "datetime", "created_at"),
    "updated_at": Column("updated_at", "datetime", "updated_at"),
    "archived_at": Column("archived_at", "datetime", "archived_at"),
}


@dataclass(frozen=True)
class Search:
    """A search, as the API takes it (and saved searches keep it)."""

    type: str | None = None
    exact_type: bool = False
    project: uuid.UUID | None = None
    archived: bool | None = None
    q: str | None = None
    where: Mapping[str, Any] | None = None
    sort: str | None = None
    descending: bool = False
    #: Only members of this collection: its ID or HRID.
    collection: str | None = None


#: What a dynamic collection keeps as its ``search``: a search without paging,
#: or a collection of its own (collections don't nest).
STORED_KEYS = ("project", "type", "exact_type", "archived", "q", "where", "sort", "descending")


def stored(data: Mapping[str, Any]) -> Search:
    """The search a dynamic collection keeps, as JSON. Raises :class:`SearchError`."""
    unknown = sorted(set(data) - set(STORED_KEYS))
    if unknown:
        raise SearchError([(f"#/{k}", "isn't part of a search.") for k in unknown])
    values = dict(data)
    if values.get("project") is not None:
        try:
            values["project"] = uuid.UUID(str(values["project"]))
        except ValueError:
            raise SearchError([("#/project", "must be a project's ID.")]) from None
    return Search(**values)


def _in_collection(user: Any, reference: str) -> Q:  # noqa: ANN401 - User
    """Entities in the collection, as ``user`` may see them. Raises :class:`SearchError`."""
    model = cast("type[Entity]", registry.model("core.Collection"))
    try:
        lookup = Q(pk=uuid.UUID(reference))
    except ValueError:
        lookup = Q(hrid=reference)
    collection = access.visible_of(user, model).filter(lookup).first()
    if collection is None:
        raise SearchError([("#/collection", "No such collection, or you can’t see it.")])
    data = collection.search  # type: ignore[attr-defined]
    if data is None:
        items = shape("core.Collection").part_of("members").list_model("members")
        return Q(pk__in=items._default_manager.filter(entity_id=collection.pk).values("item_id"))
    try:
        inner = queryset(user, stored(data))
    except SearchError as error:
        details = " ".join(d for _, d in error.errors)
        raise SearchError(
            [("#/collection", f"{collection.hrid}'s search no longer works: {details}")]
        ) from error
    return Q(pk__in=inner.values("pk"))


class _Compiler:
    def __init__(self, user: Any, form: Shape | None) -> None:  # noqa: ANN401 - User
        self.user = user
        self.form = form
        self.errors: list[tuple[str, str]] = []
        self.count = 0

    def fail(self, pointer: str, detail: str) -> Q:
        self.errors.append((pointer, detail))
        return Q()

    def column(self, name: Any, pointer: str) -> Column | None:  # noqa: ANN401 - JSON
        if not isinstance(name, str):
            self.fail(pointer, "must name a field.")
            return None
        found = column(self.form, name)
        if found is None:
            where = f"{self.form.entity_type.qualified_name}" if self.form else "every entity"
            self.fail(
                pointer,
                f"{where} has no field `{name}`."
                + ("" if self.form else " Name a type to search its fields."),
            )
        return found

    def condition(self, node: Any, pointer: str, depth: int = 1) -> Q:  # noqa: ANN401 - JSON
        if not isinstance(node, Mapping):
            return self.fail(pointer, "must be a condition: an object.")
        if depth > MAX_DEPTH:
            return self.fail(pointer, f"nests too deeply: at most {MAX_DEPTH} levels.")
        groups = [k for k in ("all", "any", "not") if k in node]
        if groups:
            if len(node) != 1:
                return self.fail(pointer, f"has `{groups[0]}`, so it can't have anything else.")
            key = groups[0]
            if key == "not":
                return ~self.condition(node["not"], f"{pointer}/not", depth + 1)
            children = node[key]
            if not isinstance(children, list) or not children:
                return self.fail(f"{pointer}/{key}", "must be a list of conditions.")
            combined = Q()
            for index, child in enumerate(children):
                q = self.condition(child, f"{pointer}/{key}/{index}", depth + 1)
                combined = combined & q if key == "all" else combined | q
            return combined
        return self.leaf(node, pointer)

    def leaf(self, node: Mapping[str, Any], pointer: str) -> Q:
        self.count += 1
        if self.count == MAX_CONDITIONS + 1:
            return self.fail(pointer, f"is one condition too many: at most {MAX_CONDITIONS}.")
        if self.count > MAX_CONDITIONS:
            return Q()
        extra = sorted(set(node) - {"field", "op", "value"})
        if extra:
            return self.fail(
                pointer, f"has `{extra[0]}`: conditions have `field`, `op` and `value`."
            )
        if "field" not in node:
            return self.fail(pointer, "needs a `field`, or `all`, `any` or `not`.")
        col = self.column(node["field"], f"{pointer}/field")
        if col is None:
            return Q()
        op = node.get("op")
        allowed = ("is_set", *OPERATORS[col.kind])
        if op not in allowed:
            return self.fail(
                f"{pointer}/op",
                f"`{col.name}` takes {_either(allowed)}"
                + (f", not `{op}`." if isinstance(op, str) else "."),
            )
        if "value" not in node:
            return self.fail(f"{pointer}/value", "is missing.")
        value = node["value"]
        at = f"{pointer}/value"
        if op == "is_set":
            if not isinstance(value, bool):
                return self.fail(at, "must be true or false.")
            return _is_set(col) if value else ~_is_set(col)
        if op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_VALUES:
                return self.fail(at, f"must be a list of 1 to {MAX_VALUES} values.")
            converted = [self.value(col, v, f"{at}/{i}") for i, v in enumerate(value)]
            if any(v is _BAD for v in converted):
                return Q()
            if col.kind in ("float", "quantity"):
                return _any(_equal(col, v) for v in converted)
            return Q(**{f"{col.path}__in": converted})
        if op == "has":
            assert col.field is not None and col.field.items is not None  # noqa: S101 - lists
            item = Column(col.name, col.field.items.type, col.path, col.field.items)
            converted = self.value(item, value, at)
            if converted is _BAD:
                return Q()
            if col.items is not None:
                return Q(
                    Exists(col.items._default_manager.filter(entity=OuterRef("pk"), item=converted))
                )
            return Q(**{f"{col.path}__contains": [converted]})
        if op in ("contains", "starts_with", "matches"):
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
                return self.fail(at, f"must be some text, up to {MAX_TEXT} characters.")
            if op == "matches":
                # `-fragment` matches text without the word, not entities without text.
                return Q(**{f"{col.path}__isnull": False}) & Q(
                    SearchVectorExact(
                        SearchVector(col.path, config="simple"),
                        SearchQuery(value, config="simple", search_type="websearch"),
                    )
                )
            lookup = "containing" if op == "contains" else "starting_with"
            return Q(**{f"{col.path}__{lookup}": value})
        converted = self.value(col, value, at)
        if converted is _BAD:
            return Q()
        if op in ("eq", "ne"):
            equal = (
                _equal(col, converted)
                if col.kind in ("float", "quantity")
                else Q(**{col.path: converted})
            )
            return equal if op == "eq" else ~equal
        return Q(**{f"{col.path}__{op}": converted})

    def value(self, col: Column, value: Any, pointer: str) -> Any:  # noqa: ANN401 - JSON
        try:
            return _CONVERT[col.kind](self, col, value)
        except ValueError as error:
            self.fail(pointer, str(error))
            return _BAD


_BAD = object()


#: How near a number must be to be equal: values converted between units (86 °F is
#: 30.000000000000004 °C) differ from what was typed in their last digits.
RELATIVE_TOLERANCE = 1e-9


def _equal(col: Column, value: float) -> Q:
    margin = abs(value) * RELATIVE_TOLERANCE
    return Q(**{f"{col.path}__gte": value - margin, f"{col.path}__lte": value + margin})


def _any(conditions: Iterable[Q]) -> Q:
    out = Q()
    for q in conditions:
        out |= q
    return out


def _is_set(col: Column) -> Q:
    if col.items is not None:
        return Q(Exists(col.items._default_manager.filter(entity=OuterRef("pk"))))
    if col.kind == "list":
        return Q(**{f"{col.path}__isnull": False}) & ~Q(**{col.path: []})
    return Q(**{f"{col.path}__isnull": False})


def _either(choices: tuple[str, ...]) -> str:
    quoted = [f"`{c}`" for c in choices]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} or {quoted[-1]}"


# --- Values -------------------------------------------------------------------------------------


def _text(_: _Compiler, col: Column, value: Any) -> str:  # noqa: ANN401 - JSON
    if not isinstance(value, str):
        raise ValueError("must be text.")
    if col.kind == "enum" and col.field is not None and value not in col.field.values:
        raise ValueError(f"must be one of {_either(tuple(col.field.values or ()))}.")
    return value


def _int(_: _Compiler, __: Column, value: Any) -> int:  # noqa: ANN401 - JSON
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be a whole number.")
    return value


def _float(_: _Compiler, __: Column, value: Any) -> float:  # noqa: ANN401 - JSON
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("must be a number.")
    return float(value)


def _quantity(c: _Compiler, col: Column, value: Any) -> float:  # noqa: ANN401 - JSON
    assert col.field is not None and col.field.unit is not None  # noqa: S101 - quantities
    unit = col.field.unit
    if isinstance(value, Mapping):
        if set(value) != {"value", "unit"} or not isinstance(value["unit"], str):
            raise ValueError(f'must be a number in {unit}, or {{"value": …, "unit": …}}.')
        number = _float(c, col, value["value"])
        try:
            return units.convert(number, value["unit"], unit)
        except units.UnitError as error:
            raise ValueError(f"has a unit Iki can't use here: {error}") from None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f'must be a number in {unit}, or {{"value": …, "unit": …}}.')
    return _float(c, col, value)


def _bool(_: _Compiler, __: Column, value: Any) -> bool:  # noqa: ANN401 - JSON
    if not isinstance(value, bool):
        raise ValueError("must be true or false.")
    return value


def _date(_: _Compiler, __: Column, value: Any) -> dt.date:  # noqa: ANN401 - JSON
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("must be a date, such as 2026-03-01.") from None


def _datetime(_: _Compiler, __: Column, value: Any) -> dt.datetime:  # noqa: ANN401 - JSON
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("must be a date and time, such as 2026-03-01T09:30:00Z.") from None
    if parsed.tzinfo is None:
        raise ValueError("needs a time zone, such as Z or +01:00.")
    return parsed


def _uuid(_: _Compiler, __: Column, value: Any) -> uuid.UUID:  # noqa: ANN401 - JSON
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("must be an ID.") from None


def _ref(c: _Compiler, _: Column, value: Any) -> uuid.UUID:  # noqa: ANN401 - JSON
    if not isinstance(value, str):
        raise ValueError("must be an entity's ID or HRID.")
    try:
        lookup = Q(pk=uuid.UUID(value))
    except ValueError:
        lookup = Q(hrid=value)
    found: uuid.UUID | None = (
        access.visible(c.user).filter(lookup).values_list("pk", flat=True).first()
    )
    if found is None:
        raise ValueError("refers to no entity you can read.")
    return found


def _type(_: _Compiler, __: Column, value: Any) -> str:  # noqa: ANN401 - JSON
    if not isinstance(value, str) or value not in registry.types():
        raise ValueError("must be an entity type, such as protein.Protein.")
    return value


_CONVERT: Mapping[str, Callable[[_Compiler, Column, Any], Any]] = {
    "string": _text,
    "text": _text,
    "enum": _text,
    "int": _int,
    "float": _float,
    "quantity": _quantity,
    "bool": _bool,
    "date": _date,
    "datetime": _datetime,
    "ref": _ref,
    "file": _uuid,
    "user": _uuid,
    "project": _uuid,
    "type": _type,
}


# --- Columns ------------------------------------------------------------------------------------


def column(form: Shape | None, name: str) -> Column | None:
    """The column a search names, on a type (with its parents and extensions), or None."""
    if name in _BUILT_IN:
        return _BUILT_IN[name]
    if form is None:
        return None
    f = form.fields().get(name)
    if f is None:
        return None
    part = form.part_of(name)
    prefix = f"{naming.extension_related_name(part.plugin)}__" if part.extension else ""
    if is_list_table(f):
        return Column(name, f.type, prefix + name, f, items=part.list_model(name))
    if f.type in ("ref", "file"):
        return Column(name, f.type, f"{prefix}{name}_id", f)
    return Column(name, f.type, prefix + name, f)


def form_of(type_name: str | None) -> Shape | None:
    """The shape a search's conditions name fields of: None for any entity."""
    if type_name is None or type_name == "core.Entity":
        return None
    return shape(type_name)


# --- Running ------------------------------------------------------------------------------------


def queryset(user: Any, search: Search) -> QuerySet[Entity]:  # noqa: ANN401 - User
    """The entities the user can read that match. Raises :class:`SearchError`."""
    if search.type is not None and search.type not in registry.types():
        raise SearchError([("#/type", f"There’s no entity type {search.type}.")])
    form = form_of(search.type)
    # A type's table holds its subtypes' rows too.
    found = access.visible_of(user, form.model if form else Entity)
    if search.type is not None and search.exact_type:
        found = found.filter(type=search.type)
    if search.project is not None:
        found = found.filter(links__project_id=search.project)
    if search.archived is not None:
        found = found.filter(archived_at__isnull=not search.archived)
    if search.collection is not None:
        found = found.filter(_in_collection(user, search.collection))
    if search.q and search.q.strip():
        names = (
            ({search.type} if search.exact_type else subtypes(search.type))
            if search.type
            else set(registry.types())
        )
        found = found.filter(matching(search.q.strip(), names))
    errors: list[tuple[str, str]] = []
    if search.where is not None:
        compiler = _Compiler(user, form)
        where = compiler.condition(search.where, "#/where")
        errors += compiler.errors
        found = found.filter(where)
    if search.sort is not None:
        col = column(form, search.sort)
        if col is None:
            errors.append(("#/sort", f"There’s no field `{search.sort}` to sort by."))
        elif col.kind not in SORTABLE or col.items is not None:
            errors.append(("#/sort", f"`{search.sort}` can't be sorted by."))
    if errors:
        raise SearchError(errors)
    return found.select_related("created_by")


def title_text_fields(type_name: str) -> list[str]:
    """The text fields a type's title shows, which ``q`` searches."""
    entity_type = registry.types().get(type_name)
    template = registry.display_title(entity_type) if entity_type is not None else None
    if template is None:
        return []
    fields = shape(type_name).fields()
    words = {m.group("word") for m in titles.TOKEN.finditer(template) if m.group("word")}
    return sorted(w for w in words if w in fields and fields[w].type in ("string", "text"))


def matching(q: str, type_names: set[str]) -> Q:
    """Entities whose HRID contains ``q``, or whose title's text fields do.

    One ``IN`` over a union, not an ``OR``: each part then uses its own trigram index.
    """
    parts: list[QuerySet[Any]] = [Entity.objects.filter(hrid__containing=q).values("pk")]
    for name in sorted(type_names):
        entity_type = registry.types()[name]
        if name == "core.Entity" or entity_type.abstract:
            continue
        # Titles name the type's own fields; an extension's are in other tables.
        form = shape(name)
        fields = [f for f in title_text_fields(name) if not form.part_of(f).extension]
        if not fields:
            continue
        text = Q()
        for f in fields:
            text |= Q(**{f"{f}__containing": q})
        parts.append(form.model._default_manager.filter(text).values("pk"))
    return Q(pk__in=parts[0].union(*parts[1:]) if len(parts) > 1 else parts[0])


_SALT = "iki.entities.search"


def page(
    found: QuerySet[Entity], search: Search, cursor: str | None, limit: int
) -> tuple[list[Entity], str | None]:
    """One page of the search's results, and the cursor for the next (or None)."""
    form = form_of(search.type)
    col = column(form, search.sort) if search.sort else None
    key = search.sort, search.descending
    # Sorting by when entities were made is sorting by ID (UUIDv7), which is indexed.
    by_id = col is None or col.name == "created_at"
    after = _decode(cursor, key) if cursor else None
    if by_id:
        descending = col is not None and search.descending
        ordered = found.order_by("-pk" if descending else "pk")
        if after is not None:
            ordered = (
                ordered.filter(pk__lt=after[1]) if descending else ordered.filter(pk__gt=after[1])
            )
        rows = list(ordered[: limit + 1])
        more = len(rows) > limit
        rows = rows[:limit]
        last = rows[-1] if rows and more else None
        return rows, _encode(key, None, last.pk) if last is not None else None
    assert col is not None  # noqa: S101 - not by ID
    expression = F(col.path)
    order = (
        expression.desc(nulls_last=True) if search.descending else expression.asc(nulls_last=True)
    )
    ordered = found.annotate(sort_value=expression).order_by(order, "pk")
    if after is not None:
        value, pk = _restore(col, after[0]), after[1]
        if value is None:
            ordered = ordered.filter(**{f"{col.path}__isnull": True, "pk__gt": pk})
        else:
            beyond = "lt" if search.descending else "gt"
            ordered = ordered.filter(
                Q(**{f"{col.path}__{beyond}": value})
                | Q(**{col.path: value, "pk__gt": pk})
                | Q(**{f"{col.path}__isnull": True})
            )
    rows = list(ordered[: limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows and more else None
    if last is None:
        return rows, None
    return rows, _encode(key, _plain(getattr(last, "sort_value", None)), last.pk)


def _plain(value: Any) -> Any:  # noqa: ANN401 - a column's value
    return value.isoformat() if isinstance(value, dt.date | dt.datetime) else value


def _restore(col: Column, value: Any) -> Any:  # noqa: ANN401 - from a cursor
    if value is None:
        return None
    if col.kind == "datetime":
        return dt.datetime.fromisoformat(value)
    if col.kind == "date":
        return dt.date.fromisoformat(value)
    return value


def _encode(key: tuple[str | None, bool], value: Any, pk: uuid.UUID) -> str:  # noqa: ANN401
    return signing.Signer(salt=_SALT).sign_object([*key, value, str(pk)], compress=True)


def _decode(cursor: str, key: tuple[str | None, bool]) -> tuple[Any, uuid.UUID]:
    try:
        sort, descending, value, pk = signing.Signer(salt=_SALT).unsign_object(cursor)
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise SearchError([("#/cursor", "Not a cursor from this API.")]) from error
    if (sort, descending) != key:
        raise SearchError([("#/cursor", "This cursor is from a search sorted another way.")])
    return value, uuid.UUID(pk)


@contextmanager
def time_limit(ms: int = TIME_LIMIT_MS) -> Iterator[None]:
    """Runs the block's queries in a transaction that cancels any taking longer than ``ms``.
    Raises :class:`TooSlow` if one does."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f"{ms}ms"])
            yield
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) == "57014":  # query_canceled
            raise TooSlow from error
        raise
