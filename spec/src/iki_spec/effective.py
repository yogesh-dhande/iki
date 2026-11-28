"""A type as its records have it: its effective declaration (ADR 0282).

A type declares only what it adds to its parent's. What its records have is its own
declaration and every ancestor's, merged block by block, each block by one rule, its
*policy*, which :data:`POLICIES` gives for every key an ``EntityType`` manifest has:

=============  ===================================================================
``own``        not inherited: each type's own (its name, HRID prefix, whether it's
               abstract).
``nearest``    the type's own, else its nearest ancestor's: a subtype's replaces it.
``once``       declared by one type of a lineage at most, and its subtypes have it.
``accumulate`` every ancestor's and its own, from the top down, none of its own
               named as an ancestor's is.
``narrow``     its ancestors', and a subtype may only narrow it: its records take
               less than its parent's, never more, so each is one of its parent's.
``merge``      its nearest ancestor's, with the parts a subtype may add merged in.
=============  ===================================================================

:func:`resolve` gives every type's :class:`Effective` declaration. ``iki plugin build``
and ``iki deploy build`` write each plugin's into its ``manifest.json``, beside the
declarations it was read from, and the server, codegen and preflight read only it: so
what a reviewer reads in the manifest is what Iki does (ADR 0032).

Extensions (another plugin's fields on a type) aren't part of it: they're the extending
plugin's, added where it's installed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from iki_spec import naming
from iki_spec.model import (
    Amount,
    AnyExport,
    DrawTarget,
    EntityType,
    Field,
    Guard,
    Refinement,
    RequestSpec,
    ResultSpec,
    Rule,
    StockCatalogue,
    Storage,
    TypeNotification,
    Viewer,
    WorkItemSpec,
    _amount,
    _export,
    _field,
    _guards,
    _notification,
    _request,
    _result,
    _rule,
    _storage,
    _to_json,
    _viewer,
)

__all__ = [
    "POLICIES",
    "POLICY_TEXT",
    "RESIDUE_ALPHABETS",
    "SEQUENCE",
    "Effective",
    "Policy",
    "ancestors",
    "carried",
    "effective_from_dict",
    "effective_to_dict",
    "is_a",
    "lineage",
    "narrowed",
    "resolve",
    "resolve_one",
]


class Policy(StrEnum):
    """How a block of a type's declaration is inherited (ADR 0282)."""

    OWN = "own"
    NEAREST = "nearest"
    ONCE = "once"
    ACCUMULATE = "accumulate"
    NARROW = "narrow"
    MERGE = "merge"


#: Each policy, in the words the manifest's schema and the docs give it.
POLICY_TEXT: Mapping[Policy, str] = {
    Policy.OWN: "Not inherited: each type's own.",
    Policy.NEAREST: "Inherited: a subtype has its nearest ancestor's, unless it declares its "
    "own, which replaces it.",
    Policy.ONCE: "Inherited, and declared once in a lineage: a subtype has its ancestor's, "
    "and can't declare another.",
    Policy.ACCUMULATE: "Inherited and added to: a subtype has every ancestor's and its own, "
    "none named as an ancestor's is.",
    Policy.NARROW: "Inherited, and a subtype may only narrow it: its records take what its "
    "parent's do or less, never more, so each of them is one of its parent's too.",
    Policy.MERGE: "Inherited, and a subtype may add to it: it has its nearest ancestor's, with "
    "what each type below adds.",
}

#: The abstract type of everything with a sequence of residues (ADR 0285).
SEQUENCE = "iki/core.Sequence"
#: A residues field's format, and the alphabet a run's input names it by (ADR 0284).
RESIDUE_ALPHABETS: Mapping[str, str] = {
    "dna_sequence": "dna",
    "rna_sequence": "rna",
    "protein_sequence": "protein",
    "helm": "helm",
}

#: The policy of each key of an ``EntityType`` manifest: the contract of ``extends``. The
#: entity-type schema gives each key's in its description, and a test holds them equal.
POLICIES: Mapping[str, Policy] = {
    "name": Policy.OWN,
    "label": Policy.OWN,
    "label_plural": Policy.OWN,
    "description": Policy.OWN,
    "schema_version": Policy.OWN,
    "extends": Policy.OWN,
    "abstract": Policy.OWN,
    "hrid": Policy.OWN,
    # A lab's metadata describes the type it's on, not its subtypes (ADR 0293).
    "annotations": Policy.OWN,
    # Once a sample type, its subtypes' records are samples too (ADR 0177).
    "sample": Policy.ACCUMULATE,
    "identity": Policy.ONCE,
    "fields": Policy.ACCUMULATE,
    "refines": Policy.NARROW,
    "rules": Policy.ACCUMULATE,
    "draws_received": Policy.NEAREST,
    "draw_guards": Policy.ACCUMULATE,
    "roles": Policy.ACCUMULATE,
    "draw_purposes": Policy.NARROW,
    "draw_target": Policy.NEAREST,
    "due": Policy.NEAREST,
    "notifications": Policy.ACCUMULATE,
    # A subtype adds service fields of its own (ADR 0232); the rest is its ancestor's.
    "request": Policy.MERGE,
    "work_item": Policy.NEAREST,
    "result": Policy.NEAREST,
    # Its title and its viewers, each on its own.
    "display": Policy.NEAREST,
    "exports": Policy.NEAREST,
    "storage": Policy.NEAREST,
    "stock_catalogue": Policy.NEAREST,
    "amount": Policy.ONCE,
    # Reviewed once in a lineage (ADR 0301): the fields it adds are its subtypes' fields.
    "review": Policy.ONCE,
}


