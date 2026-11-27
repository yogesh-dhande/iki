"""Writes a plugin's migrations in the names of ``iki_server.plugins.operations`` (ADR 0296).

``iki plugin makemigrations`` runs Django's command with this writer in place of
Django's, for every plugin but the core one. It writes each operation, field, index,
constraint and expression by its wrapper's name, with Iki's arguments, imports nothing
but the operations module (and the standard library, for a default value that needs it),
and depends on Iki's own migrations only through the release anchor of the oldest release
the plugin's ``iki:`` admits. It refuses anything without a wrapper rather than write a
migration a later Iki couldn't read.

What it writes reads back to the same operations: written again, a migration is the
same file, byte for byte.
"""

from __future__ import annotations

import datetime
import decimal
import inspect
import math
import uuid
from collections.abc import Iterable
from typing import Any

from django.conf import SettingsReference, settings
from django.core.management.base import CommandError
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.writer import MigrationWriter
from django.db.models import Field

from iki_server.plugins import operations as ops
from iki_spec import migration_api, naming

ALIAS = migration_api.ALIAS
HEADER = (
    "# Made by `iki plugin makemigrations`. Review it, and commit it with the manifests it\n"
    "# migrates: once a deployment has applied it, it never changes.\n"
)
#: The wrappers' names, by the object or core model label they stand for.
_CONSTANTS = {id(value): name for name, value in ops.CONSTANTS.items() if callable(value)}
_LABELS = {value: name for name, value in ops.CONSTANTS.items() if isinstance(value, str)}
#: Operations written with each item of these arguments on a line of its own.
_EXPANDED = {"fields", "indexes", "constraints"}


class WriteError(CommandError):
    """What a plugin migration can't be written with."""


class PluginMigrationWriter(MigrationWriter):
    """Django's writer, writing a plugin's migrations in Iki's operations.

    The core plugin's migrations are Iki's own, like ``iki_entities``'s: they're written
    as Django writes them, and depend on Iki's migrations by name (ADR 0296)."""

    def as_string(self) -> str:
        label = self.migration.app_label
        if naming.plugin_of_label(label) in (None, naming.CORE):
            return str(super().as_string())
        anchor = anchor_of(label)
        check_at(self.migration, anchor)
        return render(self.migration, dependencies(self.migration, anchor))


def anchor_of(app_label: str) -> migration_api.Anchor:
    """The anchor a plugin's migrations depend on: that of the oldest release its ``iki:``
    admits (ADR 0296)."""
    from iki_server.entities import registry

    plugin = registry.plugins()[naming.plugin_of_label(app_label) or app_label]
    anchor = migration_api.anchor_for(plugin.iki)
    if anchor is None:
        first = migration_api.anchors()[0]
        raise WriteError(
            f"{plugin.name}'s `iki:` ({plugin.iki}) admits releases older than Iki "
            f"{first.version}, the first with an anchor for plugin migrations to depend on. "
            f'Raise it to `">={first.version}"`.'
        )
    return anchor


def check_at(migration: Migration, anchor: migration_api.Anchor) -> None:
    """Refuses a migration that refers to a model of Iki's that ``anchor``'s release hasn't
    got, naming the release that has it: a plugin's ``iki:`` must admit only those."""
    newest = migration_api.anchors()[-1]
    if anchor == newest:
        return
    loader = MigrationLoader(None, ignore_no_migrations=True)
    at = loader.project_state(anchor.key, at_end=True).models
    for label in sorted(_refers(migration.operations)):
        app, _, model = label.partition(".")
        if not _is_core(app) or (app, model) in at:
            continue
        later = [a for a in migration_api.anchors() if a.release > anchor.release]
        has = next(
            (a for a in later if (app, model) in loader.project_state(a.key, at_end=True).models),
            newest,
        )
        plugin = naming.plugin_of_label(migration.app_label)
        raise WriteError(
            f"{plugin} {migration.name} refers to {label}, which Iki {has.version} adds, and "
            f'{plugin}\'s `iki:` admits {anchor.version}. Raise it to `">={has.version}"`.'
        )


