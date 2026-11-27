"""Read a proposed build's manifest.json as data for preflight (ADR 0181)."""

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import TypeAdapter, ValidationError
from referencing.jsonschema import DRAFT202012

from iki_server.entities import registry
from iki_spec import API_VERSION, schemas, units
from iki_spec.effective import lineage
from iki_spec.model import (
    BUILTIN_TYPES,
    FIELD_TYPES,
    FORMATS,
    EntityType,
    Field,
    Guard,
    Plugin,
    Rule,
    plugin_from_dict,
)
from iki_spec.naming import ENTITY
from iki_spec.validate import RESERVED_FIELD_NAMES, check_compiled_rules, pattern_problems

_PLUGIN = TypeAdapter(Plugin)


def read(
    name: str, plugin: dict[str, Any], *, installed: Mapping[str, Plugin] | None = None
) -> Plugin:
    """Check the compiled dataclasses and the names and expressions preflight will inspect."""
    if not {"types", "extensions", "settings", "actions", "environments"} <= plugin.keys():
        raise ValueError("Choose the manifest.json from the compiled plugin.")
    try:
        _PLUGIN.validate_json(json.dumps(plugin, allow_nan=False), strict=True, extra="forbid")
    except ValidationError as error:
        issue = error.errors(include_input=False)[0]
        raise ValueError(f"{'.'.join(map(str, issue['loc']))}: {issue['msg']}") from error
    # The reader used by a built image preserves integer numeric bounds. Pydantic's
    # float coercion (2 -> 2.0) would give this same manifest a different SHA-256.
    try:
        proposed = plugin_from_dict(plugin)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(
            "Malformed compiled plugin. Choose the package's manifest.json."
        ) from error
    if proposed.name != name:
        raise ValueError(
            f"The manifest is for {proposed.name}, but the requested plugin is {name}."
        )
    _named("pluginName", name)
    manifest: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "Plugin",
        "name": name,
        "version": proposed.version,
    }
    # Compiled manifests include qualified IDs and explicit defaults. Check the
    # original declaration schema too, including the supported readers and viewers.
    if proposed.formats:
        manifest["formats"] = {
            key.removeprefix(f"{name}."): {
                k: v
                for k, v in value.items()
                if k not in {"id", "order"} and v is not None and (k != "signatures" or v)
            }
            for key, value in plugin["formats"].items()
        }
    if issues := schemas.check("plugin", manifest):
        raise ValueError(issues[0].message)

    if installed is None:
        installed = registry.plugins()
    types = dict(BUILTIN_TYPES)
    types.update({q: t for p in installed.values() if p.name != name for q, t in p.types.items()})
    types.update(proposed.types)
    for qualified, entity_type in proposed.types.items():
        _named("typeName", entity_type.name)
        if entity_type.plugin != name or entity_type.qualified_name != qualified:
            raise ValueError(f"{qualified} must be a type declared by {name} under its own name.")
        _ancestors(entity_type, types)
        _fields(entity_type.fields, types)
        _rules(entity_type.rules, name, extension=False)
        _guards(entity_type.draw_guards)
    for ext in proposed.extensions:
        if ext.plugin != name or ext.extends_type not in types:
            raise ValueError(f"Invalid extension of {ext.extends_type} by {ext.plugin}.")
        _fields(ext.fields, types)
        _rules(ext.rules, name, extension=True)
    return check_compiled_rules(proposed, installed)


def _named(kind: str, name: str) -> None:
    if issues := schemas.check("field", name, part=kind):
        raise ValueError(f"Invalid {kind} {name!r}: {issues[0].message}")


def _ancestors(entity_type: EntityType, types: Mapping[str, EntityType]) -> None:
    """Each of its ancestors is a type the deployment would have, and none is itself."""
    chain = lineage(types, entity_type.qualified_name)
    top = types.get(chain[-2]) if len(chain) > 1 else entity_type
    parent = top.extends if top is not None else chain[-2]
    if top is None:
        raise ValueError(f"Unknown parent type {parent}.")
    if parent is not None and parent != ENTITY:
        raise ValueError(f"Circular inheritance at {parent}.")