@dataclass(frozen=True)
class Effective:
    """A type's effective declaration: every block as its records have it, under
    :data:`POLICIES`. Built by :func:`resolve`; JSON by :func:`effective_to_dict`."""

    #: The type's qualified name.
    name: str
    #: The type, then its ancestors, nearest first, ending with ``iki/core.Entity``.
    lineage: tuple[str, ...]
    #: Every field its records have, its ancestors' first (from the top down), then its own,
    #: each as the nearest refinement narrows it (ADR 0283). Removed fields too: history
    #: keeps them.
    fields: dict[str, Field] = field(default_factory=dict)
    #: The type of ``lineage`` whose identity its entities have (ADR 0115), and it.
    identity_owner: str | None = None
    identity: tuple[str, ...] = ()
    sample: bool = False
    display_title: str | None = None
    viewers: tuple[Viewer, ...] = ()
    exports: tuple[AnyExport, ...] = ()
    due: str | None = None
    rules: dict[str, Rule] = field(default_factory=dict)
    notifications: dict[str, TypeNotification] = field(default_factory=dict)
    request: RequestSpec | None = None
    result: ResultSpec | None = None
    work_item: WorkItemSpec | None = None
    amount: Amount | None = None
    draws_received: str | None = None
    stock_catalogue: StockCatalogue | None = None
    draw_guards: dict[str, Guard] = field(default_factory=dict)
    draw_purposes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    draw_target: DrawTarget | None = None
    storage: Storage | None = None
    #: The field playing each role (ADR 0284), its own or an ancestor's.
    roles: dict[str, str] = field(default_factory=dict)
    #: Whose declaration each inherited part is, by the type of ``lineage`` declaring it:
    #: a block by its key (``storage``, ``display.title``), and a named part of one by
    #: ``key.name`` (``fields.material``, ``rules.expiry_after_made``). A field's
    #: ``refines.name`` is the nearest type narrowing it.
    sources: dict[str, str] = field(default_factory=dict)

    def run_roles(self) -> dict[str, Any]:
        """What a run's input says of the field playing each of its roles (ADR 0284):
        ``material`` from its amount, ``residues`` with their alphabet, ``formula``."""
        out: dict[str, Any] = {}
        if self.amount is not None and self.amount.material is not None:
            out["material"] = self.amount.material
        if (name := self.roles.get("residues")) is not None:
            alphabet = RESIDUE_ALPHABETS.get(self.fields[name].format or "")
            if alphabet is not None:
                out["residues"] = {"field": name, "alphabet": alphabet}
        if (name := self.roles.get("formula")) is not None:
            out["formula"] = name
        return out

    @property
    def ancestors(self) -> tuple[str, ...]:
        """Its ancestors, nearest first, ``iki/core.Entity`` last."""
        return self.lineage[1:]

    @property
    def chain(self) -> tuple[str, ...]:
        """The type and its ancestors, nearest first, without ``iki/core.Entity``, which
        declares nothing: the types whose tables its records have rows in."""
        return tuple(n for n in self.lineage if n != naming.ENTITY)

    def is_a(self, wanted: str) -> bool:
        """Whether its records are ``wanted``'s: it's that type, or a subtype of it."""
        return wanted in self.lineage

    def declared_by(self, name: str) -> str:
        """The type declaring field ``name``, whose table holds its column."""
        return self.sources[f"fields.{name}"]

    def own_fields(self, type_name: str) -> dict[str, Field]:
        """The fields ``type_name``, one of its lineage, declares, as this type's records have
        them: the columns of that type's table."""
        return {n: f for n, f in self.fields.items() if self.sources[f"fields.{n}"] == type_name}

    @property
    def current_fields(self) -> dict[str, Field]:
        """Its records' fields now, without removed ones."""
        return {n: f for n, f in self.fields.items() if not f.removed}


