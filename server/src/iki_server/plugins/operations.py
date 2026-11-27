"""What plugin migrations are written in: the one module of Iki's they import (ADR 0296).

``iki plugin makemigrations`` writes a plugin's migrations with these names alone::

    from iki_server.plugins import operations as iki

so a Django upgrade changes this module, never the migrations labs have committed and
deployments have applied. Every name in ``__all__`` is API: its name and arguments never
change incompatibly, and ``iki_spec/migration_api.json`` lists it with the release it came
in. They are:

- Django's operations, fields, index, constraint and expressions, as the command writes
  them. Each translates its arguments to what this release's Django needs: most read like
  Django's today, but ``CreateModel`` takes ``table``, ``indexes`` and ``constraints`` and
  ``Q`` takes ``negated`` and ``connector``.
- Iki's own operations, which keep history as ADR 0033 says (``AppendOnly``,
  ``ConvertUnit``, ``RecordRename``, ``RenameEntityType``, the state operations of ADR 0125),
  and ``MigrateValues``, the one way a plugin migration changes stored values.
- Iki's checks of array items, and the core models plugin tables refer to (``ENTITY``,
  ``FILE``, ``USER``).

There's no ``RunPython`` or ``RunSQL``: a plugin migration that changes values gives each
record a revision and an audit event, which ``MigrateValues`` does and raw code wouldn't.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, TypedDict, TypeVar, Unpack

from django.contrib.postgres.fields import ArrayField as _ArrayField
from django.contrib.postgres.indexes import GinIndex as _GinIndex
from django.contrib.postgres.search import SearchVector as _SearchVector
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import ProjectState
from django.db.models import functions, lookups

from iki_server.entities import fields as entity_fields
from iki_server.entities.checks import (
    AllBetweenFloat,
    AllBetweenInt,
    AllLengthsBetween,
    AllMatch,
    IsPlainList,
    NoneMatch,
)

_F = TypeVar("_F", bound=Callable[..., Any])

#: Set for a transaction, it lets a migration change locked records' values (ADR 0125):
#: the triggers ``sync_plugins`` puts on records' tables let it through.
UNLOCKED_SETTING = "iki.unlocked"


def record_change(
    schema_editor: BaseDatabaseSchemaEditor,
    app_label: str,
    kind: str,
    name: str,
    to: str,
    *,
    part: str = "",
    backwards: bool = False,
    units: tuple[str, str] = ("", ""),
    conversion: tuple[float, float] | None = None,
) -> None:
    """Records a change a migration makes to values kept history holds (ADR 0148), in
    ``iki_history_change``, in the migration's transaction: signed by this deployment's
    signing key, if it has one yet, as an electronic signature is (ADR 0126)."""
    from django.utils import timezone

    from iki_server.bundles import keys
    from iki_server.core import crypto
    from iki_server.plugins.models import HistoryChange
    from iki_spec import naming

    scale, offset = conversion if conversion is not None else (None, None)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence('iki_history_change', 'id'))")
        (number,) = cursor.fetchone()
        # The signing key, read as it's kept: this runs before later migrations of the
        # signatures app may have.
        cursor.execute(
            "SELECT public, private_encrypted FROM iki_signing_key WHERE retired_at IS NULL"
        )
        signing = cursor.fetchone()
    change = HistoryChange(
        pk=number,
        plugin=naming.plugin_of_label(app_label) or app_label,
        backwards=backwards,
        kind=kind,
        part=part,
        name=name,
        to=to,
        from_unit=units[0],
        to_unit=units[1],
        scale=scale,
        offset=offset,
        made_at=timezone.now(),
    )
    key, signature = "", b""
    if signing is not None:
        private = keys.private_key(crypto.decrypt(signing[1]))
        if keys.public_text(private) != signing[0]:
            raise crypto.DecryptionError(
                "This deployment's signing key doesn't decrypt with IKI_ENCRYPTION_KEYS."
            )
        key, signature = signing[0], keys.sign(private, change.signed())
    schema_editor.execute(
        'INSERT INTO iki_history_change (id, plugin, backwards, kind, part, name, "to",'
        ' from_unit, to_unit, scale, "offset", made_at, key, signature)'
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [
            change.pk,
            change.plugin,
            backwards,
            kind,
            part,
            name,
            to,
            *units,
            scale,
            offset,
            change.made_at,
            key,
            signature,
        ],
    )


@contextmanager
def unlocked(schema_editor: BaseDatabaseSchemaEditor) -> Iterator[None]:
    schema_editor.execute("SELECT set_config(%s, 'on', true)", [UNLOCKED_SETTING])
    try:
        yield
    finally:
        schema_editor.execute("SELECT set_config(%s, '', true)", [UNLOCKED_SETTING])


def same_value(left: Any, right: Any) -> bool:  # noqa: ANN401 - JSON
    """Compare stored values, distinguishing JSON booleans from numbers at every depth."""
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_value(v, right[k]) for k, v in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_value(a, b) for a, b in zip(left, right, strict=True)
        )
    return isinstance(left, bool) == isinstance(right, bool) and bool(left == right)


class MigrateValues(migrations.operations.base.Operation):
    """Backfill a plugin table. The callback reads a historical model instance and
    returns the column values to set, keyed by field name (foreign keys take IDs).
    Each changed record gets its own revision, history snapshot and audit event.
    Define callbacks in the migration file so the archived migration is self-contained.
    """

    def __init__(
        self,
        model_name: str,
        forwards: Callable[[Any], Mapping[str, Any]],
        *,
        version: str,
        migration: str,
        reason: str,
        backwards: Callable[[Any], Mapping[str, Any]] | None = None,
    ) -> None:
        if not all(value.strip() for value in (version, migration, reason)):
            raise ValueError(
                "MigrateValues requires the plugin version, migration name and reason."
            )
        self.model_name = model_name
        self.forwards = forwards
        self.backwards = backwards
        self.version = version
        self.migration = migration
        self.reason = reason.strip()

    @property
    def reversible(self) -> bool:  # type: ignore[override]  # Like RunPython, depends on its callback.
        return self.backwards is not None

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._apply(app_label, schema_editor, to_state, self.forwards, "Applying")

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        if self.backwards is None:
            raise ValueError(f"{self.migration} has no reverse backfill.")
        self._apply(app_label, schema_editor, from_state, self.backwards, "Reversing")

    def _apply(
        self,
        app_label: str,
        editor: BaseDatabaseSchemaEditor,
        state: ProjectState,
        values_for: Callable[[Any], Mapping[str, Any]],
        direction: str,
    ) -> None:
        from django.db.models.functions import Now

        from iki_server.entities.fields import StateField
        from iki_spec import naming

        alias = editor.connection.alias
        live = state.apps.get_model(app_label, self.model_name)
        entities = state.apps.get_model("iki_entities", "Entity").objects.using(alias)
        revisions = state.apps.get_model("iki_entities", "Revision").objects.using(alias)
        events = state.apps.get_model("iki_audit", "AuditEvent").objects.using(alias)
        columns = {
            f.name: f
            for f in live._meta.concrete_fields
            if naming.plugin_of_label(f.model._meta.app_label)
            and not f.primary_key
            and not isinstance(f, StateField)
        }
        plugin = naming.plugin_of_label(app_label) or app_label
        reason = f"{direction} {plugin} {self.version} {self.migration}: {self.reason}"
        rows = live.objects.using(alias)
        # Record edits lock the entity before touching any of its tables. Read each
        # part under the same lock so both the callback and its snapshot see that edit.
        records = entities.filter(pk__in=rows.values("pk")).select_for_update().order_by("pk")
        for entity in records.iterator():
            row = rows.get(pk=entity.pk)
            # A callback may edit a JSON object in place before returning it.
            before = {name: deepcopy(getattr(row, f.attname)) for name, f in columns.items()}
            updates: dict[Any, dict[str, Any]] = {}
            files = set()
            for name, value in values_for(row).items():
                if name not in columns:
                    raise ValueError(f"{live.__name__}.{name} is not a backfillable column.")
                field = columns[name]
                value = (field.target_field if field.is_relation else field).to_python(value)
                if not same_value(before[name], value):
                    updates.setdefault(field.model, {})[name] = value
                    if (
                        value
                        and field.is_relation
                        and field.related_model._meta.label_lower == "iki_files.file"
                    ):
                        files.add(value)
            if not updates:
                continue
            number = entity.revision + 1
            changed = sorted(name for part in updates.values() for name in part)
            for owner, values in updates.items():
                history = state.apps.get_model(
                    owner._meta.app_label, naming.history_model(owner.__name__)
                )
                kept = history.objects.using(alias)
                snapshot = kept.filter(entity_id=entity.pk).order_by("-revision").values().first()
                if snapshot is None:
                    raise ValueError(f"{entity.hrid} has no {owner.__name__} history to migrate.")
                owner.objects.using(alias).filter(pk=entity.pk).update(**values)
                kept.create(**{**snapshot, **values, "revision": number})
            revisions.create(
                entity_id=entity.pk,
                number=number,
                kind="migrated",
                changed_fields=changed,
                via="system",
                reason=reason,
            )
            if files:
                uses = state.apps.get_model("iki_files", "FileUse")
                uses.objects.using(alias).bulk_create(
                    [uses(file_id=f, entity_id=entity.pk, revision=number) for f in sorted(files)],
                    ignore_conflicts=True,
                )
            entities.filter(pk=entity.pk).update(revision=number, updated_at=Now())
            events.create(
                action="entity.migrated",
                via="system",
                target_type="entity",
                target_id=str(entity.pk),
                target_label=entity.hrid,
                project_id=entity.owner_project_id,
                data={"revision": number, "changed_fields": changed, "reason": reason},
            )
        editor.connection.check_constraints()

    def describe(self) -> str:
        return f"Migrate {self.model_name} values for {self.version}: {self.reason}"


class _StateOperation(migrations.operations.base.Operation):
    """What the operations on state fields (ADR 0125) share: moving records from one state
    to another as revisions, each with its history row, and keeping ``locked_by``."""

    reversible = True
    model_name: str

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def _models(self, app_label: str, state: ProjectState) -> tuple[Any, Any]:
        model = state.apps.get_model(app_label, self.model_name)
        history = state.apps.get_model(app_label, f"{model.__name__}_History")
        return model, history

    @staticmethod
    def _move(
        schema_editor: BaseDatabaseSchemaEditor,
        models: tuple[Any, Any],
        field: str,
        moving: str,
        params: Sequence[Any],
        *,
        cleared: str | None = None,
    ) -> None:
        """Sets ``field`` of the records ``moving`` selects, as ``(entity_id, old, new,
        reason)``: a revision each, by no one, via the system, with its history row: the
        record's latest, with the new state (and ``cleared``, a field going, empty)."""
        model, history = models
        q = schema_editor.quote_name
        table, hist = q(model._meta.db_table), q(history._meta.db_table)
        column = q(model._meta.get_field(field).column)
        live = {f.name: f.column for f in model._meta.local_concrete_fields}
        kept = [
            (f.name, f.column)
            for f in history._meta.local_concrete_fields
            if f.name not in ("entity_id", "revision")
        ]

        def value(name: str, col: str, source: str | None) -> str:
            if name == field:
                return "m.new"
            if name == cleared:
                return "NULL"
            if source == "h":
                return f"h.{q(col)}"
            return f"t.{q(live[name])}" if name in live else "NULL"

        names = "".join(f", {q(c)}" for _, c in kept)
        with unlocked(schema_editor):
            schema_editor.execute(
                "CREATE TEMPORARY TABLE iki_moving (entity_id uuid PRIMARY KEY, old text,"
                " new text, reason text) ON COMMIT DROP"
            )
            schema_editor.execute(f"INSERT INTO iki_moving {moving}", params)
            # The database stamps history_seen when present. Archived operations can
            # also run against the earlier revision table during an initial migration.
            schema_editor.execute(
                "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, via,"
                " client, reason, state_field, state_from, state_to, created_at)"
                " SELECT uuidv7(), e.id, e.revision + 1, 'state_changed', ARRAY[%s], 'system',"
                " '', m.reason, %s, m.old, m.new, now()"
                " FROM iki_entity e JOIN iki_moving m ON m.entity_id = e.id",
                [field, field],
            )
            schema_editor.execute(
                "INSERT INTO iki_audit_event (id, occurred_at, action, actor_label, via,"
                " target_type, target_id, target_label, project_id, data, request_id)"
                " SELECT uuidv7(), now(), 'entity.state_changed', '', 'system', 'entity',"
                " e.id::text, e.hrid, e.owner_project_id, jsonb_build_object("
                " 'field', %s::text, 'from', m.old, 'to', m.new, 'revision', e.revision + 1,"
                " 'reason', m.reason), '' FROM iki_entity e"
                " JOIN iki_moving m ON m.entity_id = e.id",
                [field],
            )
            schema_editor.execute(
                "UPDATE iki_entity e SET revision = e.revision + 1, updated_at = now()"
                " FROM iki_moving m WHERE m.entity_id = e.id"
            )
            schema_editor.execute(
                f"UPDATE {table} t SET {column} = m.new FROM iki_moving m"  # noqa: S608 - quoted names
                " WHERE m.entity_id = t.entity_id"
            )
            # Each revision's history row: the record's latest row, changed; or, for a record
            # this table has none of yet, its values here.
            latest = (
                f"FROM iki_moving m JOIN iki_entity e ON e.id = m.entity_id JOIN LATERAL ("  # noqa: S608 - quoted names
                f"SELECT * FROM {hist} x WHERE x.entity_id = m.entity_id"
                " ORDER BY x.revision DESC LIMIT 1) h ON true"
            )
            selected = "".join(f", {value(n, c, 'h')}" for n, c in kept)
            schema_editor.execute(
                f"INSERT INTO {hist} (entity_id, revision{names})"
                f" SELECT m.entity_id, e.revision{selected} {latest}"
            )
            selected = "".join(f", {value(n, c, 't')}" for n, c in kept)
            schema_editor.execute(
                f"INSERT INTO {hist} (entity_id, revision{names})"  # noqa: S608 - quoted names
                f" SELECT m.entity_id, e.revision{selected}"
                f" FROM iki_moving m JOIN iki_entity e ON e.id = m.entity_id"
                f" JOIN {table} t ON t.entity_id = m.entity_id"
                f" WHERE NOT EXISTS (SELECT 1 FROM {hist} x WHERE x.entity_id = m.entity_id"
                "  AND x.revision < e.revision)"
            )
            schema_editor.execute("DROP TABLE iki_moving")

    @staticmethod
    def _settle(schema_editor: BaseDatabaseSchemaEditor) -> None:
        """Runs the foreign key checks its changes queued. Updating a row twice in one
        transaction queues its deferred checks, and PostgreSQL won't alter a table with
        checks pending, as the migration's next operation may."""
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")

    @staticmethod
    def _lock(
        schema_editor: BaseDatabaseSchemaEditor,
        model: Any,  # noqa: ANN401 - a model in a migration's state
        field: str,
        locking: Sequence[str],
    ) -> None:
        """Whether ``field`` locks each record, in its ``locked_by``, as ``locking`` says."""
        table = schema_editor.quote_name(model._meta.db_table)
        column = schema_editor.quote_name(model._meta.get_field(field).column)
        schema_editor.execute(
            "UPDATE iki_entity e SET locked_by = CASE"  # noqa: S608 - quoted names
            f" WHEN t.{column} = ANY(%s) THEN array_append(array_remove(e.locked_by, %s), %s)"
            " ELSE array_remove(e.locked_by, %s) END"
            f" FROM {table} t WHERE t.entity_id = e.id"
            f" AND (%s = ANY(e.locked_by)) IS DISTINCT FROM (t.{column} = ANY(%s))",
            [list(locking), field, field, field, field, list(locking)],
        )

    @staticmethod
    def _unlock(
        schema_editor: BaseDatabaseSchemaEditor,
        model: Any,  # noqa: ANN401 - a model in a migration's state
        field: str,
    ) -> None:
        table = schema_editor.quote_name(model._meta.db_table)
        schema_editor.execute(
            "UPDATE iki_entity e SET locked_by = array_remove(e.locked_by, %s)"  # noqa: S608 - a quoted name
            f" FROM {table} t WHERE t.entity_id = e.id AND %s = ANY(e.locked_by)",
            [field, field],
        )

    #: Records whose latest revision is a migration's move of a field (the first
    #: parameter) from one state (the second) to another (the third).
    MOVED_BACK = (
        " AND EXISTS (SELECT 1 FROM iki_revision r JOIN iki_entity e ON e.id = r.entity_id"
        " WHERE r.entity_id = t.entity_id AND r.number = e.revision"
        " AND r.kind = 'state_changed' AND r.via = 'system' AND r.user_id IS NULL"
        " AND r.state_field = %s AND r.state_from = %s AND r.state_to = %s)"
    )


