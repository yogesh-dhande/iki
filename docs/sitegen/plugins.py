"""The first-party plugins, as the site's Plugins reference shows them.

Each plugin under ``plugins/``, and the core plugin in ``spec/src/iki_spec/core``, is read
with the validator's own strict YAML 1.2 reader, and its record types, extensions and
actions are written to ``src/generated/plugins.json``: what each holds, and the manifest
it's declared in. A manifest that can't be read stops the generation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from iki_spec.yamlsource import Document, read_documents
from sitegen.repo import GENERATED, ROOT

PLUGINS = ROOT / "plugins"
CORE = ROOT / "spec" / "src" / "iki_spec" / "core"
#: The directories whose manifests the reference shows.
DIRECTORIES = ("types", "actions")


class UnreadableError(Exception):
    """A first-party manifest the validator's reader refuses."""


def roots() -> list[Path]:
    """Every first-party plugin's directory, the core plugin's first."""
    return [CORE, *sorted(path.parent for path in PLUGINS.glob("*/plugin.yaml"))]


def _path(file: Path) -> str:
    """A manifest's path in the repository, as its source link names it."""
    return (file.relative_to(ROOT) if file.is_relative_to(ROOT) else file).as_posix()


def _documents(file: Path) -> list[Document]:
    name = _path(file)
    documents, problems = read_documents(file, name)
    if problems:
        found = "; ".join(f"{problem.code}: {problem.message}" for problem in problems)
        raise UnreadableError(f"{name}: {found}")
    return documents


def _qualified(plugin: str, name: str) -> str:
    """A type as the manifests name it, in full: ``Sample`` in iki/example_lims is
    ``iki/example_lims.Sample``; another plugin's is already in full."""
    return name if "." in name else f"{plugin}.{name}"


def _words(key: str) -> str:
    """A key's label when its manifest gives none, as the validator makes it."""
    return key.replace("_", " ").capitalize()


def _targets(plugin: str, to: Any) -> list[str]:  # noqa: ANN401 - manifest data
    if to is None:
        return []
    names = to if isinstance(to, list) else [to]
    return [_qualified(plugin, str(name)) for name in names]


