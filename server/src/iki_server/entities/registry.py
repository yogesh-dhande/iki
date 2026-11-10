"""The installed plugins and their types, read from what `iki plugin build` wrote.

Each generated app holds its plugin's manifest as ``manifest.json``, so the
server knows every type's fields, units and rules without the YAML.
"""

import json
from functools import cache
from pathlib import Path

from django.apps import apps
from django.db import models

from iki_spec.model import BUILTIN_TYPES, EntityType, Extension, Plugin, plugin_from_dict


@cache
def plugins() -> dict[str, Plugin]:
    """Installed plugins by name, dependencies first."""
    out: dict[str, Plugin] = {}
    for config in apps.get_app_configs():
        if config.name.startswith("iki_plugin_"):
            data = json.loads((Path(config.path) / "manifest.json").read_text("utf-8"))
            out[config.label] = plugin_from_dict(data["plugin"])
    return out


@cache
def types() -> dict[str, EntityType]:
    """Every type by qualified name, ``core.Entity`` included."""
    out = dict(BUILTIN_TYPES)
    for plugin in plugins().values():
        out.update(plugin.types)
    return out


def extensions() -> list[Extension]:
    return [e for plugin in plugins().values() for e in plugin.extensions]


def ancestors(entity_type: EntityType) -> list[EntityType]:
    """Parent types, nearest first, without ``core.Entity``."""
    out = []
    every = types()
    current = entity_type
    while current.extends and current.extends != "core.Entity":
        current = every[current.extends]
        out.append(current)
    return out


def identity_owner(entity_type: EntityType) -> EntityType | None:
    """The type whose identity ``entity_type``'s entities have: its own or the nearest parent's."""
    return next((t for t in [entity_type, *ancestors(entity_type)] if t.identity), None)


def model(qualified: str) -> type[models.Model]:
    plugin, name = qualified.split(".")
    return apps.get_model(plugin, name)