class SyncStates(_StateOperation):
    """Keeps records in step with a state field's states (ADR 0125) when a plugin changes them.

    Records in a state the plugin removed move to the state it says (``moved``),
    each with a revision by no one, via the system, and every record's
    ``locked_by`` names the field while it's in a state that locks now
    (``locking``). The reverse migration moves them back, as revisions too, and
    locks them as ``locking_before`` did.
    """

    def __init__(
        self,
        model_name: str,
        field: str,
        moved: Mapping[str, str],
        locking: Sequence[str],
        locking_before: Sequence[str],
    ) -> None:
        self.model_name = model_name
        self.field = field
        self.moved = dict(moved)
        self.locking = list(locking)
        self.locking_before = list(locking_before)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        models = self._models(app_label, to_state)
        for old, new in sorted(self.moved.items()):
            self._states(schema_editor, models, old, new, back=False)
        self._lock(schema_editor, models[0], self.field, self.locking)
        self._settle(schema_editor)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        models = self._models(app_label, to_state)
        for old, new in sorted(self.moved.items()):
            self._states(schema_editor, models, new, old, back=True)
        self._lock(schema_editor, models[0], self.field, self.locking_before)
        self._settle(schema_editor)

    def _states(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        models: tuple[Any, Any],
        old: str,
        new: str,
        *,
        back: bool,
    ) -> None:
        """Moves the records in ``old`` to ``new``. Backwards, only those whose latest
        revision is the move forwards."""
        table = schema_editor.quote_name(models[0]._meta.db_table)
        column = schema_editor.quote_name(models[0]._meta.get_field(self.field).column)
        reason = (
            f"Its plugin's migration moved it back from {old} to {new}."
            if back
            else f"Its plugin removed the state {old}: its migration moved it to {new}."
        )
        moving = f"SELECT t.entity_id, %s, %s, %s FROM {table} t WHERE t.{column} = %s"  # noqa: S608 - quoted names
        params: list[Any] = [old, new, reason, old]
        if back:
            moving += self.MOVED_BACK
            params += [self.field, new, old]
        self._move(schema_editor, models, self.field, moving, params)

    def describe(self) -> str:
        moves = ", ".join(f"{a} to {b}" for a, b in sorted(self.moved.items()))
        return f"Keep {self.model_name}'s records in the states of {self.field}" + (
            f", moving them from {moves}" if moves else ""
        )

    @property
    def migration_name_fragment(self) -> str:
        return f"states_{self.model_name.lower()}_{self.field}"


