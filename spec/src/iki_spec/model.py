"""A plugin as Iki understands it, once its manifests have been read and checked.

Codegen (YAML → Django app) and the build read these objects, never the raw
YAML, so every rule `iki plugin validate` enforces holds for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from iki_spec import naming

__all__ = [
    "BUILTIN_TYPES",
    "FIELD_TYPES",
    "FORMATS",
    "TIERS",
    "Action",
    "Creates",
    "EntityType",
    "Environment",
    "Extension",
    "Field",
    "Input",
    "Plugin",
    "Resources",
    "Updates",
    "Volume",
    "action_from_dict",
    "plugin_from_dict",
    "plugin_to_dict",
]

FIELD_TYPES = (
    "string",
    "text",
    "int",
    "float",
    "bool",
    "enum",
    "date",
    "datetime",
    "quantity",
    "file",
    "ref",
    "list",
    "json",
)

#: Built-in string formats, as regular expressions both Python and PostgreSQL
#: understand (the database checks them too). Sequences accept IUPAC codes in
#: either case, since lower case often marks masked regions.
FORMATS: dict[str, str] = {
    "dna_sequence": r"^[ACGTRYSWKMBDHVNacgtryswkmbdhvn]*$",
    "rna_sequence": r"^[ACGURYSWKMBDHVNacguryswkmbdhvn]*$",
    "protein_sequence": r"^[ACDEFGHIKLMNPQRSTVWYBZXUOJacdefghiklmnpqrstvwybzxuoj*]*$",
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "uri": r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$",
}

_FORMAT_RE = {name: re.compile(pattern) for name, pattern in FORMATS.items()}


def matches_format(value: str, name: str) -> bool:
    return _FORMAT_RE[name].fullmatch(value) is not None


@dataclass(frozen=True)
class Field:
    """A field of an entity type or extension, a list's item, or a plugin setting."""

    name: str
    type: str
    label: str = ""
    description: str | None = None
    required: bool = False
    immutable: bool = False
    computed: bool = False
    indexed: bool = False
    #: Gone from the type, but kept in its history (removed fields are never deleted).
    removed: bool = False
    has_default: bool = False
    default: Any = None
    format: str | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    values: tuple[str, ...] = ()
    unit: str | None = None
    display_unit: str | None = None
    media_types: tuple[str, ...] = ()
    max_size: int | None = None  # bytes
    to: str | None = None  # a qualified type name
    items: Field | None = None
    min_items: int | None = None
    max_items: int | None = None
    schema: dict[str, Any] | None = None

    @classmethod
    def from_manifest(cls, name: str, data: dict[str, Any], *, plugin: str) -> Field:
        items = data.get("items")
        to = data.get("to")
        return cls(
            name=name,
            type=data["type"],
            label=data.get("label") or name.replace("_", " ").capitalize(),
            description=data.get("description"),
            required=bool(data.get("required", False)),
            immutable=bool(data.get("immutable", False)),
            computed=bool(data.get("computed", False)),
            indexed=bool(data.get("indexed", False)),
            removed=bool(data.get("removed", False)),
            has_default="default" in data,
            default=data.get("default"),
            format=data.get("format"),
            pattern=data.get("pattern"),
            min_length=data.get("min_length"),
            max_length=data.get("max_length"),
            min=data.get("min"),
            max=data.get("max"),
            values=tuple(data.get("values", ())),
            unit=data.get("unit"),
            display_unit=data.get("display_unit"),
            media_types=tuple(data.get("media_types", ())),
            max_size=parse_size(data["max_size"]) if "max_size" in data else None,
            to=qualify(to, plugin) if isinstance(to, str) else None,
            items=cls.from_manifest(name, items, plugin=plugin)
            if isinstance(items, dict)
            else None,
            min_items=data.get("min_items"),
            max_items=data.get("max_items"),
            schema=data.get("schema"),
        )


@dataclass(frozen=True)
class EntityType:
    plugin: str
    name: str
    schema_version: int
    label: str = ""
    label_plural: str = ""
    description: str | None = None
    extends: str | None = None  # qualified
    abstract: bool = False
    hrid_prefix: str | None = None
    hrid_digits: int = 6
    identity: tuple[str, ...] = ()
    on_duplicate: str | None = None
    fields: dict[str, Field] = field(default_factory=dict)
    display_title: str | None = None
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"

    @property
    def table(self) -> str:
        return naming.type_table(self.qualified_name)

    @property
    def current_fields(self) -> dict[str, Field]:
        """The fields entities have now, without the removed ones."""
        return {name: f for name, f in self.fields.items() if not f.removed}


