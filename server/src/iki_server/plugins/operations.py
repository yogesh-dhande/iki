"""Migration operations `iki plugin makemigrations` adds to plugin migrations.

Plugin migrations import them, so they are API: their names and arguments
never change incompatibly.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import ProjectState

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
            schema_editor.execute(
                "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, via,"
                " client, reason, state_field, state_from, state_to, created_at)"
                " SELECT uuidv7(), e.id, e.revision + 1, 'state_changed', ARRAY[%s], 'system',"
                " '', m.reason, %s, m.old, m.new, now()"
                " FROM iki_entity e JOIN iki_moving m ON m.entity_id = e.id",
                [field, field],
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
        if field.get_internal_type() == "ArrayField":
            value = (
                f"ARRAY(SELECT item * %s + %s FROM unnest({column}) "  # noqa: S608 - a quoted name
                "WITH ORDINALITY AS u(item, i) ORDER BY i)"
            )
        else:
            value = f"{column} * %s + %s"
        history = getattr(model, "APPEND_ONLY", False) or model.__name__.endswith("_History")
        if history:
            # The same quantity in a new unit: the one change history allows, in a migration.
            schema_editor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {AppendOnly.ROW_TRIGGER}")
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
        if history:
            schema_editor.execute(f"ALTER TABLE {table} ENABLE TRIGGER {AppendOnly.ROW_TRIGGER}")

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


class AddPluginField(migrations.AddField):
    """Add a field to a plugin table where sync_plugins can install column guards.

    PostgreSQL refuses to drop a column named in an UPDATE OF trigger. Reversing this
    operation first drops the affected Iki guards in the same migration transaction.
    Consult the database since even a later, migration-free manifest can add a rule.
    """

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