class RetireStateField(_StateOperation):
    """Before a plugin removes a state field (ADR 0125): each record's state in it moves to
    another of its type's state fields, ``moved_to`` (``{"field": …, "states": {old:
    new}}``), as a revision of that field; or, without one, the migration is refused while
    any record has the field. The field no longer locks anything; the field it moved to
    locks as its states do (``locking``).

    Backwards, after the field is back, each record's state in it is its latest in its
    history, and the move to the other field is undone where it's still the latest
    revision. ``locking_before``: the removed field's states that locked."""

    def __init__(
        self,
        model_name: str,
        field: str,
        moved_to: Mapping[str, Any] | None,
        locking: Sequence[str],
        locking_before: Sequence[str],
    ) -> None:
        self.model_name = model_name
        self.field = field
        self.moved_to = (
            {"field": moved_to["field"], "states": dict(moved_to["states"])} if moved_to else None
        )
        self.locking = list(locking)
        self.locking_before = list(locking_before)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        models = self._models(app_label, from_state)
        model = models[0]
        table = schema_editor.quote_name(model._meta.db_table)
        if self.moved_to is None:
            with schema_editor.connection.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 - a quoted name
                (count,) = cursor.fetchone()
            if count:
                from django.core.management.base import CommandError

                raise CommandError(
                    f"{app_label}.{self.model_name}'s state field `{self.field}` is removed, but "
                    f"{count} record{'s are' if count != 1 else ' is'} in its states, which "
                    "would be lost (ADR 0125).\n"
                    "  Give it `moved_to:` another of the type's state fields, with the state "
                    "each of its states moves to there."
                )
            return
        target = self.moved_to["field"]
        column = schema_editor.quote_name(model._meta.get_field(self.field).column)
        into = schema_editor.quote_name(model._meta.get_field(target).column)
        for old, new in sorted(self.moved_to["states"].items()):
            reason = (
                f"Its plugin removed the state field {self.field}: its migration moved its "
                f"{old} to {target} {new}."
            )
            self._move(
                schema_editor,
                models,
                target,
                f"SELECT t.entity_id, t.{into}, %s, %s FROM {table} t"  # noqa: S608 - quoted names
                f" WHERE t.{column} = %s AND t.{into} IS DISTINCT FROM %s",
                [new, reason, old, new],
                cleared=self.field,
            )
        self._unlock(schema_editor, model, self.field)
        self._lock(schema_editor, model, target, self.locking)
        self._settle(schema_editor)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        models = self._models(app_label, to_state)
        model, history = models
        q = schema_editor.quote_name
        table, hist = q(model._meta.db_table), q(history._meta.db_table)
        column = q(model._meta.get_field(self.field).column)
        kept = q(history._meta.get_field(self.field).column)
        with unlocked(schema_editor):
            schema_editor.execute(
                f"UPDATE {table} t SET {column} = h.{kept} FROM ("  # noqa: S608 - quoted names
                f" SELECT DISTINCT ON (x.entity_id) x.entity_id, x.{kept} FROM {hist} x"
                f" WHERE x.{kept} IS NOT NULL ORDER BY x.entity_id, x.revision DESC) h"
                " WHERE h.entity_id = t.entity_id"
            )
        if self.moved_to is not None:
            target = self.moved_to["field"]
            into = q(model._meta.get_field(target).column)
            self._move(
                schema_editor,
                models,
                target,
                f"SELECT t.entity_id, t.{into}, r.state_from,"  # noqa: S608 - quoted names
                " 'Its plugin''s migration put back the state field ' || %s || '.'"
                f" FROM {table} t JOIN iki_entity e ON e.id = t.entity_id"
                " JOIN iki_revision r ON r.entity_id = e.id AND r.number = e.revision"
                " WHERE r.kind = 'state_changed' AND r.via = 'system' AND r.user_id IS NULL"
                " AND r.state_field = %s AND r.reason LIKE %s",
                [self.field, target, f"Its plugin removed the state field {self.field}:%"],
            )
            self._lock(schema_editor, model, target, self.locking)
        self._lock(schema_editor, model, self.field, self.locking_before)
        self._settle(schema_editor)

    def describe(self) -> str:
        where = f", moving its records' states to {self.moved_to['field']}" if self.moved_to else ""
        return f"Retire {self.model_name}'s state field {self.field}{where}"

    @property
    def migration_name_fragment(self) -> str:
        return f"retire_{self.model_name.lower()}_{self.field}"