def _refers(operations: Iterable[Operation]) -> set[str]:
    """The models the operations' fields refer to, and their models' bases, by label."""
    found: set[str] = set()
    for op in operations:
        fields = [f for _, f in getattr(op, "fields", ())]
        if hasattr(op, "field"):
            fields.append(op.field)
        for field in fields:
            target = getattr(getattr(field, "remote_field", None), "model", None)
            if isinstance(target, SettingsReference):
                target = getattr(settings, _setting(target))
            if isinstance(target, str):
                found.add(target.lower())
        found.update(b.lower() for b in getattr(op, "bases", ()) if isinstance(b, str))
    return found


def dependencies(migration: Migration, anchor: migration_api.Anchor) -> list[tuple[str, str]]:
    """The migration's dependencies, with each of Iki's own migrations (and the user model's
    swappable one) replaced by ``anchor``. A plugin's first migration depends on it too."""
    kept: list[tuple[str, str]] = []
    anchored = False
    for app, name in migration.dependencies:
        if app == migration_api.ANCHORS_APP or _is_core(app):
            anchored = True
            continue
        kept.append((app, name))
    if anchored or not any(app == migration.app_label for app, _ in kept):
        kept.append(anchor.key)
    return sorted(set(kept))


def _is_core(app: str) -> bool:
    plugin = naming.plugin_of_label(app)
    return app == "__setting__" or plugin is None or plugin == naming.CORE


def render(migration: Migration, dependencies: Iterable[tuple[str, str]]) -> str:
    """The migration's file."""
    if migration.replaces:
        raise WriteError("A plugin migration doesn't replace others: Iki doesn't squash them.")
    if migration.run_before:
        raise WriteError("A plugin migration can't run before another app's.")
    if not migration.atomic:
        raise WriteError("A plugin migration runs in its deployment's transaction.")
    imports: set[str] = set()
    body = "".join(_operation(op, imports) for op in migration.operations)
    needs = "".join(f"        {_value(tuple(d), imports)},\n" for d in dependencies)
    return (
        HEADER
        + "\n"
        + "".join(f"{line}\n" for line in sorted(imports))
        + f"from iki_server.plugins import operations as {ALIAS}\n\n\n"
        + f"class Migration({ALIAS}.Migration):\n"
        + ("\n    initial = True\n" if migration.initial else "")
        + f"\n    dependencies = [\n{needs}    ]\n"
        + f"\n    operations = [\n{body}    ]\n"
    )