@dataclass(frozen=True)
class Extension:
    plugin: str
    extends_type: str  # qualified
    fields: dict[str, Field]
    description: str | None = None
    file: str | None = None

    @property
    def table(self) -> str:
        return naming.extension_table(self.plugin, self.extends_type)

    @property
    def current_fields(self) -> dict[str, Field]:
        return {name: f for name, f in self.fields.items() if not f.removed}


#: Where actions run, from the person's own machine to a virtual machine.
TIERS = ("client", "backend", "container", "vm")


@dataclass(frozen=True)
class Resources:
    """A machine's size: what an environment needs at least, or an action more."""

    cpu: float | None = None
    memory: int | None = None  # bytes
    gpu_count: int = 0
    gpu_memory: int | None = None  # bytes, per GPU
    scratch_disk: int | None = None  # bytes

    @classmethod
    def from_manifest(cls, data: dict[str, Any] | None) -> Resources:
        data = data or {}
        gpu = data.get("gpu") or {}
        return cls(
            cpu=data.get("cpu"),
            memory=_quantity(data.get("memory")),
            gpu_count=gpu.get("count", 0),
            gpu_memory=_quantity(gpu.get("min_memory")),
            scratch_disk=_quantity(data.get("scratch_disk")),
        )

    def at_least(self, other: Resources) -> Resources:
        """The larger of each of these and `other`'s."""

        def most(a: Any, b: Any) -> Any:  # noqa: ANN401 - numbers or None
            return b if a is None else a if b is None else max(a, b)

        return Resources(
            cpu=most(self.cpu, other.cpu),
            memory=most(self.memory, other.memory),
            gpu_count=max(self.gpu_count, other.gpu_count),
            gpu_memory=most(self.gpu_memory, other.gpu_memory),
            scratch_disk=most(self.scratch_disk, other.scratch_disk),
        )


@dataclass(frozen=True)
class Input:
    name: str
    type: str  # qualified
    schema: str | None = None  # a schema-version constraint
    cardinality: str = "one"
    max: int | None = None
    optional: bool = False
    label: str = ""
    description: str | None = None


@dataclass(frozen=True)
class Creates:
    type: str  # qualified
    cardinality: str = "one"
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class Updates:
    input: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class Action:
    plugin: str
    name: str
    tier: str
    environment: str
    command: tuple[str, ...]
    label: str = ""
    description: str | None = None
    inputs: dict[str, Input] = field(default_factory=dict)
    params: dict[str, Field] = field(default_factory=dict)
    creates: tuple[Creates, ...] = ()
    updates: tuple[Updates, ...] = ()
    lifecycle: str | None = None  # container and vm: ephemeral or persistent
    resources: Resources = field(default_factory=Resources)
    deterministic: bool = False
    checkpointable: bool = False
    batchable: int = 1
    artifacts: tuple[str, ...] = ()  # glob patterns
    artifact_retention: int | None = None  # seconds
    network: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    timeout: int = 3600  # seconds
    retry_max: int = 0
    retry_on: tuple[str, ...] = ()
    file: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.plugin}.{self.name}"


@dataclass(frozen=True)
class Volume:
    name: str
    size: int  # bytes
    mount: str
    access: str = "read_only_many"
    performance: str = "standard"
    populated_by: str | None = None


@dataclass(frozen=True)
class Environment:
    plugin: str
    name: str
    tiers: tuple[str, ...]
    label: str = ""
    description: str | None = None
    resources: Resources = field(default_factory=Resources)
    image: str | None = None
    python_version: str | None = None
    requirements: str | None = None  # a path in the plugin
    conda_lockfile: str | None = None  # a path in the plugin
    volumes: dict[str, Volume] = field(default_factory=dict)
    setup_script: str | None = None  # a path in the plugin
    setup_timeout: int | None = None  # seconds
    setup_requires_approval: bool = False
    setup_network: tuple[str, ...] = ()
    file: str | None = None


@dataclass(frozen=True)
class Plugin:
    name: str
    version: str
    title: str = ""
    description: str | None = None
    license: str | None = None
    homepage: str | None = None
    #: plugin name → {type name → schema-version constraint}
    dependencies: dict[str, dict[str, str]] = field(default_factory=dict)
    settings: dict[str, Field] = field(default_factory=dict)
    types: dict[str, EntityType] = field(default_factory=dict)
    extensions: tuple[Extension, ...] = ()
    actions: dict[str, Action] = field(default_factory=dict)
    environments: dict[str, Environment] = field(default_factory=dict)


