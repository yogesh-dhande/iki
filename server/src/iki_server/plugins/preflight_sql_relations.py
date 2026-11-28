"""Count every participant in a proposed relational constraint (ADR 0181)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg import sql

from iki_server.plugins.preflight_projection import project
from iki_server.plugins.preflight_rules import projected_changes
from iki_server.plugins.preflight_sql import Scope, summary
from iki_server.plugins.preflight_types import Finding
from iki_spec.model import stored_in_list_table

if TYPE_CHECKING:
    from iki_server.plugins.preflight import _Shape


@dataclass(frozen=True)
class _Constraint:
    table: str
    subject: str
    compared: tuple[str, ...]
    endpoints: tuple[str, ...] = ()
    counted: tuple[tuple[str, tuple[str, ...]], ...] = ()
    entry: bool = False
    inclusive: bool = False

    @property
    def kind(self) -> str:
        return "no_overlap" if self.endpoints else "unique"

    @property
    def message(self) -> str:
        if self.endpoints:
            return "These records have overlapping ranges in a project."
        if self.entry:
            return "These records hold different entries sharing a value in a registry."
        return "These records share the same values in a project."


def _changes(before: _Shape, after: _Shape) -> Iterator[_Constraint]:
    """The rules the database is to hold that are new or read what changes: a unique
    field's one-field rule among them (ADR 0082)."""
    changed = projected_changes(before.fields, {**after.fields, **after.retired})
    for key, rule in after.rules.items():
        if rule.kind != "check" and (
            rule != before.rules.get(key)
            or after.rule_tables[key] != before.rule_tables.get(key)
            or changed.intersection(
                (*rule.unique, *rule.per, *rule.no_overlap, *(n for n, _ in rule.only_while))
            )
        ):
            yield _Constraint(
                after.rule_tables[key],
                rule.key,
                rule.unique or rule.per,
                endpoints=rule.no_overlap,
                counted=rule.only_while,
                entry=rule.within == "registry",
                inclusive=bool(rule.no_overlap and after.fields[rule.no_overlap[0]].type == "date"),
            )


def exact_relations(
    scopes: Mapping[str, tuple[_Shape, _Shape]], *, affected: set[str]
) -> dict[str, list[Finding]]:
    """A declaring table is one constraint scope, even across concrete sibling types."""
    members: dict[str, list[str]] = defaultdict(list)
    constraints: dict[_Constraint, None] = {}
    for name, (before, after) in scopes.items():
        for table in after.tables:
            members[table.table].append(name)
        if name in affected:
            constraints.update(dict.fromkeys(_changes(before, after)))

    out: dict[str, list[Finding]] = defaultdict(list)
    for constraint in constraints:
        branches = []
        unread = []
        for name in members[constraint.table]:
            before, after = scopes[name]
            counted, invalid = _rows(name, before, after, constraint)
            branches.append(counted)
            if invalid is not None:
                unread.append(invalid)
        matches = _conflicts(sql.SQL(" UNION ALL ").join(branches), constraint)
        checks = [(constraint.kind, constraint.message, matches)]
        if unread:
            checks.append(
                (
                    "unchecked",
                    "The proposed rule cannot compare these values.",
                    sql.SQL(" UNION ALL ").join(unread),
                )
            )
        for kind, message, rows in checks:
            if constraint.entry:
                # Entry uniqueness is enforced on the registry, including entries
                # no project currently holds. Report the affected live project records.
                rows = sql.SQL("""
                    SELECT h.id, h.type, h.hrid, h.entry_id FROM ({}) entries
                    JOIN iki_entity h ON h.entry_id = entries.id
                    WHERE h.registry_id IS NULL AND h.purged_at IS NULL
                """).format(rows)
            for name, (count, examples) in summary(rows).items():
                out[name].append(Finding(kind, constraint.subject, message, True, count, examples))
    return out


