"""Plugins' rules the database holds (ADR 0127): fields no two of a project's records
share together (``unique``), and ranges no two of them overlap (``no_overlap``). A field
marked ``unique``, a key such as a LIMS's ID, is a one-field ``unique`` rule named for
the field (ADR 0082), held the same way. A ``within: registry`` rule, a unique registry
fact's among them, holds of the entries in a registry instead (ADR 0115).

Each rule's fields are in one table, its declaring type's or extension's (its entries'
table, within a registry). A trigger on that table keeps a row per record in a side
table: ``iki_unique_value`` for a combination, under the key ``table:rule``, its values a
JSON array whose SHA-256 is unique per project or registry, and ``iki_range_value`` for
a range, whose exclusion constraint refuses two overlapping ones in a project
(PostgreSQL's ``EXCLUDE USING gist``). So a person, a run, a bulk import and SQL are all
held to it, and two saves at once can't both get in. A rule's ``while`` counts only
records whose enum, state or bool fields have one of its values: a tube that's stored, a
booking that isn't cancelled.

Before writing, :func:`taken` finds the record a save would clash with, so a person is
told which; two saves at the same moment are refused by the database instead, and
:func:`clash` reads which rule from its error. ``sync_plugins`` installs the triggers
(:func:`install`), drops those of rules no longer declared, fills in rows for records
saved before, and stops the deploy, naming them, if records already break a rule.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any
from uuid import UUID

from django.db import IntegrityError, models
from psycopg import sql

from iki_server.entities import registry
from iki_server.entities.models import (
    RANGE_CONSTRAINT,
    SPAN_CONSTRAINT,
    UNIQUE_CONSTRAINT,
    UNIQUE_IN_REGISTRY,
    Entity,
)
from iki_server.entities.values import FOREIGN_KEYS, Shape, shape
from iki_spec.model import Field, Rule
from iki_spec.rules import RANGE_TYPES

__all__ = [
    "Clash",
    "Constraint",
    "clash",
    "constraints_of",
    "install",
    "taken",
    "wanted",
]

#: The triggers' names start with this; ``sync_plugins`` drops those no rule needs.
TRIGGER_PREFIX = "iki_rule__"


class Clash(Exception):  # noqa: N818 - reads as "the records clash"
    """Records already break a rule the database is to hold, so it can't until they don't."""


@dataclass(frozen=True)
class Constraint:
    """A ``unique`` or ``no_overlap`` rule, where the database holds it."""

    rule: Rule
    #: The type it's declared on, or an extension's extended type.
    type_name: str
    model: type[models.Model]
    #: Its fields, by name, as its table has them.
    fields: Mapping[str, Field]

    @property
    def table(self) -> str:
        return str(self.model._meta.db_table)

    @property
    def in_registry(self) -> bool:
        """It holds of a registry's entries (``within: registry``), not a project's records."""
        return self.rule.within == "registry"

    @property
    def key(self) -> str:
        return f"{self.table}:{self.rule.name}"[:127]

    def column(self, name: str) -> str:
        return str(self.model._meta.get_field(name).column)  # type: ignore[union-attr]

    @property
    def compared(self) -> tuple[str, ...]:
        """The fields records are compared by: a combination, or what ranges are per."""
        return self.rule.unique or self.rule.per

    @property
    def range_kind(self) -> str:
        return RANGE_TYPES[self.fields[self.rule.no_overlap[0]].type]

    def columns_json(self) -> str:
        return json.dumps([self.column(n) for n in self.compared])

    def counted_json(self) -> str:
        return json.dumps(
            {self.column(n): list(values) for n, values in self.rule.only_while},
            sort_keys=True,
        )


def _own_model(t: str, rule: Rule) -> tuple[type[models.Model], Mapping[str, Field]] | None:
    """The table a rule's fields are in: its type's or extension's, or within a registry,
    the one of their entries' fields."""
    form = shape(t)
    entry = rule.within == "registry"
    for part in form.parts:
        if part.entry != entry:
            continue
        if rule.extension and not (part.extension and part.plugin == rule.plugin):
            continue
        if not rule.extension and (part.extension or part.name != (f"{t}#entry" if entry else t)):
            continue
        return part.model, part.fields
    return None


