"""Searching entities (ADR 0037): typed conditions on a type's fields, sorted by one.

A search names a type, and its conditions name that type's fields: its own,
its parents', and those extensions add. Entities of every subtype match too,
since each type's table holds its subtypes' rows. Without a type (or with
``iki/core.Entity``), only the columns every entity has can be named: ``hrid``,
``type``, ``owner_project``, ``sent_to`` (the project a request was sent to, ADR
0132), ``created_by``, ``created_at``, ``updated_at``,
``archived_at``, and ``made``: how it was made (ADR 0106), matched with ``has``
against one making at a time, ``{"software": "AlphaFold", "by": "iki"}``.
A state field (ADR 0125) is searched as an enum of its states, by name.
``upstream`` and ``downstream`` (ADR 0123) find what an entity was derived from, or what
was derived from it, through references and runs: ``{"field": "downstream", "op": "of",
"value": "TUB-000001"}``, or with ``{"entity": …, "depth": …, "edges": […]}``; ``is_set``
finds entities with anything that way, or nothing.
``held`` and ``breaks`` (ADR 0127, ADR 0135) find what plugins' rules caught: records held
for review by a rule, and records that break a rule they were saved before, by the rule's
name (``eq``, ``ne``, ``in``), or by any rule (``is_set``).

A sequence (a string with a sequence format) also takes ``motif`` (ADR 0175): bases or
residues in IUPAC codes, ``GAATTY``, found on a duplex's other strand as its reverse
complement, and across a circle's origin, as its ``strands`` and ``topology`` fields say.

A date or a time can be compared with ``{{ now }}``: when the search runs (a date with
today's, in UTC). Fields naming people (ADR 0122) take a person's ID, their email address, or
``{{ me }}``, whoever searches (so a saved search shared with a project means each
of its people); ``created_by`` does too. ``assigned`` finds records someone is
assigned now, in any of their type's assignment fields that counts in their state,
and ``needs_reassigning`` those whose assignees can't be named there any more.

Conditions are JSON, so the same search can be sent, saved and shared::

    {
        "all": [
            {"field": "organism", "op": "eq", "value": "TAX-000001"},
            {"field": "molecular_weight", "op": "gte", "value": {"value": 50, "unit": "ku"}},
            {"not": {"field": "name", "op": "contains", "value": "fragment"}},
        ]
    }

Each kind of field takes the operators that make sense for it, and values
as it stores them: a quantity in any unit of its kind, a reference by ID or
HRID. Mistakes are reported with a JSON pointer to the condition.

A search can be limited to the members of a collection (``iki/core.Collection``):
those it lists, or those its own search finds, run as the same caller.

Results come in pages, sorted by one field, then by ID. Pages are keyset
pages, so they stay fast and stable however deep they go. Searches run with
a time limit, so one search can't hold the database for long.

A sort can go on to others (``then``) for what it ties, as a core's queue is sorted by
its own ranking, then priority, then when each is needed (ADR 0232). Requests also sort
by the projects on their two sides, by name (``asked_by``, ``sent_to``), which each side
sees of the other (ADR 0132). Blanks are last in each, either way.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from django.contrib.postgres.search import SearchQuery, SearchVector, SearchVectorExact
from django.core import signing
from django.db import OperationalError, connection, transaction
from django.db.models import (
    Case,
    Exists,
    F,
    IntegerField,
    Model,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Concat, Left, Length, Lower, Right
from django.db.models.lookups import Exact, GreaterThanOrEqual, IRegex

from iki_server.entities import access, registry
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import FOREIGN_KEYS, Shape, is_list_table, shape, subtypes
from iki_spec import conditions, identity, motifs, naming, titles, units
from iki_spec.model import Field
from iki_spec.notifications import now_of

#: Bounds on one search, so none is too costly to plan or run.
MAX_CONDITIONS = 50
MAX_DEPTH = 6
MAX_VALUES = 100
MAX_TEXT = 200
#: How long a search may run, in milliseconds.
TIME_LIMIT_MS = 15_000

# Each kind of field's operators, and what can be sorted by: the validator checks the
# searches components declare with the same table (ADR 0117).
EQUALITY, ORDER = conditions.EQUALITY, conditions.ORDER
OPERATORS = conditions.OPERATORS
ALL_OPERATORS = conditions.ALL_OPERATORS
SORTABLE = conditions.SORTABLE


class SearchError(Exception):
    """What's wrong with a search: ``(pointer, detail)`` pairs, pointers into its JSON."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{p}: {d}" for p, d in errors))
        self.errors = errors


