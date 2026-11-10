"""Each entity type's values as a JSON Schema (2020-12), for clients and forms.

The schema describes the ``values`` object the API returns and accepts:

- ``required`` lists what a new entity needs. An edit sends only the fields
  it changes.
- ``readOnly`` marks fields people can't set: computed fields (actions set
  them) and files (attached through the files API).
- ``x-iki-immutable`` marks fields that can be set once.
- Quantities are numbers in ``x-iki-unit`` (UCUM). An input may instead be
  ``{"value": …, "unit": …}`` in any convertible unit.
- References are entity IDs, of ``x-iki-ref`` or one of its subtypes.

The server checks every value itself, so the schema helps clients but
doesn't have to be complete.
"""

from __future__ import annotations

from typing import Any

from iki_server.entities.values import shape
from iki_spec.model import FORMATS, Field

DIALECT = "https://json-schema.org/draft/2020-12/schema"


def for_type(qualified: str) -> dict[str, Any]:
    form = shape(qualified)
    entity_type = form.entity_type
    fields = form.fields()
    out: dict[str, Any] = {
        "$schema": DIALECT,
        "title": entity_type.label or entity_type.name,
        "type": "object",
        "properties": {name: field_schema(f) for name, f in fields.items()},
        "required": [n for n, f in fields.items() if f.required and not f.computed],
        "additionalProperties": False,
    }
    if entity_type.description:
        out["description"] = entity_type.description
    return out


def field_schema(f: Field) -> dict[str, Any]:
    schema = _value(f)
    if not f.required:
        schema = {"anyOf": [schema, {"type": "null"}]}
    schema["title"] = f.label
    if f.description:
        schema["description"] = f.description
    if f.computed:
        schema["readOnly"] = True
    if f.immutable:
        schema["x-iki-immutable"] = True
    if f.has_default:
        schema["default"] = f.default
    return schema


def _value(f: Field) -> dict[str, Any]:
    kind = f.type
    if kind in ("string", "text"):
        out: dict[str, Any] = {"type": "string", "minLength": 1 if f.min_length is None else 0}
        if f.min_length:
            out["minLength"] = f.min_length
        if f.max_length is not None:
            out["maxLength"] = f.max_length
        patterns = [FORMATS[f.format]] if f.format else []
        if f.pattern:
            patterns.append(f.pattern)
        if kind == "string":
            patterns.append(r"^[^\r\n]*$")
        if len(patterns) == 1:
            out["pattern"] = patterns[0]
        elif patterns:
            out["allOf"] = [{"pattern": p} for p in patterns]
        if f.format:
            out["x-iki-format"] = f.format
        return out
    if kind in ("int", "float", "quantity"):
        out = {"type": "integer" if kind == "int" else "number"}
        if f.min is not None:
            out["minimum"] = f.min
        if f.max is not None:
            out["maximum"] = f.max
        if kind == "quantity":
            out["x-iki-unit"] = f.unit
            if f.display_unit:
                out["x-iki-display-unit"] = f.display_unit
        return out
    if kind == "bool":
        return {"type": "boolean"}
    if kind == "enum":
        return {"type": "string", "enum": list(f.values)}
    if kind == "date":
        return {"type": "string", "format": "date"}
    if kind == "datetime":
        return {"type": "string", "format": "date-time"}
    if kind == "ref":
        return {"type": "string", "format": "uuid", "x-iki-ref": f.to}
    if kind == "file":
        out = {"type": "string", "format": "uuid", "x-iki-file": True}
        if f.media_types:
            out["x-iki-media-types"] = list(f.media_types)
        if f.max_size is not None:
            out["x-iki-max-size"] = f.max_size
        return out
    if kind == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        out = {"type": "array", "items": _value(f.items)}
        if f.min_items is not None:
            out["minItems"] = f.min_items
        if f.max_items is not None:
            out["maxItems"] = f.max_items
        return out
    # json
    return dict(f.schema) if f.schema is not None else {}