@cache
def wanted() -> tuple[Constraint, ...]:
    """Every installed rule the database holds, once each (not per subtype)."""
    out: list[Constraint] = []
    for entity_type in registry.types().values():
        if entity_type.plugin == "iki/core" and not entity_type.rules:
            continue
        for rule in entity_type.rules.values():
            if rule.kind == "check":
                continue
            found = _own_model(entity_type.qualified_name, rule)
            if found is not None:
                out.append(Constraint(rule, entity_type.qualified_name, *found))
    for extension in registry.extensions():
        if extension.adds_to not in registry.types():
            continue
        for rule in extension.rules.values():
            if rule.kind == "check":
                continue
            found = _own_model(extension.adds_to, rule)
            if found is not None:
                out.append(Constraint(rule, extension.adds_to, *found))
    return tuple(out)


@cache
def constraints_of(type_name: str) -> tuple[Constraint, ...]:
    """Those a type's records keep: declared on it, an ancestor, or an extension of one."""
    entity_type = registry.types()[type_name]
    names = {type_name} | {t.qualified_name for t in registry.ancestors(entity_type)}
    return tuple(c for c in wanted() if c.type_name in names)


# --- Before writing -----------------------------------------------------------------------------


def taken(
    project_id: Any,  # noqa: ANN401 - a UUID
    form: Shape,
    values: Mapping[str, Any],
    *,
    entity: UUID | None = None,
    changed: Iterable[str] | None = None,
    registry_id: Any = None,  # noqa: ANN401 - a UUID
) -> list[tuple[Constraint, str, UUID]]:
    """The rules a record with these values (every field each rule reads, as the database
    stores them) would break, each with the HRID and ID of the record it would clash with.
    With ``registry_id``, the ``within: registry`` rules an entry with these values would
    break against the registry's other entries; otherwise a project's record's. On an
    edit, ``changed`` names what it changes: rules reading none of it aren't checked."""
    out: list[tuple[Constraint, str, UUID]] = []
    touched = set(changed) if changed is not None else None
    for c in constraints_of(form.entity_type.qualified_name):
        rule = c.rule
        if c.in_registry != (registry_id is not None):
            continue
        if touched is not None and not touched & set(rule.reads):
            continue
        if any(values.get(n) is None for n in c.compared):
            continue
        if any(not _counts(c.fields[n], values.get(n), allowed) for n, allowed in rule.only_while):
            continue
        scope = (
            Entity.objects.filter(registry_id=registry_id)
            if c.in_registry
            else Entity.objects.filter(owner_project_id=project_id)
        )
        rows = c.model._default_manager.filter(entity_id__in=scope.values("pk"))
        if entity is not None:
            rows = rows.exclude(entity_id=entity)
        for name in c.compared:
            f = c.fields[name]
            key = f"{name}_id" if f.type in FOREIGN_KEYS else name
            rows = rows.filter(**{key: values[name]})
        for name, allowed in rule.only_while:
            rows = rows.filter(**{f"{name}__in": _typed(c.fields[name], allowed)})
        if rule.kind == "no_overlap":
            start, end = rule.no_overlap
            low, high = values.get(start), values.get(end)
            if low is None or high is None or low > high:
                continue
            found = _overlapping(c, rows, low, high)
        else:
            found = rows.values_list("entity_id", flat=True).first()
        if found is not None:
            hrid = Entity.objects.filter(pk=found).values_list("hrid", flat=True).first()
            out.append((c, hrid or "another record", found))
    return out


def _typed(f: Field, allowed: Iterable[str]) -> list[Any]:
    if f.type == "bool":
        return [a == "true" for a in allowed]
    return list(allowed)


def _counts(f: Field, value: Any, allowed: Iterable[str]) -> bool:  # noqa: ANN401 - a model value
    if value is None:
        return False
    text = ("true" if value else "false") if f.type == "bool" else str(value)
    return text in set(allowed)


def _overlapping(c: Constraint, rows: models.QuerySet[Any], low: Any, high: Any) -> Any:  # noqa: ANN401 - values and an ID
    """The first of ``rows`` whose range overlaps [low, high), or [low, high] for dates."""
    start, end = (c.column(n) for n in c.rule.no_overlap)
    kind = c.range_kind
    if kind == "date":
        # Dates include both ends: one range ends before the other starts, or after.
        condition = f'"{start}" <= %s AND "{end}" >= %s'
        params: list[Any] = [high, low]
    else:
        # Half-open: one can end as the next starts. An empty range overlaps nothing.
        condition = f'"{start}" < %s AND "{end}" > %s AND "{start}" < "{end}"'
        params = [high, low]
        if low == high:
            return None
    found = rows.extra(where=[f'"{start}" <= "{end}"', condition], params=params)  # noqa: S610 - columns are the manifest's, quoted; values are parameters
    return found.values_list("entity_id", flat=True).first()


# --- Two saves at once --------------------------------------------------------------------------