class RenameStateField(_StateOperation):
    """After a plugin renames a state field (ADR 0125): records it locks name it anew in
    ``locked_by``. Their earlier state changes keep the name it had then, as revisions'
    changed fields do: revisions are append-only, and history isn't rewritten."""

    def __init__(self, model_name: str, old_name: str, new_name: str) -> None:
        self.model_name = model_name
        self.old_name = old_name
        self.new_name = new_name

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._rename(app_label, schema_editor, to_state, self.old_name, self.new_name)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._rename(app_label, schema_editor, from_state, self.new_name, self.old_name)

    def _rename(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        state: ProjectState,
        old: str,
        new: str,
    ) -> None:
        model = state.apps.get_model(app_label, self.model_name)
        table = schema_editor.quote_name(model._meta.db_table)
        schema_editor.execute(
            "UPDATE iki_entity e SET locked_by = array_replace(e.locked_by, %s, %s)"  # noqa: S608 - a quoted name
            f" FROM {table} t WHERE t.entity_id = e.id AND %s = ANY(e.locked_by)",
            [old, new, old],
        )
        self._settle(schema_editor)

    def describe(self) -> str:
        return f"Rename {self.model_name}'s state field {self.old_name} to {self.new_name}"

    @property
    def migration_name_fragment(self) -> str:
        return f"rename_states_{self.model_name.lower()}_{self.new_name}"


