"""What installed plugins need besides their migrations: ``iki-server sync_plugins``.

``migrate_deployment`` runs it as the migrator, in the transaction that runs the
plugins' migrations and before it grants the app role its privileges, so nothing
writes to a table whose triggers haven't caught up with it (ADR 0134). It is
idempotent, and works from the manifests the build installed
(:mod:`iki_server.entities.registry`):

- **HRID sequences**, one per prefix (``iki_hrid_prt``). They are created if
  missing and never dropped, so a number is never handed out twice.
- **Comments** on every plugin table and column: labels, descriptions,
  units and what references point to. Database tools and data catalogs
  show them, and they change without a migration.
- **Read-only views** for analysts and BI tools, in the schema ``iki_views``.
  There is one per type (``iki_views.protein_protein``), of each project's
  records, joining the core entity row, the type's table, its parents' tables
  and every installed extension, and for a type with an identity, its
  registry entry's (ADR 0115): its identity and the values marked
  ``registry``. Lists of references or files appear as arrays. The views use
  ``security_invoker``, so whoever queries one needs the right to read what
  it reads.
- **Unique fields' triggers** (ADR 0082): each keeps ``iki_unique_value`` in
  step with a unique column, so the database refuses a second entity in a
  project with the same value. Values already there are filled in, and if
  entities already share one, the sync stops, saying which.
- **References' edges** (ADR 0123): triggers on every table holding
  references, and every list of them, keep ``iki_graph_edge`` in step, so the
  graph across references is whatever the tables hold. Tables whose triggers
  are new are filled in, and edges of fields no longer there are dropped.
- **Plugins' rules** (ADR 0127): triggers keep the rows the database holds
  ``unique`` and ``no_overlap`` rules with, filling in records already there; if
  records already break one, the sync stops, saying which. Then each type whose
  checks, or whose plugins' versions, changed has its records checked again: those
  breaking a rule are reported, or for a rule that holds, held for review
  (:mod:`iki_server.entities.breaches`).
- **References' types** (ADR 0136): triggers on every table holding a reference to
  an abstract type, or to several, refuse a write naming an entity of another type,
  whatever makes it (:mod:`iki_server.entities.targets`).
- **Locked records' guards** (ADR 0125): a trigger on every table holding
  the values of records with state fields refuses a change to a record a
  state locks, whatever writes it. Tables that no longer need one lose it.
- **Plugins' states** (ADR 0091): each plugin the lockfile installs, updates,
  detaches or reattaches is audited, by the deploy that does it.
- **Lab lists' starting options** (ADR 0270): each registry gets those of its
  plugins' lists it hasn't got, by code, at the end of each list. What its
  curators did to the options it has is never undone.
- **An analyst role**, if ``IKI_DB_ANALYST_ROLE`` names one. It gets
  read-only access to the views and the tables behind them, current values
  and history, and nothing else.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps
from django.db import connection, models
from django.db.models.functions import Now
from psycopg import sql

from iki_server.audit import log as audit
from iki_server.entities import (
    breaches,
    constraints,
    holds,
    hrids,
    lists,
    registry,
    requests,
    states,
    targets,
    unique,
)
from iki_server.entities.values import shape
from iki_server.graph import edges
from iki_server.plugins import baseline
from iki_server.plugins.models import PluginState
from iki_spec import naming
from iki_spec.model import EntityType, Extension, Field, split_entry, stored_in_list_table

VIEWS_SCHEMA = "iki_views"

#: The core entity row's columns, first in every view.
ENTITY_COLUMNS = (
    "id",
    "hrid",
    "type",
    "owner_project_id",
    "entry_id",
    "created_by_id",
    "created_at",
    "archived_at",
)


@dataclass
class Report:
    sequences: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    comments: int = 0
    #: What changed about each plugin, such as ``dna detached``.
    plugins: list[str] = field(default_factory=list)
    unique: list[str] = field(default_factory=list)
    #: The lab lists whose labels the database holds once in a registry in any case.
    labels: list[str] = field(default_factory=list)
    graph: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    #: The rules the database holds, as ``table:rule``.
    rules: list[str] = field(default_factory=list)
    #: Each type whose records were checked again.
    checked: list[breaches.Checked] = field(default_factory=list)
    #: The lab lists that got starting options, and where (ADR 0270).
    lists: list[str] = field(default_factory=list)
    analyst: str | None = None


def sync(analyst_role: str | None = None) -> Report:
    """Runs every step, in one transaction (the caller's)."""
    holds.lock_rule_deploy(shared=False)
    report = Report()
    with connection.cursor() as cursor:
        for prefix in sorted(_prefixes()):
            name = hrids.sequence_name(prefix)
            cursor.execute(
                sql.SQL("CREATE SEQUENCE IF NOT EXISTS {} AS bigint MINVALUE 1 NO CYCLE").format(
                    sql.Identifier(name)
                )
            )
            cursor.execute(
                sql.SQL("COMMENT ON SEQUENCE {} IS {}").format(
                    sql.Identifier(name),
                    sql.Literal(f"HRID numbers for the prefix {prefix}. Never reset or reused."),
                )
            )
            report.sequences.append(name)
        for statement in _comments():
            cursor.execute(statement)
            report.comments += 1
        report.unique = unique.install(
            cursor,
            unique.columns((model, fields) for _, model, fields, _ in _tables()),
            _barcode_types(),
        )
        report.labels = lists.install_label_indexes(cursor)
        report.graph = edges.install(
            cursor, edges.tables((plugin, model, fields) for plugin, model, fields, _ in _tables())
        )
        report.rules = constraints.install(cursor, constraints.wanted())
        report.targets = targets.install(
            cursor,
            targets.tables((plugin, model, fields) for plugin, model, fields, _ in _tables()),
        )
        report.guards = states.install_guards(cursor, states.guarded_tables())
        report.guards += requests.install_guards(cursor, requests.guarded_tables())
        report.views = _views(cursor)
        report.plugins = _states()
        report.lists = lists.seed()
        report.checked = breaches.recheck()
        if analyst_role:
            for statement in _analyst_grants(analyst_role):
                cursor.execute(statement)
            report.analyst = analyst_role
        baseline.keep()
    return report


def _barcode_types() -> dict[str, list[str]]:
    """The concrete types that designate each physical column as their vendor barcode."""
    out: dict[str, list[str]] = {}
    for type_name, entity_type in registry.types().items():
        if entity_type.abstract or type_name == naming.ENTITY:
            continue
        form = shape(type_name)
        if name := form.vendor_barcode:
            out.setdefault(unique.key(form.part_of(name).model, name), []).append(type_name)
    return out


def describe(report: Report) -> list[str]:
    """What a sync made, a line each, as the commands print it."""
    lines = [f"  HRID sequence {name}" for name in report.sequences]
    lines += [f"  unique {key}" for key in report.unique]
    lines += [f"  labels of {name}, once in any case" for name in report.labels]
    lines += [f"  references of {table}" for table in report.graph]
    lines += [f"  rule {key}" for key in report.rules]
    for checked in report.checked:
        found = [f"{n} break {rule}" for rule, n in sorted(checked.broken.items())]
        found += [f"{n} held by {rule}" for rule, n in sorted(checked.held.items())]
        lines.append(
            f"  checked {checked.records} {checked.type}"
            + (": " + ", ".join(found) if found else "")
        )
    lines += [f"  references' types in {table}" for table in report.targets]
    lines += [f"  plugin {change}" for change in report.plugins]
    lines += [f"  starting options of {added}" for added in report.lists]
    lines += [f"  view {name}" for name in report.views]
    if report.analyst:
        lines.append(f"  {report.analyst} can read the views")
    lines.append(
        f"Synced {len(report.views)} entity type(s): {len(report.sequences)} HRID "
        f"sequence(s), {report.comments} comment(s)."
    )
    return lines


def check_analyst(role: str) -> str | None:
    """Why ``role`` can't be the analyst role, or None."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if cursor.fetchone() is None:
            return f"Postgres has no role {role!r}. Create it first."
    return None


def _states() -> list[str]:
    """Audits what this deploy changes about each plugin: installed, updated (another
    version or commit), detached or reattached. A build without a lockfile has none."""
    record = registry.installed()
    if record is None:
        return []
    known = {row.name: row for row in PluginState.objects.select_for_update()}
    changed = []
    for entry in record.get("plugins", ()):
        row = known.get(entry["name"])
        if entry.get("state") == PluginState.State.PURGED:
            # migrate_deployment marks it purged, and audits it, as it drops the tables.
            continue
        now = {
            "state": entry.get("state", PluginState.State.ACTIVE),
            "version": entry["version"],
            "sha": entry.get("sha") or registry.LOCAL,
        }
        if row is None:
            row = PluginState.objects.create(name=entry["name"], **now)
            action, data = "plugin.installed", now
        else:
            data = {k: [getattr(row, k), v] for k, v in now.items() if getattr(row, k) != v}
            if not data:
                continue
            for key, value in now.items():
                setattr(row, key, value)
            row.changed_at = Now()
            row.save()
            if "state" not in data:
                action = "plugin.updated"
            elif now["state"] == PluginState.State.DETACHED:
                action = "plugin.detached"
            else:
                action = "plugin.reattached"
        if entry.get("fetched_from") and (action == "plugin.installed" or "sha" in data):
            # Where the commit came from: its repository, or a mirror of Iki's (ADR 0114).
            data = {**data, "fetched_from": entry["fetched_from"]}
        audit.record(action, target=row, data=data)
        changed.append(f"{row.name} {action.removeprefix('plugin.')}")
    return changed


def analyst_role() -> str | None:
    return os.environ.get("IKI_DB_ANALYST_ROLE") or None


def _prefixes() -> set[str]:
    return {t.numbering_prefix for t in registry.types().values() if t.numbering_prefix}


# --- Models ------------------------------------------------------------------------------------


def _type_model(t: EntityType) -> type[models.Model]:
    return registry.model(t.qualified_name)


def _extension_model(e: Extension) -> type[models.Model]:
    return apps.get_model(naming.app_label(e.plugin), naming.extension_model(e.extends_type))


def _entry_model(plugin: str, model_name: str) -> type[models.Model]:
    """The table of registry entry fields (ADR 0115) beside a type's or an extension's."""
    return apps.get_model(naming.app_label(plugin), naming.entry_model(model_name))


def _history_model(plugin: str, model: type[models.Model]) -> type[models.Model]:
    return apps.get_model(naming.app_label(plugin), naming.history_model(model.__name__))


def _list_model(plugin: str, owner: type[models.Model], f: Field) -> type[models.Model]:
    return apps.get_model(naming.app_label(plugin), naming.list_model(owner.__name__, f.name))


def _column(model: type[models.Model], name: str) -> str:
    column = model._meta.get_field(name).column  # type: ignore[union-attr]
    return str(column)


def _tables() -> Iterable[tuple[str, type[models.Model], dict[str, Field], str]]:
    """(plugin, model, fields, description) for every type's and extension's own tables:
    of its records, and of its registry entries (ADR 0115) where it has any."""
    for t in registry.types().values():
        # The core plugin's types are generated like any plugin's, but iki/core.Entity is
        # the server's own model.
        if t.qualified_name == "iki/core.Entity":
            continue
        record, entry = split_entry(t.fields, t.identity)
        described = f"{t.label} ({t.qualified_name}), schema version {t.schema_version}."
        yield t.plugin, _type_model(t), record, _sentence(described, t.description)
        if t.identity:
            yield (
                t.plugin,
                _entry_model(t.plugin, t.name),
                entry,
                f"Registry entries of {t.label} ({t.qualified_name}): their identity and "
                "the values every project holding one shares (ADR 0115).",
            )
        elif registry.identity_owner(t) is not None:
            yield (
                t.plugin,
                _entry_model(t.plugin, t.name),
                entry,
                f"Registry entries of {t.label} ({t.qualified_name}): the registry fields "
                "it adds to its parent's (ADR 0165).",
            )
    for e in registry.extensions():
        record, entry = split_entry(e.fields)
        described = f"Fields the {e.plugin} plugin adds to {e.extends_type}."
        if record or not entry:
            yield e.plugin, _extension_model(e), record, _sentence(described, e.description)
        if entry:
            yield (
                e.plugin,
                _entry_model(e.plugin, naming.extension_model(e.extends_type)),
                entry,
                _sentence(
                    f"Registry fields the {e.plugin} plugin adds to {e.extends_type}'s "
                    "entries (ADR 0115).",
                    e.description,
                ),
            )


# --- Comments ----------------------------------------------------------------------------------


def _sentence(*parts: str | None) -> str:
    out = []
    for part in parts:
        if part:
            text = part.strip()
            out.append(text if text.endswith((".", "!", "?")) else text + ".")
    return " ".join(out)


def field_comment(f: Field) -> str:
    """What a column holds: its label and description, its unit, what it refers to."""
    item = f.items if f.type == "list" and f.items is not None else f
    unit = f"In {item.unit} (UCUM)." if item.type == "quantity" else None
    target = f"Refers to a {' or a '.join(item.to)}." if item.type == "ref" else None
    blob = "Refers to a file (iki_file)." if item.type == "file" else None
    removed = "Removed: kept for history only." if f.removed else None
    return _sentence(f.label, f.description, unit, target, blob, removed)


def _comment_on_table(table: str, text: str, *, schema: str | None = None) -> sql.Composable:
    name = sql.Identifier(schema, table) if schema else sql.Identifier(table)
    kind = sql.SQL("VIEW") if schema else sql.SQL("TABLE")
    return sql.SQL("COMMENT ON {} {} IS {}").format(kind, name, sql.Literal(text))


def _comment_on_column(
    table: str, column: str, text: str, *, schema: str | None = None
) -> sql.Composable:
    name = sql.Identifier(schema, table, column) if schema else sql.Identifier(table, column)
    return sql.SQL("COMMENT ON COLUMN {} IS {}").format(name, sql.Literal(text))


def _comments() -> list[sql.Composable]:
    out: list[sql.Composable] = []
    for plugin, model, fields, described in _tables():
        table = model._meta.db_table
        history = _history_model(plugin, model)
        out.append(_comment_on_table(table, described))
        out.append(
            _comment_on_table(
                history._meta.db_table,
                f"History of {table}: its values at each revision of each entity. Append-only.",
            )
        )
        for f in fields.values():
            text = field_comment(f)
            out.append(_comment_on_column(history._meta.db_table, _column(history, f.name), text))
            if f.removed:
                continue
            if stored_in_list_table(f):
                items = _list_model(plugin, model, f)
                out.append(
                    _comment_on_table(
                        items._meta.db_table,
                        f"Items of {table}.{f.name}, one row each, in order of position. {text}",
                    )
                )
                if f.is_rows:
                    assert f.items is not None  # noqa: S101 - lists have items
                    out.extend(
                        _comment_on_column(
                            items._meta.db_table, _column(items, n), field_comment(cell)
                        )
                        for n, cell in f.items.fields.items()
                    )
            else:
                out.append(_comment_on_column(table, _column(model, f.name), text))
    return out


# --- Views -------------------------------------------------------------------------------------


def drop_views(cursor: Any) -> None:  # noqa: ANN401 - Django's cursor
    """Drops Iki's views. Migrations run without them, since a view stops its columns from
    being dropped or changed; ``sync_plugins`` makes them again, in the plugins' migrations'
    transaction, so analysts never see them gone (ADR 0134). A view someone else built
    on one of these makes the drop fail, and the deploy with it, rather than being dropped
    silently."""
    cursor.execute(
        "SELECT viewname FROM pg_views WHERE schemaname = %s ORDER BY viewname", [VIEWS_SCHEMA]
    )
    for (name,) in cursor.fetchall():
        cursor.execute(sql.SQL("DROP VIEW {}").format(sql.Identifier(VIEWS_SCHEMA, name)))


def _views(cursor: Any) -> list[str]:  # noqa: ANN401 - Django's cursor
    cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(VIEWS_SCHEMA)))
    cursor.execute(
        sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            sql.Identifier(VIEWS_SCHEMA),
            sql.Literal("Read-only views of every entity type, for analysts. Managed by Iki."),
        )
    )
    # Views are rebuilt from scratch, since a changed type may have changed their
    # columns.
    drop_views(cursor)
    made = []
    for t in sorted(registry.types().values(), key=lambda t: t.qualified_name):
        if t.qualified_name == "iki/core.Entity":
            continue
        name, select, comments = _view(t)
        cursor.execute(
            sql.SQL("CREATE VIEW {} WITH (security_invoker = true) AS {}").format(
                sql.Identifier(VIEWS_SCHEMA, name), select
            )
        )
        for statement in comments:
            cursor.execute(statement)
        made.append(f"{VIEWS_SCHEMA}.{name}")
    return made


def _view(t: EntityType) -> tuple[str, sql.Composable, list[sql.Composable]]:
    """The view's name, its SELECT, and its comments."""
    name = t.table
    columns: list[sql.Composable] = [
        sql.SQL("e.{}").format(sql.Identifier(c)) for c in ENTITY_COLUMNS
    ]
    joins: list[sql.Composable] = []
    comments: list[sql.Composable] = [
        _comment_on_table(
            name,
            _sentence(
                f"Every {t.label} ({t.qualified_name}) with its fields, its parent types' and "
                "those other plugins add. Read-only.",
                t.description,
            ),
            schema=VIEWS_SCHEMA,
        )
    ]
    # A record's own parts join on its id; its registry entry's (ADR 0115) on its entry's,
    # always there for a record of a type with an identity, so both are inner joins, but
    # an extension's, which an entity may not have a row of, are left joins.
    for index, part in enumerate(shape(t.qualified_name).parts):
        plugin, model, fields = part.plugin, part.model, part.fields
        alias = sql.Identifier(f"t{index}")
        join = sql.SQL("LEFT JOIN") if part.extension else sql.SQL("JOIN")
        on = sql.SQL("e.entry_id" if part.entry else "e.id")
        joins.append(
            sql.SQL(" {} {} {} ON {}.entity_id = {}").format(
                join, sql.Identifier(model._meta.db_table), alias, alias, on
            )
        )
        for f in fields.values():
            if f.removed:
                continue
            if f.is_rows:
                # Each row an object of its fields (ADR 0137); its table has a column each.
                assert f.items is not None  # noqa: S101 - lists have items
                items = _list_model(plugin, model, f)
                pairs = sql.SQL(", ").join(
                    sql.SQL("{}, l.{}").format(sql.Literal(n), sql.Identifier(_column(items, n)))
                    for n in f.items.fields
                )
                column = sql.SQL(
                    "COALESCE((SELECT jsonb_agg(jsonb_build_object({}) ORDER BY l.position) "
                    "FROM {} l WHERE l.entity_id = {}), '[]'::jsonb) AS {}"
                ).format(pairs, sql.Identifier(items._meta.db_table), on, sql.Identifier(f.name))
                label = f.name
            elif stored_in_list_table(f):
                items = _list_model(plugin, model, f)
                column = sql.SQL(
                    "ARRAY(SELECT l.item_id FROM {} l WHERE l.entity_id = {} "
                    "ORDER BY l.position) AS {}"
                ).format(sql.Identifier(items._meta.db_table), on, sql.Identifier(f.name))
                label = f.name
            else:
                label = _column(model, f.name)
                column = sql.SQL("{}.{}").format(alias, sql.Identifier(label))
            columns.append(column)
            comments.append(_comment_on_column(name, label, field_comment(f), schema=VIEWS_SCHEMA))
    # Each project's records alone: entries are seen through them.
    select = sql.SQL("SELECT {} FROM iki_entity e{} WHERE e.owner_project_id IS NOT NULL").format(
        sql.SQL(", ").join(columns), sql.SQL("").join(joins)
    )
    return name, select, comments


# --- The analyst role ----------------------------------------------------------------------------


def _analyst_grants(role: str) -> list[sql.Composable]:
    who = sql.Identifier(role)
    tables = ["iki_entity"]
    for plugin, model, fields, _ in _tables():
        tables += [model._meta.db_table, _history_model(plugin, model)._meta.db_table]
        tables += [
            _list_model(plugin, model, f)._meta.db_table
            for f in fields.values()
            if stored_in_list_table(f) and not f.removed
        ]
    return [
        sql.SQL("GRANT USAGE ON SCHEMA {}, public TO {}").format(sql.Identifier(VIEWS_SCHEMA), who),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
            sql.Identifier(VIEWS_SCHEMA), who
        ),
        sql.SQL("GRANT SELECT ON {} TO {}").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in tables), who
        ),
    ]
