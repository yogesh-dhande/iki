"""An entity's identity hash, from its identity fields' values (see :mod:`iki_spec.identity`)."""

from typing import Any

from iki_server.entities import registry
from iki_spec.identity import identity_hash
from iki_spec.model import EntityType


def hash_for(entity: Any, entity_type: EntityType) -> str | None:  # noqa: ANN401 - a generated model
    """The hash for ``entity``, of type ``entity_type``, or None if its type has no identity."""
    owner = registry.identity_owner(entity_type)
    if owner is None:
        return None
    fields = {**_fields(owner), **owner.fields}
    values: dict[str, tuple[str, Any]] = {}
    for name in owner.identity:
        f = fields[name]
        if f.type == "ref":
            value: Any = getattr(entity, f"{name}_id")
        elif f.type == "file":
            value = getattr(entity, name).sha256
        else:
            value = getattr(entity, name)
        values[name] = (f.type, value)
    return identity_hash(owner.qualified_name, values)


def _fields(entity_type: EntityType) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ancestor in reversed(registry.ancestors(entity_type)):
        out.update(ancestor.fields)
    return out