class RecordRename(migrations.operations.base.Operation):
    """Records, after a history table's field is renamed, that it was (ADR 0148): a
    signature made before reads its record's history by the field's new name. Django's
    ``RenameField`` renames the column; this only records it, forwards and back."""

    reversible = True

    def __init__(self, model_name: str, old_name: str, new_name: str, part: str) -> None:
        self.model_name = model_name
        self.old_name = old_name
        self.new_name = new_name
        #: The part of a type the table holds, as its shape names it: ``acme/lab.Sample``.
        self.part = part

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        record_change(
            schema_editor, app_label, "rename", self.old_name, self.new_name, part=self.part
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        record_change(
            schema_editor,
            app_label,
            "rename",
            self.new_name,
            self.old_name,
            part=self.part,
            backwards=True,
        )

    def describe(self) -> str:
        return f"Record that {self.model_name}.{self.old_name} is now {self.new_name}"

    @property
    def migration_name_fragment(self) -> str:
        return f"record_rename_{self.model_name.lower()}_{self.new_name}"


class ConvertUnit(migrations.operations.base.Operation):
    """Converts a quantity's stored values when its canonical unit changes.

    New values are ``old * scale + offset``, which covers every UCUM
    conversion except the logarithmic ones (such as ``[pH]``). The
    reverse migration converts back.
    """

    reversible = True

    def __init__(
        self,
        model_name: str,
        name: str,
        from_unit: str,
        to_unit: str,
        scale: float,
        offset: float = 0.0,
        part: str = "",
    ) -> None:
        self.model_name = model_name
        self.name = name
        self.from_unit = from_unit
        self.to_unit = to_unit
        self.scale = scale
        self.offset = offset
        #: For a history table: the part of a type it holds, as its shape names it
        #: (``acme/lab.Sample``), so the conversion is recorded for signatures (ADR 0148).
        self.part = part

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._convert(
            app_label,
            schema_editor,
            to_state,
            (self.from_unit, self.to_unit),
            self.scale,
            self.offset,
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._convert(
            app_label,
            schema_editor,
            to_state,
            (self.to_unit, self.from_unit),
            1 / self.scale,
            -self.offset / self.scale,
            backwards=True,
        )

    def _convert(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        state: ProjectState,
        units: tuple[str, str],
        scale: float,
        offset: float,
        *,
        backwards: bool = False,
    ) -> None:
        if self.part:
            record_change(
                schema_editor,
                app_label,
                "convert",
                self.name,
                self.name,
                part=self.part,
                backwards=backwards,
                units=units,
                conversion=(scale, offset),
            )
        model = state.apps.get_model(app_label, self.model_name)
        field: Any = model._meta.get_field(self.name)
        table, column = (schema_editor.quote_name(n) for n in (model._meta.db_table, field.column))
        history = getattr(model, "APPEND_ONLY", False) or model.__name__.endswith("_History")
        if history:
            # A recorded change of units changes the column's representation. PostgreSQL
            # rewrites it with its append-only guards installed throughout (ADR 0148).
            expression = f"{column} * %s + %s"
            array = field.get_internal_type() == "ArrayField"
            if array:
                # ALTER TYPE's USING expression cannot itself contain a subquery.
                schema_editor.execute("""
                    CREATE FUNCTION pg_temp.iki_convert_quantities(double precision[],
                        double precision, double precision) RETURNS double precision[]
                    LANGUAGE SQL IMMUTABLE STRICT AS $$
                    SELECT ARRAY(SELECT x * $2 + $3 FROM unnest($1) WITH ORDINALITY AS v(x, i)
                                 ORDER BY i) $$
                """)
                expression = f"pg_temp.iki_convert_quantities({column}, %s, %s)"
            schema_editor.connection.check_constraints()
            column_type = field.db_type(schema_editor.connection)
            schema_editor.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {column_type} "
                f"USING ({expression})",
                [scale, offset],
            )
            if array:
                schema_editor.execute(
                    "DROP FUNCTION pg_temp.iki_convert_quantities("
                    "double precision[], double precision, double precision)"
                )
            return
        if field.get_internal_type() == "ArrayField":
            value = (
                f"ARRAY(SELECT item * %s + %s FROM unnest({column}) "  # noqa: S608 - a quoted name
                "WITH ORDINALITY AS u(item, i) ORDER BY i)"
            )
        else:
            value = f"{column} * %s + %s"
        # The same values, so locked records (ADR 0125) take them too.
        with unlocked(schema_editor):
            schema_editor.execute(
                f"UPDATE {table} SET {column} = {value} WHERE {column} IS NOT NULL",  # noqa: S608 - quoted names
                [scale, offset],
            )
        # Updating a row twice in one transaction queues its deferred foreign key checks,
        # and PostgreSQL won't alter a table with checks pending: run them now.
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")

    def describe(self) -> str:
        return f"Convert {self.model_name}.{self.name} from {self.from_unit} to {self.to_unit}"

    @property
    def migration_name_fragment(self) -> str:
        return f"convert_{self.model_name.lower()}_{self.name}"


class RenameEntityType(migrations.operations.base.Operation):
    """Renames a type in ``iki_entity.type`` when a plugin renames its model."""

    reversible = True

    def __init__(self, old_name: str, new_name: str) -> None:
        self.old_name = old_name
        self.new_name = new_name

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._rename(app_label, schema_editor, self.old_name, self.new_name)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._rename(app_label, schema_editor, self.new_name, self.old_name, backwards=True)

    @staticmethod
    def _rename(
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        old: str,
        new: str,
        *,
        backwards: bool = False,
    ) -> None:
        # Signatures name the type they signed (ADR 0148).
        record_change(schema_editor, app_label, "retype", old, new, backwards=backwards)
        schema_editor.execute("UPDATE iki_entity SET type = %s WHERE type = %s", [new, old])

    def describe(self) -> str:
        return f"Rename the entity type {self.old_name} to {self.new_name}"

    @property
    def migration_name_fragment(self) -> str:
        return f"rename_type_{self.new_name.split('.')[-1].lower()}"


class _Django:
    """One of Django's operations under Iki's name and arguments."""

    @classmethod
    def of(cls, operation: migrations.operations.base.Operation) -> Any:  # noqa: ANN401 - an operation of this class
        _, args, kwargs = operation.deconstruct()
        return cls(*args, **kwargs)


class AddField(_Django, migrations.AddField):
    """Adds a column, where ``sync_plugins`` may later put column guards.

    PostgreSQL refuses to drop a column named in an UPDATE OF trigger. Reversing this
    operation first drops the affected Iki guards in the same migration transaction.
    It consults the database, since even a later, migration-free manifest can add a rule.
    """

    def __init__(
        self,
        model_name: str,
        name: str,
        field: models.Field[Any, Any],
        preserve_default: bool = True,
    ) -> None:
        super().__init__(model_name, name, field, preserve_default)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        model = from_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        field = model._meta.get_field(self.name)
        schema_editor.execute(
            """
            DO $$
            DECLARE guard record;
            BEGIN
                FOR guard IN
                    SELECT t.tgname, t.tgrelid::regclass AS relation
                    FROM pg_trigger t JOIN pg_attribute a ON a.attrelid = t.tgrelid
                    WHERE t.tgrelid = %s::regclass AND a.attname = %s
                      AND a.attnum = ANY(t.tgattr) AND NOT t.tgisinternal
                      AND (starts_with(t.tgname, 'iki_rule__')
                           OR starts_with(t.tgname, 'iki_unique__'))
                    ORDER BY t.tgname
                LOOP
                    EXECUTE format('DROP TRIGGER %%I ON %%s', guard.tgname, guard.relation);
                END LOOP;
            END $$;
            """,
            [schema_editor.quote_name(model._meta.db_table), field.column],
        )
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class AppendOnly(migrations.operations.base.Operation):
    """Makes a history table append-only for every role, its owner included.

    Triggers refuse UPDATE, DELETE and TRUNCATE. The app's role also lacks
    those privileges (``APPEND_ONLY`` models, ADR 0025); the triggers hold
    even for the migrator, outside a migration.
    """

    reversible = True
    ROW_TRIGGER = "iki_history_no_change"
    TRUNCATE_TRIGGER = "iki_history_no_truncate"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, schema_editor, to_state)
        schema_editor.execute(
            f"CREATE TRIGGER {self.ROW_TRIGGER} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION iki_refuse_history_change()"
        )
        schema_editor.execute(
            f"CREATE TRIGGER {self.TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION iki_refuse_history_change()"
        )

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, schema_editor, from_state)
        for trigger in (self.ROW_TRIGGER, self.TRUNCATE_TRIGGER):
            schema_editor.execute(f"DROP TRIGGER {trigger} ON {table}")

    def _table(
        self, app_label: str, schema_editor: BaseDatabaseSchemaEditor, state: ProjectState
    ) -> str:
        model = state.apps.get_model(app_label, self.model_name)
        return str(schema_editor.quote_name(model._meta.db_table))

    def describe(self) -> str:
        return f"Make {self.model_name} append-only"

    @property
    def migration_name_fragment(self) -> str:
        return f"append_only_{self.model_name.lower()}"


