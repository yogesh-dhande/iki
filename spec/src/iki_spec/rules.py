"""Rules checked at save (ADR 0127) and guards on state changes (ADR 0128): reading them,
what their expressions can read, and checking them when a plugin is validated.

Both are Google's Common Expression Language (CEL), checked and evaluated by iki-wasm
(:mod:`iki_spec.wasm`), the module browsers run, so the validator, the server and a
form agree on what an expression means. An expression reads **variables**, each with a
schema the module type-checks it against: a rule reads ``record`` (the record as it will
be saved, and one step into what it refers to), ``now`` and ``today``; a guard reads
those, and who is acting (``actor``), the change (``state``), each state field's changes
(``history``, and ``last``, the latest change into each state) and the record's open
holds (``holds``). The server builds the same schemas from the installed types, so what
was checked here is what is evaluated there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from iki_spec import wasm
from iki_spec.model import (
    SIGNATURE_MEANINGS,
    EntityType,
    Extension,
    Field,
    Guard,
    Rule,
    in_entry,
    lab_role_name,
)

if TYPE_CHECKING:
    from iki_spec import validate as _validate
    from iki_spec.problems import Path
    from iki_spec.yamlsource import Document

__all__ = [
    "CONDITION",
    "MESSAGE",
    "Checked",
    "Outcome",
    "check",
    "evaluate",
    "guard_variables",
    "read",
    "record_schema",
    "rule_variables",
    "type_check",
]

#: The kinds of expression: one that's true or false, and text with expressions in it.
CONDITION = "condition"
MESSAGE = "message"

#: A field's type, and the CEL type its value is.
SCALARS: Mapping[str, str] = {
    "string": "string",
    "text": "string",
    "int": "int",
    "float": "double",
    "quantity": "double",
    "bool": "bool",
    "date": "timestamp",
    "datetime": "timestamp",
    "person": "string",
    "file": "string",
    "json": "dyn",
}
#: Fields a combination of `unique` fields can be: those whose values are equal or not.
UNIQUE_TYPES = frozenset(
    {"string", "int", "bool", "enum", "state", "date", "datetime", "ref", "person"}
)
#: Fields a range can start and end at, and the kind of range each makes.
RANGE_TYPES: Mapping[str, str] = {
    "date": "date",
    "datetime": "datetime",
    "int": "number",
    "float": "number",
    "quantity": "number",
}
#: Fields `while` can name.
WHILE_TYPES = frozenset({"enum", "state", "bool"})

_TIMESTAMP: dict[str, Any] = {"type": "timestamp"}
_STRING: dict[str, Any] = {"type": "string"}


def read(data: object, plugin: str, *, extension: bool = False) -> dict[str, Rule]:
    """A manifest's ``rules``, by name."""
    if not isinstance(data, dict):
        return {}
    return {
        name: Rule.from_manifest(name, body, plugin=plugin, extension=extension)
        for name, body in data.items()
        if isinstance(body, dict)
    }


# --- What expressions read ----------------------------------------------------------------------

#: A type's label and its fields, by its qualified name, for what a reference reads; None
#: for a type that can't be found.
RefFields = Callable[[str], "tuple[str, Mapping[str, Field]] | None"]


def field_schema(f: Field, refs: RefFields | None) -> dict[str, Any]:
    """What a field's value is to an expression. A reference is the record it refers to
    (its ID, HRID and fields, its own references as IDs) when ``refs`` gives them."""
    if f.type == "enum":
        return {"type": "string", "label": f.label, "values": list(f.values)}
    if f.type == "state":
        return {"type": "string", "label": f.label, "values": list(f.live_states)}
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        return {"type": "list", "items": field_schema(f.items, refs)}
    if f.type == "ref":
        if len(f.to) > 1:
            # A reference to one of several types (ADR 0136): what every one of them has.
            return record_schema(f"{f.label} ({', '.join(f.to)})", {}, None)
        target = refs(f.to[0]) if refs is not None and f.to else None
        if target is None:
            return dict(_STRING)
        label, fields = target
        return record_schema(f"{f.label} ({label})", fields, None)
    return {"type": SCALARS.get(f.type, "dyn")}