def _field(plugin: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    items = spec.get("items") if isinstance(spec.get("items"), dict) else None
    field: dict[str, Any] = {
        "key": key,
        "type": str(spec.get("type", "")),
        "label": str(spec.get("label") or _words(key)),
        "description": str(spec.get("description", "")).strip(),
        "flags": [
            flag
            for flag in ("required", "immutable", "computed", "unique", "indexed")
            if spec.get(flag) is True
        ],
    }
    if isinstance(spec.get("registry"), str):
        field["registry"] = spec["registry"]
    if spec.get("fixed") is True:
        field["flags"].append("fixed")
    if "unit" in spec:
        field["unit"] = str(spec["unit"])
    if isinstance(spec.get("values"), list):
        field["values"] = [str(value) for value in spec["values"]]
    to = _targets(plugin, spec.get("to"))
    if items:
        field["items"] = str(items.get("type", ""))
        to = to or _targets(plugin, items.get("to"))
        if isinstance(items.get("values"), list):
            field["values"] = [str(value) for value in items["values"]]
        if isinstance(items.get("fields"), dict):
            field["fields"] = _fields(plugin, items["fields"])
    if to:
        field["to"] = to
    if isinstance(spec.get("fields"), dict) and "fields" not in field:
        field["fields"] = _fields(plugin, spec["fields"])
    states = spec.get("states")
    if isinstance(states, dict):
        field["states"] = [
            {
                "name": name,
                "label": str(state.get("label") or _words(name)),
                "description": str(state.get("description", "")).strip(),
                "locks": state.get("locks") is True,
            }
            for name, state in states.items()
            if isinstance(state, dict) and state.get("removed") is not True
        ]
        if "initial" in spec:
            field["initial"] = str(spec["initial"])
    return field


def _fields(plugin: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
    """A type's fields, in the manifest's order. A removed field is kept only for the
    records' history, so the reference leaves it out."""
    return [
        _field(plugin, key, spec)
        for key, spec in fields.items()
        if isinstance(spec, dict) and spec.get("removed") is not True
    ]


def _type(plugin: str, body: dict[str, Any], path: str) -> dict[str, Any]:
    hrid = body.get("hrid")
    extends = body.get("extends")
    found: dict[str, Any] = {
        "name": str(body["name"]),
        "label": str(body.get("label") or body["name"]),
        "description": str(body.get("description", "")).strip(),
        "abstract": body.get("abstract") is True,
        "extends": _targets(plugin, extends),
        "path": path,
        "fields": _fields(plugin, body.get("fields") or {}),
    }
    if isinstance(hrid, dict) and "prefix" in hrid:
        found["hrid"] = str(hrid["prefix"])
    if isinstance(body.get("identity"), list):
        found["identity"] = [str(key) for key in body["identity"]]
    amount = body.get("amount")
    if isinstance(amount, dict) and "unit" in amount:
        found["amount"] = str(amount["unit"])
    return found


def _extension(plugin: str, body: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "extends": _qualified(plugin, str(body["extends_type"])),
        "path": path,
        "fields": _fields(plugin, body.get("fields") or {}),
    }


def _named(plugin: str, specs: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - manifest data
    """An action's inputs or params: each one's name, what it takes, label and description."""
    if not isinstance(specs, dict):
        return []
    named = []
    for name, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("type", ""))
        entry: dict[str, Any] = {
            "name": name,
            "type": _qualified(plugin, kind) if kind[:1].isupper() or "." in kind else kind,
            "label": str(spec.get("label") or _words(name)),
            "description": str(spec.get("description", "")).strip(),
            "required": spec.get("required") is True,
        }
        if spec.get("cardinality") == "many":
            entry["many"] = True
        if spec.get("optional") is True:
            entry["optional"] = True
        if "default" in spec:
            entry["default"] = json.dumps(spec["default"])
        if isinstance(spec.get("values"), list):
            entry["values"] = [str(value) for value in spec["values"]]
        named.append(entry)
    return named


def _action(plugin: str, body: dict[str, Any], path: str) -> dict[str, Any]:
    effects: dict[str, Any] = body["effects"] if isinstance(body.get("effects"), dict) else {}
    creates = [
        _qualified(plugin, str(create["type"]))
        for create in effects.get("creates") or []
        if isinstance(create, dict) and "type" in create
    ]
    updates = [
        {"input": str(update["input"]), "fields": [str(f) for f in update.get("fields") or []]}
        for update in effects.get("updates") or []
        if isinstance(update, dict) and "input" in update
    ]
    return {
        "name": str(body["name"]),
        "label": str(body.get("label") or _words(str(body["name"]))),
        "description": str(body.get("description", "")).strip(),
        "path": path,
        "inputs": _named(plugin, body.get("inputs")),
        "params": _named(plugin, body.get("params")),
        "creates": creates,
        "updates": updates,
    }


def plugin(root: Path) -> dict[str, Any]:
    """One plugin, as the reference shows it."""
    manifest = root / "plugin.yaml"
    [document, *_] = _documents(manifest)
    head = document.data
    name = str(head["name"])
    short = name.split("/", 1)[1]
    roles = head.get("roles") if isinstance(head.get("roles"), dict) else {}
    found: dict[str, Any] = {
        "name": name,
        "short": short,
        "slug": short.replace("_", "-"),
        "title": str(head.get("title") or short),
        "description": str(head.get("description", "")).strip(),
        "version": str(head.get("version", "")),
        "path": _path(manifest),
        "roles": [
            {
                "name": role,
                "label": str(spec.get("label") or _words(role)),
                "description": str(spec.get("description", "")).strip(),
            }
            for role, spec in roles.items()
            if isinstance(spec, dict)
        ],
        "dependencies": sorted(head.get("dependencies") or {}),
        "starters": sorted(head.get("starters") or {}),
        "types": [],
        "extensions": [],
        "actions": [],
    }
    for directory in DIRECTORIES:
        for file in sorted((root / directory).glob("*.yaml")):
            path = _path(file)
            for document in _documents(file):
                body = document.data
                if not isinstance(body, dict):
                    raise UnreadableError(f"{path}: a document isn't a mapping")
                kind = body.get("kind")
                if kind == "EntityType":
                    found["types"].append(_type(name, body, path))
                elif kind == "EntityTypeExtension":
                    found["extensions"].append(_extension(name, body, path))
                elif kind == "Action":
                    found["actions"].append(_action(name, body, path))
    found["types"].sort(key=lambda entry: entry["name"])
    found["actions"].sort(key=lambda entry: entry["name"])
    return found


def plugins() -> list[dict[str, Any]]:
    """Every first-party plugin, the core plugin's first, then by name."""
    return [plugin(root) for root in roots()]


def write(directory: Path = GENERATED) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugins.json"
    path.write_text(json.dumps({"plugins": plugins()}, indent=2) + "\n", encoding="utf-8")
    return path