# --- Django's operations, as plugin migrations are written in them (ADR 0296) -------------------
#
# Each is Django's, with Iki's arguments. ``of`` makes one from the operation Django's
# autodetector made, for the writer; ``deconstruct`` gives Iki's arguments back, so a
# migration read from its file writes the same file again.


class Migration(migrations.Migration):
    """A plugin's migration: ``class Migration(iki.Migration)``."""


class CreateModel(_Django, migrations.CreateModel):
    """A table: its model's fields, its table's name, and its indexes and constraints."""

    def __init__(
        self,
        name: str,
        fields: Sequence[tuple[str, models.Field[Any, Any]]],
        *,
        table: str,
        indexes: Sequence[models.Index] = (),
        constraints: Sequence[models.BaseConstraint] = (),
        bases: Sequence[str] = (),
    ) -> None:
        options: dict[str, Any] = {"db_table": table}
        if indexes:
            options["indexes"] = list(indexes)
        if constraints:
            options["constraints"] = list(constraints)
        super().__init__(name, list(fields), options=options, bases=tuple(bases) or None)

    @classmethod
    def of(cls, operation: migrations.operations.base.Operation) -> CreateModel:
        assert isinstance(operation, migrations.CreateModel)  # noqa: S101 - the writer's lookup
        options = dict(operation.options)
        unknown = sorted(set(options) - {"db_table", "indexes", "constraints"})
        if unknown or "db_table" not in options:
            raise ValueError(
                f"{operation.name} has the options {unknown or 'without db_table'}, which "
                "Iki's CreateModel doesn't take."
            )
        if operation.managers and operation.managers != [("objects", models.Manager())]:
            raise ValueError(
                f"{operation.name} has managers, which Iki's CreateModel doesn't take."
            )
        bases = () if operation.bases in (None, (models.Model,)) else tuple(operation.bases or ())
        if not all(isinstance(b, str) for b in bases):
            raise ValueError(f"{operation.name}'s bases aren't models' labels: {bases}.")
        return cls(
            operation.name,
            operation.fields,
            table=options["db_table"],
            indexes=options.get("indexes", ()),
            constraints=options.get("constraints", ()),
            bases=[str(b) for b in bases],
        )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "name": self.name,
            "fields": self.fields,
            "table": self.options["db_table"],
        }
        for option in ("indexes", "constraints"):
            if self.options.get(option):
                kwargs[option] = self.options[option]
        if self.bases and self.bases != (models.Model,):
            kwargs["bases"] = self.bases
        return (self.__class__.__qualname__, [], kwargs)


class DeleteModel(_Django, migrations.DeleteModel):
    def __init__(self, name: str) -> None:
        super().__init__(name)


class RenameModel(_Django, migrations.RenameModel):
    def __init__(self, old_name: str, new_name: str) -> None:
        super().__init__(old_name, new_name)