def _fields(fields: Mapping[str, Field], types: Mapping[str, EntityType]) -> None:
    if len(fields) > 500:
        raise ValueError("A type or extension can declare at most 500 fields.")
    for name, f in fields.items():
        _named("fieldName", name)
        if (
            f.name != name
            or name in RESERVED_FIELD_NAMES
            or name.startswith("iki_")
            or name.endswith("_ptr")
        ):
            raise ValueError(f"Invalid field name {name!r} for {f.name!r}.")
        _field(f, types)


def _field(f: Field, types: Mapping[str, EntityType]) -> None:
    if f.type not in (*FIELD_TYPES, "state"):
        raise ValueError(f"Unknown field type {f.type!r}.")
    if f.format and f.format not in FORMATS:
        raise ValueError(f"Unknown field format {f.format!r}.")
    if f.pattern is not None:
        if len(f.pattern) > 1000:
            raise ValueError(f"The pattern for {f.name} is too long.")
        try:
            problems = pattern_problems(f.pattern)
        except (OverflowError, RecursionError) as error:
            raise ValueError(f"Invalid pattern for {f.name}.") from error
        if problems:
            raise ValueError(problems[0])
    if f.type == "quantity":
        units.parse(f.unit or "")
    if f.type == "enum" and (not 1 <= len(f.values) <= 500 or len(set(f.values)) != len(f.values)):
        raise ValueError(f"Enum {f.name} needs 1 to 500 distinct values.")
    if f.type == "ref" and (not f.to or not all(t in types for t in f.to)):
        raise ValueError(f"Unknown target of reference {f.name}.")
    if f.schema is not None:
        if f.type != "json":
            raise ValueError(f"Only a JSON field may have a schema: {f.name}.")
        _local_references(f.schema)
    if (f.type == "list") != (f.items is not None):
        raise ValueError(f"A list must declare its items: {f.name}.")
    if f.items is not None:
        if f.items.name != f.name or f.items.type in {"json", "state", "list"}:
            raise ValueError(f"Invalid list items in {f.name}.")
        if f.items.type == "row":
            if not 1 <= len(f.items.fields) <= 30:
                raise ValueError(f"A row in {f.name} needs 1 to 30 fields.")
            _fields(f.items.fields, types)
        else:
            _field(f.items, types)
    if f.type == "state" and f.initial not in f.live_states:
        raise ValueError(f"The initial state of {f.name} must be a live state.")
    if len(f.states) > 30:
        raise ValueError(f"State field {f.name} can declare at most 30 states.")
    for key, state in f.states.items():
        _named("fieldName", key)
        if key != state.name:
            raise ValueError(f"Invalid state name {state.name} in {f.name}.")
        _guards(state.guards)


def _local_references(schema: Mapping[str, Any]) -> None:
    """Keep an uploaded JSON schema self-contained, including nested references."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError(f"Invalid JSON schema: {error.message}") from error
    nodes: list[Any] = [schema]
    while nodes:
        node = nodes.pop()
        if isinstance(node, dict):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                if keyword in node and not (
                    isinstance(node[keyword], str) and node[keyword].startswith("#")
                ):
                    raise ValueError("A JSON schema reference must be local to the manifest.")
            nodes.extend(DRAFT202012.subresources_of(node))


def _guards(guards: Mapping[str, Guard]) -> None:
    if len(guards) > 20:
        raise ValueError("A manifest can declare at most 20 guards per operation.")
    for key, guard in guards.items():
        _named("fieldName", key)
        if key != guard.name:
            raise ValueError(f"Invalid guard name {guard.name}.")
        _expressions(guard)


def _expressions(rule: Guard | Rule) -> None:
    for kind, expression in (
        ("expression", rule.check),
        ("expression", rule.when),
        ("message", rule.message),
    ):
        if expression is not None and (issues := schemas.check("rule", expression, part=kind)):
            raise ValueError(f"{rule.name}: {issues[0].message}")


def _rules(rules: Mapping[str, Rule], plugin: str, *, extension: bool) -> None:
    if len(rules) > 50:
        raise ValueError("A type or extension can declare at most 50 rules.")
    for key, rule in rules.items():
        _named("fieldName", key)
        if (rule.name, rule.plugin, rule.extension) != (key, plugin, extension):
            raise ValueError(f"Rule {key} must belong to {plugin}.")
        if len(rule.unique) > 5:
            raise ValueError(f"Rule {key} supports up to 5 unique fields.")
        if rule.no_overlap and len(rule.no_overlap) != 2:
            raise ValueError(f"Rule {key} needs both ends of its range.")
        _expressions(rule)
