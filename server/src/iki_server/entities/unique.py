"""Keys: fields marked ``unique`` (ADR 0082), such as the ID of the record a LIMS or ELN
keeps for an entity.

A unique field is a one-field ``unique`` rule named for it (ADR 0127), which
:mod:`~iki_server.entities.constraints` installs and the database holds as it holds any:
no two records in a project share its value, whatever writes it, or for a registry fact,
no two entries in a registry (ADR 0115). What makes the field a key beyond its rule is
here: a record is found by its value (the API's lookup, a run's ``match_on``, a
spreadsheet's and a bundle's imports), its value may link to the other system, and a
vendor barcode's reserved prefix is refused.

Projects are kept apart: the same record may be in two projects, as two entities, and
nothing here says what another project holds. Who holds an entry's value in a registry is
told only to someone who can read it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any
from uuid import UUID

from django.db import IntegrityError, models
from django.db.models.expressions import RawSQL
from psycopg import sql

from iki_server.entities import constraints, registry
from iki_server.entities.models import LIST_LABEL_INDEX, Entity, UniqueValue
from iki_server.entities.values import Shape
from iki_server.org.models import Project, Registry, registry_of
from iki_spec.model import Field

__all__ = [
    "BARCODE_TRIGGER_PREFIX",
    "holders",
    "in_registry",
    "install_barcodes",
    "is_label_clash",
    "key",
    "link",
    "match",
    "owned",
    "rows",
    "taken",
    "text",
]

#: The vendor barcodes' triggers' names start with this; ``sync_plugins`` drops those no
#: type needs.
BARCODE_TRIGGER_PREFIX = "iki_vendor_barcode__"

# Match codes.has_iki_prefix under any database locale. These are Python's leading
# whitespace characters and the characters that uppercase to I and K.
_RESERVED_VENDOR = (
    r"^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A"
    r"\u2028\u2029\u202F\u205F\u3000]*[Ii\u0131][Kk][Ii\u0131]:"
)

#: The digest of one value as its rule's row holds it, from SQL text: what
#: ``iki_rule_values`` makes of a one-field rule's column.
_ONE = "sha256(convert_to(jsonb_build_array(({})::text)::text, 'UTF8'))"


def key(model: type[models.Model], name: str) -> str:
    """The key a unique field's values are held under: its rule's, ``table:field``."""
    return f"{model._meta.db_table}:{name}"[:127]


def match(value_sql: str) -> str:
    """SQL true of an ``iki_unique_value u`` row holding ``value_sql``, a text expression, as
    a unique field's value."""
    return f"u.digest = {_ONE.format(value_sql)}"


def rows(field_key: str, values: Iterable[object]) -> models.QuerySet[UniqueValue]:
    """The rows holding any of ``values`` of the unique field ``field_key``: :func:`text`
    reads each one's value. Narrow them to a project or registry before reading any."""
    texts = sorted({str(v) for v in values})
    digests = f"SELECT {_ONE.format('v')} FROM unnest(%s::text[]) AS v"  # noqa: S608 - ours
    return UniqueValue.objects.filter(
        key=field_key,
        digest__in=RawSQL(digests, [texts]),  # noqa: S611 - ours; values are parameters
    )


def text(stored: str) -> str:
    """A unique field's value from its rule's row's ``value``, a one-item JSON array."""
    return str(json.loads(stored)[0])


def taken(
    registry_id: UUID,
    form: Shape,
    values: Mapping[str, Any],
    *,
    entry: UUID | None = None,
    changed: Iterable[str] | None = None,
    holder: Project | None = None,
) -> dict[str, list[str]]:
    """What a registry entry with these values (each of its fields' a rule reads, as
    stored) would break of its type's ``within: registry`` rules, a unique fact's among
    them, against the other entries in the registry: field name → what's wrong. ``entry``
    is the entry being changed, if any, and ``changed`` what it changes.

    Another entry is named to a registrar, who sees every entry, but to a ``holder``
    project registering or filling in its entry only if it holds a record of it: a
    registry doesn't tell a project what others registered (ADR 0115). A project's
    record's own ``unique`` rules, a unique field's among them, are checked with its other
    rules (``checking.apart``)."""
    errors: dict[str, list[str]] = {}
    found = constraints.taken(
        None, form, values, entity=entry, changed=changed, registry_id=registry_id
    )
    for c, hrid, other in found:
        shown = (
            holder is None or Entity.objects.filter(entry_id=other, owner_project=holder).exists()
        )
        if c.rule.from_field:
            whose = f"{hrid}’s" if shown else "another entry’s"
            message = f"is {whose} already: no two entries in the registry share it."
        else:
            message = f"{c.rule.message} {hrid if shown else 'Another entry'} has the same."
        for name in c.rule.fields or c.rule.reads:
            errors.setdefault(name, []).append(message)
    return errors


