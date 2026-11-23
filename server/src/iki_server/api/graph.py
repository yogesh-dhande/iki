"""Across the graph (ADR 0123): what an entity was derived from, and what was derived from it.

- ``GET /entities/{ref}/upstream``: what it depends on, through its references, the runs
  that made it, the files they read and its registry entry, and on, up to ``depth`` edges.
- ``GET /entities/{ref}/downstream``: what depends on it, the same way: for impact
  analysis, recalls and consent withdrawal.
- Each comes as CSV too (``…/upstream.csv``, ``…/downstream.csv``), everything at once.
- ``GET /entities/{ref}/graph``: a few steps each way, with the edges between, to draw.

Each result has its ``path``: how the walk reached it from the entity, an edge at a time.
Only what you can read is in a result or a path. Upstream, a reference or an input you can
see leading to something you can't is counted in ``hidden``, never named; downstream,
what you can't read isn't there at all, as nothing of it is yours to see.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema

from iki_server.accounts.models import User
from iki_server.api import csvs
from iki_server.api.common import caller
from iki_server.api.entities import RefOut, RunRefOut, _find, _lookup
from iki_server.api.pagination import DEFAULT_LIMIT, MAX_LIMIT
from iki_server.api.problems import Problem
from iki_server.api.schemas import ProjectRef
from iki_server.entities import access, registry, search
from iki_server.entities.models import Entity
from iki_server.graph import queries
from iki_server.org import permissions
from iki_server.runs.services import visible_runs

router = Router(tags=["graph"])

#: What a walk may be narrowed to: a record's edge to its entry is always followed.
FollowedName = Literal["reference", "run", "file", "aliquot", "transfer", "used"]
EdgeKindName = Literal[FollowedName, "record"]
Direction = Literal["upstream", "downstream"]

#: The most rows a CSV has: past this, narrow it.
MAX_EXPORT = 100_000
#: How big a graph to draw gets before it stops and says it's cut short.
MAX_GRAPH_ENTITIES = 200
MAX_GRAPH_EDGES = 500
#: How many steps each way a graph to draw goes, at most.
MAX_GRAPH_STEPS = 10

_EDGES = (
    "Only these kinds of edge: `reference` (a ref field names it), `run` (a run made or "
    "updated it from it), `file` (a run made it from a file it held, or that a run on it "
    "wrote), `aliquot` (split from it), `transfer` (some of it was moved into it), or "
    "`used` (a draw from it was used in the other record). All, if none are given. "
    "A project's record of a registry entry and the entry "
    "are one thing (ADR 0115), so the walk always goes between them, at no depth."
)


class WalkParams(Schema):
    project: UUID | None = Field(
        None, description="Whose record, when an entry's ID or HRID names several (ADR 0115)."
    )
    depth: int = Field(
        queries.DEFAULT_DEPTH,
        ge=1,
        le=queries.MAX_DEPTH,
        description="How many edges away, at most.",
    )
    edges: list[FollowedName] = Field(default_factory=list, description=_EDGES)
    type: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Only results of these types, with their subtypes. The walk still goes "
        "through every type.",
    )
    archived: bool | None = Field(None, description="Only archived results, or only others.")


class WalkPageParams(WalkParams):
    cursor: str | None = Field(None, description="From the previous page's next_cursor.")
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Results per page.")


class GraphEntityOut(RefOut):
    id: UUID
    entry: bool = Field(
        False,
        description="It's a registry entry (ADR 0115): the identity its records are of.",
    )
    project: ProjectRef | None = Field(
        None, description="The project it belongs to, if you see it."
    )


class WalkFileOut(Schema):
    id: UUID
    name: str


class WalkStepOut(Schema):
    kind: EdgeKindName = Field(
        description="How the entities are linked: `reference`, `run`, `file`, `aliquot`, "
        "`transfer`, or `used` for a material draw. A `record` step goes on to another "
        "project's record of the same registry entry (ADR 0115)."
    )
    field: str = Field(
        description="A reference's field; a run's input (`antibody.components.part` for what "
        "it included); a file param; empty for a record."
    )
    run: RunRefOut | None = Field(description="The run, if you can read its project.")
    hidden_run: bool = Field(
        description="A run made it, in a project you can't read: it isn't named."
    )
    file: WalkFileOut | None = Field(description="For a file edge, the file, if you can read it.")
    revision: int | None = Field(
        description="For a run's input: its revision the run read. Empty for a reference, "
        "which names what's there now."
    )
    entity: GraphEntityOut = Field(
        description="Where this step reaches. Downstream, it depends on the step before (or "
        "the root); upstream, the step before depends on it."
    )


class WalkItemOut(Schema):
    entity: GraphEntityOut
    depth: int = Field(description="How many edges from the root its path takes.")
    path: list[WalkStepOut] = Field(description="From the root to it, the first way it was found.")


class WalkOut(Schema):
    root: GraphEntityOut
    direction: Direction
    depth: int
    items: list[WalkItemOut]
    next_cursor: str | None
    total: int = Field(description="Results in every page.")
    hidden: int = Field(
        description="Upstream: references and inputs you can see that lead to entities you "
        "can't read, so the walk stops there. Ask someone who can read them. Always 0 "
        "downstream: what you can't read never says it depends on anything."
    )
    beyond: bool = Field(description=f"There's more past `depth` (at most {queries.MAX_DEPTH}).")


def _walk(me: User, ref: str, params: WalkParams, direction: str) -> tuple[Entity, queries.Query]:
    root = _find(me, ref, params.project)
    unknown = [t for t in params.type if t not in registry.types()]
    if unknown:
        raise Problem(
            422,
            "Some fields are invalid.",
            code="validation_failed",
            errors=[
                {"parameter": "type", "detail": f"There’s no entity type {t}."} for t in unknown
            ],
        )
    return root, queries.Query(
        root.pk,
        direction,
        params.depth,
        queries.kinds_of(params.edges),
        tuple(params.type),
        params.archived,
    )


@contextmanager
def _limited() -> Iterator[None]:
    try:
        with search.time_limit():
            yield
    except search.TooSlow as error:
        raise Problem(
            422,
            "This took too long: the graph is too wide this far. Give a smaller `depth`, fewer "
            "kinds of `edges`, or start nearer what you're looking for.",
            code="search_too_slow",
        ) from error


class _Out:
    """Entities, runs and files as a caller sees them, looked up a batch at a time."""

    def __init__(self, me: User) -> None:
        self.me = me
        self.entities: dict[Any, dict[str, Any]] = {}
        self.runs: dict[Any, dict[str, Any] | None] = {}
        self.files: dict[Any, dict[str, Any] | None] = {}

    def load(self, ids: set[Any], runs: set[Any], files: set[Any]) -> None:
        ids -= set(self.entities)
        if ids:
            refs, _ = _lookup(self.me, {"ref": ids, "file": set()})
            rows = list(
                Entity.objects.filter(pk__in=refs).values_list(
                    "pk", "registry_id", "owner_project_id", "archived_at"
                )
            )
            # Entries have no owner project: their registry holds them.
            owners = {owner for _, _, owner, _ in rows} - {None}
            projects = {
                p.pk: p for p in permissions.visible_projects(self.me).filter(pk__in=owners)
            }
            for pk, in_registry, owner, archived in rows:
                project = projects.get(owner)
                self.entities[pk] = {
                    "id": pk,
                    **refs[pk],
                    "archived": archived is not None or refs[pk]["archived"],
                    "entry": in_registry is not None,
                    "project": {"id": project.pk, "name": project.name, "slug": project.slug}
                    if project is not None
                    else None,
                }
        runs -= set(self.runs)
        if runs:
            named = {
                r.pk: {"id": r.pk, "hrid": r.hrid, "action": r.action}
                for r in visible_runs(self.me).filter(pk__in=runs)
            }
            self.runs.update({pk: named.get(pk) for pk in runs})
        files -= set(self.files)
        if files:
            names = dict(
                access.readable_files(self.me).filter(pk__in=files).values_list("pk", "name")
            )
            self.files.update(
                {pk: {"id": pk, "name": names[pk]} if pk in names else None for pk in files}
            )

    def step(self, s: queries.Step) -> dict[str, Any]:
        run = self.runs.get(s.run) if s.run else None
        return {
            "kind": s.kind,
            "field": s.field,
            "run": run,
            "hidden_run": s.run is not None and run is None,
            "file": self.files.get(s.file) if s.file else None,
            "revision": s.revision,
            "entity": self.entities[s.node],
        }


def _folded(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A path as people follow it: a record and its registry entry are one thing (ADR
    0115), so the step between them goes. A reference to an entry reaches the record it's
    of; a step on from a record's entry starts at the record; a step from the entry to
    another project's record of it says that, as a ``record`` step."""
    out: list[dict[str, Any]] = []
    for step in steps:
        if out and out[-1]["entity"]["entry"] and step["kind"] == "record":
            out[-1] = {**out[-1], "entity": step["entity"]}
        else:
            out.append(step)
    return [s for s in out if not (s["kind"] == "record" and s["entity"]["entry"])]


