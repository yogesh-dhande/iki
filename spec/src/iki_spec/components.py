"""Components: reading their manifests, and the rules beyond them (ADR 0084).

A plugin's components live under `components/`, each a Web Component in one JavaScript
module that Iki runs in a sandboxed frame. The schema checks each document's shape; the
rules here check that the module is a file in the plugin, a UTF-8 text file of at most
10 MiB and no symbolic link, that the types its slots and selections name exist, and
that no two components define the same element. Each module's digest is kept, so the
build copies what was checked.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Any

from iki_spec import validate as _validate
from iki_spec.model import Component, ComponentSlot, qualify

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = ["MAX_MODULE_BYTES", "check", "read_component"]

#: The largest module a component may have: Mol* and its like fit, bundled.
MAX_MODULE_BYTES = 10 * 1024**2


def read_component(plugin: str, body: dict[str, Any], document: Document) -> Component:
    name = body["name"]
    label = body.get("label") or name.replace("_", " ").capitalize()
    selects = (body.get("events") or {}).get("select")
    return Component(
        plugin=plugin,
        name=name,
        label=label,
        description=body.get("description"),
        module=body["module"],
        element=body["element"],
        slots=tuple(
            ComponentSlot(
                slot=s["slot"], type=qualify(s["type"], plugin), label=s.get("label") or label
            )
            for s in body["slots"]
        ),
        selects=qualify(selects["type"], plugin) if selects else "",
        file=document.file,
    )


def check(semantics: _validate._Semantics) -> dict[str, Component]:
    """Check the plugin's components. Returns them with each module's digest."""
    out: dict[str, Component] = {}
    elements: dict[str, str] = {}
    for name, component in semantics.plugin.components.items():
        source = semantics.loaded.component_sources[name]
        for index, slot in enumerate(component.slots):
            semantics.reference(slot.type, source, ("slots", index, "type"))
        if component.selects:
            semantics.reference(component.selects, source, ("events", "select", "type"))
        other = elements.setdefault(component.element, name)
        if other != name:
            semantics.add(
                "element-twice",
                f"`{other}` defines `{component.element}` too.",
                source,
                ("element",),
                hint="Give each component an element of its own.",
            )
        digest = _module(semantics, component, source)
        out[name] = dataclasses.replace(component, sha256=digest or "")
    return out


def _module(semantics: _validate._Semantics, component: Component, source: Document) -> str | None:
    """The module's SHA-256, or None after saying why it can't be used."""
    root: FilePath = semantics.loaded.directory
    relative = component.module.removeprefix("./")
    target = root / relative
    path = ("module",)
    if any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(root)):
        semantics.add(
            "symlink",
            f"`{component.module}` is a symbolic link.",
            source,
            path,
            hint="Copy the file into the plugin instead, so what's reviewed is what's built.",
        )
        return None
    if not target.is_file():
        semantics.add(
            "missing-file",
            f"There's no file `{component.module}` in this plugin.",
            source,
            path,
            hint="Bundle the component into one module, with everything it needs.",
        )
        return None
    data = target.read_bytes()
    if len(data) > MAX_MODULE_BYTES:
        semantics.add(
            "component-too-big",
            f"`{component.module}` is {len(data) / 1024**2:.1f} MiB, more than the 10 MiB a "
            "component's module may be.",
            source,
            path,
            hint="Leave out what it doesn't use, and minify it.",
        )
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        semantics.add(
            "unreadable-file", f"`{component.module}` isn't a UTF-8 text file.", source, path
        )
        return None
    return hashlib.sha256(data).hexdigest()