class TooSlow(Exception):  # noqa: N818 - reads as "the search is too slow"
    """The search ran out of time, or would: ``detail`` says why, when it's known."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


#: Sorting by a field without an index sorts every match: past this many, too slow for a
#: page (about half a second a million, ADR 0103).
UNINDEXED_SORT_MAX = 1_000_000
#: The columns every entity has that an index orders. The rest of them don't.
_INDEXED_BUILT_IN = frozenset({"hrid", "created_at", "updated_at"})


@dataclass(frozen=True)
class Column:
    """What a condition or sort names: how to reach it, and what kind of value it holds."""

    name: str
    kind: str
    path: str
    field: Field | None = None
    #: For lists of references or files: the table of their items.
    items: type[Model] | None = None
    #: For a registry entry's field (ADR 0115): the table of the type's entries, which
    #: ``path`` starts from. A record matches by its entry.
    entry: type[Model] | None = None


_BUILT_IN = {
    "hrid": Column("hrid", "string", "hrid"),
    "type": Column("type", "type", "type"),
    "owner_project": Column("owner_project", "project", "owner_project_id"),
    # The service a request was sent to (ADR 0132): its queue is ``sent_to`` eq its project.
    "sent_to": Column("sent_to", "project", "iki_request__service_id"),
    "created_by": Column("created_by", "user", "created_by_id"),
    "created_at": Column("created_at", "datetime", "created_at"),
    "updated_at": Column("updated_at", "datetime", "updated_at"),
    "archived_at": Column("archived_at", "datetime", "archived_at"),
    "made": Column("made", "making", "pk"),
    # Worked out by :mod:`iki_server.entities.assignments` (ADR 0122).
    "assigned": Column("assigned", "person", "pk"),
    "needs_reassigning": Column("needs_reassigning", "bool", "pk"),
    # Across the graph (ADR 0123), worked out by :mod:`iki_server.graph.queries`.
    "upstream": Column("upstream", "graph", "pk"),
    "downstream": Column("downstream", "graph", "pk"),
    # What plugins' rules caught (ADR 0127): open holds, and breaches found after a save.
    "held": Column("held", "rule", "pk"),
    "breaks": Column("breaks", "rule", "pk"),
}

#: The most conditions across the graph one search may have: each is a walk of its own.
MAX_GRAPH_CONDITIONS = 3

#: Columns whose conditions :mod:`iki_server.entities.assignments` compiles.
_ASSIGNMENT_COLUMNS = frozenset({"assigned", "needs_reassigning"})

#: Columns whose conditions name plugins' rules (ADR 0127).
_RULE_COLUMNS = frozenset({"held", "breaks"})

#: The longest name a rule has (``RuleHold.rule``).
MAX_RULE_NAME = 160

#: What a condition's value can say for the person searching (ADR 0117, ADR 0122).
ME = "{{ me }}"


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
    #: Only what this run wrote, or found already written: its ID or HRID. A spreadsheet
    #: import's records (ADR 0178), say.
    run: str | None = None
    #: Of any one of these types, or their subtypes: what a reference to several types may
    #: name (ADR 0136). Conditions then name only the columns every entity has.
    types: tuple[str, ...] = ()
    #: Further sorts, each a field and whether it's descending, for what ``sort`` ties.
    then: tuple[tuple[str, bool], ...] = ()


#: What a dynamic collection keeps as its ``search``: a search without paging,
#: or a collection of its own (collections don't nest).
STORED_KEYS = (
    "project",
    "type",
    "exact_type",
    "archived",
    "q",
    "where",
    "sort",
    "descending",
    "then",
)
#: The most sorts a search has, its first among them.
MAX_SORTS = 4


def then_of(value: Any) -> tuple[tuple[str, bool], ...]:  # noqa: ANN401 - JSON
    """A search's further sorts, as JSON keeps them: ``[{"field": …, "descending": …}]``.
    Raises :class:`SearchError`."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise SearchError([("#/then", "must be a list of sorts.")])
    out = []
    for index, item in enumerate(value):
        if isinstance(item, list | tuple) and len(item) == 2:
            field, descending = item
        elif isinstance(item, Mapping) and set(item) <= {"field", "descending"}:
            field, descending = item.get("field"), item.get("descending", False)
        else:
            raise SearchError([(f"#/then/{index}", "must be a field and its direction.")])
        if not isinstance(field, str) or not isinstance(descending, bool):
            raise SearchError([(f"#/then/{index}", "must be a field and its direction.")])
        out.append((field, descending))
    return tuple(out)


def then_json(then: Iterable[tuple[str, bool]]) -> list[dict[str, Any]]:
    """Further sorts, as JSON keeps them."""
    return [{"field": f, "descending": d} for f, d in then]


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
    values["then"] = then_of(values.get("then"))
    return Search(**values)


def _by_run(user: Any, reference: str) -> Q:  # noqa: ANN401 - User
    """Entities the run wrote, or found written (ADR 0044), as ``user`` may see the run.
    Raises :class:`SearchError`."""
    from iki_server.runs.models import RunEffect
    from iki_server.runs.services import visible_runs

    try:
        lookup = Q(pk=uuid.UUID(reference))
    except ValueError:
        lookup = Q(hrid=reference.upper())
    run = visible_runs(user).filter(lookup).first()
    if run is None:
        raise SearchError([("#/run", "No such run, or you can’t see it.")])
    return Q(pk__in=RunEffect.objects.filter(run=run).values("entity_id"))


def _in_collection(user: Any, reference: str) -> Q:  # noqa: ANN401 - User
    """Entities in the collection, as ``user`` may see them. Raises :class:`SearchError`."""
    model = cast("type[Entity]", registry.model("iki/core.Collection"))
    try:
        lookup = Q(pk=uuid.UUID(reference))
    except ValueError:
        lookup = Q(hrid=reference)
    collection = access.visible_of(user, model).filter(lookup).first()
    if collection is None:
        raise SearchError([("#/collection", "No such collection, or you can’t see it.")])
    data = collection.search  # type: ignore[attr-defined]
    if data is None:
        items = shape("iki/core.Collection").part_of("members").list_model("members")
        members = items._default_manager.filter(entity_id=collection.pk).values("item_id")
        # A member of a type with an identity is its registry entry: its records match.
        return Q(pk__in=members) | Q(entry_id__in=members)
    try:
        inner = queryset(user, stored(data))
    except SearchError as error:
        details = " ".join(d for _, d in error.errors)
        raise SearchError(
            [("#/collection", f"{collection.hrid}'s search no longer works: {details}")]
        ) from error
    return Q(pk__in=inner.values("pk"))