class _CoreTypes(Mapping[str, EntityType]):
    """The core plugin's types, read from its manifests (``iki_spec/core``) when first used."""

    def _types(self) -> Mapping[str, EntityType]:
        from iki_spec.builtin import core_plugin

        return core_plugin().types

    def __getitem__(self, key: str) -> EntityType:
        return self._types()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._types())

    def __len__(self) -> int:
        return len(self._types())


#: Types every plugin can use without declaring a dependency: the core plugin's.
BUILTIN_TYPES: Mapping[str, EntityType] = _CoreTypes()


def qualify(ref: str, plugin: str) -> str:
    return ref if "." in ref else f"{plugin}.{ref}"


_SIZE_UNITS = {
    "B": 1,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def parse_size(text: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([A-Za-z]+)", text)
    if match is None or match.group(2) not in _SIZE_UNITS:
        raise ValueError(text)
    return int(match.group(1)) * _SIZE_UNITS[match.group(2)]


_QUANTITY_UNITS = {"Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def parse_quantity(text: str) -> int:
    """Bytes in a Kubernetes-style quantity of memory or disk, such as `64Gi`."""
    match = re.fullmatch(r"([1-9][0-9]*)(Mi|Gi|Ti)", text)
    if match is None:
        raise ValueError(text)
    return int(match.group(1)) * _QUANTITY_UNITS[match.group(2)]


def _quantity(text: str | None) -> int | None:
    return parse_quantity(text) if text is not None else None


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Seconds in a duration such as `90s`, `30m`, `8h` or `365d`."""
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", text)
    if match is None:
        raise ValueError(text)
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


# --- JSON ---------------------------------------------------------------------------------------
# `iki plugin build` writes each plugin as JSON next to its generated models,
# so the server reads labels, units and rules without the YAML.


_DATACLASSES = (
    Field,
    EntityType,
    Extension,
    Plugin,
    Action,
    Input,
    Creates,
    Updates,
    Resources,
    Environment,
    Volume,
)


def plugin_to_dict(plugin: Plugin) -> dict[str, Any]:
    """A JSON-ready dict; `plugin_from_dict` reads it back to an equal `Plugin`."""
    return _to_json(plugin)  # type: ignore[no-any-return]


def _to_json(value: Any) -> Any:  # noqa: ANN401 - walks dataclasses of JSON-shaped values
    if isinstance(value, _DATACLASSES):
        return {f.name: _to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    return value


def plugin_from_dict(data: dict[str, Any]) -> Plugin:
    return Plugin(
        **{
            **data,
            "settings": {k: _field(v) for k, v in data["settings"].items()},
            "types": {
                k: EntityType(
                    **{
                        **v,
                        "identity": tuple(v["identity"]),
                        "fields": {n: _field(f) for n, f in v["fields"].items()},
                    }
                )
                for k, v in data["types"].items()
            },
            "extensions": tuple(
                Extension(**{**e, "fields": {n: _field(f) for n, f in e["fields"].items()}})
                for e in data["extensions"]
            ),
            "actions": {k: _action(v) for k, v in data["actions"].items()},
            "environments": {k: _environment(v) for k, v in data["environments"].items()},
        }
    )


def action_from_dict(data: dict[str, Any]) -> Action:
    """An action from its JSON form (``dataclasses.asdict``), as a run keeps it."""
    return _action(data)


def _action(data: dict[str, Any]) -> Action:
    return Action(
        **{
            **data,
            "command": tuple(data["command"]),
            "inputs": {n: Input(**i) for n, i in data["inputs"].items()},
            "params": {n: _field(f) for n, f in data["params"].items()},
            "creates": tuple(
                Creates(**{**c, "derived_from": tuple(c["derived_from"])}) for c in data["creates"]
            ),
            "updates": tuple(
                Updates(**{**u, "fields": tuple(u["fields"])}) for u in data["updates"]
            ),
            "resources": Resources(**data["resources"]),
            "artifacts": tuple(data["artifacts"]),
            "network": tuple(data["network"]),
            "secrets": tuple(data["secrets"]),
            "retry_on": tuple(data["retry_on"]),
        }
    )


def _environment(data: dict[str, Any]) -> Environment:
    return Environment(
        **{
            **data,
            "tiers": tuple(data["tiers"]),
            "resources": Resources(**data["resources"]),
            "volumes": {n: Volume(**v) for n, v in data["volumes"].items()},
            "setup_network": tuple(data["setup_network"]),
        }
    )


def _field(data: dict[str, Any]) -> Field:
    return Field(
        **{
            **data,
            "values": tuple(data["values"]),
            "media_types": tuple(data["media_types"]),
            "items": _field(data["items"]) if data["items"] is not None else None,
        }
    )