def _items(
    me: User, found: queries.Reach, rows: list[tuple[UUID, int, str]], out: _Out
) -> list[dict[str, Any]]:
    paths = found.paths([node for node, _, _ in rows])
    steps = [s for path in paths.values() for s in path]
    out.load(
        {node for node, _, _ in rows} | {s.node for s in steps},
        {s.run for s in steps if s.run},
        {s.file for s in steps if s.file},
    )
    return [
        {
            "entity": out.entities[node],
            "depth": depth,
            "path": _folded([out.step(s) for s in paths.get(node, [])]),
        }
        for node, depth, _ in rows
        if node in out.entities and all(s.node in out.entities for s in paths.get(node, []))
    ]


def _page(request: HttpRequest, ref: str, params: WalkPageParams, direction: str) -> dict[str, Any]:
    me = caller(request)
    root, q = _walk(me, ref, params, direction)
    try:
        after = queries.decode(q, params.cursor) if params.cursor else None
    except ValueError as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"parameter": "cursor", "detail": str(error)}],
        ) from error
    out = _Out(me)
    with _limited():
        found = queries.reach(me, q)
        rows = found.page(after, params.limit + 1)
        more = len(rows) > params.limit
        rows = rows[: params.limit]
        items = _items(me, found, rows, out)
        total, hidden, beyond = found.total(), found.hidden(), found.beyond()
        out.load({root.pk}, set(), set())
    return {
        "root": out.entities[root.pk],
        "direction": direction,
        "depth": q.depth,
        "items": items,
        "next_cursor": queries.encode(q, rows[-1]) if more and rows else None,
        "total": total,
        "hidden": hidden,
        "beyond": beyond,
    }