def carried(type_name: str, fields: Collection[str], *, in_registry: bool) -> list[str]:
    """The keys of a type's unique rules whose fields ``fields`` all are: a record moved to
    another type, or an entry superseded by one, hands on its values of them, so its rows
    of those rules go with them (ADR 0082). Those of a registry's entries' rules, or of a
    project's records'."""
    names = set(fields)
    return [
        c.key
        for c in constraints.constraints_of(type_name)
        if c.rule.kind == "unique" and c.in_registry == in_registry and set(c.rule.unique) <= names
    ]


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
    found = rows(field_key, [value])
    in_registry_ = found.filter(registry_id=registry_of(project)).values("entity_id")
    records = Entity.objects.filter(owner_project=project, entry_id__in=in_registry_)
    if records.exists():
        return records.values_list("pk", flat=True)  # type: ignore[return-value]
    return found.filter(project=project).values_list("entity_id", flat=True)


def in_registry(registry_: Registry, field_key: str, value: object) -> UUID | None:
    """The entry in ``registry_`` with this value of a unique fact, if any."""
    return (
        rows(field_key, [value])
        .filter(registry=registry_)
        .values_list("entity_id", flat=True)
        .first()
    )


def holders(field_key: str, value: str) -> models.QuerySet[UniqueValue, UUID]:
    """The IDs of the entities, in any project, with this value of a unique field: narrow
    them to what the reader can see before showing any."""
    return rows(field_key, [value]).values_list("entity_id", flat=True)


def is_label_clash(error: IntegrityError) -> bool:
    """Whether a database error is a lab list's label written twice in a registry, in any
    case (ADR 0270): see :func:`iki_server.entities.lists.install_label_indexes`."""
    diag = getattr(error.__cause__, "diag", None)
    return str(getattr(diag, "constraint_name", None) or "").startswith(LIST_LABEL_INDEX)


def install_barcodes(
    cursor: Any,  # noqa: ANN401 - Django's cursor
    barcodes: Mapping[tuple[str, str], list[str]],
) -> list[str]:
    """Triggers refusing the reserved ``IKI:`` prefix in each (table, column) a concrete
    type reads as its vendor barcode, for those types. Drops those no type needs any more.
    Raises :class:`constraints.Clash` when records already have one: the deploy stops,
    saying which."""
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgname LIKE %s",
        [BARCODE_TRIGGER_PREFIX.replace("_", r"\_") + "%"],
    )
    current = {(table, name) for table, name in cursor.fetchall()}
    needed = {(table, _barcode_trigger(column)) for table, column in barcodes}
    for table, name in sorted(current - needed):
        cursor.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
        )
    for (table, column), types in sorted(barcodes.items()):
        cursor.execute(
            sql.SQL(
                "CREATE OR REPLACE TRIGGER {name} AFTER INSERT OR UPDATE OF {column} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION iki_vendor_barcode_check({args})"
            ).format(
                name=sql.Identifier(_barcode_trigger(column)),
                column=sql.Identifier(column),
                table=sql.Identifier(table),
                # Plugin type names cannot contain array punctuation.
                args=sql.SQL(", ").join(
                    sql.Literal(arg)
                    for arg in (column, "{" + ",".join(sorted(types)) + "}", _RESERVED_VENDOR)
                ),
            )
        )
        # The trigger now holds the writer lock through this transaction. Check old rows
        # before a deployment accepts writes with the new declaration.
        cursor.execute(
            sql.SQL(
                "SELECT e.hrid FROM {table} t JOIN iki_entity e ON e.id = t.entity_id "
                'WHERE e.type = ANY(%s) AND t.{column} COLLATE "C" ~ %s ORDER BY e.hrid LIMIT 10'
            ).format(table=sql.Identifier(table), column=sql.Identifier(column)),
            [sorted(types), _RESERVED_VENDOR],
        )
        if records := cursor.fetchall():
            raise constraints.Clash(
                f"Existing vendor barcodes in `{table}.{column}` start with the reserved IKI: "
                f"prefix on {', '.join(hrid for (hrid,) in records)}. Correct them and deploy "
                "again."
            )
    return [f"{table}.{column}" for table, column in sorted(barcodes)]


def _barcode_trigger(column: str) -> str:
    return (BARCODE_TRIGGER_PREFIX + column)[:63]
