"""Unique fields: no two entities in a project share a value (ADR 0082).

A plugin marks an indexed string field ``unique``, such as the ID of the
record a LIMS or ELN keeps for an entity. Each value is held once per owner
project in :class:`UniqueValue`, which triggers on the plugin tables keep in
step (``sync_plugins`` installs them, see :func:`install`), so the database
refuses a duplicate whoever writes it. Before writing, :func:`taken` finds
which entity has a value already, so a person is told which.

Projects are kept apart: the same record may be in two projects, as two
entities, and nothing here says what another project holds.

A registry fact marked unique (a ChEMBL ID) is held once per registry instead
(ADR 0115), as the entries hold it: two entries in a registry can't share
one. Who holds the other is told only to someone who can read it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, models
from psycopg import sql

from iki_server.entities import registry
from iki_server.entities.models import UNIQUE_CONSTRAINT, UNIQUE_IN_REGISTRY, UniqueValue
from iki_server.entities.values import Shape
from iki_server.org.models import Project, Registry, registry_of
from iki_spec.model import Field

__all__ = [
    "Clash",
    "Column",
    "clash_message",
    "columns",
    "holders",
    "install",
    "is_clash",
    "key",
    "link",
    "owned",
    "taken",
]

#: The triggers' names start with this; ``sync_plugins`` drops those no field needs.
TRIGGER_PREFIX = "iki_unique__"

# Match codes.has_iki_prefix under any database locale. These are Python's leading
# whitespace characters and the characters that uppercase to I and K.
_RESERVED_VENDOR = (
    r"^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A"
    r"\u2028\u2029\u202F\u205F\u3000]*[Ii\u0131][Kk][Ii\u0131]:"
)


@dataclass(frozen=True)
class Column:
    table: str
    column: str

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


def key(model: type[models.Model], name: str) -> str:
    """The key a field's values are held under: ``table.column``."""
    field = model._meta.get_field(name)
    return f"{model._meta.db_table}.{field.column}"  # type: ignore[union-attr]


def columns(tables: Iterable[tuple[type[models.Model], Mapping[str, Any]]]) -> list[Column]:
    """The unique columns of these (model, fields) tables, fields removed or not unique left out."""
    out = []
    for model, fields in tables:
        for f in fields.values():
            if f.unique and not f.removed:
                column = model._meta.get_field(f.name).column  # type: ignore[union-attr]
                out.append(Column(model._meta.db_table, str(column)))
    return out


def taken(
    project: Project,
    form: Shape,
    values: Mapping[str, Any],
    *,
    entity: UUID | None = None,
    entry: UUID | None = None,
) -> dict[str, list[str]]:
    """Each of ``values`` that a unique field of ``form`` has on another entity in the
    project, or for a registry fact, on another entry in its registry: field name → what's
    wrong. ``entity`` is the record being changed and ``entry`` the entry, if any.

    Another entry's ID is named only if the project holds it: a registry doesn't tell a
    project what others registered (ADR 0115)."""
    errors: dict[str, list[str]] = {}
    for part in form.parts:
        for name, f in part.fields.items():
            value = values.get(name)
            if not f.unique or f.removed or value is None:
                continue
            field_key = key(part.model, name)
            if part.entry:
                rows = UniqueValue.objects.filter(
                    registry_id=registry_of(project), key=field_key, value=str(value)
                )
                if entry is not None:
                    rows = rows.exclude(entity_id=entry)
                found = rows.values_list("entity_id", "entity__hrid").first()
                if found is not None:
                    errors[name] = [_entry_clash(project, *found)]
                continue
            rows = UniqueValue.objects.filter(project=project, key=field_key, value=str(value))
            if entity is not None:
                rows = rows.exclude(entity_id=entity)
            found_hrid = rows.values_list("entity__hrid", flat=True).first()
            if found_hrid is not None:
                errors[name] = [
                    f"is {found_hrid}’s already: no two entities in a project share it."
                ]
    return errors


