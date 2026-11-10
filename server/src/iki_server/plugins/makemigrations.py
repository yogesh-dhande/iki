"""Makes a plugin's migrations: Django's makemigrations, with Iki's rules for history.

``iki plugin makemigrations`` builds the plugin and its dependencies into a
temporary directory and runs this module there::

    python -m iki_server.plugins.makemigrations PLUGIN... [--name NAME] [--check] [--no-input]

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
- A new history table is made append-only for every role (``AppendOnly``).

Migrations are written as Django renders them, never through a code
formatter, so every machine writes the same files.
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

    # A formatter on the PATH (black) would make output differ between machines.
    makemigrations.run_formatters = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
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
    return 0


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


def _autodetector() -> type:
    from django.core.management.base import CommandError
    from django.db import migrations
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.migration import Migration
    from django.db.migrations.state import ProjectState

    from iki_server.plugins.operations import AppendOnly, ConvertUnit, RenameEntityType

    class PluginAutodetector(MigrationAutodetector):
        def __init__(
            self,
            from_state: ProjectState,
            to_state: ProjectState,
            questioner: Any = None,  # noqa: ANN401 - Django's questioner
        ) -> None:
            wrapped: Any = _HistoryQuestioner(questioner) if questioner else None
            super().__init__(from_state, to_state, wrapped)

        def changes(self, *args: Any, **kwargs: Any) -> dict[str, list[Migration]]:  # noqa: ANN401 - Django's signature
            changes: dict[str, list[Migration]] = super().changes(*args, **kwargs)
            named = kwargs.get("migration_name") or (args[3] if len(args) > 3 else None)
            for app_label, app_migrations in changes.items():
                if not _is_plugin(app_label):
                    continue
                for migration in app_migrations:
                    operations = self._with_history_rules(app_label, migration.operations)
                    migration.operations = operations  # type: ignore[misc]
                    if any(isinstance(op, AppendOnly) for op in operations):
                        # AppendOnly's trigger function.
                        needed = ("iki_entities", "0002_history_append_only")
                        if needed not in migration.dependencies:
                            migration.dependencies = [*migration.dependencies, needed]  # type: ignore[misc]
                if len(app_migrations) == 1 and not named:
                    _rename(app_migrations[0])
            return changes

        def _with_history_rules(self, app_label: str, operations: Sequence[Any]) -> list[Any]:
            out: list[Any] = []
            for op in operations:
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
                    raise CommandError(
                        f"{owner[0].upper()}{owner[1:]} is gone. Iki keeps every entity's "
                        "history, so a plugin can't delete a type or an extension: put its "
                        "manifest back.\n"
                        "  If you renamed the type, run this again and answer yes when asked "
                        "whether it was renamed."
                    )
                if isinstance(op, migrations.AlterField):
                    old = self.from_state.models[app_label, op.model_name.lower()].fields[op.name]
                    if _is_history(op.model_name):
                        self._check_history_type(app_label, op, old)
                    conversion = self._conversion(app_label, op, old)
                    if conversion is not None:
                        out.append(conversion)
                if isinstance(op, migrations.CreateModel) and _is_history(op.name):
                    out.append(AppendOnly(op.name))
                if isinstance(op, migrations.RenameModel) and not _is_derived(op.new_name):
                    out.append(
                        RenameEntityType(f"{app_label}.{op.old_name}", f"{app_label}.{op.new_name}")
                    )
            return out

        def _describe(self, app_label: str, model_name: str, state: ProjectState) -> str:
            name = state.models[app_label, model_name.lower()].name
            base = name[: -len(HISTORY)] if _is_history(name) else name
            if base.startswith("Ext_"):
                _, plugin, type_name = base.split("_", 2)
                return f"the fields {app_label} adds to {plugin}.{type_name}"
            return f"{app_label}.{base}"

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
            scale, offset = _linear_conversion(before, after, f"`{op.name}` of {owner}")
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


def _is_plugin(app_label: str) -> bool:
    from django.apps import apps

    return apps.get_app_config(app_label).name.startswith("iki_plugin_")


def _is_history(model_name: str) -> bool:
    return model_name.lower().endswith(HISTORY)


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


def _linear_conversion(before: str, after: str, what: str) -> tuple[float, float]:
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
