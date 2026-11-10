"""Migrating the database to the deployed build, forwards or back: ``migrate_deployment``.

The Helm migrate Job runs it as the migrator, in place of ``migrate``. A
build installs the plugins its lockfile pins (ADR 0034). Rolling back means
deploying an older lockfile, whose build lacks the newer migrations the
database has applied. Django ignores those, which would leave a schema the
code doesn't know. So before migrating, this:

1. Refuses what it can't do safely:
   - a database that a newer Iki migrated (restore a backup instead)
   - a plugin missing from the build (removing plugins is ADR 0008's detach)
   - a migration that changed after it was applied
2. Reverses the plugin migrations the build doesn't have, from the copies
   it kept of them (:class:`~iki_server.plugins.models.PluginMigration`).
   It does so only if no reversal can lose data. Every operation must be
   reversible. No table it would drop may hold rows, no column it would drop
   may hold anything but its default, and no column whose type it would
   change may hold values. Otherwise it changes nothing and says why.
3. Runs ``migrate``.
4. Keeps a copy of every plugin migration now applied.
"""

from __future__ import annotations

import hashlib
import sys
import types
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.apps import apps
from django.core.management import call_command
from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from psycopg import sql

from iki_server.plugins.models import PluginMigration
from iki_server.plugins.operations import AppendOnly, ConvertUnit, RenameEntityType
from iki_spec.validate import RESERVED_PLUGIN_NAMES

Key = tuple[str, str]

#: Operations whose reversal can't lose data.
LOSSLESS = (
    migrations.RemoveField,  # re-adds the column, empty
    migrations.RenameField,
    migrations.RenameModel,
    migrations.AddConstraint,
    migrations.RemoveConstraint,
    migrations.AddIndex,
    migrations.RemoveIndex,
    migrations.RenameIndex,
    migrations.AlterModelOptions,
    migrations.AlterModelManagers,
    migrations.AlterModelTableComment,
    ConvertUnit,
    RenameEntityType,
    AppendOnly,
)


class DeployError(Exception):
    """The database can't be migrated to this build; the message says why and what to do."""


@dataclass
class Report:
    reversed: list[Key] = field(default_factory=list)
    archived: list[Key] = field(default_factory=list)


def plugin_apps() -> list[str]:
    return [c.label for c in apps.get_app_configs() if c.name.startswith("iki_plugin_")]


def migrate_deployment(stdout: Any = None) -> Report:  # noqa: ANN401 - a Django OutputWrapper
    report = Report()
    executor = MigrationExecutor(connection)
    applied = set(executor.recorder.applied_migrations())
    code = executor.loader.disk_migrations
    installed = set(plugin_apps())
    archive = _archive()
    unknown = sorted(k for k in applied if k not in code)

    gone = {k[0] for k in unknown if k[0] not in installed} | {
        a for a in archive if a not in installed and any(k[0] == a for k in applied)
    }
    removed = sorted(a for a in gone if a in archive or _could_be_plugin(a))
    newer = [k for k in unknown if k[0] not in installed and k[0] not in removed]
    if newer:
        raise DeployError(
            f"A newer Iki migrated this database ({_names(newer)}), and this is Iki "
            f"{_version()}. Deploy that version or later, or restore the database from the backup "
            "taken before the upgrade."
        )
    if removed:
        raise DeployError(
            f"The database has the plugin(s) {', '.join(removed)}, but this build doesn't. "
            "Removing a plugin needs it detached first (ADR 0008): keep it in the lockfile."
        )
    changed = [
        k
        for k in sorted(applied)
        if k[0] in installed
        and k in code
        and k in archive.get(k[0], {})
        and archive[k[0]][k].sha256 != _sha256(_source(code[k]))
    ]
    if changed:
        raise DeployError(
            f"Applied migrations changed since they ran here: {_names(changed)}. A migration "
            "can't change once a database has applied it: put the change in a new migration."
        )

    to_reverse = [k for k in unknown if k[0] in installed]
    if to_reverse:
        report.reversed = _roll_back(to_reverse, archive, applied)
    call_command("migrate", interactive=False, verbosity=1, stdout=stdout)
    report.archived = _keep_copies(installed)
    return report


