"""Queries across the graph (ADR 0123): what an entity depends on, and what depends on it.

``upstream(X)`` is what X was derived from, and what that was, as far as asked: what its
references name, the inputs of the runs that made it, and so on. ``downstream(X)`` is
everything derived from X: what refers to it, what runs made from it, and on.

Each walk is a recursive CTE over ``iki_graph_edge``, as the caller can read it:

- **An edge is the data of the entity that states it**: a reference is its holder's, a
  run's edge is the made entity's provenance, a record's is the record's. So a walk
  downstream reaches only entities the caller can read: what refers to X from a record
  they can't read is that record's data, and never counted. A walk upstream from what
  they can read may reach an entity they can't, as its references show; that's a dead
  end, counted but never named, and never walked through (GitHub's dependency graph shows
  only the dependents you can see, ADR 0066 shows an input you can't read as one).
- A record and its registry entry are one thing (ADR 0115): ``record`` edges go both
  ways, but only to records the caller can read, so how many projects hold an entry
  never shows.
- Each node is walked once per depth it's reached at, so diamonds and cycles cost no more
  than the nodes times the depth; depth is bounded, and each query runs within search's
  time limit (:func:`iki_server.entities.search.time_limit`), raising ``TooSlow``.

Every result comes with its path back to X: the first way the walk found it, one edge at
a time, so people can see why it's there.
"""

# The walks are SQL built here: every value in them is a literal checked to be a UUID, a
# name or a number (_uuid, _uuids, _texts, int), so nothing a caller sends is in them.
# ruff: noqa: S608, S611

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.core import signing
from django.db import connection
from django.db.models import Q
from django.db.models.expressions import RawSQL

from iki_server.entities import access, registry
from iki_server.entities.values import shape, subtypes
from iki_server.graph.models import EdgeKind

__all__ = [
    "DEFAULT_DEPTH",
    "DIRECTIONS",
    "KINDS",
    "MAX_DEPTH",
    "Graph",
    "Query",
    "Reach",
    "Reader",
    "Step",
    "condition",
    "graph",
    "reach",
    "reader",
    "related",
]

DIRECTIONS = ("upstream", "downstream")
#: The kinds of edge a walk may be narrowed to. A record's edge to its entry is always
#: followed: they're one thing.
KINDS: tuple[str, ...] = tuple(k for k in EdgeKind.values if k != EdgeKind.RECORD)
#: How far a walk goes, at most and when it isn't said.
MAX_DEPTH = 30
DEFAULT_DEPTH = 10
#: The type whose entities show others (ADR 0110): readable only with all they show.
VIEW = "iki/core.View"

_NAME = re.compile(r"^[A-Za-z0-9_/.:#-]{1,200}$")


def _uuids(values: Iterable[Any]) -> str:
    """A uuid[] literal: each value is checked to be a UUID, so nothing else gets in."""
    items = ",".join(f"'{uuid.UUID(str(v))}'" for v in values)
    return f"ARRAY[{items}]::uuid[]"


def _uuid(value: Any) -> str:  # noqa: ANN401 - a UUID or its text
    return f"'{uuid.UUID(str(value))}'::uuid"


def _texts(values: Iterable[str]) -> str:
    """A text[] literal of names (types, kinds), each checked against what names may be."""
    items = []
    for v in values:
        if not _NAME.fullmatch(v):
            raise ValueError(f"Not a name: {v!r}")
        items.append(f"'{v}'")
    return f"ARRAY[{','.join(items)}]::text[]"


@dataclass(frozen=True)
class Reader:
    """What someone can read, as the walk's SQL checks it: the projects they read, and the
    table of views' targets, since a view is readable only with everything it shows."""

    projects: tuple[uuid.UUID, ...]
    views: tuple[str, ...] = ()
    view_items: str = ""

    def readable(self, expr: str) -> str:
        """SQL: the entity with ID ``expr`` is readable: a record linked into one of their
        projects (a view, only with everything it shows), or an entry with such a record;
        never one a purge tombstoned (ADR 0092)."""
        p = _uuids(self.projects)
        view = ""
        if self.view_items:
            view = (
                f" AND (x.type <> ALL({_texts(self.views)}) OR NOT EXISTS (SELECT 1 FROM "
                f'"{self.view_items}" i WHERE i.entity_id = x.id AND NOT EXISTS (SELECT 1 FROM '
                "iki_entity_project l2 WHERE l2.entity_id = i.item_id AND "
                f"l2.project_id = ANY({p})) AND NOT EXISTS (SELECT 1 FROM iki_entity r2 JOIN "
                "iki_entity_project l3 ON l3.entity_id = r2.id WHERE r2.entry_id = i.item_id AND "
                f"l3.project_id = ANY({p}))))"
            )
        return (
            f"EXISTS (SELECT 1 FROM iki_entity x WHERE x.id = {expr} AND x.purged_at IS NULL "
            "AND ("
            "(x.registry_id IS NULL AND EXISTS (SELECT 1 FROM iki_entity_project l WHERE "
            f"l.entity_id = x.id AND l.project_id = ANY({p})){view}) OR (x.registry_id IS NOT "
            "NULL AND EXISTS (SELECT 1 FROM iki_entity r JOIN iki_entity_project l ON "
            "l.entity_id = r.id WHERE r.entry_id = x.id AND r.purged_at IS NULL AND "
            f"l.project_id = ANY({p})))))"
        )