class _Compiler:
    def __init__(
        self,
        user: Any,  # noqa: ANN401 - User
        form: Shape | None,
        templated: Callable[[Any], bool] | None = None,
        within: Iterable[str] | None = None,
    ) -> None:
        self.user = user
        self.form = form
        #: The types searched (with their subtypes), or None for every type.
        self.within = within
        self.templated = templated
        self.errors: list[tuple[str, str]] = []
        self.count = 0
        self.graphs = 0
        self._named: Column | None = None

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
        condition = self._condition(node, pointer)
        col = self._named
        if col is None or col.entry is None or not condition:
            return condition
        # A registry entry's field (ADR 0115): the records whose entry matches.
        return Q(entry_id__in=col.entry._default_manager.filter(condition).values("pk"))

    def _condition(self, node: Mapping[str, Any], pointer: str) -> Q:
        self._named = None
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
        self._named = col
        op = node.get("op")
        allowed = (
            "is_set",
            *conditions.operators(col.kind, col.field.format if col.field else None),
        )
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
        if self.templated is not None and self.templated(value):
            # Worked out where it's used (an app's expression, checked on its own), so only
            # the field and operator are checked here.
            return Q()
        if col.kind == "graph":
            return self.graph(col, op, value, at)
        if col.name in _ASSIGNMENT_COLUMNS and col.field is None:
            return self.assignment(col, op, value, at)
        if col.name in _RULE_COLUMNS and col.field is None:
            return self.caught(col, op, value, at)
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
        if op == "has" and col.kind == "making":
            return self.making(value, at)
        if op == "has" and col.field is not None and col.field.is_rows:
            return self.row(col, value, at)
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
        if op == "motif":
            return self.motif(col, value, at)
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

    def motif(self, col: Column, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """A sequence's ``motif`` (ADR 0175): its alphabet's IUPAC codes, matching a residue
        each certainly is; on a duplex's other strand too, and across a circle's origin."""
        assert col.field is not None and col.field.format is not None  # noqa: S101 - sequences
        format = col.field.format
        if not isinstance(value, str):
            return self.fail(at, "must be bases or residues in IUPAC codes, such as `GAATTC`.")
        try:
            wanted = motifs.read(format, value)
        except motifs.MotifError as error:
            return self.fail(at, str(error))
        if len(wanted) > MAX_TEXT:
            return self.fail(at, f"must be at most {MAX_TEXT} codes long.")
        return having_motif(
            col.path,
            format,
            wanted,
            topology=self.beside(col, col.field.topology),
            strands=self.beside(col, col.field.strands),
        )

    def beside(self, col: Column, name: str | None) -> str | None:
        """The path to a field a sequence's identity reads it by (ADR 0138), from where the
        sequence is: its topology or its strands."""
        found = column(self.form, name) if name is not None else None
        if found is None or found.entry is not col.entry:
            return None
        return found.path

    def graph(self, col: Column, op: str, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """``upstream`` and ``downstream`` (ADR 0123): what an entity was derived from, or
        what was derived from it, as far as the searcher can read."""
        from iki_server.graph import queries

        self.graphs += 1
        if self.graphs > MAX_GRAPH_CONDITIONS:
            return self.fail(
                at.removesuffix("/value"),
                f"is one condition across the graph too many: at most {MAX_GRAPH_CONDITIONS}.",
            )
        if op == "is_set":
            if not isinstance(value, bool):
                return self.fail(at, "must be true or false.")
            return queries.condition(self.user, col.name, None, is_set=value)
        given = value if isinstance(value, Mapping) else {"entity": value}
        extra = sorted(set(given) - {"entity", "depth", "edges"})
        if extra:
            return self.fail(f"{at}/{extra[0]}", "isn't one: give `entity`, `depth` and `edges`.")
        depth = given.get("depth", queries.DEFAULT_DEPTH)
        if isinstance(depth, bool) or not isinstance(depth, int):
            return self.fail(f"{at}/depth", "must be a whole number.")
        if not 1 <= depth <= queries.MAX_DEPTH:
            return self.fail(f"{at}/depth", f"must be from 1 to {queries.MAX_DEPTH}.")
        edges = given.get("edges")
        if edges is not None and (
            not isinstance(edges, list)
            or not edges
            or any(not isinstance(e, str) or e not in queries.KINDS for e in edges)
        ):
            return self.fail(f"{at}/edges", f"must be a list of {_either(queries.KINDS)}.")
        root = self.root(given.get("entity"), f"{at}/entity" if given is value else at)
        if root is None:
            return Q()
        wanted = queries.Query(root, col.name, depth, queries.kinds_of(edges))
        return queries.condition(self.user, col.name, wanted)

    def root(self, value: Any, at: str) -> uuid.UUID | None:  # noqa: ANN401 - JSON
        """Where a walk starts: a record by its ID, or what an HRID names. An identity's
        HRID is its registry entry, whose records the walk reaches first (ADR 0115)."""
        if not isinstance(value, str) or not value.strip():
            self.fail(at, "must be an entity's ID or HRID.")
            return None
        records = access.visible(self.user)
        entries = access.visible_entries(self.user)
        try:
            key = uuid.UUID(value)
        except ValueError:
            hrid = value.strip().upper()
            found = entries.filter(hrid=hrid).values_list("pk", flat=True).first()
            if found is None:
                found = (
                    records.filter(hrid=hrid, entry__isnull=True)
                    .values_list("pk", flat=True)
                    .first()
                )
        else:
            found = records.filter(pk=key).values_list("pk", flat=True).first()
            if found is None:
                found = entries.filter(pk=key).values_list("pk", flat=True).first()
        if found is None:
            self.fail(at, "refers to no entity you can read.")
        return found

    def assignment(self, col: Column, op: str, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """``assigned`` and ``needs_reassigning`` (ADR 0122)."""
        from iki_server.entities import assignments

        if op == "is_set" or col.name == "needs_reassigning":
            if not isinstance(value, bool):
                return self.fail(at, "must be true or false.")
            return assignments.condition(self.within, col.name, op, value=value)
        values = value if op == "in" else [value]
        if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= MAX_VALUES):
            return self.fail(at, f"must be a list of 1 to {MAX_VALUES} values.")
        persons = [
            self.value(col, v, f"{at}/{i}" if op == "in" else at) for i, v in enumerate(values)
        ]
        if any(p is _BAD for p in persons):
            return Q()
        return assignments.condition(self.within, col.name, op, persons)

    def caught(self, col: Column, op: str, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """``held`` and ``breaks`` (ADR 0127): records a rule holds for review now, and
        records that break a rule they were saved before, by the rule, or by any."""
        from iki_server.entities.models import RuleBreach, RuleHold

        rows: QuerySet[Any] = (
            RuleHold.objects.filter(reviewed_at__isnull=True)
            if col.name == "held"
            else RuleBreach.objects.all()
        )
        if op == "is_set":
            if not isinstance(value, bool):
                return self.fail(at, "must be true or false.")
            caught = Q(pk__in=rows.values("entity_id"))
            return caught if value else ~caught
        if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= MAX_VALUES):
            return self.fail(at, f"must be a list of 1 to {MAX_VALUES} rules.")
        given = value if op == "in" else [value]
        names = [self.rule(col, v, f"{at}/{i}" if op == "in" else at) for i, v in enumerate(given)]
        if any(n is None for n in names):
            return Q()
        caught = Q(pk__in=rows.filter(rule__in=names).values("entity_id"))
        return ~caught if op == "ne" else caught

    def rule(self, col: Column, value: Any, at: str) -> str | None:  # noqa: ANN401 - JSON
        """A rule's name, as ``held`` or ``breaks`` takes it: with a type, one of its rules
        that could catch a record that way, or one that has (a rule its plugin dropped
        or changed since still holds what it held)."""
        from iki_server.entities import checking
        from iki_server.entities.models import RuleBreach, RuleHold

        if not isinstance(value, str) or not value.strip() or len(value) > MAX_RULE_NAME:
            self.fail(at, "must name a rule, such as `purity_in_specification`.")
            return None
        if self.within is None or self.form is None:
            return value
        searched = self.form.entity_type.qualified_name
        holding = col.name == "held"
        # In the order the type declares them, its subtypes' after.
        known = list(
            dict.fromkeys(
                r.key
                for name in sorted(self.within, key=lambda n: (n != searched, n))
                if name in registry.types()
                for r in checking.checks_of(name)
                if bool(r.hold) == holding
            )
        )
        if value in known:
            return value
        rows = RuleHold.objects if holding else RuleBreach.objects
        if rows.filter(entity__type__in=self.within, rule=value).exists():
            return value
        what = self.form.entity_type.label_plural or self.form.entity_type.label
        if not known:
            self.fail(
                at,
                f"{what} have no rules that "
                + ("hold them for review." if holding else "they can break once saved."),
            )
        else:
            kind = "that holds them for review" if holding else "they can break once saved"
            self.fail(at, f"isn't a rule {kind}: {what} have {_either(tuple(known))}.")
        return None

    def row(self, col: Column, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """Entities a row of whose list of rows (ADR 0137) has every value ``value`` gives:
        one row, so "a heavy chain of PRT-000001" can't match PRT-000001 as a light chain
        beside another heavy chain."""
        assert col.field is not None and col.field.items is not None  # noqa: S101 - rows
        assert col.items is not None  # noqa: S101 - rows have a table
        cells = col.field.items.fields
        if not isinstance(value, Mapping) or not value:
            return self.fail(at, f"must be an object with any of {_either(tuple(cells))}.")
        extra = sorted(set(value) - set(cells))
        if extra:
            return self.fail(f"{at}/{extra[0]}", f"isn't one: give {_either(tuple(cells))}.")
        rows = col.items._default_manager.filter(entity=OuterRef("pk"))
        for key, given in value.items():
            cell = cells[key]
            path = f"{key}_id" if cell.type in FOREIGN_KEYS else key
            item = Column(key, cell.type, path, cell)
            if cell.type not in _CONVERT or cell.type in ("text", "file"):
                return self.fail(f"{at}/{key}", f"`{key}` can't be searched for in a row.")
            converted = self.value(item, given, f"{at}/{key}")
            if converted is _BAD:
                return Q()
            if cell.type in ("float", "quantity"):
                margin = abs(converted) * RELATIVE_TOLERANCE
                rows = rows.filter(**{f"{path}__gte": converted - margin})
                rows = rows.filter(**{f"{path}__lte": converted + margin})
            else:
                rows = rows.filter(**{path: converted})
        return Q(Exists(rows))

    def making(self, value: Any, at: str) -> Q:  # noqa: ANN401 - JSON
        """Entities one of whose makings (ADR 0106) has every key ``value`` gives: one row,
        so "AlphaFold, recorded by Iki" can't match a source's AlphaFold and Iki's
        ImmuneBuilder. Software is matched whatever its case; the rest exactly."""
        from iki_server.runs.models import EntityMaking, MadeBy, Making

        if not isinstance(value, Mapping) or not value:
            return self.fail(at, f"must be an object with any of {_either(_MAKING_KEYS)}.")
        extra = sorted(set(value) - set(_MAKING_KEYS))
        if extra:
            return self.fail(f"{at}/{extra[0]}", f"isn't one: give {_either(_MAKING_KEYS)}.")
        allowed = {"kind": Making.values, "by": MadeBy.values}
        rows = EntityMaking.objects.filter(entity=OuterRef("pk"))
        for key, given in value.items():
            if not isinstance(given, str) or not given.strip() or len(given) > 100:
                return self.fail(f"{at}/{key}", "must be some text, up to 100 characters.")
            if key in allowed and given not in allowed[key]:
                return self.fail(f"{at}/{key}", f"must be {_either(tuple(allowed[key]))}.")
            if key == "software":
                rows = rows.alias(named=Lower("software")).filter(named=given.strip().lower())
            else:
                rows = rows.filter(**{key: given})
        return Q(Exists(rows))

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


#: What a condition on ``made`` may give: each must match the same making.
_MAKING_KEYS = ("software", "version", "kind", "by")


def having_motif(
    path: str, format: str, wanted: str, *, topology: str | None, strands: str | None
) -> Q:
    """Where a sequence at ``path`` has the motif (ADR 0175): as it's written; as its other
    strand reads it, where ``strands`` says it's a duplex; and across its origin, in the
    last bases and the first, where ``topology`` says it's a circle.

    A motif without ambiguity is found as text, with ``ILIKE``, and one with it as a
    regular expression (``~*``): a trigram index on the field serves both (ADR 0037). The
    origin is read in a window of each circle's ends, which no index holds."""
    reverse = (
        motifs.reverse_complement(format, wanted)
        if strands is not None and format in identity.NUCLEIC_FORMATS
        else None
    )
    if reverse == wanted:
        # A palindrome, as most restriction sites are: the other strand reads the same.
        reverse = None
    double = Q(**{strands: "double"}) if strands is not None else Q()

    def on(sequence: str | Concat, given: str) -> Q:
        if isinstance(sequence, str):
            if motifs.plain(format, given):
                return Q(**{f"{sequence}__containing": given})
            return Q(**{f"{sequence}__iregex": motifs.pattern(format, given)})
        return Q(IRegex(sequence, motifs.pattern(format, given)))

    def strands_of(sequence: str | Concat) -> Q:
        found = on(sequence, wanted)
        return found | (double & on(sequence, reverse)) if reverse is not None else found

    found = strands_of(path)
    k = len(wanted)
    if topology is not None and k > 1:
        bases = F(path)
        ends = Concat(Right(bases, k - 1), Left(bases, k - 1), output_field=TextField())
        found |= (
            Q(**{topology: "circular"})
            # A motif longer than the circle would read it round more than once.
            & Q(GreaterThanOrEqual(Length(bases), k))
            & strands_of(ends)
        )
    return found


def _is_set(col: Column) -> Q:
    if col.kind == "making":
        from iki_server.runs.models import EntityMaking

        return Q(Exists(EntityMaking.objects.filter(entity=OuterRef("pk"))))
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
    if now_of(value):
        return dt.datetime.now(dt.UTC).date()
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("must be a date, such as 2026-03-01.") from None


def _datetime(_: _Compiler, __: Column, value: Any) -> dt.datetime:  # noqa: ANN401 - JSON
    if now_of(value):
        return dt.datetime.now(dt.UTC)
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


#: Someone nobody is: an email address no person has finds nothing, as one who's in no
#: record does, so a search says nothing about who exists.
_NOBODY = uuid.UUID(int=0)


def _person(c: _Compiler, _: Column, value: Any) -> uuid.UUID:  # noqa: ANN401 - JSON
    """A person, by their ID, their email address, or ``{{ me }}``."""
    if isinstance(value, str) and value.replace(" ", "") == ME.replace(" ", ""):
        return cast("uuid.UUID", c.user.pk)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must name a person: their ID, their email address, or `{{ me }}`.")
    try:
        return uuid.UUID(value)
    except ValueError:
        pass
    if "@" not in value:
        raise ValueError("must name a person: their ID, their email address, or `{{ me }}`.")
    from iki_server.accounts.models import User

    found = User.objects.filter(email=value.strip()).values_list("pk", flat=True).first()
    return found or _NOBODY


def _ref(c: _Compiler, _: Column, value: Any) -> uuid.UUID:  # noqa: ANN401 - JSON
    if not isinstance(value, str):
        raise ValueError("must be an entity's ID or HRID.")
    try:
        lookup = Q(pk=uuid.UUID(value))
    except ValueError:
        lookup = Q(hrid=value)
    # A reference to a record of a registry entry holds the entry (ADR 0115).
    record = access.visible(c.user).filter(lookup).values_list("pk", "entry_id").first()
    if record is not None:
        own: uuid.UUID = record[0]
        entry: uuid.UUID | None = record[1]
        return entry or own
    found: uuid.UUID | None = (
        access.visible_entries(c.user).filter(lookup).values_list("pk", flat=True).first()
    )
    if found is None:
        raise ValueError("refers to no entity you can read.")
    return found


def _type(_: _Compiler, __: Column, value: Any) -> str:  # noqa: ANN401 - JSON
    if not isinstance(value, str) or value not in registry.types():
        raise ValueError("must be an entity type, such as iki/protein.Protein.")
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
    "user": _person,
    "person": _person,
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
    entry = None
    if part.entry:
        # From the type's entries' table, under which are its parents' (ADR 0165): an
        # extension's entry fields are beside it.
        entry = form.entry_model
        prefix = f"{naming.entry_related_name(part.plugin)}__" if part.extension else ""
    else:
        prefix = f"{naming.extension_related_name(part.plugin)}__" if part.extension else ""
    if is_list_table(f):
        return Column(name, f.type, prefix + name, f, items=part.list_model(name), entry=entry)
    if f.type in FOREIGN_KEYS:
        return Column(name, f.type, f"{prefix}{name}_id", f, entry=entry)
    if f.type == "state":
        # A state field (ADR 0125) is searched as an enum of its states.
        states = Field(name=name, type="enum", label=f.label, values=f.live_states)
        return Column(name, "enum", prefix + name, states, entry=entry)
    return Column(name, f.type, prefix + name, f, entry=entry)


def form_of(type_name: str | None) -> Shape | None:
    """The shape a search's conditions name fields of: None for any entity."""
    if type_name is None or type_name == "iki/core.Entity":
        return None
    return shape(type_name)


# --- Running ------------------------------------------------------------------------------------


def queryset(
    user: Any,  # noqa: ANN401 - User
    search: Search,
    templated: Callable[[Any], bool] | None = None,
) -> QuerySet[Entity]:
    """The entities the user can read that match. Raises :class:`SearchError`.

    ``templated`` says which conditions' values are filled in later, as an app's
    expressions are: their fields and operators are checked, not their values."""
    if search.type is not None and search.type not in registry.types():
        raise SearchError([("#/type", f"There’s no entity type {search.type}.")])
    unknown = [t for t in search.types if t not in registry.types()]
    if unknown:
        raise SearchError([("#/types", f"There’s no entity type {unknown[0]}.")])
    if search.types and search.type is not None:
        raise SearchError([("#/types", "Give a `type` or `types`, not both.")])
    form = form_of(search.type)
    # A type's table holds its subtypes' rows too.
    found = access.visible_of(user, form.model if form else Entity)
    if search.type is not None and search.exact_type:
        found = found.filter(type=search.type)
    any_of: set[str] = set()
    for name in search.types:
        any_of |= {name} if search.exact_type else subtypes(name)
    if search.types and "iki/core.Entity" not in search.types:
        found = found.filter(type__in=sorted(any_of))

    if search.project is not None:
        found = found.filter(links__project_id=search.project)
    else:
        found = found.filter(_in_projects_named(user))
    if search.archived is not None:
        found = found.filter(archived_at__isnull=not search.archived)
    if search.collection is not None:
        found = found.filter(_in_collection(user, search.collection))
    if search.run is not None:
        found = found.filter(_by_run(user, search.run))
    if search.q and search.q.strip():
        names = (
            ({search.type} if search.exact_type else subtypes(search.type))
            if search.type
            else any_of or set(registry.types())
        )
        found = found.filter(matching(search.q.strip(), names))
    errors: list[tuple[str, str]] = []
    if search.where is not None:
        within = (
            None
            if search.type is None or search.type == "iki/core.Entity"
            else ({search.type} if search.exact_type else subtypes(search.type))
        )
        compiler = _Compiler(user, form, templated, within)
        where = compiler.condition(search.where, "#/where")
        errors += compiler.errors
        found = found.filter(where)
    if search.sort is not None:
        errors += _sort_problems(form, search.sort, "#/sort")
    if search.then and search.sort is None:
        errors.append(("#/then", "goes on from a sort: give `sort` first."))
    elif search.then:
        if len(search.then) >= MAX_SORTS:
            errors.append(("#/then", f"A search sorts by at most {MAX_SORTS} fields."))
        seen = {search.sort}
        for index, (name, _) in enumerate(search.then):
            if name in seen:
                errors.append((f"#/then/{index}", f"`{name}` is sorted by already."))
            seen.add(name)
            errors += _sort_problems(form, name, f"#/then/{index}")
    if errors:
        raise SearchError(errors)
    return found.select_related("created_by")


def _sort_problems(form: Shape | None, name: str, pointer: str) -> list[tuple[str, str]]:
    col = sort_column(form, name)
    if col is None:
        return [(pointer, f"There’s no field `{name}` to sort by.")]
    if (col.kind not in SORTABLE and name not in _SIDES) or col.items is not None:
        return [(pointer, f"`{name}` can't be sorted by.")]
    return []


#: What requests also sort by (ADR 0132, ADR 0232): the project on either side, by its name,
#: which each side's people see of the other. Other records have none.
_SIDES = {
    "asked_by": Column("asked_by", "project", "owner_project_id"),
    "sent_to": _BUILT_IN["sent_to"],
}


def sort_column(form: Shape | None, name: str) -> Column | None:
    """The column a search sorts by, or None: a field, a column every entity has, or a
    request's side."""
    return _SIDES.get(name) or column(form, name)


def _sort_expression(col: Column) -> Any:  # noqa: ANN401 - a query expression
    """What a search sorts by for a column: its value, its registry entry's, or for a
    request's side, its project's name."""
    if col.name in _SIDES:
        from iki_server.entities.requests import request_types
        from iki_server.org.models import Project

        name = Subquery(Project.objects.filter(pk=OuterRef(col.path)).values("name")[:1])
        if col.name == "sent_to":
            return name
        return Case(
            When(type__in=request_types(), then=name),
            default=Value(None),
            output_field=TextField(),
        )
    value: Any = F(col.path)
    if col.entry is not None:
        # A registry entry's field: each record sorts by its entry's.
        value = Subquery(
            col.entry._default_manager.filter(pk=OuterRef("entry_id")).values(col.path)[:1]
        )
    if col.kind == "enum" and col.field is not None and col.field.values:
        # An enum, or a state field's states, in the order its type declares them, as a
        # queue's priority is routine then urgent (ADR 0232): a value it no longer has is
        # blank.
        return Case(
            *(When(Exact(value, Value(v)), then=Value(i)) for i, v in enumerate(col.field.values)),
            default=Value(None),
            output_field=IntegerField(),
        )
    return value


#: The most projects a search across them names one by one, for Postgres to plan by.
PROJECTS_NAMED_MAX = 500


def _in_projects_named(user: Any) -> Q:  # noqa: ANN401 - User
    """Linked into one of the projects the user can read, named by ID.

    It adds nothing to what :func:`access.visible_of` finds, but it lets Postgres plan by
    how many records those projects hold, which its statistics know by project and can't
    see through a subquery of memberships. Without it, someone whose one project holds a
    handful of records among millions had their page found by reading everyone's records
    newest first until theirs turned up: twelve seconds at two million (ADR 0176). With
    it, Postgres reads their projects' links and sorts the few it finds."""
    named = list(
        access.readable_projects(user).values_list("pk", flat=True)[: PROJECTS_NAMED_MAX + 1]
    )
    if len(named) > PROJECTS_NAMED_MAX:
        # So many projects hold most records anyway: reading newest first finds them soon.
        return Q()
    return Q(Exists(ProjectLink.objects.filter(entity=OuterRef("pk"), project_id__in=named)))


def title_text_fields(type_name: str) -> list[str]:
    """The text fields a type's title shows, which ``q`` searches."""
    entity_type = registry.types().get(type_name)
    template = registry.display_title(entity_type) if entity_type is not None else None
    if template is None:
        return []
    fields = shape(type_name).fields()
    words = {m.group("word") for m in titles.TOKEN.finditer(template) if m.group("word")}
    return sorted(w for w in words if w in fields and fields[w].type in ("string", "text"))


#: The start of an HRID, as people type one: its prefix, a hyphen, and some of its number.
_HRID_START = re.compile(r"[A-Za-z][A-Za-z0-9]{1,7}-[0-9]*")


def looks_like_hrid(q: str) -> bool:
    """Whether ``q`` is shaped like an HRID, or the start of one: ``prt-0001``, ``PRT-``."""
    return _HRID_START.fullmatch(q) is not None


def matching(q: str, type_names: set[str]) -> Q:
    """Entities whose HRID contains ``q``, or whose title's text fields do.

    One ``IN`` over a union, not an ``OR``: each part then uses its own trigram index.
    ``q`` shaped like the start of an HRID (``prt-0001``) finds the HRIDs that start
    with it, by the HRIDs' own index: their digits' trigrams are in nearly every HRID,
    so at 100M entities a trigram search for one took most of a second (ADR 0103).
    """
    hrid = Q(hrid__startswith=q.upper()) if looks_like_hrid(q) else Q(hrid__containing=q)
    parts: list[QuerySet[Any]] = [Entity.objects.filter(hrid).values("pk")]
    for name in sorted(type_names):
        entity_type = registry.types()[name]
        if name == "iki/core.Entity" or entity_type.abstract:
            continue
        # Titles name the type's own fields; an extension's are in other tables.
        form = shape(name)
        fields = [f for f in title_text_fields(name) if not form.part_of(f).extension]
        own = [f for f in fields if not form.part_of(f).entry]
        if own:
            text = Q()
            for f in own:
                text |= Q(**{f"{f}__containing": q})
            parts.append(form.model._default_manager.filter(text).values("pk"))
        held = [f for f in fields if form.part_of(f).entry]
        if held:
            # A registry entry's (ADR 0115): its records, by their entry.
            text = Q()
            for f in held:
                text |= Q(**{f"{f}__containing": q})
            entries = form.part_of(held[0]).model._default_manager.filter(text).values("pk")
            parts.append(Entity.objects.filter(entry_id__in=entries).values("pk"))
    return Q(pk__in=parts[0].union(*parts[1:]) if len(parts) > 1 else parts[0])


_SALT = "iki.entities.search"


def page(
    found: QuerySet[Entity], search: Search, cursor: str | None, limit: int
) -> tuple[list[Entity], str | None]:
    """One page of the search's results, and the cursor for the next (or None)."""
    form = form_of(search.type)
    if search.sort and search.then:
        return _page_by_sorts(found, search, form, cursor, limit)
    col = sort_column(form, search.sort) if search.sort else None
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
        got = list(ordered[: limit + 1])
        more = len(got) > limit
        got = got[:limit]
        last = got[-1] if got and more else None
        return got, _encode(key, None, last.pk) if last is not None else None
    assert col is not None  # noqa: S101 - not by ID
    expression: Any = _sort_expression(col)
    if col.field is None and search.type is not None and not search.exact_type:
        # Every entity's own columns (when it changed) are indexed after its type. The type's
        # table holds only it and its subtypes, but saying so lets that index serve.
        names = sorted(subtypes(search.type))
        found = found.filter(type=names[0]) if len(names) == 1 else found.filter(type__in=names)
    ordered = found.annotate(sort_value=expression)
    value, pk = (_restore(col, after[0]), after[1]) if after is not None else (None, None)
    in_blanks = after is not None and value is None
    rows: list[Entity] = []
    if not in_blanks:
        # Those with a value first, in an order its index reads: descending, that's with
        # blanks first, which there are none of here (blanks last, no index would serve),
        # and ties newest first, as an index on the value and the ID reads backwards.
        valued = ordered.filter(sort_value__isnull=False)
        if after is not None:
            near, past, tie = ("lte", "lt", "lt") if search.descending else ("gte", "gt", "gt")
            valued = valued.filter(
                Q(**{f"sort_value__{near}": value}),
                Q(**{f"sort_value__{past}": value}) | Q(**{f"pk__{tie}": pk}),
            )
        order = (
            expression.desc(nulls_first=True)
            if search.descending
            else expression.asc(nulls_last=True)
        )
        valued = valued.order_by(order, "-pk" if search.descending else "pk")
        if not _indexed(col):
            _refuse_a_long_sort(valued[: limit + 1], col, form)
        rows = list(valued[: limit + 1])
    if len(rows) <= limit:
        # Then those without one, oldest first.
        blanks = ordered.filter(sort_value__isnull=True).order_by("pk")
        if in_blanks:
            blanks = blanks.filter(pk__gt=pk)
        rows += list(blanks[: limit + 1 - len(rows)])
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows and more else None
    if last is None:
        return rows, None
    return rows, _encode(key, _plain(getattr(last, "sort_value", None)), last.pk)


def _page_by_sorts(
    found: QuerySet[Entity], search: Search, form: Shape | None, cursor: str | None, limit: int
) -> tuple[list[Entity], str | None]:
    """A page sorted by several fields: each in its direction, blanks last in each, then
    by ID. Keyset pages, as one sort's, past the row each ends with."""
    assert search.sort is not None  # noqa: S101 - only with sorts
    sorts = [(search.sort, search.descending), *search.then]
    cols = [sort_column(form, name) for name, _ in sorts]
    assert all(c is not None for c in cols)  # noqa: S101 - checked by the search
    key = (search.sort, search.descending, [[n, d] for n, d in search.then])
    names = [f"sort_{i}" for i in range(len(sorts))]
    ordered = found.annotate(
        **{n: _sort_expression(c) for n, c in zip(names, cols, strict=True) if c is not None}
    ).order_by(
        *(
            F(n).desc(nulls_last=True) if descending else F(n).asc(nulls_last=True)
            for n, (_, descending) in zip(names, sorts, strict=True)
        ),
        "pk",
    )
    if cursor:
        values, pk = _decode(cursor, key)
        if not isinstance(values, list) or len(values) != len(sorts):
            raise SearchError([("#/cursor", "This cursor is from a search sorted another way.")])
        restored = [_restore(c, v) for c, v in zip(cols, values, strict=True) if c is not None]
        ordered = ordered.filter(_past(names, [d for _, d in sorts], restored, pk))
    first = cols[0]
    assert first is not None  # noqa: S101 - checked by the search
    _refuse_a_long_sort(ordered[: limit + 1], first, form)
    rows = list(ordered[: limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    if not rows or not more:
        return rows, None
    last = rows[-1]
    return rows, _encode(key, [_plain(getattr(last, n)) for n in names], last.pk)


def _past(names: list[str], descending: list[bool], values: list[Any], pk: uuid.UUID) -> Q:
    """The rows after one with these sort values and ID, blanks last in each sort."""
    after = Q(pk__gt=pk)
    for name, down, value in reversed(list(zip(names, descending, values, strict=True))):
        if value is None:
            # Blank: only blanks are level with it, and none come after it.
            after = Q(**{f"{name}__isnull": True}) & after
        else:
            beyond = Q(**{f"{name}__{'lt' if down else 'gt'}": value}) | Q(
                **{f"{name}__isnull": True}
            )
            after = beyond | (Q(**{name: value}) & after)
    return after


def _indexed(col: Column) -> bool:
    if col.entry is not None:
        # Indexed in its entries' table, which can't order the records.
        return False
    if col.field is None:
        return col.name in _INDEXED_BUILT_IN
    # An enum sorts in its declared order, which its index on the values can't read.
    return col.field.indexed and col.kind != "enum"


def _refuse_a_long_sort(query: QuerySet[Entity], col: Column, form: Shape | None) -> None:
    """Raises :class:`TooSlow` at once, rather than after its time, if Postgres expects to
    sort more than :data:`UNINDEXED_SORT_MAX` rows to answer."""
    rows = _sorted_rows(json.loads(query.explain(format="json"))[0]["Plan"])
    if rows <= UNINDEXED_SORT_MAX:
        return
    what = (form.entity_type.label_plural.lower() or "entities") if form else "entities"
    label = col.field.label if col.field is not None and col.field.label else col.name
    raise TooSlow(
        f"Sorting about {_about(rows)} {what} by {label} would take too long: it isn't "
        "indexed. Narrow the search with conditions on indexed fields, or sort by an "
        "indexed field."
    )


def _sorted_rows(plan: Mapping[str, Any], workers: int = 1) -> float:
    """The most rows a sort in the plan takes, by Postgres's estimate: a parallel plan's
    are each worker's, and the leader's."""
    if plan["Node Type"] in ("Gather", "Gather Merge"):
        workers = plan.get("Workers Planned", 0) + 1
    own = plan["Plan Rows"] * workers if plan["Node Type"] == "Sort" else 0.0
    return max([own, *(_sorted_rows(p, workers) for p in plan.get("Plans", []))])


def _about(rows: float) -> str:
    """A count, roughly: 120 million, 3.4 million, 20,000."""
    if rows >= 1_000_000:
        millions = rows / 1_000_000
        return f"{millions:.0f} million" if millions >= 10 else f"{millions:.1f} million"
    return f"{round(rows, -3):,.0f}"


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


def _encode(key: tuple[Any, ...], value: Any, pk: uuid.UUID) -> str:  # noqa: ANN401
    return signing.Signer(salt=_SALT).sign_object([*key, value, str(pk)], compress=True)


def _decode(cursor: str, key: tuple[Any, ...]) -> tuple[Any, uuid.UUID]:
    try:
        *sorted_by, value, pk = signing.Signer(salt=_SALT).unsign_object(cursor)
        found = uuid.UUID(pk)
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise SearchError([("#/cursor", "Not a cursor from this API.")]) from error
    if sorted_by != list(key):
        raise SearchError([("#/cursor", "This cursor is from a search sorted another way.")])
    return value, found


#: What :func:`time_limit` sets, for the rest of its block.
_LIMITED = ("statement_timeout", "join_collapse_limit", "from_collapse_limit", "enable_mergejoin")


@contextmanager
def time_limit(ms: int = TIME_LIMIT_MS, *, pages: bool = True) -> Iterator[None]:
    """Runs the block's queries in a transaction that cancels any taking longer than ``ms``.
    Raises :class:`TooSlow` if one does. Inside a transaction already (a trigger's firing,
    say), what it set is put back after the block, so what the transaction does next isn't
    limited.

    ``pages`` plans the block's joins for pages deep into large tables, as below. Planning
    so takes longer: searching everything (ADR 0176) reads only the first few of each kind,
    and turns it off."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT " + ", ".join(f"current_setting('{name}')" for name in _LIMITED)
                )
                before = cursor.fetchone()
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [f"{ms}ms"])
            if pages:
                with connection.cursor() as cursor:
                    # A page is at most a hundred rows, of a join of a type's tables (its own,
                    # its parents', Entity's), its projects' and its caller's (ADR 0103):
                    # - Postgres reorders at most 8 by default; past that it joins them as
                    #   written, matches last, reading every row before narrowing to a few.
                    # - A merge join reads each table's key from its start: a page deep into
                    #   100M rows would read 100M of each. Looking each row up in the others,
                    #   by key, reads a page's worth wherever the page is.
                    cursor.execute(
                        "SELECT set_config('join_collapse_limit', '12', true), "
                        "set_config('from_collapse_limit', '12', true), "
                        "set_config('enable_mergejoin', 'off', true)"
                    )
            yield
            if before is not None:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT "
                        + ", ".join(f"set_config('{name}', %s, true)" for name in _LIMITED),
                        list(before),
                    )
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) == "57014":  # query_canceled
            raise TooSlow from error
        raise