def _entry_clash(project: Project, entry: UUID, hrid: str) -> str:
    from iki_server.entities.models import Entity

    held = Entity.objects.filter(entry_id=entry, owner_project=project).exists()
    if held:
        return f"is {hrid}’s already: no two entries in the registry share it."
    return "is another entry’s in the registry already: no two entries share it."


def link(f: Field, plugin: str) -> str | None:
    """Where a field's values lead in another system: its link with the plugin's settings
    filled in and ``{value}`` left for each value, or None. Only an https link is given, so a
    deployment's setting can't make one run script or leave TLS."""
    if f.link is None:
        return None
    values = registry.settings(plugin)
    out = re.sub(
        r"\{settings\.([^{}]+)\}", lambda m: str(values.get(m[1]) or "").rstrip("/"), f.link
    )
    if not out.startswith("https://") or "{value}" not in out or len(out) <= len("https://"):
        return None
    return out


def owned(project: Project, field_key: str, value: object) -> models.QuerySet[UniqueValue, UUID]:
    """The ID of the entity of ``project``'s own with this value of a unique field, if any.
    For a registry fact's key, the project's record of the entry with it."""
    from iki_server.entities.models import Entity

    rows = UniqueValue.objects.filter(key=field_key, value=str(value))
    in_registry = rows.filter(registry_id=registry_of(project)).values("entity_id")
    records = Entity.objects.filter(owner_project=project, entry_id__in=in_registry)
    if records.exists():
        return records.values_list("pk", flat=True)  # type: ignore[return-value]
    return rows.filter(project=project).values_list("entity_id", flat=True)


def in_registry(registry: Registry, field_key: str, value: object) -> UUID | None:
    """The entry in ``registry`` with this value of a unique fact, if any."""
    return (
        UniqueValue.objects.filter(registry=registry, key=field_key, value=str(value))
        .values_list("entity_id", flat=True)
        .first()
    )


def holders(field_key: str, value: str) -> models.QuerySet[UniqueValue, UUID]:
    """The IDs of the entities, in any project, with this value of a unique field: narrow
    them to what the reader can see before showing any."""
    return UniqueValue.objects.filter(key=field_key, value=value).values_list(
        "entity_id", flat=True
    )


class Clash(Exception):  # noqa: N818 - reads as "the values clash"
    """Entities already share a value of a field marked unique, so it can't be."""


def is_clash(error: IntegrityError) -> bool:
    """Whether a database error is a unique field's value written twice in a project, or a
    unique fact's twice in a registry."""
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None) in (UNIQUE_CONSTRAINT, UNIQUE_IN_REGISTRY)


def clash_message(error: IntegrityError) -> str:
    """What a unique field's value written twice in a project was, in a sentence."""
    diag = getattr(error.__cause__, "diag", None)
    if getattr(diag, "constraint_name", None) == UNIQUE_IN_REGISTRY:
        return "An entry in the registry has one of these unique values already, or two lines do."
    detail = getattr(diag, "message_detail", None) or ""
    # Key (project_id, key, value)=(…, protein_protein.lims_id, LIMS-1) already exists.
    found = re.search(r"=\([^,]+, ([^,]+), (.*)\) already exists", detail)
    if found is None:
        return "An entity in the project has one of these unique values already."
    return f"An entity in the project has `{found[1]}` “{found[2]}” already, or two lines do."