def reader(user: Any) -> Reader:  # noqa: ANN401 - User or AnonymousUser
    projects = tuple(access.readable_projects(user).values_list("pk", flat=True))
    if VIEW not in registry.types():
        return Reader(projects)
    items = shape(VIEW).part_of("targets").list_model("targets")._meta.db_table
    return Reader(projects, tuple(sorted(subtypes(VIEW))), items)


@dataclass(frozen=True)
class Query:
    """A walk from ``root``: ``direction``, up to ``depth`` edges, along edges of ``kinds``.
    ``types`` and ``archived`` narrow what it finds, not where it goes."""

    root: uuid.UUID
    direction: str = "downstream"
    depth: int = DEFAULT_DEPTH
    kinds: tuple[str, ...] = KINDS
    #: Results of these types, with their subtypes; empty for any.
    types: tuple[str, ...] = ()
    archived: bool | None = None

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"No direction {self.direction!r}.")
        if not 1 <= self.depth <= MAX_DEPTH:
            raise ValueError(f"A depth is from 1 to {MAX_DEPTH}.")
        if not self.kinds or set(self.kinds) - set(KINDS):
            raise ValueError("Unknown kinds of edge.")

    @property
    def down(self) -> bool:
        return self.direction == "downstream"

    @property
    def followed(self) -> str:
        """SQL: the edge ``e`` is one this walk follows: of its kinds, or a record's."""
        return f"(e.kind = ANY({_texts(self.kinds)}) OR e.kind = 'record')"


#: An edge's cost: a record and its entry are one thing, so going between them is free.
_COST = "(CASE WHEN e.kind = 'record' THEN 0 ELSE 1 END)"


def walk(q: Query, r: Reader) -> str:
    """The recursive CTE ``walk(node, depth, rec, open)``: each node reached, at each depth
    it's reached at. ``rec`` counts the record edges just taken (a record to its entry, and
    on to another record of it, at most), so the free edges end; ``open`` is whether the
    caller can read it, so the walk goes on from it."""
    d = int(q.depth)
    # A record edge costs nothing, so it may be taken at the last depth too.
    allowed = (
        f"w.open AND {q.followed} AND (CASE WHEN e.kind = 'record' THEN w.rec < 2 "
        f"ELSE w.depth < {d} END)"
    )
    rec = "(CASE WHEN e.kind = 'record' THEN w.rec + 1 ELSE 0 END)"
    if q.down:
        step = (
            f"SELECT e.node_id, w.depth + {_COST}, {rec}, true FROM walk w JOIN iki_graph_edge "
            f"e ON e.on_id = w.node WHERE {allowed} AND {r.readable('e.node_id')}"
        )
    else:
        far = r.readable("e.on_id")
        step = (
            f"SELECT e.on_id, w.depth + {_COST}, {rec}, {far} FROM walk w JOIN iki_graph_edge "
            f"e ON e.node_id = w.node WHERE {allowed} AND (e.kind <> 'record' OR {far})"
        )
    return (
        "WITH RECURSIVE walk(node, depth, rec, open) AS "
        f"(SELECT {_uuid(q.root)}, 0, 0, true UNION {step})"
    )


def _found(q: Query, alias: str = "e") -> str:
    """SQL narrowing reached entities to results: records (entries are walked through, as
    their records are what people hold), of the types and archived state asked for, and
    not the root itself, nor another record of its entry: they're the same thing."""
    out = (
        f"{alias}.registry_id IS NULL AND coalesce({alias}.entry_id, {alias}.id) <> "
        f"(SELECT coalesce(x.entry_id, x.id) FROM iki_entity x WHERE x.id = {_uuid(q.root)})"
    )
    if q.types:
        names = sorted({t for name in q.types for t in subtypes(name)})
        out += f" AND {alias}.type = ANY({_texts(names)})"
    if q.archived is not None:
        out += f" AND {alias}.archived_at IS {'NOT ' if q.archived else ''}NULL"
    return out