@router.get("/entities/{ref}/upstream", response=WalkOut, summary="What it was derived from")
def upstream(request: HttpRequest, ref: str, params: Query[WalkPageParams]) -> dict[str, Any]:
    """Everything it depends on that you can read: what its references name, what the runs
    that made it were given (and the files they read), its registry entry, and what those
    depend on in turn, nearest first. Each result says how it was reached."""
    return _page(request, ref, params, "upstream")


@router.get("/entities/{ref}/downstream", response=WalkOut, summary="What was derived from it")
def downstream(request: HttpRequest, ref: str, params: Query[WalkPageParams]) -> dict[str, Any]:
    """Everything that depends on it that you can read: what refers to it, what runs made
    from it, what was made from its files, and what depends on those, nearest first: what a
    recall, a consent withdrawal or a correction affects. Each result says how it was
    reached."""
    return _page(request, ref, params, "downstream")


# --- CSV ---------------------------------------------------------------------------------------


_VERBS = {
    "reference": "refers to",
    "run": "was made from",
    "file": "was made from a file of",
    "record": "is a record of",
}


def _path_text(root: dict[str, Any], path: list[dict[str, Any]], direction: str) -> str:
    """A path as a line people read: ``SAM-000002 (donor) DNR-000001``, nearest the root
    first, each step saying why."""
    parts = [root["hrid"]]
    for step in path:
        why = step["field"] or step["kind"]
        if step["run"]:
            why += f" by {step['run']['hrid']}"
        if step["file"]:
            why += f", {step['file']['name']}"
        arrow = "←" if direction == "downstream" else "→"
        parts.append(f"{arrow} [{step['kind']}: {why}] {step['entity']['hrid']}")
    return " ".join(parts)


