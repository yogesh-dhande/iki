"""Migrating the database to the deployed build, forwards or back: ``migrate_deployment``.

The Helm migrate Job runs it as the migrator, in place of ``migrate``. A
build installs the plugins its lockfile pins (ADR 0034). Rolling back means
deploying an older lockfile, whose build lacks the newer migrations the
database has applied. Django ignores those, which would leave a schema the
code doesn't know. So before migrating, this:

1. Refuses what it can't do safely:
   - a database that a newer Iki migrated (restore a backup instead)
   - a plugin missing from the build, unless the lockfile purges it
   - a migration that changed after it was applied
   - a plugin migration that isn't atomic
2. Runs Iki's own migrations, and Django's, each in a transaction of its own as
   ``migrate`` does: some build indexes ``CONCURRENTLY``, which no transaction can
   hold. None of them touches a plugin's table.
3. Then, in one transaction (ADR 0134):

   1. Drops the tables of each plugin the lockfile purges (ADR 0092), once its
      purge has waited and left a tombstone for every entity. See :func:`_purge`.
   2. Reverses the plugin migrations the build doesn't have, from the copies
      it kept of them (:class:`~iki_server.plugins.models.PluginMigration`).
      It does so only if no reversal can lose data. Every operation must be
      reversible. No table it would drop may hold rows, no column it would drop
      may hold anything but its default, and no column whose type it would
      change may hold values. Otherwise it changes nothing and says why.
   3. Runs ``migrate`` for the plugins.
   4. Keeps a copy of every plugin migration now applied.
   5. Runs ``sync_plugins`` (:mod:`iki_server.plugins.sync`): the triggers that
      hold unique fields, rules and locks, the views and the rest.
   6. Grants the app role what it needs (``grant_app_privileges``), if named.

   Other connections, such as the old pods serving through a deploy, see the old
   schema with its guards until the commit, then the new one with its guards, so
   no write lands on a table whose guards haven't caught up with it. If any step
   fails, nothing of them is kept.
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
from django.core.management.base import OutputWrapper
from django.core.management.commands import migrate as django_migrate
from django.core.management.sql import emit_pre_migrate_signal
from django.db import DatabaseError, connection, migrations, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.db.models import Q
from django.utils import timezone
from psycopg import sql

from iki_server.audit import log as audit
from iki_server.core import db_roles
from iki_server.entities import registry
from iki_server.entities.models import Entity, ProjectLink, Tombstone, UniqueValue
from iki_server.org import holds
from iki_server.plugins import sync
from iki_server.plugins.models import PluginMigration, PluginPurge, PluginState
from iki_server.plugins.operations import AppendOnly, ConvertUnit, RenameEntityType
from iki_spec import naming, releases

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


#: Added to an error from the deploy's transaction, which keeps nothing of it.
KEPT_NOTHING = (
    "Nothing of the plugins' changes was kept: their migrations, the triggers guarding "
    "their tables, the views and the app role's grants are as they were before this deploy "
    "(ADR 0134)."
)


class DeployError(Exception):
    """The database can't be migrated to this build; the message says why and what to do."""


@dataclass
class Report:
    reversed: list[Key] = field(default_factory=list)
    archived: list[Key] = field(default_factory=list)
    #: What each purge did, a line per plugin.
    purged: list[str] = field(default_factory=list)
    #: What ``sync_plugins`` made, in the same transaction.
    synced: sync.Report = field(default_factory=sync.Report)
    #: The statements granting the app role its privileges, if one was named.
    granted: list[str] = field(default_factory=list)


def plugin_apps() -> list[str]:
    """The installed plugins' apps, by label (``iki__protein``): migrations are by label."""
    return [c.label for c in apps.get_app_configs() if c.name.startswith("iki_plugin_")]


def _plugin(label: str) -> str:
    """The plugin an app label is, by name: ``iki/protein`` for ``iki__protein``."""
    return naming.plugin_of_label(label) or label


