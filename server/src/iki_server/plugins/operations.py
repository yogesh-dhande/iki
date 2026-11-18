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

#: Set for a transaction, it lets a migration change locked records' values (ADR 0118):
#: the triggers ``sync_plugins`` puts on records' tables let it through.
UNLOCKED_SETTING = "iki.unlocked"


@contextmanager
def unlocked(schema_editor: BaseDatabaseSchemaEditor) -> Iterator[None]:
    schema_editor.execute("SELECT set_config(%s, 'on', true)", [UNLOCKED_SETTING])
    try:
        yield
    finally:
        schema_editor.execute("SELECT set_config(%s, '', true)", [UNLOCKED_SETTING])


class SyncStates(migrations.operations.base.Operation):
    """Keeps records in step with their type's states (ADR 0118) when a plugin changes them.

    Records in a state the plugin removed move to the state it says (``moved``),
    each with a revision by no one naming the move (``iki.moved``), and every
    record's ``locked`` follows the states that lock now (``locking``). The
    reverse migration moves them back, as revisions too, and locks them as
    ``locking_before`` did.
    """

    reversible = True

    #: The transition a revision names when a migration moved its record.
    MOVED = "iki.moved"

    def __init__(
        self,
        model_name: str,
        moved: Mapping[str, str],
        locking: Sequence[str],
        locking_before: Sequence[str],
    ) -> None:
        self.model_name = model_name
        self.moved = dict(moved)
        self.locking = list(locking)
        self.locking_before = list(locking_before)

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, to_state)
        for old, new in sorted(self.moved.items()):
            self._move(schema_editor, table, old, new, back=False)
        self._lock(schema_editor, table, self.locking)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        table = self._table(app_label, to_state)
        for old, new in sorted(self.moved.items()):
            self._move(schema_editor, table, new, old, back=True)
        self._lock(schema_editor, table, self.locking_before)

    def _table(self, app_label: str, state: ProjectState) -> str:
        model = state.apps.get_model(app_label, self.model_name)
        return str(model._meta.db_table)

    def _move(
        self,
        schema_editor: BaseDatabaseSchemaEditor,
        table: str,
        old: str,
        new: str,
        *,
        back: bool,
    ) -> None:
        """Moves the records in ``old`` to ``new``, each with a revision. Backwards, only
        those whose latest revision is the move forwards."""
        quoted = schema_editor.quote_name(table)
        moving = f"SELECT t.entity_id FROM {quoted} t WHERE t.iki_state = %s"  # noqa: S608 - a quoted name
        params: list[Any] = [old]
        if back:
            moving += (
                " AND EXISTS (SELECT 1 FROM iki_revision r JOIN iki_entity e ON e.id = r.entity_id"
                " WHERE r.entity_id = t.entity_id AND r.number = e.revision"
                " AND r.transition = %s AND r.state_from = %s AND r.state_to = %s)"
            )
            params += [self.MOVED, new, old]
        reason = (
            f"Its plugin's migration moved it back from {old} to {new}."
            if back
            else f"Its plugin removed the state {old}: its migration moved it to {new}."
        )
        with unlocked(schema_editor):
            schema_editor.execute(
                f"CREATE TEMPORARY TABLE iki_moving ON COMMIT DROP AS {moving}", params
            )
            schema_editor.execute(
                "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, via,"
                " client, reason, transition, state_from, state_to, created_at)"
                " SELECT uuidv7(), e.id, e.revision + 1, 'transitioned', '{}', 'system', '', %s,"
                " %s, %s, %s, now() FROM iki_entity e JOIN iki_moving m ON m.entity_id = e.id",
                [reason, self.MOVED, old, new],
            )
            schema_editor.execute(
                "UPDATE iki_entity e SET revision = e.revision + 1, updated_at = now()"
                " FROM iki_moving m WHERE m.entity_id = e.id"
            )
            schema_editor.execute(
                f"UPDATE {quoted} t SET iki_state = %s FROM iki_moving m"  # noqa: S608 - a quoted name
                " WHERE m.entity_id = t.entity_id",
                [new],
            )
            schema_editor.execute("DROP TABLE iki_moving")

    @staticmethod
    def _lock(schema_editor: BaseDatabaseSchemaEditor, table: str, locking: Sequence[str]) -> None:
        quoted = schema_editor.quote_name(table)
        schema_editor.execute(
            f"UPDATE iki_entity e SET locked = (t.iki_state = ANY(%s)) FROM {quoted} t"  # noqa: S608 - a quoted name
            " WHERE t.entity_id = e.id AND e.locked IS DISTINCT FROM (t.iki_state = ANY(%s))",
            [list(locking), list(locking)],
        )

    def describe(self) -> str:
        moves = ", ".join(f"{a} to {b}" for a, b in sorted(self.moved.items()))
        return f"Keep {self.model_name}'s records in its states" + (
            f", moving them from {moves}" if moves else ""
        )

    @property
    def migration_name_fragment(self) -> str:
        return f"states_{self.model_name.lower()}"


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
    ) -> None:
        self.model_name = model_name
        self.name = name
        self.from_unit = from_unit
        self.to_unit = to_unit
        self.scale = scale
        self.offset = offset

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        pass

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._convert(app_label, schema_editor, to_state, self.scale, self.offset)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._convert(app_label, schema_editor, to_state, 1 / self.scale, -self.offset / self.scale)

    def _convert(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        state: ProjectState,
        scale: float,
        offset: float,
    ) -> None:
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
        # The same values, so locked records (ADR 0118) take them too.
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
        self._rename(schema_editor, self.old_name, self.new_name)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        self._rename(schema_editor, self.new_name, self.old_name)

    @staticmethod
    def _rename(schema_editor: BaseDatabaseSchemaEditor, old: str, new: str) -> None:
        schema_editor.execute("UPDATE iki_entity SET type = %s WHERE type = %s", [new, old])

    def describe(self) -> str:
        return f"Rename the entity type {self.old_name} to {self.new_name}"

    @property
    def migration_name_fragment(self) -> str:
        return f"rename_type_{self.new_name.split('.')[-1].lower()}"


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
