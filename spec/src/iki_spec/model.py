"""A plugin as Iki understands it, once its manifests have been read and checked.

Codegen (YAML → Django app) and the build read these objects, never the raw
YAML, so every rule `iki plugin validate` enforces holds for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_TYPES",
    "FIELD_TYPES",
    "FORMATS",
    "EntityType",
    "Extension",
    "Field",
    "Plugin",
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
        return f"{self.plugin}_{self.name.lower()}"


@dataclass(frozen=True)
class Extension:
    plugin: str
    extends_type: str  # qualified
    fields: dict[str, Field]
    description: str | None = None
    file: str | None = None

    @property
    def table(self) -> str:
        return f"{self.plugin}_{self.extends_type.split('.')[1].lower()}_ext"


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


#: Types every plugin can use without declaring a dependency.
BUILTIN_TYPES: dict[str, EntityType] = {
    "core.Entity": EntityType(
        plugin="core",
        name="Entity",
        schema_version=1,
        label="Entity",
        label_plural="Entities",
        description="Any entity, of any type.",
        abstract=True,
    )
}


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