def _could_be_plugin(label: str) -> bool:
    """Whether an app the build doesn't have was a plugin rather than part of Iki or Django."""
    return not label.startswith("iki_") and label not in RESERVED_PLUGIN_NAMES


# --- The archive ---------------------------------------------------------------------------------


def _archive() -> dict[str, dict[Key, PluginMigration]]:
    """The latest copy of each plugin migration, by app. Empty before the table exists."""
    if PluginMigration._meta.db_table not in connection.introspection.table_names():
        return {}
    out: dict[str, dict[Key, PluginMigration]] = {}
    for row in PluginMigration.objects.order_by("archived_at", "id"):
        out.setdefault(row.app, {})[(row.app, row.name)] = row
    return out


def _source(migration: Migration) -> str:
    module = sys.modules[type(migration).__module__]
    return Path(module.__file__ or "").read_text("utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keep_copies(installed: Iterable[str]) -> list[Key]:
    loader = MigrationLoader(connection)
    applied = MigrationRecorder(connection).applied_migrations()
    archive = _archive()
    kept = []
    for key, migration in sorted(loader.disk_migrations.items()):
        if key[0] not in installed or key not in applied:
            continue
        source = _source(migration)
        digest = _sha256(source)
        if key in archive.get(key[0], {}) and archive[key[0]][key].sha256 == digest:
            continue
        PluginMigration.objects.create(app=key[0], name=key[1], sha256=digest, source=source)
        kept.append(key)
    return kept


# --- Rolling back --------------------------------------------------------------------------------


class _WithCopies(MigrationLoader):
    """The build's migrations, plus the kept copies of those the database has and it doesn't."""

    def __init__(self, copies: dict[Key, PluginMigration], *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Django's
        self.copies = copies
        super().__init__(*args, **kwargs)

    def load_disk(self) -> None:
        super().load_disk()
        for key, row in self.copies.items():
            if _sha256(row.source) != row.sha256:
                raise DeployError(f"The kept copy of {_names([key])} doesn't match its hash.")
            app, name = key
            package = self.migrations_module(app)[0] or f"iki_plugin_{app}.migrations"
            module = types.ModuleType(f"{package}.{name}")
            module.__file__ = f"<kept copy of {app}.{name}>"
            exec(compile(row.source, module.__file__, "exec"), module.__dict__)  # noqa: S102 - code this database already ran, as kept by the migrator
            self.disk_migrations[key] = module.Migration(name, app)


def _roll_back(
    to_reverse: list[Key], archive: dict[str, dict[Key, PluginMigration]], applied: set[Key]
) -> list[Key]:
    missing = [k for k in to_reverse if k not in archive.get(k[0], {})]
    if missing:
        raise DeployError(
            f"Can't roll back {_names(missing)}: this build doesn't have them, and Iki kept no "
            "copy (they ran before it kept copies). Restore the database from the backup taken "
            "before they ran, or deploy a lockfile with those plugins at that version or later."
        )
    copies = {k: archive[k[0]][k] for k in to_reverse}
    executor = MigrationExecutor(connection)
    executor.loader = _WithCopies(copies, connection)
    code = MigrationLoader(connection)
    targets = []
    for app in sorted({k[0] for k in to_reverse}):
        target = _last_applied(code, app, applied)
        if target is None:
            raise DeployError(
                f"Can't roll back {app}: none of this build's migrations for it have run here."
            )
        targets.append(target)
    plan = executor.migration_plan(targets)
    unexpected = [(m.app_label, m.name) for m, backwards in plan if not backwards]
    reversing = [(m.app_label, m.name) for m, backwards in plan if backwards]
    if unexpected or set(reversing) - set(to_reverse):
        raise DeployError(f"Rolling back would also change {_names(unexpected or reversing)}.")
    blockers = _data_loss(executor, plan)
    if blockers:
        raise DeployError(
            "Rolling back would lose data, so nothing was changed:\n"
            + "".join(f"  - {b}\n" for b in blockers)
            + "Deploy a lockfile with these plugins at their newer versions, or restore the "
            "database from the backup taken before they were upgraded."
        )
    executor.migrate(targets, plan=plan)
    return reversing


def _last_applied(loader: MigrationLoader, app: str, applied: set[Key]) -> Key | None:
    """The latest of the build's migrations for ``app`` that the database has applied."""
    for leaf in loader.graph.leaf_nodes(app):
        for node in reversed(loader.graph.forwards_plan(leaf)):
            if node[0] == app and node in applied:
                return (str(node[0]), str(node[1]))
    return None


def _data_loss(executor: MigrationExecutor, plan: list[tuple[Migration, bool]]) -> list[str]:
    """What reversing ``plan`` would destroy, if anything."""
    blockers = []
    with connection.cursor() as cursor:
        for migration, _ in plan:
            key = (migration.app_label, migration.name)
            state = executor.loader.project_state(key, at_end=False)
            for operation in migration.operations:
                before = state.clone()
                operation.state_forwards(migration.app_label, state)
                problem = _lost_by_reversing(cursor, migration.app_label, operation, before, state)
                if problem:
                    blockers.append(f"{migration.app_label}.{migration.name}: {problem}")
    return blockers


def _lost_by_reversing(
    cursor: Any,  # noqa: ANN401 - Django's cursor
    app: str,
    operation: Any,  # noqa: ANN401 - any migration operation
    before: ProjectState,
    after: ProjectState,
) -> str | None:
    if not operation.reversible:
        return f"{operation.describe()} can't be reversed."
    if isinstance(operation, migrations.CreateModel):
        model = after.apps.get_model(app, operation.name)
        if _exists(cursor, "SELECT 1 FROM {} LIMIT 1", model._meta.db_table):
            return f"{model._meta.db_table} has rows, and reversing would drop it."
        return None
    if isinstance(operation, migrations.AddField):
        model = after.apps.get_model(app, operation.model_name)
        column = model._meta.get_field(operation.name)
        table = model._meta.db_table
        default = None if getattr(model, "IKI_HISTORY_OF", None) else column.get_default()
        value = column.get_db_prep_save(default, connection) if default is not None else None
        if value is None:
            found = _exists(
                cursor, "SELECT 1 FROM {} WHERE {} IS NOT NULL LIMIT 1", table, column.column
            )
        else:
            found = _exists(
                cursor,
                "SELECT 1 FROM {} WHERE {} IS DISTINCT FROM %s LIMIT 1",
                table,
                column.column,
                params=[value],
            )
        if found:
            return f"{table}.{column.column} has values, and reversing would drop it."
        return None
    if isinstance(operation, migrations.AlterField):
        old = before.apps.get_model(app, operation.model_name)._meta.get_field(operation.name)
        new_model = after.apps.get_model(app, operation.model_name)
        new = new_model._meta.get_field(operation.name)
        if old.db_type(connection) != new.db_type(connection) and _exists(
            cursor,
            "SELECT 1 FROM {} WHERE {} IS NOT NULL LIMIT 1",
            new_model._meta.db_table,
            new.column,
        ):
            return (
                f"{new_model._meta.db_table}.{new.column} has values, and reversing would change "
                f"its type back from {new.db_type(connection)} to {old.db_type(connection)}."
            )
        if (
            not old.null
            and new.null
            and _exists(
                cursor,
                "SELECT 1 FROM {} WHERE {} IS NULL LIMIT 1",
                new_model._meta.db_table,
                new.column,
            )
        ):
            return (
                f"{new_model._meta.db_table}.{new.column} has empty values, which it didn't "
                "allow before."
            )
        return None
    if isinstance(operation, LOSSLESS):
        return None
    if isinstance(operation, migrations.RunPython | migrations.RunSQL):
        # Written by the plugin's author, with a reverse they wrote and reviewed.
        return None
    return f"Iki doesn't know whether reversing {operation.describe()} keeps data."


def _exists(cursor: Any, query: str, *names: str, params: list[Any] | None = None) -> bool:  # noqa: ANN401 - Django's cursor
    cursor.execute(
        sql.SQL(query).format(*(sql.Identifier(n) for n in names)).as_string(cursor.connection),
        params or [],
    )
    return cursor.fetchone() is not None


def _names(keys: Iterable[Key]) -> str:
    return ", ".join(f"{app}.{name}" for app, name in keys)


def _version() -> str:
    from iki_server import __version__

    return __version__