def _csv(request: HttpRequest, ref: str, params: WalkParams, direction: str) -> Any:  # noqa: ANN401
    me = caller(request)
    root, q = _walk(me, ref, params, direction)
    out = _Out(me)
    with _limited():
        found = queries.reach(me, q)
        if found.total() > MAX_EXPORT:
            raise Problem(
                422,
                f"There are more than {MAX_EXPORT:,} to export. Narrow it with `type`, "
                "`depth` or `edges`.",
                code="too_many",
            )
        rows = found.page(None, MAX_EXPORT)
        items = _items(me, found, rows, out)
        hidden = found.hidden()
        out.load({root.pk}, set(), set())
    top = out.entities[root.pk]
    columns = ["hrid", "title", "type", "project", "depth", "archived", "path", "id"]

    def table() -> Iterator[list[Any]]:
        yield columns
        for item in items:
            e = item["entity"]
            yield [
                e["hrid"],
                e["title"],
                e["type"],
                e["project"]["name"] if e["project"] else "",
                item["depth"],
                "yes" if e["archived"] else "",
                _path_text(top, item["path"], direction),
                e["id"],
            ]
        if hidden:
            note = f"{hidden} more lead to entities you can't read: ask someone who can."
            yield ["", note, "", "", "", "", "", ""]

    return csvs.streamed(request, csvs.chunks(table()), f"{top['hrid']}-{direction}.csv")


_CSV = {"responses": {200: {"description": "As CSV", "content": {"text/csv": {}}}}}


@router.get(
    "/entities/{ref}/upstream.csv", summary="Download what it was derived from", openapi_extra=_CSV
)
def upstream_csv(request: HttpRequest, ref: str, params: Query[WalkParams]) -> Any:  # noqa: ANN401
    """Every result of ``…/upstream``, with its path as text, nearest first."""
    return _csv(request, ref, params, "upstream")


@router.get(
    "/entities/{ref}/downstream.csv",
    summary="Download what was derived from it",
    openapi_extra=_CSV,
)
def downstream_csv(request: HttpRequest, ref: str, params: Query[WalkParams]) -> Any:  # noqa: ANN401
    """Every result of ``…/downstream``, with its path as text, nearest first: the impact
    list, for a recall or a consent withdrawal's records."""
    return _csv(request, ref, params, "downstream")


# --- A picture -------------------------------------------------------------------------------


class GraphParams(Schema):
    project: UUID | None = Field(
        None, description="Whose record, when an entry's ID or HRID names several (ADR 0115)."
    )
    up: int = Field(3, ge=0, le=MAX_GRAPH_STEPS, description="How many steps upstream.")
    down: int = Field(3, ge=0, le=MAX_GRAPH_STEPS, description="How many steps downstream.")
    edges: list[FollowedName] = Field(default_factory=list, description=_EDGES)


class GraphNodeOut(GraphEntityOut):
    at: int = Field(description="Steps from the root: negative upstream, positive downstream.")


class GraphRunOut(Schema):
    key: str = Field(description="How edges refer to it: its HRID, or a key for this response.")
    run: RunRefOut | None = Field(description="The run, if you can read its project.")