def condition(user: Any, direction: str, value: Any, *, is_set: bool | None = None) -> Q:  # noqa: ANN401
    """A search condition: entities ``direction`` of ``value`` (a query's root), or with
    ``is_set``, those with (or without) anything that way: downstream, that they can read;
    upstream, any, as what an entity depends on is its own data."""
    if is_set is not None:
        if direction == "downstream":
            ids = "SELECT e.on_id FROM iki_graph_edge e WHERE e.kind <> 'record' AND " + reader(
                user
            ).readable("e.node_id")
        else:
            ids = "SELECT e.node_id FROM iki_graph_edge e WHERE e.kind <> 'record'"
        # A record's entry's edges are the record's too.
        found = Q(pk__in=RawSQL(ids, ())) | Q(entry_id__in=RawSQL(ids, ()))
        return found if is_set else ~found
    assert isinstance(value, Query)  # noqa: S101 - the search compiles it
    return Q(
        pk__in=RawSQL(
            f"{walk(value, reader(user))} SELECT w.node FROM walk w JOIN iki_entity e ON "
            f"e.id = w.node WHERE w.open AND {_found(value)}",
            (),
        )
    )


# --- Reaching ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One edge of a path: from ``prev`` (nearer the root) to ``node``. ``node`` depends on
    ``prev`` downstream, and ``prev`` on ``node`` upstream."""

    node: uuid.UUID
    prev: uuid.UUID
    kind: str
    field: str
    run: uuid.UUID | None
    file: uuid.UUID | None
    #: A run's input's revision it read.
    revision: int | None = None


@dataclass
class Reach:
    """A walk's reach, in the temporary table ``iki_reach`` (node, depth, rec, open): each
    node once, as near as it was reached, for the transaction it was made in."""

    query: Query
    reader: Reader
    #: Each node's step back towards the root, once :meth:`paths` has found it.
    steps: dict[uuid.UUID, Step] = field(default_factory=dict)

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def total(self) -> int:
        return int(
            self._rows(
                "SELECT count(*) FROM iki_reach r JOIN iki_entity e ON e.id = r.node "
                f"WHERE r.open AND {_found(self.query)}"
            )[0][0]
        )

    def page(
        self, after: tuple[int, str, uuid.UUID] | None, limit: int
    ) -> list[tuple[uuid.UUID, int, str]]:
        """Results nearest first, then by HRID: (ID, depth, HRID), ``limit`` of them."""
        keyset = ""
        params: list[Any] = []
        if after is not None:
            keyset = " AND (r.depth, e.hrid, r.node) > (%s, %s, %s)"
            params = [after[0], after[1], after[2]]
        params.append(limit)
        return [
            (row[0], int(row[1]), row[2])
            for row in self._rows(
                "SELECT r.node, r.depth, e.hrid FROM iki_reach r JOIN iki_entity e ON "
                f"e.id = r.node WHERE r.open AND {_found(self.query)}{keyset} "
                "ORDER BY r.depth, e.hrid, r.node LIMIT %s",
                params,
            )
        ]

    def hidden(self) -> int:
        """Upstream: how many edges from what the caller can read lead to what they can't.
        Each is a reference or an input they can see is there (ADR 0066). Downstream: none,
        since what depends on something says so only to those who read it."""
        q = self.query
        if q.down:
            return 0
        return int(
            self._rows(
                "SELECT count(*) FROM iki_reach r JOIN iki_graph_edge e ON e.node_id = r.node "
                "JOIN iki_reach h ON h.node = e.on_id AND NOT h.open WHERE r.open AND "
                f"r.depth < {int(q.depth)} AND {q.followed} AND e.kind <> 'record'"
            )[0][0]
        )

    def beyond(self) -> bool:
        """Whether the walk would find more past its depth."""
        q, r = self.query, self.reader
        if q.down:
            edge = (
                f"e.on_id = f.node AND {r.readable('e.node_id')} AND NOT EXISTS "
                "(SELECT 1 FROM iki_reach o WHERE o.node = e.node_id)"
            )
        else:
            edge = (
                "e.node_id = f.node AND NOT EXISTS (SELECT 1 FROM iki_reach o WHERE "
                "o.node = e.on_id)"
            )
        return bool(
            self._rows(
                "SELECT EXISTS (SELECT 1 FROM iki_reach f JOIN iki_graph_edge e ON "
                f"{edge} AND e.kind = ANY({_texts(q.kinds)}) WHERE f.open AND "
                f"f.depth = {int(q.depth)})"
            )[0][0]
        )

    def nodes(self, limit: int) -> list[tuple[uuid.UUID, int, bool]]:
        """What the caller can read in it, root first, then nearest first: (ID, depth,
        whether it's a registry entry), up to the ``limit``-th that isn't. An entry is drawn
        as its record (ADR 0115), so it doesn't count against what can be drawn."""
        return [
            (row[0], int(row[1]), bool(row[2]))
            for row in self._rows(
                "SELECT s.node, s.depth, s.entry FROM (SELECT r.node, r.depth, r.rec, "
                "x.registry_id IS NOT NULL AS entry, count(*) FILTER (WHERE x.registry_id IS "
                "NULL) OVER (ORDER BY r.depth, r.rec, r.node) AS n FROM iki_reach r JOIN "
                "iki_entity x ON x.id = r.node WHERE r.open) s WHERE s.n <= %s "
                "ORDER BY s.depth, s.rec, s.node",
                [limit],
            )
        ]

    def paths(self, nodes: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[Step]]:
        """Each node's path from the root: its first step back, that one's, and so on.

        Each step back is to a node reached earlier, nearer or by fewer record edges at the
        same depth, so a path never comes back on itself."""
        q = self.query
        wanted = {n for n in nodes if n != q.root}
        near, far = ("e.node_id", "e.on_id") if q.down else ("e.on_id", "e.node_id")
        todo = wanted - set(self.steps)
        while todo:
            rows = self._rows(
                f"SELECT DISTINCT ON (r.node) r.node, {far}, e.kind, e.field, e.run_id, "
                f"e.file_id, e.revision FROM iki_reach r JOIN iki_graph_edge e ON {near} = "
                f"r.node JOIN iki_reach p ON p.node = {far} AND p.open AND (p.depth, p.rec) < "
                f"(r.depth, r.rec) AND p.depth + {_COST} <= r.depth WHERE r.node = ANY(%s) "
                f"AND {q.followed} ORDER BY r.node, p.depth DESC, p.rec, e.kind = 'record', e.id",
                [list(todo)],
            )
            for node, prev, kind, name, run, file, revision in rows:
                self.steps[node] = Step(node, prev, kind, name, run, file, revision)
            todo = {s.prev for s in self.steps.values() if s.prev != q.root} - set(self.steps)
            if not rows:
                break
        out = {}
        for node in wanted:
            path: list[Step] = []
            at = node
            while at != q.root and at in self.steps and len(path) <= 3 * MAX_DEPTH:
                path.append(self.steps[at])
                at = self.steps[at].prev
            out[node] = list(reversed(path))
        return out


def reach(user: Any, q: Query) -> Reach:  # noqa: ANN401 - User
    """Walks the graph into ``iki_reach``. Call it inside search's time limit, whose
    transaction the table lasts for."""
    r = reader(user)
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS pg_temp.iki_reach")
        cursor.execute(
            f"CREATE TEMPORARY TABLE iki_reach ON COMMIT DROP AS {walk(q, r)} "
            "SELECT DISTINCT ON (node) node, depth, rec, open FROM walk "
            "ORDER BY node, depth, rec"
        )
        cursor.execute("CREATE UNIQUE INDEX ON iki_reach (node)")
        cursor.execute("ANALYZE iki_reach")
    return Reach(q, r)


def related(
    user: Any,  # noqa: ANN401 - User
    project: uuid.UUID,
    q: Query,
    at_most: int,
) -> list[uuid.UUID]:
    """What a trigger gives an input (ADR 0123): the records of ``q``'s types in
    ``project``, not archived, that ``q`` reaches walking only ``project``'s records, as
    ``user`` reads them; at most ``at_most`` + 1, so the caller can tell there are more.
    Raises ``TooSlow`` past search's time limit."""
    from iki_server.entities import search

    r = reader(user)
    if project not in r.projects:
        return []
    mine = Reader((project,), r.views, r.view_items)
    wanted = Query(q.root, q.direction, q.depth, q.kinds, q.types, archived=False)
    with search.time_limit(), connection.cursor() as cursor:
        cursor.execute(
            f"{walk(wanted, mine)} SELECT DISTINCT ON (w.node) w.node FROM walk w JOIN "
            f"iki_entity e ON e.id = w.node WHERE w.open AND {_found(wanted)} "
            "ORDER BY w.node LIMIT %s",
            [at_most + 1],
        )
        return [row[0] for row in cursor.fetchall()]


# --- Cursors -------------------------------------------------------------------------------------

_SALT = "iki.graph.page"


def encode(q: Query, last: tuple[uuid.UUID, int, str]) -> str:
    return signing.Signer(salt=_SALT).sign_object(
        [str(q.root), q.direction, last[1], last[2], str(last[0])], compress=True
    )


def decode(q: Query, cursor: str) -> tuple[int, str, uuid.UUID]:
    """Raises ValueError for a cursor that isn't this walk's."""
    try:
        root, direction, depth, hrid, node = signing.Signer(salt=_SALT).unsign_object(cursor)
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise ValueError("Not a cursor from this API.") from error
    if (root, direction) != (str(q.root), q.direction):
        raise ValueError("This cursor is from another walk.")
    return int(depth), str(hrid), uuid.UUID(node)


# --- Both ways, for a picture -------------------------------------------------------------------


@dataclass
class Graph:
    root: uuid.UUID
    #: What the caller can read in it, and how far from the root (negative: upstream).
    nodes: dict[uuid.UUID, int]
    #: (node, on, kind, field, run, file, revision), among ``nodes``: ``node`` depends on
    #: ``on``.
    edges: list[tuple[Any, ...]]
    hidden: int
    earlier: bool
    later: bool
    capped: bool


def graph(
    user: Any,  # noqa: ANN401 - User
    root: uuid.UUID,
    up: int,
    down: int,
    kinds: tuple[str, ...] = KINDS,
    *,
    max_nodes: int = 200,
    max_edges: int = 500,
) -> Graph:
    """``up`` steps upstream and ``down`` downstream of ``root``, with the edges between
    what's in it: at most ``max_nodes`` entities besides registry entries (each drawn as its
    record, ADR 0115) and ``max_edges`` edges. Inside search's time limit."""
    nodes: dict[uuid.UUID, int] = {root: 0}
    entries: set[uuid.UUID] = set()
    hidden = 0
    more = {"upstream": False, "downstream": False}
    capped = False
    for direction, depth in (("upstream", up), ("downstream", down)):
        if depth == 0:
            more[direction] = _any_way(user, root, direction, kinds)
            continue
        found = reach(user, Query(root, direction, depth, kinds))
        if direction == "upstream":
            hidden = found.hidden()
        more[direction] = found.beyond()
        sign = -1 if direction == "upstream" else 1
        # Registry entries come with their records and don't count (``Reach.nodes``).
        room = max_nodes - sum(1 for n in nodes if n not in entries)
        got = found.nodes(room + 2)  # the root, room more and one to tell there are more
        records = 0
        for node, at, entry in got:
            if node == root:
                continue
            records += not entry
            if records > room:
                capped = True
                break
            nodes.setdefault(node, sign * at)
            if entry:
                entries.add(node)
    ids = list(nodes)
    rows: list[Any] = []
    if len(ids) > 1:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT e.node_id, e.on_id, e.kind, e.field, e.run_id, e.file_id, e.revision FROM "
                "iki_graph_edge e WHERE e.node_id = ANY(%s) AND e.on_id = ANY(%s) AND "
                f"(e.kind = ANY({_texts(kinds)}) OR e.kind = 'record') ORDER BY e.id LIMIT %s",
                [ids, ids, max_edges + 1],
            )
            rows = list(cursor.fetchall())
    capped = capped or len(rows) > max_edges
    return Graph(
        root=root,
        nodes=nodes,
        edges=[tuple(row) for row in rows[:max_edges]],
        hidden=hidden,
        earlier=more["upstream"],
        later=more["downstream"],
        capped=capped,
    )


def _any_way(user: Any, root: uuid.UUID, direction: str, kinds: tuple[str, ...]) -> bool:  # noqa: ANN401
    """Whether anything readable is one step ``direction`` of ``root``."""
    return reach(user, Query(root, direction, 1, kinds)).total() > 0


def kinds_of(given: Iterable[str] | None) -> tuple[str, ...]:
    """The kinds asked for, in the order :data:`KINDS` has them; all of them for none."""
    wanted = set(given or ())
    return tuple(k for k in KINDS if k in wanted) if wanted else KINDS


def labels() -> Mapping[str, str]:
    return dict(EdgeKind.choices)
