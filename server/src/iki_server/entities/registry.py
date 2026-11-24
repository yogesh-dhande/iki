"""The installed plugins and their types, read from what `iki deploy build` wrote.

Each generated app holds its plugin's manifest as ``manifest.json``, so the
server knows every type's fields, units and rules without the YAML, and the
deployment's settings for it as ``settings.json``.
"""

import json
from functools import cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from iki_spec import naming
from iki_spec.model import (
    BUILTIN_TYPES,
    EntityType,
    Export,
    Extension,
    Plugin,
    ReportExport,
    Viewer,
    plugin_from_dict,
)


@cache
def plugins() -> dict[str, Plugin]:
    """Installed plugins by name (``iki/protein``), dependencies first."""
    out: dict[str, Plugin] = {}
    for config in apps.get_app_configs():
        if config.name.startswith("iki_plugin_"):
            data = json.loads((Path(config.path) / "manifest.json").read_text("utf-8"))
            plugin = plugin_from_dict(data["plugin"])
            out[plugin.name] = plugin
    return out


@cache
def types() -> dict[str, EntityType]:
    """Every type by qualified name, ``iki/core.Entity`` included."""
    out = dict(BUILTIN_TYPES)
    for plugin in plugins().values():
        out.update(plugin.types)
    return out


def draws_received(type_name: str) -> str | None:
    """The record field its amounts ledger maintains, including an inherited declaration."""
    t = types()[type_name]
    return next((x.draws_received for x in [t, *ancestors(t)] if x.draws_received), None)


def extensions() -> list[Extension]:
    return [e for plugin in plugins().values() for e in plugin.extensions]


def ancestors(entity_type: EntityType) -> list[EntityType]:
    """Parent types, nearest first, without ``iki/core.Entity``."""
    out = []
    every = types()
    current = entity_type
    while current.extends and current.extends != naming.ENTITY:
        current = every[current.extends]
        out.append(current)
    return out


def sample_types() -> set[str]:
    """The types whose records are samples (ADR 0177): those that say so, and their
    subtypes."""
    every = types()
    return {name for name, t in every.items() if any(a.sample for a in [t, *ancestors(t)])}


def identity_owner(entity_type: EntityType) -> EntityType | None:
    """The type whose identity ``entity_type``'s entities have: its own or the nearest parent's."""
    return next((t for t in [entity_type, *ancestors(entity_type)] if t.identity), None)


def display_title(entity_type: EntityType) -> str | None:
    """The title template for ``entity_type``'s entities: its own or the nearest parent's."""
    return next(
        (t.display_title for t in [entity_type, *ancestors(entity_type)] if t.display_title), None
    )


def viewers(entity_type: EntityType) -> tuple[Viewer, ...]:
    """Iki's own viewers on ``entity_type``'s pages: its own or the nearest parent's."""
    return next((t.viewers for t in [entity_type, *ancestors(entity_type)] if t.viewers), ())


def exports(entity_type: EntityType) -> tuple[Export | ReportExport, ...]:
    """The files Iki writes of ``entity_type``'s records (ADR 0171): its own or the nearest
    parent's."""
    return next((t.exports for t in [entity_type, *ancestors(entity_type)] if t.exports), ())


def has_entries(qualified: str) -> bool:
    """Whether a type's entities are registry entries with records (ADR 0115): it has an
    identity, its own or a parent's."""
    found = types().get(qualified)
    return found is not None and identity_owner(found) is not None


def model(qualified: str) -> type[models.Model]:
    """The Django model of a type's records, by its qualified name (``iki/protein.Protein``)."""
    plugin, name = qualified.split(".")
    return apps.get_model(naming.app_label(plugin), name)


def entry_model(qualified: str) -> type[models.Model] | None:
    """The Django model of a type's registry entries (ADR 0115). A subtype's is under its
    parent's, as its records' is (ADR 0165), with the registry fields it adds. None for a
    type without an identity."""
    found = types().get(qualified)
    owner = identity_owner(found) if found is not None else None
    if found is None or owner is None:
        return None
    return apps.get_model(naming.app_label(found.plugin), naming.entry_model(found.name))


def app(plugin: str) -> Any:  # noqa: ANN401 - Django's AppConfig
    """A plugin's Django app, by its name (``iki/protein``)."""
    return apps.get_app_config(naming.app_label(plugin))


@cache
def settings(plugin: str) -> dict[str, Any]:
    """The deployment's values for a plugin's settings, with defaults, as the build checked them."""
    file = Path(app(plugin).path) / "settings.json"
    if not file.is_file():
        raise ImproperlyConfigured(
            f"The {plugin} plugin was built without a deployment's settings. "
            "Build the plugins with `iki deploy build`."
        )
    values: dict[str, Any] = json.loads(file.read_text("utf-8"))
    return values


#: What a plugin from a local directory's bundle is named for, in place of its commit:
#: development builds only (ADR 0061).
LOCAL = "local"


def bundle(name: str, sha: str) -> Path | None:
    """The bundle of `name`'s code at `sha` that ``iki deploy build`` wrote, for workers to
    fetch (ADR 0059), if this build installs that plugin at that commit. ``local`` names a
    plugin from a local directory, in a development build."""
    directory = bundles()
    if directory is None:
        return None
    for entry in (installed() or {}).get("plugins", ()):
        if entry.get("name") == name and (entry.get("sha") or LOCAL) == sha and entry.get("bundle"):
            found = directory / bundle_file(name, sha)
            return found if found.is_file() else None
    return None


def bundle_file(name: str, sha: str) -> str:
    """The file a plugin's bundle is in: ``iki__protein-<sha>.tar.gz``, by its label, since a
    file name can't hold the ``/`` of its name."""
    return f"{naming.app_label(name)}-{sha}.tar.gz"


def bundles() -> Path | None:
    """Where ``iki deploy build`` put the plugins' bundles."""
    spec = find_spec("iki_installed_plugins")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))) / "bundles"


@cache
def installed() -> dict[str, Any] | None:
    """Where each plugin came from: the lockfile the build read, and each plugin's commit.

    None when the plugins weren't built from a lockfile (``iki plugin build``).
    """
    spec = find_spec("iki_installed_plugins")
    if spec is None or not spec.submodule_search_locations:
        return None
    file = Path(next(iter(spec.submodule_search_locations))) / "lock.json"
    if not file.is_file():
        return None
    record: dict[str, Any] = json.loads(file.read_text("utf-8"))
    return record


def detached() -> frozenset[str]:
    """Plugins the lockfile detaches (ADR 0091): their data stays and can be read, but
    nothing changes it, and their actions and triggers are off."""
    return frozenset(
        entry["name"]
        for entry in (installed() or {}).get("plugins", ())
        if entry.get("state") == "detached"
    )


def purged() -> frozenset[str]:
    """Plugins the lockfile purges (ADR 0092): not built, and their tables dropped by the
    migrate Job once their purge is due and every entity has its tombstone."""
    return frozenset(
        entry["name"]
        for entry in (installed() or {}).get("plugins", ())
        if entry.get("state") == "purged"
    )


def detached_tables() -> list[str]:
    """The tables of detached plugins, which the app's database role can only read."""
    return sorted(
        model._meta.db_table
        for name in detached()
        if name in plugins()
        for model in app(name).get_models(include_auto_created=True)
    )