def lineage(types: Mapping[str, EntityType], name: str) -> tuple[str, ...]:
    """``name``, then its ancestors, nearest first, then ``iki/core.Entity``. For types the
    validator hasn't passed it stops at a type that isn't there, or one already in it."""
    out = [name]
    current = types.get(name)
    while (
        current is not None
        and current.extends
        and current.extends != naming.ENTITY
        and current.extends not in out
    ):
        out.append(current.extends)
        current = types.get(current.extends)
    if name != naming.ENTITY:
        out.append(naming.ENTITY)
    return tuple(out)


def ancestors(types: Mapping[str, EntityType], entity_type: EntityType) -> list[EntityType]:
    """``entity_type``'s ancestors that ``types`` has, nearest first, without
    ``iki/core.Entity``."""
    return [
        types[n]
        for n in lineage(types, entity_type.qualified_name)[1:]
        if n != naming.ENTITY and n in types
    ]


def is_a(types: Mapping[str, EntityType], name: str, wanted: str) -> bool:
    """Whether ``name``'s records are ``wanted``'s: it's that type, or a subtype of it."""
    return name in types and wanted in lineage(types, name)


def narrowed(f: Field, r: Refinement) -> Field:
    """Field ``f`` as a subtype's refinement ``r`` narrows it (ADR 0283)."""
    changes: dict[str, Any] = {}
    items = f.items
    if r.required:
        changes["required"] = True
    if r.values:
        if f.type == "list" and items is not None:
            items = replace(items, values=r.values)
        else:
            changes["values"] = r.values
    if r.min is not None:
        changes["min"] = r.min
    if r.max is not None:
        changes["max"] = r.max
    if r.has_default:
        changes["has_default"] = True
        changes["default"] = r.default
    if r.to:
        if f.type == "list" and items is not None:
            items = replace(items, to=r.to)
        else:
            changes["to"] = r.to
    if r.roles and items is not None:
        # Its roles are every role its rows may have (ADR 0164).
        items = replace(items, roles=dict(r.roles), closed=True)
    if items is not f.items:
        changes["items"] = items
    return replace(f, **changes) if changes else f


def _narrowest(chain: Sequence[EntityType], name: str) -> tuple[Field, dict[str, str]]:
    """Field ``name`` as the first of ``chain`` (a type, then its ancestors, nearest first)
    has it, and the nearest type narrowing it."""
    at = next(i for i, t in enumerate(chain) if name in t.fields)
    f = chain[at].fields[name]
    sources: dict[str, str] = {}
    # From just below the declaring type down: each refinement within the one above it.
    for t in reversed(chain[:at]):
        r = t.refines.get(name)
        if r is not None:
            f = narrowed(f, r)
            sources[f"refines.{name}"] = t.qualified_name
    return f, sources


