"""Exact preflight counts over project records using the deployed schema (ADR 0181)."""

from collections.abc import Sequence

from django.db import connection
from psycopg import sql

from iki_server.entities.values import FOREIGN_KEYS
from iki_server.plugins.preflight_records import Table
from iki_spec.model import stored_in_list_table

EXAMPLES = 5


def _example(alias: str) -> sql.Composed:
    """The same record identifier as Record.example, including the record ID for an entry."""
    return sql.SQL(
        "CASE WHEN {entry} IS NULL THEN {hrid} ELSE {hrid} || ' (' || {id}::text || ')' END"
    ).format(
        entry=sql.Identifier(alias, "entry_id"),
        hrid=sql.Identifier(alias, "hrid"),
        id=sql.Identifier(alias, "id"),
    )


class Scope:
    """Live records of the named concrete types. Only requested deployed tables are joined.

    Use column() to build expressions before calling select(). A missing candidate field
    and a missing extension row both supply SQL NULL. Each join uses a table's primary key,
    so one row still means one project record even when several records share an entry.
    Set entries=True to read the registry entries themselves, including unheld entries.
    """

    keys = sql.SQL("e.id, e.type, e.hrid, e.entry_id")

    def __init__(
        self, types: Sequence[str], tables: Sequence[Table], *, entries: bool = False
    ) -> None:
        self.types = list(types)
        self.entries = entries
        self._fields = {
            n: (table, f)
            for table in tables
            if table.entry or not entries
            for n, f in table.fields.items()
        }
        self._joins: dict[str, Table] = {}

    def column(self, name: str) -> sql.Composable:
        found = self._fields.get(name)
        if found is None:
            return sql.SQL("NULL")
        table, field = found
        if stored_in_list_table(field):
            raise ValueError(f"{name} is stored in a list table.")
        self._joins[table.table] = table
        column = f"{name}_id" if field.type in FOREIGN_KEYS else name
        return sql.Identifier(table.table, column)

    def select(
        self, columns: sql.Composable, *, where: sql.Composable | None = None
    ) -> sql.Composed:
        joins = [
            sql.SQL(" LEFT JOIN {} ON {} = {}").format(
                sql.Identifier(t.table),
                sql.Identifier(t.table, "entity_id"),
                sql.Identifier("e", "entry_id" if t.entry and not self.entries else "id"),
            )
            for t in self._joins.values()
        ]
        return sql.SQL(
            "SELECT {columns} FROM iki_entity e"
            " LEFT JOIN iki_project p ON p.id = e.owner_project_id"
            "{joins} WHERE e.type = ANY({types}::varchar[]) AND e.registry_id {registry} "
            "AND e.purged_at IS NULL{predicate}"
        ).format(
            columns=columns,
            joins=sql.Composed(joins),
            registry=sql.SQL("IS NOT NULL" if self.entries else "IS NULL"),
            types=sql.Literal(self.types),
            predicate=sql.SQL(" AND ({})").format(where) if where is not None else sql.SQL(""),
        )

    def counts(self, predicates: Sequence[sql.Composable]) -> list[int]:
        """Compute all the field counts for this scope in one aggregate pass."""
        if not predicates:
            return []
        columns = sql.SQL(", ").join(
            sql.SQL("count(*) FILTER (WHERE {})").format(p) for p in predicates
        )
        # One array column keeps wide schemas within PostgreSQL's target-list limit.
        with connection.cursor() as cursor:
            cursor.execute(self.select(sql.SQL("ARRAY[{}]").format(columns)))
            (counts,) = cursor.fetchone()
            return list(counts)

    def population(self) -> int:
        return self.counts([sql.SQL("TRUE")])[0]

    def examples(self, where: sql.Composable | None = None) -> list[str]:
        query = self.select(_example("e"), where=where)
        with connection.cursor() as cursor:
            cursor.execute(query + sql.SQL(" ORDER BY e.hrid, e.id LIMIT {}").format(EXAMPLES))
            return [example for (example,) in cursor.fetchall()]

    def count_examples(self, where: sql.Composable | None = None) -> tuple[int, list[str]]:
        n = self.counts([where if where is not None else sql.SQL("TRUE")])[0]
        return n, self.examples(where) if n else []


def summary(rows: sql.Composable, *, group: str = "type") -> dict[str, tuple[int, list[str]]]:
    """Count matched rows by group and retain at most five examples in each aggregate.

    The input identifies each affected record once with id, hrid, entry_id and a group
    column. A constraint finding may span several types while an amount change may span
    several original units.
    """
    query = sql.SQL(
        "SELECT r.{group}, count(*), array_agg({example} ORDER BY r.n) FILTER (WHERE r.n <= {cap})"
        " FROM (SELECT m.*, row_number() OVER (PARTITION BY m.{group} ORDER BY m.hrid, m.id) AS n"
        " FROM ({rows}) m) r GROUP BY r.{group} ORDER BY r.{group}"
    ).format(group=sql.Identifier(group), example=_example("r"), cap=EXAMPLES, rows=rows)
    with connection.cursor() as cursor:
        cursor.execute(query)
        return {key: (n, examples) for key, n, examples in cursor.fetchall()}
