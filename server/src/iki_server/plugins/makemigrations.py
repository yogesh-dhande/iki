"""Makes a plugin's migrations: Django's makemigrations, with Iki's rules for history.

``iki plugin makemigrations`` builds the plugin and its dependencies into a
temporary directory and runs this module there::

    python -m iki_server.plugins.makemigrations PLUGIN... [--name NAME] [--check] [--no-input]

It exits 1 when ``--check`` finds changes, and when it can't make or check migrations, which
it says on stderr, starting ``error:``.

It runs Django's own command, with its questions about renames, against the
dummy database backend, so nothing needs a database. On top, it keeps every
type's history:

- History is never deleted. A field deleted from a manifest is refused, with
  a hint to mark it ``removed: true`` (or answer that it was renamed). So is
  a deleted type or extension.
- A history column only changes type when its old values still fit.
- Renaming a field or type renames its history column or table too, without
  asking twice.
- When a quantity's unit changes, the migration converts the stored values,
  current and historical (``ConvertUnit``).
- When a type is renamed, the migration renames it in ``iki_entity.type``.
- What a migration does to kept history (a field renamed, a quantity converted, a type
  renamed) it records, so signatures made before still read what they signed
  (``RecordRename``, and ``ConvertUnit`` and ``RenameEntityType`` themselves, ADR 0148).
- A new history table is made append-only for every role (``AppendOnly``).
- A list's table is made with the entity and position it's keyed by, and dropped
  with them, however much else the migration changes (ADR 0137).
- A state field's states (ADR 0125) are kept: a state that disappears is
  refused unless the manifest marks it removed and says where its records
  go, and the migration moves them and keeps which records are locked
  (``SyncStates``). A removed state field moves its records' states to
  another of the type's state fields, or the migration is refused while
  records have it (``RetireStateField``). A renamed one keeps the records it
  locks locked (``RenameStateField``). A field can't become a state field,
  or stop being one.

A plugin's migrations are written in Iki's operations, depending on Iki's release anchors
(:mod:`iki_server.plugins.migration_writer`, ADR 0296); the core plugin's are Iki's own, as
Django writes them. Neither goes through a code formatter, so every machine writes the same
files.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

HISTORY = "_history"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m iki_server.plugins.makemigrations")
    parser.add_argument("plugin", nargs="+")
    parser.add_argument("--name")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument("--merge", action="store_true")
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args(argv)
    os.environ["DJANGO_SETTINGS_MODULE"] = "iki_server.plugins.build_settings"

    import django

    django.setup()
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.core.management.commands import makemigrations
    from django.db.migrations.exceptions import NodeNotFoundError

    from iki_server.plugins.migration_writer import PluginMigrationWriter

    # A formatter on the PATH (black) would make output differ between machines.
    makemigrations.run_formatters = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    # Plugin migrations are written in Iki's operations (ADR 0296).
    makemigrations.MigrationWriter = PluginMigrationWriter  # type: ignore[attr-defined]
    try:
        call_command(
            _command(),
            *args.plugin,
            name=args.name,
            check_changes=args.check,
            dry_run=args.dry_run,
            interactive=not args.no_input,
            merge=args.merge,
            empty=args.empty,
        )
    except CommandError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    except NodeNotFoundError as missing:
        sys.stderr.write(f"error: {_missing(missing)}\n")
        return 1
    return 0


def _missing(missing: Any) -> str:  # noqa: ANN401 - Django's NodeNotFoundError
    """What a migration depends on that the build hasn't got, without Django's traceback."""
    from iki_spec import migration_api, naming, releases

    origin = getattr(missing, "origin", None)
    needing = (
        f"{naming.plugin_of_label(origin.app_label) or origin.app_label} {origin.name}"
        if origin is not None and hasattr(origin, "app_label")
        else "A migration"
    )
    app, name = missing.node
    if app == migration_api.ANCHORS_APP:
        return (
            f"{needing} depends on Iki's anchor {name}, which this Iki ({releases.iki()}) "
            "hasn't got: it was made with a newer Iki. Use an Iki that has it, or a release of "
            "the plugin made for this one."
        )
    plugin = naming.plugin_of_label(app)
    if plugin is not None and plugin != naming.CORE:
        return f"{needing} depends on {plugin} {name}, which the {plugin} given hasn't got."
    return (
        f"{needing} depends on {app} {name}, one of Iki's own migrations this Iki hasn't got. "
        "A plugin migration depends on Iki's anchors instead (ADR 0296)."
    )


def _command() -> Any:  # noqa: ANN401 - a Django command, built after setup
    from django.core.management.commands.makemigrations import Command

    display = os.environ.get("IKI_MIGRATIONS_DISPLAY_DIR")

    class PluginCommand(Command):
        autodetector = _autodetector()

        @staticmethod
        def get_relative_path(path: str) -> str:
            # Name the file where `iki plugin makemigrations` copies it, not the build's copy.
            if display:
                return str(Path(display) / Path(path).name)
            return str(Command.get_relative_path(path))

    return PluginCommand()


def _uses_trigrams(op: Any) -> bool:  # noqa: ANN401 - a Django operation
    """Whether the operation makes an index with a pg_trgm operator class."""
    indexes = [getattr(op, "index", None), *getattr(op, "options", {}).get("indexes", [])]
    return any("gin_trgm_ops" in (getattr(i, "opclasses", None) or ()) for i in indexes)


def _autodetector() -> type:
    from django.apps import apps
    from django.core.management.base import CommandError
    from django.db import migrations
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.migration import Migration
    from django.db.migrations.state import ProjectState

    from iki_server.entities.fields import StateField
    from iki_server.plugins.operations import (
        AddField,
        AppendOnly,
        ConvertUnit,
        RecordRename,
        RenameEntityType,
        RenameStateField,
        RetireStateField,
        SyncStates,
    )

    state_ops = (SyncStates, RetireStateField, RenameStateField)
    #: Operations that record what they do to kept history (ADR 0148).
    recording = (ConvertUnit, RecordRename, RenameEntityType)

    class PluginAutodetector(MigrationAutodetector):
        #: Each app's operations, as Django makes them, before it sorts and optimizes them.
        generated_operations: dict[str, list[Any]]

        def __init__(
            self,
            from_state: ProjectState,
            to_state: ProjectState,
            questioner: Any = None,  # noqa: ANN401 - Django's questioner
        ) -> None:
            wrapped: Any = _HistoryQuestioner(questioner) if questioner else None
            super().__init__(from_state, to_state, wrapped)

        # Django makes a new model without its relations, adds each after, and leaves its
        # optimizer to fold them back in; a deleted one loses its relations before it goes.
        # The optimizer can't fold across an operation that doesn't say which models it
        # touches, such as a constraint added to another model. A list's table is keyed by
        # (entity, position) (ADR 0137), and a model without a field its key names can't be
        # made, in either direction. So each part of a composite primary key stays in its
        # model's CreateModel and goes with its DeleteModel, as Django keeps a relation that
        # is the whole primary key.

        def generate_created_models(self) -> None:
            super().generate_created_models()
            for app_label, ops in self.generated_operations.items():
                made: dict[str, Any] = {
                    op.name_lower: op
                    for op in ops
                    if isinstance(op, migrations.CreateModel) and _key(op.fields)
                }
                kept = []
                for op in ops:
                    is_add = isinstance(op, migrations.AddField)
                    create = made.get(op.model_name_lower) if is_add else None
                    if create is None or op.name not in _key(create.fields):
                        kept.append(op)
                        continue
                    order = list(self.to_state.models[app_label, create.name_lower].fields)
                    create.fields.append((op.name, op.field))
                    create.fields.sort(key=lambda named: order.index(named[0]))
                    # What the relation needs, made first; not the model it's being added to.
                    create._auto_deps += [
                        d
                        for d in op._auto_deps
                        if (d.app_label, d.model_name_lower) != (app_label, create.name_lower)
                    ]
                ops[:] = kept

        def generate_deleted_models(self) -> None:
            super().generate_deleted_models()
            for app_label, ops in self.generated_operations.items():
                deleted: dict[str, Any] = {
                    op.name_lower: op
                    for op in ops
                    if isinstance(op, migrations.DeleteModel) and self._deleted_key(app_label, op)
                }
                kept = []
                for op in ops:
                    is_removal = isinstance(op, migrations.RemoveField)
                    delete = deleted.get(op.model_name_lower) if is_removal else None
                    if delete is None or op.name not in self._deleted_key(app_label, delete):
                        kept.append(op)
                        continue
                    # After the constraints on the field go, as its removal would have been.
                    delete._auto_deps = [
                        *(
                            d
                            for d in delete._auto_deps
                            if (d.app_label, d.model_name_lower, d.field_name)
                            != (app_label, op.model_name_lower, op.name)
                        ),
                        *op._auto_deps,
                    ]
                ops[:] = kept

        def _deleted_key(self, app_label: str, op: Any) -> tuple[str, ...]:  # noqa: ANN401 - a Django operation
            return _key(self.from_state.models[app_label, op.name_lower].fields.items())

        def changes(self, *args: Any, **kwargs: Any) -> dict[str, list[Migration]]:  # noqa: ANN401 - Django's signature
            changes: dict[str, list[Migration]] = super().changes(*args, **kwargs)
            named = kwargs.get("migration_name") or (args[3] if len(args) > 3 else None)
            for app_label, app_migrations in changes.items():
                if not _is_plugin(app_label):
                    continue
                for migration in app_migrations:
                    operations = self._with_history_rules(app_label, migration.operations)
                    migration.operations = operations  # type: ignore[misc]
                    needs = []
                    if any(isinstance(op, AppendOnly) for op in operations):
                        # AppendOnly's trigger function.
                        needs.append(("iki_entities", "0002_history_append_only"))
                    if any(isinstance(op, state_ops) for op in operations):
                        # State changes' revision columns, and iki_entity.locked_by.
                        needs.append(("iki_entities", "0012_states"))
                    if any(isinstance(op, recording) for op in operations):
                        # Where they record it.
                        needs.append(("iki_plugins", "0006_history_changes"))
                    if any(_uses_trigrams(op) for op in operations):
                        # Trigram indexes need pg_trgm.
                        needs.append(("iki_entities", "0004_search"))
                    for needed in needs:
                        if needed not in migration.dependencies:
                            migration.dependencies = [*migration.dependencies, needed]  # type: ignore[misc]
                if len(app_migrations) == 1 and not named:
                    _rename(app_migrations[0])
            return changes

        def _with_history_rules(self, app_label: str, operations: Sequence[Any]) -> list[Any]:
            out: list[Any] = []
            #: Removed state fields' removals, after what their records' states move to.
            retiring: list[Any] = []
            for op in operations:
                if isinstance(op, migrations.RemoveField) and self._is_state(
                    self.from_state, app_label, op.model_name, op.name
                ):
                    retiring += [self._retire(app_label, op), op]
                    continue
                if isinstance(op, migrations.AddField):
                    model = apps.get_model(app_label, op.model_name)
                    if owner := getattr(model, "IKI_HISTORY_OF", None):
                        # Apply a new live column's manifest default to its historical
                        # twin once, when adding it. Later snapshots supply their own values.
                        live = apps.get_model(app_label, owner)
                        if op.name in {f.name for f in live._meta.local_concrete_fields}:
                            column = live._meta.get_field(op.name)
                            if column.has_db_default():
                                field = op.field.clone()
                                field.default = column.db_default
                                op = migrations.AddField(
                                    op.model_name, op.name, field, preserve_default=False
                                )
                    elif hasattr(model, "IKI_TYPE") or hasattr(model, "IKI_ADDS_TO"):
                        # Live plugin columns can acquire a field or rule guard even in a
                        # later manifest with no migration (ADR 0127).
                        op = AddField(op.model_name, op.name, op.field, op.preserve_default)
                out.append(op)
                if isinstance(op, migrations.RemoveField) and _is_history(op.model_name):
                    owner = self._describe(app_label, op.model_name, self.from_state)
                    raise CommandError(
                        f"`{op.name}` is gone from {owner}. Iki keeps every field's history, "
                        "so a field can't be deleted.\n"
                        "  If you renamed it, run this again and answer yes when asked whether "
                        "it was renamed.\n"
                        "  If you no longer want it, put it back and mark it `removed: true`: "
                        "its column goes, its history stays."
                    )
                if isinstance(op, migrations.DeleteModel) and _is_history(op.name):
                    owner = self._describe(app_label, op.name, self.from_state)
                    # A type's name starts lowercase, with its plugin's namespace: only
                    # "the fields …" begins with a word to capitalize.
                    owner = f"T{owner[1:]}" if owner.startswith("the ") else owner
                    raise CommandError(
                        f"{owner} is gone. Iki keeps every entity's "
                        "history, so a plugin can't delete a type or an extension: put its "
                        "manifest back.\n"
                        "  If you renamed the type, run this again and answer yes when asked "
                        "whether it was renamed."
                    )
                if isinstance(op, migrations.RenameField) and self._is_state(
                    self.to_state, app_label, op.model_name, op.new_name
                ):
                    out.append(RenameStateField(op.model_name, op.old_name, op.new_name))
                if isinstance(op, migrations.RenameField) and _is_history(op.model_name):
                    out.append(
                        RecordRename(
                            op.model_name,
                            op.old_name,
                            op.new_name,
                            part=_part(app_label, op.model_name),
                        )
                    )
                if isinstance(op, migrations.AlterField):
                    old = self.from_state.models[app_label, op.model_name.lower()].fields[op.name]
                    if isinstance(old, StateField) or isinstance(op.field, StateField):
                        found = self._states(app_label, op, old)
                        if found is not None:
                            out.append(found)
                        continue
                    if _is_history(op.model_name):
                        self._check_history_type(app_label, op, old)
                    conversion = self._conversion(app_label, op, old)
                    if conversion is not None:
                        out.append(conversion)
                if isinstance(op, migrations.CreateModel) and _is_history(op.name):
                    out.append(AppendOnly(op.name))
                if isinstance(op, migrations.RenameModel) and not _is_derived(op.new_name):
                    plugin = _plugin(app_label)
                    out.append(
                        RenameEntityType(f"{plugin}.{op.old_name}", f"{plugin}.{op.new_name}")
                    )
            return out + retiring

        def _is_state(self, state: ProjectState, app_label: str, model: str, name: str) -> bool:
            if _is_history(model):
                return False
            found = state.models.get((app_label, model.lower()))
            return found is not None and isinstance(found.fields.get(name), StateField)

        def _retire(self, app_label: str, op: Any) -> Any:  # noqa: ANN401 - Django operations
            """What moves a removed state field's records' states to another (ADR 0125)."""
            old = self.from_state.models[app_label, op.model_name.lower()].fields[op.name]
            now = self.to_state.models[app_label, op.model_name.lower()].fields
            target = next(
                (
                    (name, f)
                    for name, f in now.items()
                    if isinstance(f, StateField) and op.name in f.moved_from
                ),
                None,
            )
            if target is None:
                return RetireStateField(op.model_name, op.name, None, (), old.locking)
            name, f = target
            moved = {"field": name, "states": f.moved_from[op.name]}
            return RetireStateField(op.model_name, op.name, moved, f.locking, old.locking)

        def _states(self, app_label: str, op: Any, old: Any) -> Any:  # noqa: ANN401 - Django fields and operations
            """What keeps records in a state field's states as they change (ADR 0125)."""
            owner = self._describe(app_label, op.model_name, self.from_state)
            if not isinstance(op.field, StateField):
                raise CommandError(
                    f"`{op.name}` of {owner} is a state field, which records are each in one "
                    "state of: it can't become another kind of field (ADR 0125).\n"
                    "  Mark it `removed: true`, with `moved_to:` another state field if its "
                    "states should carry on there, and add a new field."
                )
            if not isinstance(old, StateField):
                raise CommandError(
                    f"`{op.name}` of {owner} would become a state field, but records' values "
                    "in it aren't states (ADR 0125).\n"
                    "  Add a new state field, and mark this one `removed: true` if it's no "
                    "longer wanted."
                )
            gone = [s for s in old.states if s not in op.field.states]
            stranded = [s for s in gone if s not in op.field.moved]
            if stranded:
                owner = self._describe(app_label, op.model_name, self.from_state)
                names = ", ".join(f"`{s}`" for s in stranded)
                raise CommandError(
                    f"{owner} no longer has the state{'s' if len(stranded) > 1 else ''} "
                    f"{names}, which records may be in.\n"
                    "  Keep each in the manifest, marked `removed: true` with `moved_to:` the "
                    "state its records move to. The migration moves them, with a revision each."
                )
            moved = {s: op.field.moved[s] for s in gone}
            if not moved and list(op.field.locking) == list(old.locking):
                return None
            return SyncStates(op.model_name, op.name, moved, op.field.locking, old.locking)

        def _describe(self, app_label: str, model_name: str, state: ProjectState) -> str:
            name = state.models[app_label, model_name.lower()].name
            base = name[: -len(HISTORY)] if _is_history(name) else name
            if base.startswith("Ext_"):
                target = state.models[app_label, model_name.lower()].fields["entity"].related_model
                return f"the fields {_plugin(app_label)} adds to {_type_of(target)}"
            return f"{_plugin(app_label)}.{base}"

        def _check_history_type(self, app_label: str, op: Any, old: Any) -> None:  # noqa: ANN401 - Django fields and operations
            before, after = _kind(old), _kind(op.field)
            if before == after or (before, after) in _WIDENING:
                return
            owner = self._describe(app_label, op.model_name, self.from_state)
            raise CommandError(
                f"`{op.name}` of {owner} would change from {_KIND_WORDS.get(before, before)} to "
                f"{_KIND_WORDS.get(after, after)}, which its history can't be converted to.\n"
                "  Add a new field for the new values, and mark this one `removed: true`."
            )

        def _conversion(self, app_label: str, op: Any, old: Any) -> Any:  # noqa: ANN401 - Django fields and operations
            before, after = _unit(old), _unit(op.field)
            if before is None or after is None or before == after:
                return None
            owner = self._describe(app_label, op.model_name, self.from_state)
            scale, offset = linear_conversion(before, after, f"`{op.name}` of {owner}")
            if _is_history(op.model_name):
                # Recorded, for signatures of what history held before (ADR 0148).
                part = _part(app_label, op.model_name)
                return ConvertUnit(op.model_name, op.name, before, after, scale, offset, part=part)
            return ConvertUnit(op.model_name, op.name, before, after, scale, offset)

    return PluginAutodetector