def record_schema(
    label: str, fields: Mapping[str, Field], refs: RefFields | None
) -> dict[str, Any]:
    """A record to an expression: its ID and HRID, and each of its current fields."""
    out: dict[str, Any] = {"id": dict(_STRING), "hrid": dict(_STRING)}
    for name, f in fields.items():
        if not f.removed:
            out[name] = field_schema(f, refs)
    return {"type": "object", "label": label, "fields": out}


def rule_variables(record: dict[str, Any]) -> dict[str, Any]:
    """What a rule's expressions read: the record as it will be saved, and the time."""
    return {"record": record, "now": dict(_TIMESTAMP), "today": dict(_TIMESTAMP)}


def _agent() -> dict[str, Any]:
    return {
        "type": "object",
        "label": "An agent",
        "fields": {"name": dict(_STRING), "client_id": dict(_STRING)},
    }


def guard_variables(
    record: dict[str, Any],
    states: Mapping[str, Field],
    roles: Iterable[str],
    field: Field | None = None,
) -> dict[str, Any]:
    """What a guard's expressions read, besides a rule's: who's acting and their lab roles
    in the record's project (``roles``: every name they could be written as), the change
    (of ``field``, whose states it's from and to), each state field's changes, and the
    record's open holds, and the signatures given for the change so far (ADR 0126)."""
    live = {name: f for name, f in states.items() if not f.removed}
    named = list(field.states) if field is not None else None

    def change(f: Field) -> dict[str, Any]:
        values = list(f.states)
        return {
            "type": "object",
            "label": f"A change of its {f.label}",
            "fields": {
                "from": {"type": "string", "label": f.label, "values": ["", *values]},
                "to": {"type": "string", "label": f.label, "values": values},
                "by": dict(_STRING),
                "agent": _agent(),
                "at": dict(_TIMESTAMP),
                "reason": dict(_STRING),
            },
        }

    names = sorted(set(roles))
    return {
        **rule_variables(record),
        "actor": {
            "type": "object",
            "label": "Who's acting",
            "fields": {
                "person": dict(_STRING),
                "agent": _agent(),
                "roles": {
                    "type": "list",
                    "items": {"type": "string", "label": "A lab role", "values": names},
                },
                "service_account": {"type": "bool"},
            },
        },
        "state": {
            "type": "object",
            "label": "The change",
            "fields": {
                "field": {"type": "string", "label": "A state field", "values": sorted(live)},
                "from": (
                    {"type": "string", "label": field.label, "values": ["", *named]}
                    if field is not None and named is not None
                    else dict(_STRING)
                ),
                "to": (
                    {"type": "string", "label": field.label, "values": named}
                    if field is not None and named is not None
                    else dict(_STRING)
                ),
            },
        },
        "history": {
            "type": "object",
            "label": "Its states' history",
            "fields": {n: {"type": "list", "items": change(f)} for n, f in live.items()},
        },
        "last": {
            "type": "object",
            "label": "The latest change into each state",
            "fields": {
                n: {
                    "type": "object",
                    "label": f"The latest change into each of its {f.label}'s states",
                    "fields": {s: change(f) for s in f.states},
                }
                for n, f in live.items()
            },
        },
        "holds": {"type": "list", "items": {"type": "string", "label": "A rule"}},
        "signatures": {
            "type": "list",
            "items": {
                "type": "object",
                "label": "A signature given for the change",
                "fields": {
                    "index": {"type": "int"},
                    "meaning": {
                        "type": "string",
                        "label": "A signature's meaning",
                        "values": list(SIGNATURE_MEANINGS),
                    },
                    "by": dict(_STRING),
                    "at": dict(_TIMESTAMP),
                },
            },
        },
    }


# --- Checking and evaluating, with iki-wasm -----------------------------------------------------


@dataclass(frozen=True)
class Checked:
    """What one expression reads, or why it doesn't type-check."""

    #: For each variable it reads, the fields it selects of it (when it's a record).
    reads: Mapping[str, tuple[str, ...]] | None = None
    error: str | None = None
    hint: str | None = None
    line: int | None = None
    column: int | None = None

    @property
    def valid(self) -> bool:
        return self.error is None

    def of(self, variable: str) -> tuple[str, ...]:
        return tuple((self.reads or {}).get(variable, ()))