class AlterModelTable(_Django, migrations.AlterModelTable):
    def __init__(self, name: str, table: str) -> None:
        super().__init__(name, table)


class RemoveField(_Django, migrations.RemoveField):
    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)


class AlterField(_Django, migrations.AlterField):
    def __init__(
        self,
        model_name: str,
        name: str,
        field: models.Field[Any, Any],
        preserve_default: bool = True,
    ) -> None:
        super().__init__(model_name, name, field, preserve_default)


class RenameField(_Django, migrations.RenameField):
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None:
        super().__init__(model_name, old_name, new_name)


class AddIndex(_Django, migrations.AddIndex):
    def __init__(self, model_name: str, index: models.Index) -> None:
        super().__init__(model_name, index)


class RemoveIndex(_Django, migrations.RemoveIndex):
    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)


class RenameIndex(_Django, migrations.RenameIndex):
    def __init__(
        self,
        model_name: str,
        new_name: str,
        old_name: str | None = None,
        old_fields: Sequence[str] | None = None,
    ) -> None:
        super().__init__(model_name, new_name, old_name, old_fields)


class AddConstraint(_Django, migrations.AddConstraint):
    def __init__(self, model_name: str, constraint: models.BaseConstraint) -> None:
        super().__init__(model_name, constraint)


class RemoveConstraint(_Django, migrations.RemoveConstraint):
    def __init__(self, model_name: str, name: str) -> None:
        super().__init__(model_name, name)


#: Iki's operation for each of Django's the autodetector makes, for the writer.
OF_DJANGO: dict[type[migrations.operations.base.Operation], type[_Django]] = {
    migrations.CreateModel: CreateModel,
    migrations.DeleteModel: DeleteModel,
    migrations.RenameModel: RenameModel,
    migrations.AlterModelTable: AlterModelTable,
    migrations.AddField: AddField,
    migrations.RemoveField: RemoveField,
    migrations.AlterField: AlterField,
    migrations.RenameField: RenameField,
    migrations.AddIndex: AddIndex,
    migrations.RemoveIndex: RemoveIndex,
    migrations.RenameIndex: RenameIndex,
    migrations.AddConstraint: AddConstraint,
    migrations.RemoveConstraint: RemoveConstraint,
}


# --- Fields, the index, the constraint and expressions ------------------------------------------
#
# Each makes Django's object, which is what models and migration states compare. The writer
# writes one with the name ``WRITTEN_AS`` gives its class, renaming the arguments Django's
# ``deconstruct`` gives to Iki's and leaving out those the wrapper itself implies.


@dataclass(frozen=True)
class WrittenAs:
    name: str
    #: Django's name for an argument, and Iki's.
    renamed: Mapping[str, str] = field(default_factory=dict)
    #: Arguments the wrapper gives Django itself, so a migration doesn't.
    implied: Mapping[str, Any] = field(default_factory=dict)


#: How each of Django's classes is written: by the name of its wrapper.
WRITTEN_AS: dict[type, WrittenAs] = {}

_UNSET: Any = models.NOT_PROVIDED


def _written_as(django: type, **how: Any) -> Callable[[_F], _F]:  # noqa: ANN401 - WrittenAs's fields
    def register(wrapper: _F) -> _F:
        WRITTEN_AS[django] = WrittenAs(wrapper.__name__, **how)
        return wrapper

    return register


class _Options(TypedDict, total=False):
    """What every field may say about its column."""

    null: bool
    default: Any
    db_default: Any
    db_index: bool
    db_column: str
    db_comment: str


@_written_as(models.TextField)
def TextField(**options: Unpack[_Options]) -> models.TextField[Any, Any]:  # noqa: N802 - named as the field
    return models.TextField(**options)


@_written_as(models.CharField)
def CharField(*, max_length: int, **options: Unpack[_Options]) -> models.CharField[Any, Any]:  # noqa: N802
    return models.CharField(max_length=max_length, **options)


@_written_as(models.BigIntegerField)
def BigIntegerField(**options: Unpack[_Options]) -> models.BigIntegerField[Any, Any]:  # noqa: N802
    return models.BigIntegerField(**options)


@_written_as(models.PositiveIntegerField)
def PositiveIntegerField(**options: Unpack[_Options]) -> models.PositiveIntegerField[Any, Any]:  # noqa: N802
    return models.PositiveIntegerField(**options)


@_written_as(models.FloatField)
def FloatField(**options: Unpack[_Options]) -> models.FloatField[Any, Any]:  # noqa: N802
    return models.FloatField(**options)


@_written_as(models.BooleanField)
def BooleanField(**options: Unpack[_Options]) -> models.BooleanField[Any, Any]:  # noqa: N802
    return models.BooleanField(**options)


@_written_as(models.DateField)
def DateField(**options: Unpack[_Options]) -> models.DateField[Any, Any]:  # noqa: N802
    return models.DateField(**options)


@_written_as(models.DateTimeField)
def DateTimeField(**options: Unpack[_Options]) -> models.DateTimeField[Any, Any]:  # noqa: N802
    return models.DateTimeField(**options)


@_written_as(models.UUIDField)
def UUIDField(**options: Unpack[_Options]) -> models.UUIDField[Any, Any]:  # noqa: N802
    return models.UUIDField(**options)


@_written_as(models.JSONField)
def JSONField(**options: Unpack[_Options]) -> models.JSONField[Any, Any]:  # noqa: N802
    return models.JSONField(**options)


class _Relation(_Options, total=False):
    related_name: str


@_written_as(models.ForeignKey)
def ForeignKey(  # noqa: N802
    *, to: str, on_delete: Callable[..., None], **options: Unpack[_Relation]
) -> models.ForeignKey[Any, Any]:
    return models.ForeignKey(to=to, on_delete=on_delete, **options)


class _ParentLink(_Relation, total=False):
    parent_link: bool
    primary_key: bool
    serialize: bool


@_written_as(models.OneToOneField)
def OneToOneField(  # noqa: N802
    *, to: str, on_delete: Callable[..., None], **options: Unpack[_ParentLink]
) -> models.OneToOneField[Any, Any]:
    return models.OneToOneField(to=to, on_delete=on_delete, **options)