def _rename(migration: Any) -> None:  # noqa: ANN401 - a Django migration
    """Names a migration after what changed, not the constraints that changed with it.

    Django names it after its first operations, which are often removed CHECK
    constraints: ``0002_remove_protein_protein_protein__organism__not_empty``.
    """
    from django.db import migrations
    from django.db.migrations.migration import Migration

    from iki_server.plugins.operations import AppendOnly

    noise = (migrations.AddConstraint, migrations.RemoveConstraint, AppendOnly)
    kept = [op for op in migration.operations if not isinstance(op, noise)]
    if not kept or migration.initial:
        return
    probe = Migration("probe", migration.app_label)
    probe.operations = kept  # type: ignore[misc]
    number = migration.name.split("_", 1)[0]
    migration.name = f"{number}_{probe.suggest_name()}"


class _HistoryQuestioner:
    """Asks about renames once: a history table follows what its live table did."""

    def __init__(self, questioner: Any) -> None:  # noqa: ANN401 - Django's questioner
        self._questioner = questioner
        self._fields: dict[tuple[str, str, str], bool] = {}
        self._models: dict[tuple[str, str], bool] = {}

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - proxies Django's questioner
        return getattr(self._questioner, name)

    def ask_rename(self, model_name: str, old_name: str, new_name: str, field: Any) -> bool:  # noqa: ANN401 - Django's field
        model = model_name.lower()
        if _is_history(model):
            known = self._fields.get((model[: -len(HISTORY)], old_name, new_name))
            if known is not None:
                return known
        answer = bool(self._questioner.ask_rename(model_name, old_name, new_name, field))
        self._fields[model, old_name, new_name] = answer
        return answer

    def ask_rename_model(self, old: Any, new: Any) -> bool:  # noqa: ANN401 - Django's model states
        key = (old.name.lower(), new.name.lower())
        if _is_history(key[0]) and _is_history(key[1]):
            known = self._models.get((key[0][: -len(HISTORY)], key[1][: -len(HISTORY)]))
            if known is not None:
                return known
        answer = bool(self._questioner.ask_rename_model(old, new))
        self._models[key] = answer
        return answer


