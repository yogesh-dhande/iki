"""An entity's identity hash, from its identity fields' values (see :mod:`iki_spec.identity`)."""

from collections.abc import Callable, Mapping
from typing import Any

from iki_server.entities import registry
from iki_spec.identity import identity_hash
from iki_spec.model import EntityType, Field


def hash_for(entity: Any, entity_type: EntityType) -> str | None:  # noqa: ANN401 - a generated model
    """The hash for ``entity``, of type ``entity_type``, or None if its type has no identity."""

    def value(name: str, f: Field) -> Any:  # noqa: ANN401 - a model value
        if f.type == "ref":
            return getattr(entity, f"{name}_id")
        if f.type == "file":
            return getattr(entity, name).sha256
        return getattr(entity, name)

    return _hash(entity_type, value)


def hash_of(
    values: Mapping[str, Any],
    entity_type: EntityType,
    file_sha256: Callable[[Any], str] | None = None,
) -> str | None:
    """The hash for an entity with these values (as the database stores them: a reference
    is the entity's ID, a file the file's), before there's a model instance for it."""

    def value(name: str, f: Field) -> Any:  # noqa: ANN401 - a model value
        given = values.get(name)
        if f.type == "file" and given is not None:
            if file_sha256 is None:
                raise ValueError(f"{entity_type.qualified_name}'s identity needs its file.")
            return file_sha256(given)
        return given

    return _hash(entity_type, value)


def _hash(entity_type: EntityType, value: Callable[[str, Field], Any]) -> str | None:
    owner = registry.identity_owner(entity_type)
    if owner is None:
        return None
    fields = {**_fields(owner), **owner.fields}
    return identity_hash(
        owner.qualified_name,
        {name: (fields[name].type, value(name, fields[name])) for name in owner.identity},
    )


def _fields(entity_type: EntityType) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ancestor in reversed(registry.ancestors(entity_type)):
        out.update(ancestor.fields)
    return out
