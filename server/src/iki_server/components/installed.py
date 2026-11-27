"""The components installed plugins provide, and their modules, as the build copied them.

`iki deploy build` copies each component's module into its plugin's app, as
``components/<name>.js``, after checking it against the digest it had when the plugin
was read. The server reads it from there, and checks the digest again, so what a frame
runs is what was reviewed.
"""

import hashlib
from functools import cache
from pathlib import Path

from django.apps import apps

from iki_server.entities import registry
from iki_spec import naming
from iki_spec.model import Component


@cache
def components() -> dict[str, Component]:
    """Installed components by qualified name."""
    return {
        c.qualified_name: c
        for plugin in registry.plugins().values()
        for c in plugin.components.values()
    }


def for_type(type_name: str, slot: str) -> list[Component]:
    """The components whose `slot` shows on the pages of entities of `type_name`, which may
    be a subtype of the type they name, in plugin and component order."""
    lineage = {type_name}
    if type_name in registry.types():
        lineage |= set(registry.effective(type_name).ancestors) - {naming.ENTITY}
    return [
        c
        for c in components().values()
        if any(s.slot == slot and s.type in lineage for s in c.slots)
    ]


class ModuleChanged(Exception):  # noqa: N818 - reads as "the module changed"
    """A component's module isn't what the build checked: someone changed the image."""


@cache
def module(qualified_name: str) -> bytes:
    """The component's module, as the build copied it. Raises :class:`KeyError` for one
    that isn't installed, :class:`ModuleChanged` if its digest doesn't match."""
    component = components()[qualified_name]
    config = apps.get_app_config(naming.app_label(component.plugin))
    data = (Path(config.path) / "components" / f"{component.name}.js").read_bytes()
    if hashlib.sha256(data).hexdigest() != component.sha256:
        raise ModuleChanged(qualified_name)
    return data