def _operation(op: Operation, imports: set[str], indent: int = 2) -> str:
    wrapped = _wrap(op)
    name, args, kwargs = wrapped.deconstruct()
    parameters = [
        p
        for p in inspect.signature(type(wrapped).__init__).parameters.values()
        if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    named = dict(zip((p.name for p in parameters), args, strict=False))
    named.update(kwargs)
    unknown = set(named) - {p.name for p in parameters}
    if unknown:
        raise WriteError(f"{name} has arguments Iki's {name} doesn't take: {sorted(unknown)}.")
    pad = "    " * indent
    out = f"{pad}{ALIAS}.{name}(\n"
    for parameter in parameters:
        if parameter.name not in named:
            continue
        value = named[parameter.name]
        if parameter.name in _EXPANDED and isinstance(value, list | tuple):
            out += f"{pad}    {parameter.name}=[\n"
            out += "".join(f"{pad}        {_value(item, imports)},\n" for item in value)
            out += f"{pad}    ],\n"
        elif parameter.name == "bases":
            out += f"{pad}    bases={_value(tuple(_label(b) for b in value), imports)},\n"
        else:
            out += f"{pad}    {parameter.name}={_value(value, imports)},\n"
    return out + f"{pad}),\n"


def _wrap(op: Operation) -> Any:  # noqa: ANN401 - one of Iki's operations
    if type(op).__module__ == ops.__name__ and type(op).__name__ in ops.__all__:
        return op
    iki = ops.OF_DJANGO.get(type(op))
    if iki is None:
        raise WriteError(
            f"Django made a {type(op).__name__} operation, which Iki's migration API has no "
            "wrapper for, so a later Iki couldn't be sure to read it. This is a bug in Iki: "
            "please report it, with the manifest change that made it."
        )
    return iki.of(op)


class _Name(str):
    """A wrapper's name, written as it is rather than quoted."""


def _label(value: Any) -> Any:  # noqa: ANN401 - a model label, or a model
    if isinstance(value, SettingsReference):
        if _setting(value) == "AUTH_USER_MODEL":
            return _Name(f"{ALIAS}.USER")
        raise WriteError(f"A plugin model refers to the setting {_setting(value)}.")
    if isinstance(value, str):
        constant = _LABELS.get(value.lower())
        if constant is not None:
            return _Name(f"{ALIAS}.{constant}")
        # A type's model, the core plugin's included, is named as its type is (ADR 0033).
        if naming.plugin_of_label(value.partition(".")[0]) is None:
            raise WriteError(
                f"A plugin model refers to Iki's model {value}, which Iki's migration API has "
                f"no name for: plugin tables refer to {', '.join(sorted(_LABELS))}."
            )
        return value
    raise WriteError(f"A plugin model's base is {value!r}, not a model's label.")


def _setting(reference: SettingsReference) -> str:
    """The setting a reference to a model is (``AUTH_USER_MODEL``)."""
    return str(getattr(reference, "setting_name"))  # noqa: B009 - set by Django, not in its stubs


def _value(value: Any, imports: set[str]) -> str:  # noqa: ANN401 - every kind of value a migration holds
    if isinstance(value, _Name):
        return str(value)
    if isinstance(value, SettingsReference):
        return _value(_label(value), imports)
    if value is None or isinstance(value, bool | int):
        return repr(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "float('nan')"
        if math.isinf(value):
            return "float('inf')" if value > 0 else "float('-inf')"
        return repr(value)
    if isinstance(value, str | bytes):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v, imports) for v in value) + "]"
    if isinstance(value, tuple):
        items = [_value(v, imports) for v in value]
        return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{_value(k, imports)}: {_value(v, imports)}" for k, v in sorted(value.items())
            )
            + "}"
        )
    if isinstance(value, set | frozenset):
        if not value:
            return "set()"
        return "{" + ", ".join(sorted(_value(v, imports) for v in value)) + "}"
    if isinstance(value, datetime.date | datetime.time | datetime.timedelta):
        tz = getattr(value, "tzinfo", None)
        if tz is not None and tz is not datetime.UTC:
            raise WriteError(f"{value!r} isn't in UTC.")
        imports.add("import datetime")
        return repr(value)
    if isinstance(value, decimal.Decimal):
        imports.add("import decimal")
        return f"decimal.Decimal({str(value)!r})"
    if isinstance(value, uuid.UUID):
        imports.add("import uuid")
        return f"uuid.UUID({str(value)!r})"
    if id(value) in _CONSTANTS:
        return f"{ALIAS}.{_CONSTANTS[id(value)]}"
    if isinstance(value, Operation):
        return _operation(value, imports).strip().rstrip(",")
    return _construct(value, imports)


def _construct(value: Any, imports: set[str]) -> str:  # noqa: ANN401 - a field, index, constraint or expression
    how = ops.WRITTEN_AS.get(type(value))
    if how is None:
        raise WriteError(
            f"A plugin migration would hold a {type(value).__module__}.{type(value).__name__}, "
            "which Iki's migration API has no wrapper for. This is a bug in Iki: please "
            "report it, with the manifest change that made it."
        )
    if isinstance(value, Field):
        _, _, args, kwargs = value.deconstruct()
    else:
        _, args, kwargs = value.deconstruct()
    arguments = [_value(a, imports) for a in args]
    named = {}
    for key, given in kwargs.items():
        if key in how.implied and _same(how.implied[key], given):
            continue
        named[how.renamed.get(key, key)] = _label(given) if key == "to" else given
    # In order of name, as Django's own writer does, so a new Django's order doesn't matter.
    arguments += [f"{key}={_value(named[key], imports)}" for key in sorted(named)]
    return f"{ALIAS}.{how.name}({', '.join(arguments)})"


def _same(left: Any, right: Any) -> bool:  # noqa: ANN401 - literals
    return type(left) is type(right) and bool(left == right)