def resolve_one(types: Mapping[str, EntityType], name: str) -> Effective:
    """``name``'s effective declaration, from ``types``, which hold its ancestors."""
    names = lineage(types, name)
    chain = [types[n] for n in names if n in types]
    top_down = list(reversed(chain))
    sources: dict[str, str] = {}
    fields: dict[str, Field] = {}
    for t in top_down:
        for n in t.fields:
            if n in fields:
                continue  # the validator refuses a field named as an ancestor's
            f, narrowing = _narrowest(chain, n)
            fields[n] = f
            sources[f"fields.{n}"] = t.qualified_name
            sources.update(narrowing)

    def accumulated(key: str, attr: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in top_down:
            for n, value in getattr(t, attr).items():
                if n not in out:
                    out[n] = value
                    sources[f"{key}.{n}"] = t.qualified_name
        return out

    owner = next((t for t in chain if t.identity), None)
    if owner is not None:
        sources["identity"] = owner.qualified_name

    request: RequestSpec | None = None
    for t in top_down:
        if t.request is None:
            continue
        if request is None:
            request = t.request
            sources["request"] = t.qualified_name
        else:
            # A subtype adds service fields of its own (ADR 0232).
            added = tuple(n for n in t.request.service_fields if n not in request.service_fields)
            request = replace(request, service_fields=(*request.service_fields, *added))
            for n in added:
                sources[f"request.service_fields.{n}"] = t.qualified_name

    purposes: dict[str, tuple[str, ...]] = {}
    for t in top_down:
        for purpose, roles in t.draw_purposes.items():
            purposes[purpose] = roles
            sources[f"draw_purposes.{purpose}"] = t.qualified_name

    sample = next((t for t in chain if t.sample), None)
    if sample is not None:
        sources["sample"] = sample.qualified_name

    return Effective(
        name=name,
        lineage=names,
        fields=fields,
        identity_owner=owner.qualified_name if owner is not None else None,
        identity=owner.identity if owner is not None else (),
        sample=sample is not None,
        display_title=_nearest(chain, "display_title", "display.title", sources),
        viewers=_nearest(chain, "viewers", "display.viewers", sources) or (),
        exports=_nearest(chain, "exports", "exports", sources) or (),
        due=_nearest(chain, "due", "due", sources),
        rules=accumulated("rules", "rules"),
        notifications=accumulated("notifications", "notifications"),
        request=request,
        result=_nearest(chain, "result", "result", sources),
        work_item=_nearest(chain, "work_item", "work_item", sources),
        amount=_nearest(chain, "amount", "amount", sources),
        draws_received=_nearest(chain, "draws_received", "draws_received", sources),
        stock_catalogue=_nearest(chain, "stock_catalogue", "stock_catalogue", sources),
        draw_guards=accumulated("draw_guards", "draw_guards"),
        draw_purposes=purposes,
        draw_target=_nearest(chain, "draw_target", "draw_target", sources),
        storage=_nearest(chain, "storage", "storage", sources),
        roles=accumulated("roles", "roles"),
        sources=sources,
    )


def _nearest(chain: Sequence[EntityType], attr: str, key: str, sources: dict[str, str]) -> Any:  # noqa: ANN401 - each block's own type
    """The first of ``chain`` (nearest first) that declares ``attr``: its value, or None."""
    for t in chain:
        value = getattr(t, attr)
        if value:
            sources[key] = t.qualified_name
            return value
    return None


def carried(
    declared: Effective, reads: Sequence[str], include: Sequence[str] = ()
) -> frozenset[str] | None:
    """The fields a run's input carries of an entity whose type is ``declared``, for an
    input that ``reads`` some (ADR 0284): those, the fields its roles name, and the
    references the input includes. None, for an input that reads every field."""
    if not reads:
        return None
    roles = declared.run_roles()
    played = {
        roles.get("material"),
        (roles.get("residues") or {}).get("field"),
        roles.get("formula"),
    }
    return frozenset(
        {*reads, *(n for n in played if n and n != "id"), *(n.split(".")[0] for n in include)}
    )


def resolve(types: Mapping[str, EntityType]) -> dict[str, Effective]:
    """Every type's effective declaration, by its qualified name."""
    return {name: resolve_one(types, name) for name in types}


# --- JSON ---------------------------------------------------------------------------------------


def effective_to_dict(e: Effective) -> dict[str, Any]:
    """A JSON-ready dict, in which empty blocks are left out; :func:`effective_from_dict`
    reads it back to an equal :class:`Effective`."""
    out: dict[str, Any] = {"lineage": list(e.lineage)}
    for name in (
        "fields",
        "identity_owner",
        "identity",
        "sample",
        "display_title",
        "viewers",
        "exports",
        "due",
        "rules",
        "notifications",
        "request",
        "result",
        "work_item",
        "amount",
        "draws_received",
        "stock_catalogue",
        "draw_guards",
        "draw_purposes",
        "draw_target",
        "storage",
        "roles",
        "sources",
    ):
        value = getattr(e, name)
        if value is None or value is False or (isinstance(value, tuple | dict) and not value):
            continue
        out[name] = _to_json(value)
    return out


def effective_from_dict(name: str, data: Mapping[str, Any]) -> Effective:
    """An :class:`Effective` from the JSON :func:`effective_to_dict` gave."""
    catalogue = data.get("stock_catalogue")
    target = data.get("draw_target")
    return Effective(
        name=name,
        lineage=tuple(data["lineage"]),
        fields={n: _field(f) for n, f in (data.get("fields") or {}).items()},
        identity_owner=data.get("identity_owner"),
        identity=tuple(data.get("identity") or ()),
        sample=bool(data.get("sample", False)),
        display_title=data.get("display_title"),
        viewers=tuple(_viewer(v) for v in data.get("viewers") or ()),
        exports=tuple(_export(x) for x in data.get("exports") or ()),
        due=data.get("due"),
        rules={n: _rule(r) for n, r in (data.get("rules") or {}).items()},
        notifications={n: _notification(x) for n, x in (data.get("notifications") or {}).items()},
        request=_request(data.get("request")),
        result=_result(data.get("result")),
        work_item=WorkItemSpec(**data["work_item"]) if data.get("work_item") else None,
        amount=_amount(data["amount"]) if data.get("amount") else None,
        draws_received=data.get("draws_received"),
        stock_catalogue=StockCatalogue.read(catalogue, name.split(".")[0]) if catalogue else None,
        draw_guards=_guards(data.get("draw_guards") or {}),
        draw_purposes={n: tuple(r) for n, r in (data.get("draw_purposes") or {}).items()},
        draw_target=DrawTarget(**{**target, "by": tuple(target["by"])}) if target else None,
        storage=_storage(data.get("storage")),
        roles=dict(data.get("roles") or {}),
        sources=dict(data.get("sources") or {}),
    )
