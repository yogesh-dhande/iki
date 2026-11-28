"""An entity's identity hash, from its identity fields' values (see :mod:`iki_spec.identity`)."""

import json
from collections.abc import Callable, Mapping
from typing import Any

from iki_server.entities import registry
from iki_spec.features import place_feature
from iki_spec.identity import Placement, identity_hash, kind_of, place
from iki_spec.model import EntityType, Field


def hash_for(
    entity: Any,  # noqa: ANN401 - a generated model
    entity_type: EntityType,
    lists: Mapping[str, Any] | None = None,
) -> str | None:
    """The hash for ``entity``, of type ``entity_type``, or None if its type has no identity.
    ``lists``: its lists' values, as stored, which aren't on the model (ADR 0138)."""

    def value(name: str, f: Field) -> Any:  # noqa: ANN401 - a model value
        if f.type == "list":
            if lists is None or name not in lists:
                raise ValueError(f"{entity_type.qualified_name}'s identity needs its {name}.")
            return lists[name]
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
    is the entity's ID, a file the file's, a list of rows its rows), before there's a model
    instance for it."""

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
    fields = _fields(owner)
    given = {name: value(name, fields[name]) for name in owner.identity}
    # A sequence is read by its topology and strands (ADR 0138), themselves identity fields.
    return identity_hash(
        owner.qualified_name,
        {name: (kind_of(fields[name], given), given[name]) for name in owner.identity},
    )


def _fields(entity_type: EntityType) -> dict[str, Field]:
    """Every field of ``entity_type``'s records, as its effective declaration has them."""
    return registry.effective(entity_type.qualified_name).fields


def identity_of(entity_type: EntityType) -> tuple[str, ...]:
    """The fields of ``entity_type``'s identity, or none."""
    owner = registry.identity_owner(entity_type)
    return owner.identity if owner is not None else ()


def spelled(
    entity_type: EntityType, given: Mapping[str, Any], stored: Mapping[str, Any]
) -> dict[str, Any]:
    """The identity values in ``given`` spelled other than the entry's ``stored`` ones,
    which they're the same as (ADR 0138): a plasmid from another origin, a list in another
    order. As JSON, to be kept with the record."""
    owner = registry.identity_owner(entity_type)
    if owner is None:
        return {}
    fields = _fields(owner)
    out: dict[str, Any] = {}
    for name in owner.identity:
        value, current = given.get(name), stored.get(name)
        if value == current or (value in (None, []) and current in (None, [])):
            continue
        if fields[name].type == "list":
            # The same items in another order: kept in the order given.
            out[name] = json.loads(json.dumps(value, default=str))
        else:
            out[name] = value if isinstance(value, str | int | bool) else str(value)
    return out


def placed(
    entity_type: EntityType,
    given: Mapping[str, Any],
    spelled: Mapping[str, Any],
    stored: Callable[[str], Any],
) -> dict[str, Any]:
    """``given``, with the features along each sequence it spells other than its entry
    placed along the entry's (ADR 0138): a sequence viewer's features, which count from
    the sequence's first base on its given strand, and each of their parts."""
    owner = registry.identity_owner(entity_type)
    if owner is None or not spelled:
        return dict(given)
    fields = _fields(owner)
    out = dict(given)
    for viewer in _viewers(entity_type):
        name = viewer.sequence
        features = out.get(viewer.features)
        if name not in spelled or not isinstance(features, list):
            continue
        f, text, current = fields[name], given.get(name), stored(name)
        if f.format is None or not isinstance(text, str) or not isinstance(current, str):
            continue
        kind = kind_of(f, given)
        found = place(f.format, text, current, circular=kind.circular, double=kind.double)
        if found is None or found == Placement():
            continue
        length = len(text)
        out[viewer.features] = [
            place_feature(x, found, length, circular=kind.circular) for x in features
        ]
    return out


def _viewers(entity_type: EntityType) -> list[Any]:
    viewers = registry.effective(entity_type.qualified_name).viewers
    return [v for v in viewers if v.viewer == "sequence" and v.features]