def _plugin(app_label: str) -> str:
    """The plugin an app is, by its name (``iki/protein``) rather than its label."""
    from iki_spec import naming

    return naming.plugin_of_label(app_label) or app_label


def _type_of(model: Any) -> str:  # noqa: ANN401 - a model reference in a migration's state
    """``iki/protein.Protein`` for ``iki__protein.protein``, as migrations' states name models."""
    label, _, name = str(model).partition(".")
    return f"{_plugin(label)}.{name}"


def _key(fields: Any) -> tuple[str, ...]:  # noqa: ANN401 - a model's (name, field) pairs
    """The fields a composite primary key among ``fields`` is made of, or none."""
    from django.db import models

    for _, field in fields:
        if isinstance(field, models.CompositePrimaryKey):
            return tuple(field.field_names)
    return ()


def _is_plugin(app_label: str) -> bool:
    from django.apps import apps

    return apps.get_app_config(app_label).name.startswith("iki_plugin_")


def _is_history(model_name: str) -> bool:
    return model_name.lower().endswith(HISTORY)


def _part(app_label: str, history_model: str) -> str:
    """The part of a type a history table holds, as the type's shape names it
    (``entities.values.Part.name``): ``acme/lab.Sample``, its registry entries'
    ``acme/lab.Sample#entry``, or the fields a plugin adds, ``acme/lims:acme/lab.Sample``.
    From the generated models being migrated to."""
    from django.apps import apps

    model = apps.get_model(app_label, history_model)
    model = apps.get_model(app_label, model.IKI_HISTORY_OF)
    extends = getattr(model, "IKI_ADDS_TO", None)
    name = f"{_plugin(app_label)}:{extends}" if extends else str(model.IKI_TYPE)
    return name + ("#entry" if getattr(model, "IKI_ENTRY", False) else "")