class GraphEdgeOut(Schema):
    node: UUID = Field(description="What depends.")
    on: UUID = Field(description="What it depends on.")
    kind: EdgeKindName
    field: str
    run: str | None = Field(description="The run's key, in runs.")
    file: WalkFileOut | None
    revision: int | None = Field(description="For a run's input: its revision the run read.")


class GraphOut(Schema):
    root: UUID
    entities: list[GraphNodeOut] = Field(description="What's in it that you can read.")
    runs: list[GraphRunOut]
    edges: list[GraphEdgeOut]
    hidden: int = Field(description="Upstream edges into entities you can't read.")
    earlier: bool = Field(description="There's more upstream than was asked for.")
    later: bool = Field(description="There's more downstream than was asked for.")
    capped: bool = Field(
        description=f"It stopped at {MAX_GRAPH_ENTITIES} entities or {MAX_GRAPH_EDGES} edges, "
        "so some within the steps asked for are left out."
    )


@router.get("/entities/{ref}/graph", response=GraphOut, summary="Its graph, to draw")
def get_graph(request: HttpRequest, ref: str, params: Query[GraphParams]) -> dict[str, Any]:
    """``up`` steps upstream and ``down`` downstream, with every edge between what's in it:
    for a picture. A record and its registry entry are drawn as one, unless more than one
    project's record of it is in the picture: then the entry joins them."""
    me = caller(request)
    root = _find(me, ref, params.project)
    kinds = queries.kinds_of(params.edges)
    with _limited():
        drawn = queries.graph(
            me,
            root.pk,
            params.up,
            params.down,
            kinds,
            max_nodes=MAX_GRAPH_ENTITIES,
            max_edges=MAX_GRAPH_EDGES,
        )
    out = _Out(me)
    out.load(
        set(drawn.nodes),
        {e[4] for e in drawn.edges if e[4]},
        {e[5] for e in drawn.edges if e[5]},
    )
    keys: dict[Any, str] = {}
    runs: list[dict[str, Any]] = []
    for edge in drawn.edges:
        run = edge[4]
        if run is None or run in keys:
            continue
        named = out.runs.get(run)
        keys[run] = named["hrid"] if named else f"hidden-{sum(r['run'] is None for r in runs) + 1}"
        runs.append({"key": keys[run], "run": named})
    # A registry entry with one record drawn is drawn as that record: they're one thing
    # (ADR 0115). One with several, another project's record of it among them, stays, with
    # a line from each record.
    entry = {pk for pk, e in out.entities.items() if e["entry"]}
    records: dict[Any, set[Any]] = {}
    for node, on, kind, *_ in drawn.edges:
        if kind == "record" and on in entry and node not in entry:
            records.setdefault(on, set()).add(node)
    alias = {e: next(iter(rs)) for e, rs in records.items() if len(rs) == 1}
    seen: set[tuple[Any, ...]] = set()
    edges = []
    for node, on, kind, name, run, file, revision in drawn.edges:
        if kind == "record" and (on not in entry or on in alias):
            continue  # each record's line to its entry once, from the record; or none
        node, on = alias.get(node, node), alias.get(on, on)
        key = (node, on, kind, name, run, file)
        if node == on or key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "node": node,
                "on": on,
                "kind": kind,
                "field": name,
                "run": keys.get(run) if run else None,
                "file": out.files.get(file) if file else None,
                "revision": revision,
            }
        )
    return {
        "root": root.pk,
        "entities": sorted(
            (
                {**out.entities[pk], "at": at}
                for pk, at in drawn.nodes.items()
                if pk in out.entities and pk not in alias
            ),
            key=lambda e: (e["at"], e["hrid"]),
        ),
        "runs": runs,
        "edges": edges,
        "hidden": drawn.hidden,
        "earlier": drawn.earlier,
        "later": drawn.later,
        "capped": drawn.capped,
    }