def _plugins(labels: Iterable[str]) -> str:
    return ", ".join(_plugin(label) for label in labels)


def migrate_deployment(
    stdout: Any = None,  # noqa: ANN401 - a stream, or Django's OutputWrapper
    *,
    app_role: str | None = None,
    analyst_role: str | None = None,
) -> Report:
    """Migrates the database to this build, syncs the plugins and grants ``app_role`` its
    privileges (none if it's None). ``analyst_role`` is ``sync_plugins``'s."""
    report = Report()
    _check_iki()
    executor = MigrationExecutor(connection)
    applied = set(executor.recorder.applied_migrations())
    code = executor.loader.disk_migrations
    installed = set(plugin_apps())
    archive = _archive()
    unknown = sorted(k for k in applied if k not in code)

    gone = {k[0] for k in unknown if k[0] not in installed} | {
        a for a in archive if a not in installed and any(k[0] == a for k in applied)
    }
    purging = sorted(gone & {naming.app_label(name) for name in registry.purged()})
    removed = sorted(a for a in gone - set(purging) if a in archive or _could_be_plugin(a))
    newer = [k for k in unknown if k[0] not in installed and k[0] not in removed + purging]
    if newer:
        raise DeployError(
            f"A newer Iki migrated this database ({_names(newer)}), and this is Iki "
            f"{_version()}. Deploy that version or later, or restore the database from the backup "
            "taken before the upgrade."
        )
    if removed:
        raise DeployError(
            f"The database has the plugin(s) {_plugins(removed)}, but this build doesn't. "
            "Removing a plugin needs it detached, then purged (ADR 0092): keep it in the "
            "lockfile."
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

    not_atomic = [
        (m.app_label, m.name)
        for m, _ in executor.migration_plan(executor.loader.graph.leaf_nodes())
        if m.app_label in installed and not m.atomic
    ]
    if not_atomic:
        raise DeployError(
            f"{_names(not_atomic)} {'is' if len(not_atomic) == 1 else 'are'}n't atomic. A "
            "deploy runs the plugins' migrations in one transaction with the triggers guarding "
            "their tables (ADR 0134), so each must be: take out `atomic = False`."
        )

    _migrate_iki(stdout, installed)
    try:
        with audit.cli(), transaction.atomic():
            if purging:
                report.purged = _purge(purging, archive, applied)
            to_reverse = [k for k in unknown if k[0] in installed]
            if to_reverse:
                # A view stops a column it reads from being dropped. migrate drops them
                # before it runs (its pre_migrate signal), but a rollback runs first; the
                # sync makes them again, in this transaction.
                with connection.cursor() as cursor:
                    sync.drop_views(cursor)
                report.reversed = _roll_back(to_reverse, archive, applied)
            call_command(_Migrate(), interactive=False, verbosity=1, stdout=stdout)
            report.archived = _keep_copies(installed)
            report.synced = sync.sync(analyst_role)
            if app_role:
                report.granted = db_roles.grant(app_role)
    except Exception as error:
        error.add_note(KEPT_NOTHING)
        raise
    return report


def _migrate_iki(stdout: Any, installed: Iterable[str]) -> None:  # noqa: ANN401 - a stream
    """Iki's and Django's own migrations, before the plugins', each in a transaction of its
    own. A plugin's may depend on them, never the other way round."""
    plugins = set(installed)
    out = OutputWrapper(stdout or sys.stdout)

    def progress(
        action: str, migration: Migration | None = None, fake: bool | None = False, /
    ) -> None:
        if action == "apply_success" and migration is not None:
            out.write(f"  Applying {migration.app_label}.{migration.name}... OK")

    executor = MigrationExecutor(connection, progress)
    executor.loader.check_consistent_history(connection)
    conflicts = {
        app: names
        for app, names in executor.loader.detect_conflicts().items()
        if app not in plugins
    }
    if conflicts:
        raise DeployError(
            f"Conflicting migrations: {conflicts}. This build of Iki is broken: deploy another."
        )
    targets = [key for key in executor.loader.graph.leaf_nodes() if key[0] not in plugins]
    plan = executor.migration_plan(targets)
    if not plan:
        return
    out.write("Running Iki's own migrations:")
    # The schema the applied migrations made, as migrate gives its receivers.
    graph = executor.loader.graph
    state = executor.loader.project_state(
        [key for key in executor.loader.applied_migrations or () if key in graph.nodes]
    )
    # Its receivers drop the analysts' views, which sync_plugins makes again.
    emit_pre_migrate_signal(1, False, connection.alias, stdout=out, apps=state.apps, plan=plan)
    executor.migrate(targets, plan=plan, state=state)


def _settle(action: str, migration: Migration | None = None, fake: bool | None = False, /) -> None:
    """After each migration in the deploy's transaction, checks the deferred constraints its
    writes queued, as its own commit did: Postgres won't alter a table with checks pending,
    and a later migration may alter one an earlier one wrote to."""
    if action in ("apply_success", "unapply_success"):
        connection.check_constraints()


class _Migrate(django_migrate.Command):
    """Django's ``migrate``, inside the deploy's transaction."""

    def migration_progress_callback(
        self,
        action: str,
        migration: Migration | None = None,
        fake: bool = False,
    ) -> None:
        super().migration_progress_callback(action, migration, fake)
        _settle(action, migration, fake)


def _could_be_plugin(label: str) -> bool:
    """Whether an app the build doesn't have was a plugin rather than part of Iki or Django:
    a plugin's label is ``namespace__name``, which no app of Iki's or Django's is."""
    return naming.plugin_of_label(label) is not None


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
            try:
                package = self.migrations_module(app)[0] or f"iki_plugin_{app}.migrations"
            except LookupError:  # a purged plugin, which the build doesn't install
                package = f"iki_plugin_{app}.migrations"
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
    executor = MigrationExecutor(connection, _settle)
    executor.loader = _WithCopies(copies, connection)
    code = MigrationLoader(connection)
    targets = []
    for app in sorted({k[0] for k in to_reverse}):
        target = _last_applied(code, app, applied)
        if target is None:
            raise DeployError(
                f"Can't roll back {_plugin(app)}: none of this build's migrations for it have "
                "run here."
            )
        targets.append(target)
    plan = executor.migration_plan(targets)
    not_atomic = [(m.app_label, m.name) for m, _ in plan if not m.atomic]
    if not_atomic:
        raise DeployError(
            f"Can't roll back {_names(not_atomic)}: a deploy reverses the plugins' migrations "
            "in one transaction (ADR 0134), and this isn't atomic. Restore the database from "
            "the backup taken before it ran."
        )
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
                    blockers.append(f"{_plugin(migration.app_label)} {migration.name}: {problem}")
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


# --- Purging -------------------------------------------------------------------------------------


def _purge(
    labels: list[str], archive: dict[str, dict[Key, PluginMigration]], applied: set[Key]
) -> list[str]:
    """Deletes the data of plugins the lockfile purges (ADR 0092), in one transaction.

    Each must have a purge whose wait is over, with every entity tombstoned, and none of
    its data on legal hold; nothing still installed may depend on it. Then:

    - its tables go whole: values, history, lists, and fields it added to other types;
    - what its entities hold in tables they inherit from other plugins goes too (such as
      ``core_sequence`` for a DNA sequence), history included;
    - their unique values and identity hashes go;
    - its migrations are no longer recorded as applied, and its purge is done.

    It drops the tables rather than reversing the plugin's migrations: a reversal runs the
    plugin's own reverse code, which could change rows Iki keeps, such as the types its
    tombstones name. The entities' core rows, revisions and tombstones stay, as does the
    audit log. If anything can't be done, nothing is, and the error says why.
    """
    with transaction.atomic():
        purges = {label: _due(_plugin(label)) for label in labels}
        state = _state_of(labels, archive, applied)
        mine = [
            m
            for m in state.apps.get_models(include_auto_created=True)
            if m._meta.app_label in labels and m._meta.managed and not m._meta.proxy
        ]
        others = [
            m
            for m in state.apps.get_models(include_auto_created=True)
            if m._meta.app_label in set(plugin_apps())
            and m._meta.managed
            and not m._meta.proxy
            and any(f.column == "entity_id" for f in m._meta.local_concrete_fields)
        ]
        ids = Tombstone.objects.filter(purge__in=purges.values()).values("entity_id")
        out = []
        with connection.cursor() as cursor:
            sync.drop_views(cursor)  # sync_plugins makes the rest again
            tables = sorted(m._meta.db_table for m in mine)
            try:
                with transaction.atomic():
                    # Without CASCADE: if anything else refers to them, nothing changes.
                    cursor.execute(
                        sql.SQL("DROP TABLE {}")
                        .format(sql.SQL(", ").join(sql.Identifier(t) for t in tables))
                        .as_string(cursor.connection)
                    )
            except DatabaseError as error:
                raise DeployError(
                    f"Couldn't drop the tables of {_plugins(labels)}, so nothing was changed: "
                    f"{error}".strip()
                ) from None
            elsewhere = sum(_delete_rows(cursor, m._meta.db_table, ids) for m in others)
        UniqueValue.objects.filter(entity__in=ids).delete()
        Entity.objects.filter(pk__in=ids).update(identity_hash=None)
        MigrationRecorder.Migration.objects.filter(app__in=labels).delete()
        PluginState.objects.filter(name__in=[_plugin(label) for label in labels]).update(
            state=PluginState.State.PURGED
        )
        now = timezone.now()
        for label, scheduled in purges.items():
            name = _plugin(label)
            scheduled.done_at = now
            scheduled.save(update_fields=["done_at"])
            dropped = sorted(m._meta.db_table for m in mine if m._meta.app_label == label)
            entities = Tombstone.objects.filter(purge=scheduled).count()
            audit.record(
                "plugin.purged",
                target=scheduled,
                data={"plugin": name, "entities": entities, "tables": dropped},
            )
            out.append(
                f"{name}: dropped {len(dropped)} table(s); {entities} entities keep their "
                "tombstones"
            )
        if elsewhere:
            out.append(f"deleted {elsewhere} row(s) their entities had in other plugins' tables")
    return out


def _due(name: str) -> PluginPurge:
    """The purge of ``name`` the deploy may carry out now, or why it can't."""
    keep = f"Deploy with {name} detached until then."
    scheduled = (
        PluginPurge.objects.select_for_update()
        .filter(plugin=name, cancelled_at__isnull=True, done_at__isnull=True)
        .first()
    )
    if scheduled is None:
        raise DeployError(
            f"The lockfile purges {name}, but no purge of it was asked for. An Org Admin asks "
            f"for one on its page in Settings → Plugins, while it's detached. {keep}"
        )
    if scheduled.run_after > timezone.now():
        raise DeployError(
            f"{name}'s purge isn't due until {scheduled.run_after:%Y-%m-%d %H:%M} UTC, and "
            f"can be cancelled until then. {keep}"
        )
    entities = Entity.objects.filter(type__startswith=f"{name}.")
    in_projects = Q(pk__in=entities.values("owner_project")) | Q(
        pk__in=ProjectLink.objects.filter(entity__in=entities.values("pk")).values("project")
    )
    if holds.held_projects().filter(in_projects).exists():
        # Holds are confidential: the deploy's log doesn't say which project or matter.
        raise DeployError(
            f"Some of {name}'s data is on legal hold, so it can't be purged until the hold is "
            f"released. {keep}"
        )
    if scheduled.tombstoned_at is None:
        raise DeployError(
            f"{name}'s entities don't all have tombstones yet. Maintenance makes them within "
            f"the hour once the wait is over: deploy again after that. {keep}"
        )
    left = entities.exclude(tombstone__purge=scheduled).count()
    if left:
        raise DeployError(
            f"{left} of {name}'s entities have no tombstone from its purge, so nothing was "
            "changed. Was it reattached and used? Ask for a new purge."
        )
    return scheduled


def _state_of(
    labels: list[str], archive: dict[str, dict[Key, PluginMigration]], applied: set[Key]
) -> ProjectState:
    """The schema the purged plugins' kept migrations made, with everything else's."""
    mine = sorted(k for k in applied if k[0] in labels)
    missing = [k for k in mine if k not in archive.get(k[0], {})]
    if missing:
        raise DeployError(
            f"Can't purge: Iki kept no copy of {_names(missing)} (they ran before it kept "
            "copies), so it can't tell which tables are the plugin's. Restore its migrations "
            "to the build, as a detached plugin, and deploy once to keep copies."
        )
    loader = _WithCopies({k: archive[k[0]][k] for k in mine}, connection)
    needing = sorted(
        (str(child.key[0]), str(child.key[1]))
        for key in mine
        for child in loader.graph.node_map[key].children
        if child.key[0] not in labels and child.key in applied
    )
    if needing:
        raise DeployError(
            f"Can't purge {_plugins(labels)}: {_names(needing)} depend(s) on it. Purge those "
            "plugins too."
        )
    return loader.project_state(mine)


def _delete_rows(cursor: Any, table: str, ids: Any) -> int:  # noqa: ANN401 - Django's cursor, a queryset
    """Deletes the purged entities' rows from another plugin's table, its history too."""
    query, params = ids.query.sql_with_params()
    triggers = _append_only(cursor, table)
    for trigger in triggers:  # only the migrate Job may: history is append-only (ADR 0025)
        cursor.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}")
            .format(sql.Identifier(table), sql.Identifier(trigger))
            .as_string(cursor.connection)
        )
    cursor.execute(
        sql.SQL("DELETE FROM {} WHERE entity_id IN ({})")
        .format(sql.Identifier(table), sql.SQL(query))  # Django's SQL; values are parameters
        .as_string(cursor.connection),
        params,
    )
    deleted = int(cursor.rowcount)
    for trigger in triggers:
        cursor.execute(
            sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}")
            .format(sql.Identifier(table), sql.Identifier(trigger))
            .as_string(cursor.connection)
        )
    return deleted