def _rows(
    name: str, before: _Shape, after: _Shape, constraint: _Constraint
) -> tuple[sql.Composable, sql.Composable | None]:
    """Project only this rule's keys, endpoints and filters from the *deployed* tables."""
    scope = Scope([name], before.tables, entries=constraint.entry)
    fields = {**after.fields, **after.retired}
    reads = {*constraint.compared, *constraint.endpoints, *(n for n, _ in constraint.counted)}
    values = {n: project(n, before.fields, fields, scope.column) for n in reads}
    scope_id = sql.SQL("e.registry_id" if constraint.entry else "e.owner_project_id")
    columns = [scope.keys, sql.SQL("{} AS scope_id").format(scope_id)]
    where = [sql.SQL("{} IS NOT NULL").format(scope_id)]
    for i, n in enumerate(constraint.compared):
        columns.append(sql.SQL("{} AS {}").format(values[n], sql.Identifier(f"k{i}")))
        where.append(sql.SQL("({}) IS NOT NULL").format(values[n]))
    membership = {
        n: sql.SQL("({})::text = ANY({}::text[])").format(values[n], sql.Literal(list(allowed)))
        for n, allowed in constraint.counted
    }
    where.extend(membership.values())
    if constraint.endpoints:
        start, end = constraint.endpoints
        lo, hi = values[start], values[end]
        if fields[start].type != fields[end].type:
            # Mixed whole and floating point endpoints. The exclusion trigger
            # compares their JSON numbers as numeric, preserving bigint precision.
            lo, hi = (sql.SQL("({})::text::numeric").format(v) for v in (lo, hi))
        columns.extend([sql.SQL("{} AS lo").format(lo), sql.SQL("{} AS hi").format(hi)])
        op = sql.SQL("<=" if constraint.inclusive else "<")
        where.append(sql.SQL("({}) {} ({})").format(lo, op, hi))
    counted = scope.select(sql.SQL(", ").join(columns), where=sql.SQL(" AND ").join(where))

    # Unread projected inputs (an old value that does not fit a changed type or
    # overflows in its new unit) must not masquerade as ordinary SQL NULLs.
    invalid = {
        n: sql.SQL("({} AND ({}) IS NULL)").format(
            sql.SQL("TRUE")
            if stored_in_list_table(before.fields[n])
            else sql.SQL("{} IS NOT NULL").format(scope.column(n)),
            values[n],
        )
        for n in values
        if n in before.fields
        and (before.fields[n].type, before.fields[n].unit) != (fields[n].type, fields[n].unit)
    }
    if not invalid:
        return counted, None
    # An incompatible value needs attention only if the row could participate.
    # A known inactive while condition or a missing companion key excludes it.
    possible = [sql.SQL("({})").format(sql.SQL(" OR ").join(invalid.values()))]
    for n in reads:
        known = membership.get(n, sql.SQL("({}) IS NOT NULL").format(values[n]))
        possible.append(sql.SQL("({} OR {})").format(known, invalid[n]) if n in invalid else known)
    unread = scope.select(scope.keys, where=sql.SQL(" AND ").join(possible))
    return counted, unread


def _conflicts(rows: sql.Composable, constraint: _Constraint) -> sql.Composable:
    group = sql.SQL(", ").join(
        [sql.SQL("scope_id"), *(sql.Identifier(f"k{i}") for i in range(len(constraint.compared)))]
    )
    if not constraint.endpoints:
        return sql.SQL("""
            WITH members AS ({rows}),
            duplicates AS (SELECT {group} FROM members GROUP BY {group} HAVING count(*) > 1)
            SELECT m.id, m.type, m.hrid, m.entry_id FROM members m JOIN duplicates USING ({group})
        """).format(rows=rows, group=group)

    # In start order, a range overlaps some earlier range iff the furthest earlier
    # end reaches it; it overlaps a later range iff it reaches the next start. This
    # counts each participant once, including nested, equal and date-touching ranges.
    op = sql.SQL("<=" if constraint.inclusive else "<")
    return sql.SQL("""
        SELECT id, type, hrid, entry_id FROM (
            SELECT r.*, max(hi) OVER (
                       ordered ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS earlier_end,
                   lead(lo) OVER ordered AS next_start
            FROM ({rows}) r WINDOW ordered AS (PARTITION BY {group} ORDER BY lo, id)
        ) sweep WHERE lo {op} earlier_end OR next_start {op} hi
    """).format(rows=rows, group=group, op=op)