def _is_derived(model_name: str) -> bool:
    """History, list and extension models, as opposed to an entity type's own model."""
    return "_" in model_name


def _kind(field: Any) -> str:  # noqa: ANN401 - a Django field
    name = type(field).__name__
    if name == "QuantityField":
        name = "FloatField"
    if name == "ArrayField":
        return f"ArrayField[{_kind(field.base_field)}]"
    return str(name)


#: History column changes that keep every old value.
_WIDENING = {
    ("BigIntegerField", "FloatField"),
    ("ArrayField[BigIntegerField]", "ArrayField[FloatField]"),
}

_KIND_WORDS = {
    "TextField": "text",
    "BigIntegerField": "a whole number",
    "FloatField": "a number",
    "BooleanField": "true or false",
    "DateField": "a date",
    "DateTimeField": "a date and time",
    "JSONField": "JSON",
    "UUIDField": "a reference or file",
}


def _unit(field: Any) -> str | None:  # noqa: ANN401 - a Django field
    if type(field).__name__ == "ArrayField":
        return _unit(field.base_field)
    return getattr(field, "unit", None)


def linear_conversion(before: str, after: str, what: str) -> tuple[float, float]:
    """``new = old * scale + offset``, or an error for units that don't convert that way."""
    from django.core.management.base import CommandError

    from iki_spec import units

    source, target = units.parse(before), units.parse(after)
    if not source.is_commensurable(target):
        raise CommandError(
            f"{what} can't change from {before} to {after}: they measure different things.\n"
            "  Add a new field for the new values, and mark this one `removed: true`."
        )
    offset = units.convert(0.0, source, target)
    scale = units.convert(1.0, source, target) - offset
    for x in (10.0, 1000.0, -7.5):
        expected = scale * x + offset
        if not math.isclose(
            units.convert(x, source, target), expected, rel_tol=1e-9, abs_tol=1e-12
        ):
            raise CommandError(
                f"{what} can't change from {before} to {after} automatically: the conversion "
                "isn't a simple multiplication.\n"
                "  Add a new field for the new values, and mark this one `removed: true`."
            )
    return scale, offset


if __name__ == "__main__":
    sys.exit(main())