#: ``Key (project_id, key, digest)=(…, protein_protein:lims_id, \x…) already exists.``: the
#: rule's key is the second value of every constraint here.
_KEY = re.compile(r"\)=\([^,]*, ([^,]*),")


def clash(error: IntegrityError) -> tuple[Constraint | None, bool]:
    """Whether a database error is one of these rules broken by two saves at once: (its
    rule, if the error says which, True), or (None, False) for another error."""
    diag = getattr(error.__cause__, "diag", None)
    if getattr(diag, "constraint_name", None) not in (
        RANGE_CONSTRAINT,
        SPAN_CONSTRAINT,
        UNIQUE_CONSTRAINT,
        UNIQUE_IN_REGISTRY,
    ):
        return None, False
    found = _KEY.search(getattr(diag, "message_detail", None) or "")
    key = found[1] if found is not None else None
    return next((c for c in wanted() if c.key == key), None), True


# --- At deploy ----------------------------------------------------------------------------------

UNIQUE_TRIGGER = (
    "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE OF {columns} ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION iki_unique_keep({key}, {compared}, {counted}, {within})"
)
RANGE_TRIGGER = (
    "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE OF {columns} ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION iki_rule_range_keep({key}, {compared}, {counted}, {start}, "
    "{end}, {kind})"
)
#: What's unique of a record's values under a rule: the SHA-256 of their JSON array's text.
DIGEST = "sha256(convert_to({}, 'UTF8'))"


def _trigger(c: Constraint) -> str:
    return (TRIGGER_PREFIX + c.rule.name)[:63]