def _append_only(cursor: Any, table: str) -> list[str]:  # noqa: ANN401 - Django's cursor
    cursor.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
        [table, AppendOnly.ROW_TRIGGER],
    )
    return [row[0] for row in cursor.fetchall()]


def _exists(cursor: Any, query: str, *names: str, params: list[Any] | None = None) -> bool:  # noqa: ANN401 - Django's cursor
    cursor.execute(
        sql.SQL(query).format(*(sql.Identifier(n) for n in names)).as_string(cursor.connection),
        params or [],
    )
    return cursor.fetchone() is not None


def _names(keys: Iterable[Key]) -> str:
    """Migrations by plugin and name, such as ``iki/protein 0002_structures``."""
    return ", ".join(f"{_plugin(app)} {name}" for app, name in keys)


def _check_iki() -> None:
    """Refuses plugins made for other Iki releases, before anything changes (ADR 0095)."""
    this = ".".join(map(str, releases.release(_version())))
    unfit = [
        f"{p.name} works with Iki {p.iki.strip()}"
        for p in registry.plugins().values()
        if p.iki and not releases.fits(p.iki, this)
    ]
    if unfit:
        raise DeployError(
            f"This is Iki {this}, and {'; '.join(unfit)}. Deploy a release of Iki they work "
            "with, or releases of them made for this one: nothing was migrated."
        )


def _version() -> str:
    from iki_server import __version__

    return __version__
