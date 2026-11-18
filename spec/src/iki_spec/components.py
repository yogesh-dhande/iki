"""Components: reading their manifests, and the rules beyond them (ADR 0084, ADR 0117).

A plugin's components live under `components/`, each a Web Component in one JavaScript
module that Iki runs in a sandboxed frame. The schema checks each document's shape; the
rules here check that the module is a file in the plugin, a UTF-8 text file of at most
10 MiB and no symbolic link, that the types its slots and selections name exist, and
that no two components define the same element. Each module's digest is kept, so the
build copies what was checked.

A component that shows many entities says which: their type and the fields it reads,
each of which must be the type's. Its slots' searches name the type's fields (or the
columns every entity has) with the operators each kind takes, sort by one that sorts,
and take the place's context only where there is one: `{{ entity }}` on a tab, as a
reference to its type, and `{{ me }}` where a person is named. A section on a person's
home must name them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path as FilePath
from typing import TYPE_CHECKING, Any

from iki_spec import conditions, schemas
from iki_spec import validate as _validate
from iki_spec.model import (
    CONTEXT,
    Component,
    ComponentEntities,
    ComponentSearch,
    ComponentSlot,
    EntityType,
    Field,
    qualify,
)
from iki_spec.problems import Path

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = ["MAX_MODULE_BYTES", "check", "context_of", "read_component"]

#: The largest module a component may have: Mol* and its like fit, bundled.
MAX_MODULE_BYTES = 10 * 1024**2
#: The context a condition's value can be: `{{ entity }}` or `{{ me }}`, and nothing else.
_CONTEXT = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
#: The columns every entity has that a declared search can name.
_COLUMNS = ("hrid", "created_by", "created_at", "updated_at")
#: Fields `{{ me }}` can be compared with. Step 57's fields that name a person join it.
_PEOPLE = ("created_by",)


def context_of(value: Any) -> str | None:  # noqa: ANN401 - JSON
    """The context a condition's value names (``entity``, ``me``), if it's one and nothing
    else; ``""`` for text that has ``{{`` in it but isn't; None for any other value."""
    if not isinstance(value, str) or "{{" not in value:
        return None
    found = _CONTEXT.fullmatch(value.strip())
    return found.group(1) if found and found.group(1) in CONTEXT else ""


def read_component(plugin: str, body: dict[str, Any], document: Document) -> Component:
    name = body["name"]
    label = body.get("label") or name.replace("_", " ").capitalize()
    selects = (body.get("events") or {}).get("select")
    many = body.get("entities")
    entities = (
        ComponentEntities(
            type=qualify(many["type"], plugin),
            fields=tuple(many["fields"]),
            limit=many.get("limit", 500),
        )
        if many
        else None
    )
    return Component(
        plugin=plugin,
        name=name,
        label=label,
        description=body.get("description"),
        module=body["module"],
        element=body["element"],
        slots=tuple(_slot(s, plugin, label, entities) for s in body["slots"]),
        entities=entities,
        selects=qualify(selects["type"], plugin) if selects else "",
        file=document.file,
    )


def _slot(
    body: Mapping[str, Any], plugin: str, label: str, entities: ComponentEntities | None
) -> ComponentSlot:
    search = body.get("search")
    # A tab is on its type's pages; the other places show the component's entities.
    shown = qualify(body["type"], plugin) if "type" in body else (entities.type if entities else "")
    return ComponentSlot(
        slot=body["slot"],
        type=shown,
        label=body.get("label") or label,
        search=ComponentSearch(
            where=search.get("where"),
            sort=search.get("sort", ""),
            descending=search.get("descending", False),
        )
        if search is not None
        else None,
    )


def check(semantics: _validate._Semantics) -> dict[str, Component]:
    """Check the plugin's components. Returns them with each module's digest."""
    out: dict[str, Component] = {}
    elements: dict[str, str] = {}
    for name, component in semantics.plugin.components.items():
        source = semantics.loaded.component_sources[name]
        _Rules(semantics, component, source).run()
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


class _Rules:
    """One component's entities and slots."""

    def __init__(
        self, semantics: _validate._Semantics, component: Component, source: Document
    ) -> None:
        self.s = semantics
        self.add = semantics.add
        self.component = component
        self.source = source
        #: The entities' type's fields, once it's known to exist.
        self.fields: dict[str, Field] | None = None
        self.shown: EntityType | None = None

    def run(self) -> None:
        entities = self.component.entities
        if entities is not None:
            self.shown = self.s.reference(entities.type, self.source, ("entities", "type"))
            if self.shown is not None:
                self.fields = self.fields_of(self.shown)
                for index, name in enumerate(entities.fields):
                    self.field(name, ("entities", "fields", index))
        seen: dict[tuple[str, str], int] = {}
        for index, slot in enumerate(self.component.slots):
            self.slot(slot, index, seen)

    def fields_of(self, entity_type: EntityType) -> dict[str, Field]:
        """The type's fields: its own, its parents', and those this plugin's extensions and
        its dependencies' add."""
        fields = {n: f for n, (f, _) in self.s.all_fields(entity_type).items()}
        chain = {
            entity_type.qualified_name,
            *(t.qualified_name for t in self.s.ancestors(entity_type)),
        }
        plugins = [self.s.plugin, *(o.plugin for o in self.s.others.values() if o.plugin)]
        for plugin in plugins:
            for extension in plugin.extensions:
                if extension.extends_type in chain:
                    fields.update(extension.fields)
        return {n: f for n, f in fields.items() if not f.removed}

    def field(self, name: str, path: Path) -> Field | None:
        assert self.fields is not None and self.shown is not None  # noqa: S101 - checked first
        found = self.fields.get(name)
        if found is None:
            self.add(
                "unknown-field",
                f"`{self.shown.qualified_name}` has no field `{name}`.",
                self.source,
                path,
                hint=schemas._did_you_mean(name, list(self.fields)),
            )
        return found

    def slot(self, slot: ComponentSlot, index: int, seen: dict[tuple[str, str], int]) -> None:
        at: Path = ("slots", index)
        tab = slot.slot == "entity.tab"
        if tab:
            self.s.reference(slot.type, self.source, (*at, "type"))
        elif self.component.entities is None:
            self.add(
                "slot-needs-entities",
                f"`{slot.slot}` shows many entities, and this component doesn't say which.",
                self.source,
                (*at, "slot"),
                hint="Add `entities:` with their `type` and the `fields` it reads.",
            )
            return
        key = (slot.slot, slot.type if tab else "")
        if key in seen:
            self.add(
                "slot-twice",
                f"Slot {seen[key]} is `{slot.slot}`"
                + (f" for `{slot.type}`" if tab else "")
                + " too.",
                self.source,
                (*at, "slot"),
                hint="Give each place one slot.",
            )
        seen.setdefault(key, index)
        if slot.search is None:
            return
        if self.component.entities is None:
            self.add(
                "slot-needs-entities",
                "A tab's `search` finds many entities, and this component doesn't say which.",
                self.source,
                (*at, "search"),
                hint="Add `entities:` with their `type` and the `fields` it reads.",
            )
            return
        if self.fields is None:
            return  # its type was reported
        found = _Search(self, slot, (*at, "search"))
        found.run()
        if slot.slot == "person.home" and "me" not in found.context:
            self.add(
                "home-not-personal",
                "A section on a person's home is theirs: its search names them, with `{{ me }}`.",
                self.source,
                (*at, "search"),
                hint='Add a condition such as `{ field: created_by, op: eq, value: "{{ me }}" }`.',
            )


def _size(node: Mapping[str, Any]) -> tuple[int, int]:
    """How deep a search's conditions nest, and how many there are."""
    for key in ("all", "any"):
        if key in node:
            sizes = [_size(child) for child in node[key]]
            return 1 + max((d for d, _ in sizes), default=0), sum(c for _, c in sizes)
    if "not" in node:
        depth, count = _size(node["not"])
        return depth + 1, count
    return 1, 1


class _Search:
    """A slot's declared search: its conditions, its sort and its context."""

    def __init__(self, rules: _Rules, slot: ComponentSlot, at: Path) -> None:
        self.rules = rules
        self.slot = slot
        self.at = at
        self.add = rules.add
        self.source = rules.source
        #: The context its conditions name.
        self.context: set[str] = set()

    def run(self) -> None:
        search = self.slot.search
        assert search is not None  # noqa: S101 - only slots with one
        if search.where is not None:
            self.bounded(search.where, (*self.at, "where"))
            self.condition(search.where, (*self.at, "where"))
        if search.sort:
            kind, _ = self.kind(search.sort, (*self.at, "sort"))
            if kind is not None and kind not in conditions.SORTABLE:
                self.add(
                    "unsortable-field",
                    f"`{search.sort}` can't be sorted by.",
                    self.source,
                    (*self.at, "sort"),
                    hint="Sort by text, a number, a date or yes/no.",
                )

    def bounded(self, where: Mapping[str, Any], path: Path) -> None:
        """Within what a refinement can be added to (ADR 0117)."""
        depth, count = _size(where)
        if depth > conditions.MAX_DEPTH or count > conditions.MAX_CONDITIONS:
            self.add(
                "search-too-big",
                f"A component's search may have at most {conditions.MAX_CONDITIONS} "
                f"conditions, {conditions.MAX_DEPTH} levels deep: this one has {count}, "
                f"{depth} deep.",
                self.source,
                path,
                hint="Split it between slots, or let the component refine it.",
            )

    def kind(self, name: str, path: Path) -> tuple[str | None, Field | None]:
        """The kind of value a field (or a column every entity has) holds."""
        if name in _COLUMNS:
            return conditions.BUILT_IN[name], None
        found = self.rules.field(name, path)
        return (found.type, found) if found is not None else (None, None)

    def condition(self, node: Mapping[str, Any], path: Path) -> None:
        for key in ("all", "any"):
            if key in node:
                for index, child in enumerate(node[key]):
                    self.condition(child, (*path, key, index))
                return
        if "not" in node:
            self.condition(node["not"], (*path, "not"))
            return
        kind, found = self.kind(node["field"], (*path, "field"))
        if kind is None:
            return
        op = node["op"]
        allowed = ("is_set", *conditions.OPERATORS[kind])
        if op not in allowed:
            self.add(
                "unknown-operator",
                f"`{node['field']}` takes {', '.join(f'`{o}`' for o in allowed)}, not `{op}`.",
                self.source,
                (*path, "op"),
            )
            return
        self.value(node, kind, found, path)

    def value(self, node: Mapping[str, Any], kind: str, found: Field | None, path: Path) -> None:
        values = node["value"] if isinstance(node["value"], list) else [node["value"]]
        for value in values:
            context = context_of(value)
            if context is None:
                continue
            at: Path = (*path, "value")
            if context == "":
                self.add(
                    "unknown-context",
                    f"`{value}` isn't a value a search can take: `{{{{ entity }}}}` and "
                    "`{{ me }}` are, as the whole value.",
                    self.source,
                    at,
                )
            elif context == "entity":
                self.entity(node, kind, found, at)
            elif node["field"] not in _PEOPLE:
                self.add(
                    "context-misplaced",
                    f"`{{{{ me }}}}` is a person, so it's compared with "
                    f"{', '.join(f'`{p}`' for p in _PEOPLE)}, not `{node['field']}`.",
                    self.source,
                    at,
                )
            self.context.add(context)

    def entity(self, node: Mapping[str, Any], kind: str, found: Field | None, at: Path) -> None:
        if self.slot.slot != "entity.tab":
            self.add(
                "context-misplaced",
                f"`{{{{ entity }}}}` is the entity whose page it is: only a tab has one, not "
                f"`{self.slot.slot}`.",
                self.source,
                at,
            )
            return
        target = found.items if found is not None and kind == "list" else found
        to = target.to if target is not None and target.type == "ref" else None
        if to is None:
            self.add(
                "context-misplaced",
                f"`{{{{ entity }}}}` is an entity, so it's compared with a reference, not "
                f"`{node['field']}`.",
                self.source,
                at,
            )
            return
        page = self.rules.s.types.get(self.slot.type)
        chain = (
            {page.qualified_name, *(t.qualified_name for t in self.rules.s.ancestors(page))}
            if page is not None
            else set()
        )
        if page is not None and to not in chain and to != "iki/core.Entity":
            self.add(
                "context-misplaced",
                f"`{node['field']}` refers to `{to}`, and this tab is on `{self.slot.type}`'s "
                "pages.",
                self.source,
                at,
                hint="Compare `{{ entity }}` with a reference to the tab's type.",
            )


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