def install(cursor: Any, constraints: Iterable[Constraint]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Triggers keeping each rule's rows, and the rows of records already there. Drops
    the triggers and rows of rules no longer declared. Raises :class:`Clash` when records
    already break one: the deploy stops, saying which."""
    wanted_ = list(constraints)
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    current = {(table, name) for table, name in cursor.fetchall()}
    needed = {(c.table, _trigger(c)) for c in wanted_}
    for table, name in sorted(current - needed):
        cursor.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
        )
    unique_keys = [c.key for c in wanted_ if c.rule.kind == "unique"]
    range_keys = [c.key for c in wanted_ if c.rule.kind == "no_overlap"]
    cursor.execute("DELETE FROM iki_unique_value WHERE NOT (key = ANY(%s))", [unique_keys])
    cursor.execute("DELETE FROM iki_range_value WHERE NOT (key = ANY(%s))", [range_keys])
    clashes = [found for c in wanted_ for found in _clashes(cursor, c)]
    if clashes:
        raise Clash(
            "Some records already break a rule the database is to hold, so it can't until "
            "they don't: " + "; ".join(clashes) + ". Change one of each, or take the rule out "
            "(or a field's `unique`), then deploy again."
        )
    for c in wanted_:
        columns = sorted(
            {
                *(c.column(n) for n in c.compared),
                *(c.column(n) for n, _ in c.rule.only_while),
                *(c.column(n) for n in c.rule.no_overlap),
            }
        )
        common = {
            "name": sql.Identifier(_trigger(c)),
            "table": sql.Identifier(c.table),
            "columns": sql.SQL(", ").join(sql.Identifier(x) for x in columns),
            "key": sql.Literal(c.key),
            "compared": sql.Literal(c.columns_json()),
            "counted": sql.Literal(c.counted_json()),
        }
        if c.rule.kind == "unique":
            cursor.execute(
                sql.SQL(UNIQUE_TRIGGER).format(**common, within=sql.Literal(c.rule.within))
            )
        else:
            start, end = (c.column(n) for n in c.rule.no_overlap)
            cursor.execute(
                sql.SQL(RANGE_TRIGGER).format(
                    **common,
                    start=sql.Literal(start),
                    end=sql.Literal(end),
                    kind=sql.Literal(c.range_kind),
                )
            )
        _fill(cursor, c)
    return sorted(c.key for c in wanted_)


def _computed(c: Constraint) -> sql.Composable:
    """Each of the table's records' rows for the rule: its project (or registry), and its
    values or range."""
    row = sql.SQL("to_jsonb(t)")
    value = sql.SQL("iki_rule_values({row}, {compared}, {counted})").format(
        row=row, compared=sql.Literal(c.columns_json()), counted=sql.Literal(c.counted_json())
    )
    if c.rule.kind == "unique":
        scope = sql.Identifier("e", "registry_id" if c.in_registry else "owner_project_id")
        return sql.SQL(
            "SELECT e.id AS entity_id, e.hrid, {scope} AS scope_id, {value} AS value "
            "FROM {table} t JOIN iki_entity e ON e.id = t.entity_id WHERE {scope} IS NOT NULL"
        ).format(scope=scope, value=value, table=sql.Identifier(c.table))
    start, end = (c.column(n) for n in c.rule.no_overlap)
    return sql.SQL(
        "SELECT e.id AS entity_id, e.hrid, e.owner_project_id AS scope_id, {value} AS scope, "
        "r.during, r.span FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
        "CROSS JOIN LATERAL iki_rule_range({row}, {start}, {end}, {kind}) r "
        "WHERE e.owner_project_id IS NOT NULL"
    ).format(
        value=value,
        row=row,
        table=sql.Identifier(c.table),
        start=sql.Literal(start),
        end=sql.Literal(end),
        kind=sql.Literal(c.range_kind),
    )


def _clashes(cursor: Any, c: Constraint) -> list[str]:  # noqa: ANN401
    computed = _computed(c)
    if c.rule.kind == "unique":
        where = sql.SQL("iki_registry" if c.in_registry else "iki_project")
        cursor.execute(
            sql.SQL(
                "SELECT p.name, array_agg(x.hrid ORDER BY x.hrid) FROM ({computed}) x "
                "JOIN {where} p ON p.id = x.scope_id WHERE x.value IS NOT NULL "
                "GROUP BY p.name, x.scope_id, x.value HAVING count(*) > 1 "
                "ORDER BY p.name LIMIT 10"
            ).format(computed=computed, where=where)
        )
        return [
            f"{', '.join(hrids)} in {scope} break “{c.rule.label}”"
            for scope, hrids in cursor.fetchall()
        ]
    cursor.execute(
        sql.SQL(
            "WITH x AS ({computed}) SELECT p.name, a.hrid, b.hrid FROM x a JOIN x b "
            "ON a.scope_id = b.scope_id AND a.scope = b.scope AND a.hrid < b.hrid "
            "AND (a.during && b.during OR a.span && b.span) "
            "JOIN iki_project p ON p.id = a.scope_id WHERE a.scope IS NOT NULL "
            "ORDER BY 1, 2, 3 LIMIT 10"
        ).format(computed=computed)
    )
    return [
        f"{first} and {second} in {project} overlap, breaking “{c.rule.label}”"
        for project, first, second in cursor.fetchall()
    ]


def _fill(cursor: Any, c: Constraint) -> None:  # noqa: ANN401
    """Rows for records whose values the rule's trigger didn't see: a rule just added, or
    changed."""
    computed = _computed(c)
    if c.rule.kind == "unique":
        cursor.execute(
            sql.SQL(
                "DELETE FROM iki_unique_value u WHERE u.key = %s AND NOT EXISTS ("
                "SELECT 1 FROM ({computed}) x WHERE x.entity_id = u.entity_id "
                "AND x.value = u.value)"
            ).format(computed=computed),
            [c.key],
        )
        cursor.execute(
            sql.SQL(
                "INSERT INTO iki_unique_value ({scope}, key, value, digest, entity_id) "
                "SELECT x.scope_id, %s, x.value, {digest}, x.entity_id FROM ({computed}) x "
                "WHERE x.value IS NOT NULL AND NOT EXISTS (SELECT 1 FROM iki_unique_value u "
                "WHERE u.entity_id = x.entity_id AND u.key = %s)"
            ).format(
                scope=sql.Identifier("registry_id" if c.in_registry else "project_id"),
                digest=sql.SQL(DIGEST.format("x.value")),
                computed=computed,
            ),
            [c.key, c.key],
        )
        return
    cursor.execute(
        sql.SQL(
            "DELETE FROM iki_range_value v WHERE v.key = %s AND NOT EXISTS ("
            "SELECT 1 FROM ({computed}) x WHERE x.entity_id = v.entity_id AND x.scope = v.scope "
            "AND x.during IS NOT DISTINCT FROM v.during AND x.span IS NOT DISTINCT FROM v.span)"
        ).format(computed=computed),
        [c.key],
    )
    cursor.execute(
        sql.SQL(
            "INSERT INTO iki_range_value (project_id, key, scope, during, span, entity_id) "
            "SELECT x.scope_id, %s, x.scope, x.during, x.span, x.entity_id FROM ({computed}) x "
            "WHERE x.scope IS NOT NULL AND (x.during IS NOT NULL OR x.span IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM iki_range_value v WHERE v.entity_id = x.entity_id "
            "AND v.key = %s)"
        ).format(computed=computed),
        [c.key, c.key],
    )