def install(
    cursor: Any,  # noqa: ANN401 - Django's cursor
    wanted: list[Column],
    barcode_types: Mapping[str, list[str]],
) -> list[str]:
    """Triggers keeping ``wanted`` columns' values, and the values already there. Drops
    triggers and values of fields no longer unique. Raises :class:`Clash` when entities in
    a project already share a value or use a reserved vendor barcode. The deploy stops,
    saying which."""
    keys = [c.key for c in wanted]
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    current = {(table, name) for table, name in cursor.fetchall()}
    needed = {(c.table, _trigger(c)) for c in wanted}
    for table, name in sorted(current - needed):
        cursor.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
        )
    # A field's key is `table.column`; a plugin rule's (ADR 0127) has a colon, and is
    # kept by `constraints.install`.
    cursor.execute(
        "DELETE FROM iki_unique_value WHERE key NOT LIKE %s AND NOT (key = ANY(%s))",
        ["%:%", keys],
    )
    clashes = [found for c in wanted for found in _clashes(cursor, c)]
    if clashes:
        raise Clash(
            "Some entities share a value of a field marked unique, so it can't be until they "
            "don't: " + "; ".join(clashes) + ". Change or clear one of each, or leave "
            "`unique` off, then deploy again."
        )
    for c in wanted:
        args = [c.column, c.key]
        types = barcode_types.get(c.key)
        if types:
            # Plugin type names cannot contain array punctuation.
            args.extend(["{" + ",".join(sorted(types)) + "}", _RESERVED_VENDOR])
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE OF {column} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION iki_unique_value_keep({args})"
            ).format(
                name=sql.Identifier(_trigger(c)),
                column=sql.Identifier(c.column),
                table=sql.Identifier(c.table),
                args=sql.SQL(", ").join(sql.Literal(arg) for arg in args),
            )
        )
        if types:
            # The trigger now holds the writer lock through this transaction. Check
            # old rows before a deployment accepts writes with the new declaration.
            _check_vendor_codes(cursor, c, types)
        _fill(cursor, c)
    return keys


def _check_vendor_codes(cursor: Any, c: Column, types: list[str]) -> None:  # noqa: ANN401
    cursor.execute(
        sql.SQL(
            "SELECT e.hrid FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
            'WHERE e.type = ANY(%s) AND t.{column} COLLATE "C" ~ %s ORDER BY e.hrid LIMIT 10'
        ).format(table=sql.Identifier(c.table), column=sql.Identifier(c.column)),
        [types, _RESERVED_VENDOR],
    )
    if records := cursor.fetchall():
        raise Clash(
            f"Existing vendor barcodes in `{c.key}` start with the reserved IKI: prefix "
            f"on {', '.join(hrid for (hrid,) in records)}. Correct them and deploy again."
        )


def _trigger(c: Column) -> str:
    return (TRIGGER_PREFIX + c.column)[:63]


def _clashes(cursor: Any, c: Column) -> list[str]:  # noqa: ANN401
    cursor.execute(
        sql.SQL(
            "SELECT coalesce(p.name, r.name), t.{column}::text, array_agg(e.hrid ORDER BY e.hrid) "
            "FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
            "LEFT JOIN iki_project p ON p.id = e.owner_project_id "
            "LEFT JOIN iki_registry r ON r.id = e.registry_id "
            "WHERE t.{column} IS NOT NULL GROUP BY 1, t.{column} HAVING count(*) > 1 "
            "ORDER BY 1, t.{column} LIMIT 10"
        ).format(table=sql.Identifier(c.table), column=sql.Identifier(c.column))
    )
    return [
        f"{', '.join(hrids)} in {project} have `{c.key}` “{value}”"
        for project, value, hrids in cursor.fetchall()
    ]


def _fill(cursor: Any, c: Column) -> None:  # noqa: ANN401
    """Values written while no trigger kept them: a field just marked unique."""
    names = {"table": sql.Identifier(c.table), "column": sql.Identifier(c.column)}
    cursor.execute(
        sql.SQL(
            "DELETE FROM iki_unique_value u USING {table} t WHERE u.key = %s "
            "AND u.entity_id = t.entity_id AND u.value IS DISTINCT FROM t.{column}::text"
        ).format(**names),
        [c.key],
    )
    cursor.execute(
        sql.SQL(
            "INSERT INTO iki_unique_value (project_id, registry_id, key, value, entity_id) "
            "SELECT e.owner_project_id, e.registry_id, %s, t.{column}::text, e.id "
            "FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
            "WHERE t.{column} IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM iki_unique_value u WHERE u.entity_id = e.id AND u.key = %s)"
        ).format(**names),
        [c.key, c.key],
    )