@_written_as(_ArrayField)
def ArrayField(  # noqa: N802
    base_field: models.Field[Any, Any], **options: Unpack[_Options]
) -> _ArrayField[Any, Any]:
    return _ArrayField(base_field, **options)


_COMPOSITE = {"blank": True, "editable": False, "primary_key": True, "serialize": False}


@_written_as(models.CompositePrimaryKey, implied=_COMPOSITE)
def CompositePrimaryKey(*fields: str) -> models.CompositePrimaryKey:  # noqa: N802
    """A key of several columns: a list's (entity, position), a history's (entity, revision)."""
    # As a model's primary key is: not serialized, which its model gives it, and its state needs.
    return models.CompositePrimaryKey(*fields, serialize=False)


@_written_as(entity_fields.QuantityField)
def QuantityField(*, unit: str, **options: Unpack[_Options]) -> entity_fields.QuantityField:  # noqa: N802
    """A quantity, in its canonical UCUM unit: a change of unit converts it (ADR 0033)."""
    return entity_fields.QuantityField(unit=unit, **options)


@_written_as(entity_fields.StateField)
def StateField(  # noqa: N802
    *,
    states: Sequence[str],
    moved: Mapping[str, str] | None = None,
    locking: Sequence[str] = (),
    moved_from: Mapping[str, Mapping[str, str]] | None = None,
    **options: Unpack[_Options],
) -> entity_fields.StateField:
    """A state field's states, where removed ones' records went, and the locking ones
    (ADR 0125)."""
    return entity_fields.StateField(
        states=states, moved=moved, locking=locking, moved_from=moved_from, **options
    )


@_written_as(_GinIndex)
def GinIndex(  # noqa: N802
    *expressions: Any,  # noqa: ANN401 - expressions
    name: str,
    fields: Sequence[str] = (),
    opclasses: Sequence[str] = (),
) -> _GinIndex:
    return _GinIndex(*expressions, name=name, fields=list(fields), opclasses=list(opclasses))


@_written_as(models.CheckConstraint)
def CheckConstraint(*, condition: Any, name: str) -> models.CheckConstraint:  # noqa: ANN401, N802 - an expression
    """A named CHECK constraint, such as ``acme__lab_sample__name__not_empty`` (ADR 0033)."""
    return models.CheckConstraint(condition=condition, name=name)


@_written_as(models.Q, renamed={"_connector": "connector", "_negated": "negated"})
def Q(  # noqa: N802
    *conditions: Any,  # noqa: ANN401 - (lookup, value) pairs and expressions
    connector: str = models.Q.AND,
    negated: bool = False,
) -> models.Q:
    """Conditions, each a ``(lookup, value)`` pair such as ``('name__regex', '^A')`` or an
    expression, all of which (``connector='AND'``) or any of which (``'OR'``) hold."""
    return models.Q(*conditions, _connector=connector, _negated=negated)


@_written_as(functions.Length)
def Length(expression: Any) -> functions.Length:  # noqa: ANN401, N802 - a column or expression
    return functions.Length(expression)


@_written_as(functions.Left)
def Left(expression: Any, length: int) -> functions.Left:  # noqa: ANN401, N802
    return functions.Left(expression, length)


@_written_as(lookups.GreaterThanOrEqual)
def GreaterThanOrEqual(lhs: Any, rhs: Any) -> lookups.GreaterThanOrEqual:  # noqa: ANN401, N802
    return lookups.GreaterThanOrEqual(lhs, rhs)


@_written_as(lookups.LessThanOrEqual)
def LessThanOrEqual(lhs: Any, rhs: Any) -> lookups.LessThanOrEqual:  # noqa: ANN401, N802
    return lookups.LessThanOrEqual(lhs, rhs)


@_written_as(_SearchVector)
def SearchVector(  # noqa: N802
    *expressions: Any,  # noqa: ANN401
    config: str | None = None,
) -> _SearchVector:
    return _SearchVector(*expressions, config=config)


# Checks of each item of an array (``iki_server.entities.checks``), by their own names.
for _check in (IsPlainList, AllMatch, NoneMatch, AllLengthsBetween, AllBetweenInt, AllBetweenFloat):
    WRITTEN_AS[_check] = WrittenAs(_check.__name__)
del _check

#: What happens to a row when what it refers to goes: references are protected, and a list's
#: rows go with their record.
PROTECT = models.PROTECT
CASCADE = models.CASCADE
#: The core models plugin tables refer to: every record, a file, and a person.
ENTITY = "iki_entities.entity"
FILE = "iki_files.file"
USER = "iki_accounts.user"

#: Constants the writer writes by name.
CONSTANTS: dict[str, object] = {
    "PROTECT": PROTECT,
    "CASCADE": CASCADE,
    "ENTITY": ENTITY,
    "FILE": FILE,
    "USER": USER,
}

__all__ = [
    "CASCADE",
    "ENTITY",
    "FILE",
    # Constants.
    "PROTECT",
    "USER",
    "AddConstraint",
    "AddField",
    "AddIndex",
    "AllBetweenFloat",
    "AllBetweenInt",
    "AllLengthsBetween",
    "AllMatch",
    "AlterField",
    "AlterModelTable",
    # Iki's.
    "AppendOnly",
    "ArrayField",
    "BigIntegerField",
    "BooleanField",
    "CharField",
    "CheckConstraint",
    "CompositePrimaryKey",
    "ConvertUnit",
    # Django's operations.
    "CreateModel",
    "DateField",
    "DateTimeField",
    "DeleteModel",
    "FloatField",
    "ForeignKey",
    # The index, the constraint and expressions.
    "GinIndex",
    "GreaterThanOrEqual",
    # Checks of array items.
    "IsPlainList",
    "JSONField",
    "Left",
    "Length",
    "LessThanOrEqual",
    "MigrateValues",
    "Migration",
    "NoneMatch",
    "OneToOneField",
    "PositiveIntegerField",
    "Q",
    "QuantityField",
    "RecordRename",
    "RemoveConstraint",
    "RemoveField",
    "RemoveIndex",
    "RenameEntityType",
    "RenameField",
    "RenameIndex",
    "RenameModel",
    "RenameStateField",
    "RetireStateField",
    "SearchVector",
    "StateField",
    "SyncStates",
    # Fields.
    "TextField",
    "UUIDField",
]