def _fuel(sources: Iterable[str], count: int = 1) -> int:
    size = sum(len(s) for s in sources)
    return wasm.FUEL * (1 + size // 1000) * max(1, count)


def type_check(
    variables: Mapping[str, Any], expressions: Sequence[tuple[str, str]]
) -> list[Checked]:
    """Whether each (source, kind) type-checks against ``variables``' schemas, and what it
    reads. Raises :class:`iki_spec.wasm.UnavailableError` without the module."""
    if not expressions:
        return []
    found = wasm.call(
        "iki_check_rules",
        {
            "variables": dict(variables),
            "expressions": [{"source": s, "kind": k} for s, k in expressions],
        },
        _fuel(s for s, _ in expressions),
    )["checked"]
    out = []
    for each in found:
        if "error" in each:
            out.append(
                Checked(
                    error=str(each["error"]),
                    hint=each.get("hint"),
                    line=each.get("line"),
                    column=each.get("column"),
                )
            )
        else:
            out.append(Checked(reads={k: tuple(v) for k, v in each["reads"].items()}))
    return out


@dataclass(frozen=True)
class Outcome:
    """One expression's result in one context: a condition's value, a message's text, or
    why it has neither, and whether that's because a value it reads is empty."""

    value: bool | None = None
    text: str | None = None
    error: str | None = None
    empty: bool = False


def evaluate(
    variables: Mapping[str, Any],
    expressions: Sequence[tuple[str, str]],
    contexts: Sequence[Mapping[str, Any]],
) -> list[list[Outcome]]:
    """Each (source, kind) in each context (the variables' values, as JSON): a list per
    context. Raises :class:`iki_spec.wasm.UnavailableError` without the module, or when
    evaluating runs out of its budget."""
    if not expressions or not contexts:
        return [[] for _ in contexts]
    found = wasm.call(
        "iki_evaluate_rules",
        {
            "variables": dict(variables),
            "expressions": [{"source": s, "kind": k} for s, k in expressions],
            "contexts": [dict(c) for c in contexts],
        },
        _fuel((s for s, _ in expressions), len(contexts)),
    )["results"]
    return [
        [
            Outcome(
                value=each.get("value"),
                text=each.get("text"),
                error=each.get("error"),
                empty=bool(each.get("empty", False)),
            )
            for each in results
        ]
        for results in found
    ]


# --- Validating a plugin's rules and guards -----------------------------------------------------


def check(
    semantics: _validate._Semantics,
) -> tuple[dict[str, EntityType], tuple[Extension, ...]]:
    """Checks the plugin's rules and guards against its types' fields. Returns its types
    and extensions with what each rule and guard reads filled in."""
    return _Rules(semantics).run()


class _Rules:
    def __init__(self, semantics: _validate._Semantics) -> None:
        self.s = semantics
        self.add = semantics.add
        self.plugin = semantics.plugin
        self.loaded = semantics.loaded
        self.reported = False

    # What a type's records hold, as the server will have them.

    def extensions_of(self, entity_type: EntityType) -> list[Extension]:
        chain = {entity_type.qualified_name} | {
            a.qualified_name for a in self.s.ancestors(entity_type)
        }
        out = [e for e in self.plugin.extensions if e.extends_type in chain]
        for other in self.s.others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant
            out.extend(e for e in other.plugin.extensions if e.extends_type in chain)
        return out

    def fields_of(self, entity_type: EntityType) -> dict[str, Field]:
        """Every current field of its records: its chain's and installed extensions'."""
        out = {n: f for n, (f, _) in self.s.all_fields(entity_type).items() if not f.removed}
        for extension in self.extensions_of(entity_type):
            for name, f in extension.current_fields.items():
                out.setdefault(name, f)
        return out

    def ref_fields(self, name: str) -> tuple[str, Mapping[str, Field]] | None:
        found = self.s.types.get(name)
        if found is None:
            return None
        fields = self.fields_of(found)
        owner = next((t for t in [found, *self.s.ancestors(found)] if t.identity), None)
        if owner is not None:
            # A reference to a type with an identity is to its registry entry (ADR 0115).
            fields = {n: f for n, f in fields.items() if in_entry(f, owner.identity)}
        return found.label, fields

    def record(self, entity_type: EntityType) -> dict[str, Any]:
        return record_schema(entity_type.label, self.fields_of(entity_type), self.ref_fields)

    def roles(self) -> list[str]:
        return role_names(self.s.roles, self.plugin.name)

    # Running.

    def run(self) -> tuple[dict[str, EntityType], tuple[Extension, ...]]:
        types = dict(self.plugin.types)
        for qualified, entity_type in self.plugin.types.items():
            source = self.loaded.type_sources[qualified]
            types[qualified] = self.entity_type(entity_type, source)
        extensions = []
        for extension, source in zip(
            self.plugin.extensions, self.loaded.extension_sources, strict=True
        ):
            extensions.append(self.extension(extension, source))
        return types, tuple(extensions)

    def entity_type(self, entity_type: EntityType, source: Document) -> EntityType:
        inherited = {
            name: ancestor
            for ancestor in reversed(self.s.ancestors(entity_type))
            for name in ancestor.rules
        }
        for name in entity_type.rules:
            if name in inherited:
                self.add(
                    "rule-shadows-parent",
                    f"`{name}` is already a rule of `{inherited[name].qualified_name}`, which "
                    "this type extends, and its records keep it.",
                    source,
                    ("rules", name),
                    key=True,
                    hint="Give this rule another name.",
                )
        fields = self.fields_of(entity_type)
        own = {n: f for n, f in entity_type.fields.items() if not f.removed}
        rules = self.rules(entity_type.rules, entity_type, fields, own, source, ("rules",))
        changed = dict(entity_type.fields)
        for name, f in entity_type.fields.items():
            if f.type == "state" and any(st.guards for st in f.states.values()):
                changed[name] = self.guards(entity_type, f, source, ("fields", name))
        return replace(entity_type, rules=rules, fields=changed)

    def extension(self, extension: Extension, source: Document) -> Extension:
        target = self.s.types.get(extension.extends_type)
        if target is None or not extension.rules:
            return extension  # The extension's own check says what's wrong.
        fields = self.fields_of(target)
        own = extension.current_fields
        rules = self.rules(extension.rules, target, fields, own, source, ("rules",))
        return replace(extension, rules=rules)

    # Rules.

    def rules(
        self,
        rules: Mapping[str, Rule],
        entity_type: EntityType,
        fields: Mapping[str, Field],
        own: Mapping[str, Field],
        source: Document,
        path: Path,
    ) -> dict[str, Rule]:
        out: dict[str, Rule] = {}
        checks: list[tuple[str, str, Path]] = []
        for name, rule in rules.items():
            at: Path = (*path, name)
            out[name] = rule
            kinds = [k for k in ("check", "unique", "no_overlap") if self._has(rule, k)]
            if len(kinds) != 1:
                self.add(
                    "rule-kind",
                    f"`{name}` needs exactly one of `check`, `unique` and `no_overlap`"
                    + (f", not {' and '.join(f'`{k}`' for k in kinds)}." if kinds else "."),
                    source,
                    at,
                    key=True,
                    hint="Split it into one rule for each.",
                )
                continue
            raw = self._raw(source, at)
            if rule.kind == "check":
                for key in ("while", "per"):
                    if key in raw:
                        self.add(
                            "rule-kind",
                            f"`{key}` is for `unique` and `no_overlap` rules: a check says "
                            "when it applies with `when`.",
                            source,
                            (*at, key),
                            key=True,
                        )
                for index, field_name in enumerate(rule.fields):
                    if field_name not in fields:
                        self.unknown_field(field_name, fields, source, (*at, "fields", index))
                for index, role in enumerate(rule.hold):
                    self.s.lab_role(role, source, (*at, "hold", "reviewed_by", index))
                assert rule.check is not None  # noqa: S101 - its kind says so
                checks.append((name, rule.check, (*at, "check")))
                if rule.when is not None:
                    checks.append((name, rule.when, (*at, "when")))
            else:
                for key in ("when", "hold", "fields"):
                    if key in raw:
                        self.add(
                            "rule-kind",
                            f"`{key}` is for `check` rules: a `{rule.kind}` rule is the "
                            "database's, which refuses whatever breaks it.",
                            source,
                            (*at, key),
                            key=True,
                        )
                if rule.kind == "unique":
                    self.unique(rule, fields, own, source, at)
                else:
                    self.overlap(rule, fields, own, source, at)
                self.while_(rule, fields, own, source, at)
        found = self.expressions(
            rule_variables(self.record(entity_type)),
            [
                *((n, e, CONDITION, p) for n, e, p in checks),
                *(
                    (n, r.message, MESSAGE, (*path, n, "message"))
                    for n, r in out.items()
                    if r.message
                ),
            ],
            source,
        )
        for name, rule in list(out.items()):
            reads = found.get(name)
            if reads is None:
                continue
            record = tuple(sorted(r for r in reads if r in fields))
            own_fields = rule.unique or (*rule.no_overlap, *rule.per)
            pointed = rule.fields or (
                tuple(sorted(r for r in found.get((name, "check"), ()) if r in fields))
                if rule.kind == "check"
                else tuple(own_fields)
            )
            out[name] = replace(
                rule,
                reads=tuple(sorted({*record, *own_fields, *(f for f, _ in rule.only_while)})),
                fields=pointed,
            )
        return out

    @staticmethod
    def _has(rule: Rule, kind: str) -> bool:
        return bool(rule.check if kind == "check" else getattr(rule, kind))

    def _raw(self, source: Document, path: Path) -> dict[str, Any]:
        node: Any = self.loaded.raw.get(id(source)) or {}
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node if isinstance(node, dict) else {}

    def unknown_field(
        self, name: str, fields: Mapping[str, Field], source: Document, path: Path
    ) -> None:
        from iki_spec import schemas

        self.add(
            "unknown-field",
            f"`{name}` isn't one of its type's fields, or it's removed.",
            source,
            path,
            hint=schemas._did_you_mean(name, sorted(fields)),
        )

    def own_field(
        self,
        name: str,
        fields: Mapping[str, Field],
        own: Mapping[str, Field],
        source: Document,
        path: Path,
        allowed: Iterable[str],
        what: str,
    ) -> Field | None:
        """A field of the rule's own table, of a type ``allowed``, or None, reported."""
        if name not in fields:
            self.unknown_field(name, fields, source, path)
            return None
        if name not in own:
            self.add(
                "rule-field",
                f"`{name}` isn't a field this manifest declares: the database holds a rule "
                "like this in one table, so its fields are the ones beside it.",
                source,
                path,
                hint="Declare the rule where the field is declared: on its type, or on the "
                "extension that adds it.",
            )
            return None
        f = own[name]
        if f.registry is not None:
            self.add(
                "rule-field",
                f"`{name}` is a registry {f.registry}, the same for every project's record of "
                "an entry (ADR 0115): a rule like this holds within a project, of its own fields.",
                source,
                path,
            )
            return None
        if f.type not in allowed:
            self.add("rule-field", f"`{name}` is a {f.type} field: {what}.", source, path)
            return None
        return f

    def unique(
        self,
        rule: Rule,
        fields: Mapping[str, Field],
        own: Mapping[str, Field],
        source: Document,
        path: Path,
    ) -> None:
        for index, name in enumerate(rule.unique):
            self.own_field(
                name,
                fields,
                own,
                source,
                (*path, "unique", index),
                UNIQUE_TYPES,
                "a combination of `unique` fields is of strings, ints, bools, enums, states, "
                "dates, datetimes, references and people",
            )

    def overlap(
        self,
        rule: Rule,
        fields: Mapping[str, Field],
        own: Mapping[str, Field],
        source: Document,
        path: Path,
    ) -> None:
        start, end = rule.no_overlap
        found = [
            self.own_field(
                name,
                fields,
                own,
                source,
                (*path, "no_overlap", key),
                RANGE_TYPES,
                "a range is from and to dates, datetimes, ints or floats",
            )
            for key, name in (("from", start), ("to", end))
        ]
        if start == end:
            self.add(
                "rule-field",
                "A range goes from one field to another: `from` and `to` are the same field.",
                source,
                (*path, "no_overlap", "to"),
            )
        elif found[0] is not None and found[1] is not None:
            kinds = {RANGE_TYPES[f.type] for f in found if f is not None}
            units = {f.unit for f in found if f is not None and f.type == "quantity"}
            if len(kinds) != 1 or len(units) > 1:
                self.add(
                    "rule-field",
                    f"`{start}` is a {found[0].type} and `{end}` a {found[1].type}: a range is "
                    "from and to the same kind of value.",
                    source,
                    (*path, "no_overlap", "to"),
                )
        for index, name in enumerate(rule.per):
            if name in rule.no_overlap:
                self.add(
                    "rule-field",
                    f"`{name}` is where a range starts or ends, so it can't also be what ranges "
                    "are compared per.",
                    source,
                    (*path, "no_overlap", "per", index),
                )
                continue
            self.own_field(
                name,
                fields,
                own,
                source,
                (*path, "no_overlap", "per", index),
                UNIQUE_TYPES,
                "ranges are compared per strings, ints, bools, enums, states, dates, "
                "datetimes, references and people",
            )

    def while_(
        self,
        rule: Rule,
        fields: Mapping[str, Field],
        own: Mapping[str, Field],
        source: Document,
        path: Path,
    ) -> None:
        for name, values in rule.only_while:
            f = self.own_field(
                name,
                fields,
                own,
                source,
                (*path, "while", name),
                WHILE_TYPES,
                "`while` names enum, state and bool fields",
            )
            if f is None:
                continue
            if f.type == "bool":
                allowed: list[str] = ["true", "false"]
            elif f.type == "state":
                allowed = list(f.live_states)
            else:
                allowed = list(f.values)
            for index, value in enumerate(values):
                if value not in allowed:
                    shown = ", ".join(f"`{a}`" for a in allowed)
                    self.add(
                        "rule-while",
                        f"`{value}` isn't one of `{name}`'s values: it's one of {shown}.",
                        source,
                        (*path, "while", name, index),
                    )

    # Guards.

    def guards(self, entity_type: EntityType, f: Field, source: Document, path: Path) -> Field:
        fields = self.fields_of(entity_type)
        states = {n: x for n, x in fields.items() if x.type == "state"}
        variables = guard_variables(self.record(entity_type), states, self.roles(), f)
        asked: list[tuple[tuple[str, str], str, str, Path]] = []
        for state in f.states.values():
            for name, guard in state.guards.items():
                at: Path = (*path, "states", state.name, "guards", name)
                key = (state.name, name)
                asked.append((key, guard.check, CONDITION, (*at, "check")))
                if guard.when is not None:
                    asked.append((key, guard.when, CONDITION, (*at, "when")))
                asked.append((key, guard.message, MESSAGE, (*at, "message")))
        found = self.expressions(variables, asked, source)
        new_states = {}
        for state_name, state in f.states.items():
            guards: dict[str, Guard] = {}
            for name, guard in state.guards.items():
                reads = found.get((state_name, name), ())
                guards[name] = replace(guard, reads=tuple(sorted(r for r in reads if r in fields)))
            new_states[state_name] = replace(state, guards=guards)
        return replace(f, states=new_states)

    # Expressions.

    def expressions(
        self,
        variables: Mapping[str, Any],
        asked: Sequence[tuple[Any, str, str, Path]],
        source: Document,
    ) -> dict[Any, tuple[str, ...]]:
        """Type-checks each (key, source, kind, path), reporting those that don't; returns
        the record's fields each key's expressions read, and each (key, "check")'s own."""
        if not asked:
            return {}
        try:
            checked = type_check(variables, [(e, k) for _, e, k, _ in asked])
        except wasm.UnavailableError as error:
            if not self.reported:
                self.reported = True
                self.add(
                    "expressions-unchecked",
                    f"Rules and guards can't be checked without iki-wasm: {error}",
                    source,
                    (),
                    hint="Build it with scripts/build-wasm.sh, or install iki-spec from a "
                    "release, which includes it.",
                )
            return {}
        out: dict[Any, set[str]] = {}
        for (key, _, kind, path), result in zip(asked, checked, strict=True):
            if not result.valid:
                where = {CONDITION: "doesn't work", MESSAGE: "has an expression that doesn't work"}
                at = (
                    f" (line {result.line}, column {result.column})"
                    if result.line is not None and result.column is not None
                    else ""
                )
                self.add(
                    "invalid-expression",
                    f"It {where[kind]}{at}: {result.error}",
                    source,
                    path,
                    hint=result.hint,
                )
                continue
            reads = result.of("record")
            out.setdefault(key, set()).update(reads)
            if path and path[-1] == "check":
                out.setdefault((key, "check"), set()).update(reads)
        return {k: tuple(sorted(v)) for k, v in out.items()}


def role_names(roles: Iterable[str], plugin: str) -> list[str]:
    """Every name ``roles`` (qualified) can be written as in a plugin's guards: each one
    in full, and the plugin's own by their short names too."""
    out: set[str] = set()
    for role in roles:
        full = lab_role_name(role, plugin)
        out.add(full)
        owner, _, short = full.rpartition(".")
        if owner == plugin:
            out.add(short)
    return sorted(out)
