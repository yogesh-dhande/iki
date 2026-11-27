"""Rules checked at save (ADR 0127) and guards on state changes (ADR 0128): reading them,
what their expressions can read, and checking them when a plugin is validated.

Both are Google's Common Expression Language (CEL), checked and evaluated by iki-wasm
(:mod:`iki_spec.wasm`), the module browsers run, so the validator, the server and a
form agree on what an expression means. An expression reads **variables**, each with a
schema the module type-checks it against: a rule reads ``record`` (the record as it will
be saved, and one step into what it refers to), ``now`` and ``today``; a guard reads
those, and who is acting (``actor``), the change (``state``), each state field's changes
(``history``, and ``last``, the latest change into each state), the value edits since each
state's latest entry (``edits``) and the record's open holds (``holds``). The server builds
the same schemas from the installed types, so what was checked here is what is evaluated
there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from iki_spec import procedures, wasm
from iki_spec.model import (
    SIGNATURE_MEANINGS,
    EntityType,
    Extension,
    Field,
    Guard,
    Rule,
    as_refined,
    in_entry,
    lab_role_name,
)
from iki_spec.problems import Severity

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
    "draw_variables",
    "evaluate",
    "guard_variables",
    "read",
    "record_schema",
    "rule_variables",
    "state_origins",
    "type_check",
]

#: The kinds of expression: one that's true or false, and text with expressions in it.
CONDITION = "condition"
MESSAGE = "message"

#: A lab list option's code, read through one of the record's references.
_LIST_CODE = re.compile(r"\brecord\s*\.\s*([a-z][a-z0-9_]*)\s*\.\s*code\b")
#: CEL's string literals, which read nothing: raw, triple-quoted, and quoted.
_CEL_STRING = re.compile(
    "[rR]?(?:'''.*?'''|\"\"\".*?\"\"\"|'(?:[^'\\\\\\n]|\\\\.)*'|\"(?:[^\"\\\\\\n]|\\\\.)*\")",
    re.DOTALL,
)

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
        if refs is None:
            return dict(_STRING)
        if len(f.to) > 1:
            # A reference to one of several types (ADR 0136): what every one of them has.
            return record_schema(f"{f.label} ({', '.join(f.to)})", {}, None)
        target = refs(f.to[0]) if f.to else None
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


def draw_variables(label: str, fields: Mapping[str, Field]) -> dict[str, Any]:
    """A draw guard reads the record's own values, clock, operation and purpose.
    References are IDs. Its refusal may be shown to anyone who can read this record."""
    return {
        **rule_variables(record_schema(label, fields, None)),
        "operation": {"type": "string", "values": ["draw", "transfer", "split"]},
        "purpose": {"type": "string", "values": ["routine", "qa_sampling"]},
    }


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

    value_names = sorted(record["fields"].keys() - {"id", "hrid"} - live.keys())
    edit = {
        "type": "object",
        "label": "An edit to the record's values",
        "fields": {
            "by": dict(_STRING),
            "fields": {
                "type": "list",
                "items": {"type": "string", "label": "A value field", "values": value_names},
            },
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
        "edits": {
            "type": "object",
            "label": "Value edits since a state was entered",
            "fields": {
                n: {
                    "type": "object",
                    "label": f"Value edits since each of its {f.label}'s states",
                    "fields": {s: {"type": "list", "items": edit} for s in f.states},
                }
                for n, f in live.items()
            },
        },
        "holds": {"type": "list", "items": {"type": "string", "label": "A rule"}},
        "covered_subjects": {
            "type": "list",
            "items": {"type": "string", "label": "Commissioned record ID"},
        },
        "draws": {
            "type": "list",
            "items": {
                "type": "object",
                "label": "A recorded draw into this record",
                "fields": {"on": dict(_TIMESTAMP)},
            },
        },
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


def state_origins(field: Field, target: str, origins: Sequence[str]) -> list[bool | None]:
    """Whether a destination's guards hold given just the origin. None means a guard
    needs the actual record or its expression cannot be settled from the transition."""
    guards = field.states[target].guards.values()
    if not guards:
        return [True] * len(origins)
    sources = [s for g in guards for s in (g.check, g.when) if s is not None]
    found = wasm.call(
        "iki_state_origins",
        {
            "field": field.name,
            "target": target,
            "origins": list(origins),
            "guards": [{"check": g.check, "when": g.when} for g in guards],
        },
        _fuel(sources, len(origins)),
    )
    results: list[bool | None] = found["origins"]
    return results


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
        out = [e for e in self.plugin.extensions if e.adds_to in chain]
        for other in self.s.others.values():
            assert other.plugin is not None  # noqa: S101 - an invariant
            out.extend(e for e in other.plugin.extensions if e.adds_to in chain)
        return out

    def fields_of(self, entity_type: EntityType) -> dict[str, Field]:
        """Every current field of its records: its chain's and installed extensions'."""
        lineage = [entity_type, *self.s.ancestors(entity_type)]
        out = {
            n: as_refined(lineage, n, f)
            for n, (f, _) in self.s.all_fields(entity_type).items()
            if not f.removed
        }
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

    def fixed_ref_fields(self, name: str) -> tuple[str, Mapping[str, Field]] | None:
        """As :meth:`ref_fields`, without what a lab list's curators can change (ADR 0270)."""
        found = self.ref_fields(name)
        if found is None or not self.s.types[name].is_list:
            return found
        label, fields = found
        return label, {n: f for n, f in fields.items() if f.immutable}

    def curated(self, entity_type: EntityType) -> list[str]:
        """The facts of the lab lists its records refer to that their curators can change,
        as ``List.fact``."""
        out: set[str] = set()
        for f in self.fields_of(entity_type).values():
            targets = f.to if f.type == "ref" else ()
            if f.items is not None:
                targets = f.items.to if f.items.type == "ref" else ()
                for cell in f.items.fields.values():
                    targets = (*targets, *(cell.to if cell.type == "ref" else ()))
            for target in targets:
                listed = self.s.types.get(target)
                found = self.ref_fields(target)
                if listed is None or found is None or not listed.is_list:
                    continue
                out.update(f"{listed.name}.{n}" for n, x in found[1].items() if not x.immutable)
        return sorted(out)

    def record(self, entity_type: EntityType, *, fixed: bool = False) -> dict[str, Any]:
        refs = self.fixed_ref_fields if fixed else self.ref_fields
        return record_schema(entity_type.label, self.fields_of(entity_type), refs)

    def roles(self) -> list[str]:
        return role_names(self.s.roles, self.plugin.name)

    # Running.

    def run(self) -> tuple[dict[str, EntityType], tuple[Extension, ...]]:
        types = dict(self.plugin.types)
        for qualified, entity_type in self.plugin.types.items():
            if qualified in self.s.invalid_types:
                continue
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
                states = {n: x for n, x in fields.items() if x.type == "state"}
                variables = guard_variables(self.record(entity_type), states, self.roles(), f)
                fixed = guard_variables(
                    self.record(entity_type, fixed=True), states, self.roles(), f
                )
                checked_states = {}
                for state_name, state in f.states.items():
                    path: Path = ("fields", name, "states", state_name, "guards")
                    checked = self.guards(
                        state.guards, variables, fields, source, path, fixed=(entity_type, fixed)
                    )
                    checked_states[state_name] = replace(state, guards=checked)
                changed[name] = replace(f, states=checked_states)
        review = entity_type.review
        if (
            review is not None
            and (review.guards or review.independent_of)
            and review.field in fields
        ):
            # A reviewed type's own guards read the review field as it names its states
            # (ADR 0301); its procedure's flows are compiled and checked where it's paired.
            f = fields[review.field]
            states = {n: x for n, x in fields.items() if x.type == "state"}
            variables = guard_variables(self.record(entity_type), states, self.roles(), f)
            fixed = guard_variables(self.record(entity_type, fixed=True), states, self.roles(), f)
            review_guards = {n: g.guard for n, g in review.guards.items()}
            if review.independent_of:
                # The people it adds to every independent state, with their conditions.
                labels = {n: st.label for n, st in f.states.items()}
                review_guards["independent"] = procedures.exclusion_guard(
                    review.field,
                    review.independent_of,
                    open=review.needs.open,
                    labels=labels,
                    people=self.s.people_kind(entity_type),
                )
            checked = self.guards(
                review_guards,
                variables,
                fields,
                source,
                ("review", "guards"),
                fixed=(entity_type, fixed),
            )
            review = replace(
                review,
                guards={n: replace(g, guard=checked[n]) for n, g in review.guards.items()},
            )
        draw = entity_type.draws.guards
        if draw:
            draw = self.guards(
                draw, draw_variables(entity_type.label, fields), fields, source, ("draws", "guards")
            )
        return replace(
            entity_type,
            rules=rules,
            fields=changed,
            draws=replace(entity_type.draws, guards=draw),
            review=review,
        )

    def extension(self, extension: Extension, source: Document) -> Extension:
        target = self.s.types.get(extension.adds_to)
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
                for ref, number in rule.reference_revisions:
                    if ref not in fields or fields[ref].type != "ref":
                        self.add(
                            "rule-reference-revision",
                            f"`{ref}` must be a reference.",
                            source,
                            (*at, "reference_revisions", ref),
                            key=True,
                        )
                    if number not in fields or fields[number].type != "int":
                        self.add(
                            "rule-reference-revision",
                            f"`{number}` must be an integer revision.",
                            source,
                            (*at, "reference_revisions", ref),
                        )
                for index, role in enumerate(rule.hold):
                    self.s.lab_role(role, source, (*at, "hold", "reviewed_by", index))
                assert rule.check is not None  # noqa: S101 - its kind says so
                checks.append((name, rule.check, (*at, "check")))
                if rule.when is not None:
                    checks.append((name, rule.when, (*at, "when")))
            else:
                for key in (
                    "when",
                    "hold",
                    "fields",
                    "reference_revisions",
                    "recheck_before_signing",
                ):
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
        for _, expression, at in checks:
            self.list_codes(expression, fields, source, at)
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
            fixed=(entity_type, rule_variables(self.record(entity_type, fixed=True))),
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
                reads=tuple(
                    sorted(
                        {
                            *record,
                            *own_fields,
                            *(f for f, _ in rule.only_while),
                            *(n for pair in rule.reference_revisions for n in pair),
                        }
                    )
                ),
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
                (*path, "while", "field"),
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
                        (*path, "while", "states", index),
                    )

    # Guards.

    def guards(
        self,
        guards: Mapping[str, Guard],
        variables: Mapping[str, Any],
        fields: Mapping[str, Field],
        source: Document,
        path: Path,
        *,
        fixed: tuple[EntityType, Mapping[str, Any]] | None = None,
    ) -> dict[str, Guard]:
        asked: list[tuple[str, str, str, Path]] = []
        for name, guard in guards.items():
            at: Path = (*path, name)
            asked.append((name, guard.check, CONDITION, (*at, "check")))
            self.list_codes(guard.check, fields, source, (*at, "check"))
            if guard.when is not None:
                asked.append((name, guard.when, CONDITION, (*at, "when")))
                self.list_codes(guard.when, fields, source, (*at, "when"))
            asked.append((name, guard.message, MESSAGE, (*at, "message")))
        found = self.expressions(variables, asked, source, fixed=fixed)
        return {
            name: replace(guard, reads=tuple(r for r in found.get(name, ()) if r in fields))
            for name, guard in guards.items()
        }

    # Expressions.

    def list_codes(
        self, expression: str, fields: Mapping[str, Field], source: Document, path: Path
    ) -> None:
        """Warns where a condition reads a lab list option's code: a curator supersedes a
        wrong code, and records then name the new one, so a rule or guard comparing the
        old one stops applying to them without anyone being told (ADR 0270)."""
        bare = _CEL_STRING.sub('""', expression)
        for name in dict.fromkeys(m[1] for m in _LIST_CODE.finditer(bare)):
            f = fields.get(name)
            if f is None or f.type != "ref":
                continue
            lists = [t for t in f.to if (found := self.s.types.get(t)) and found.is_list]
            if not lists:
                continue
            self.add(
                "list-code-in-expression",
                f"It reads `{name}.code`, an option of the lab list `{lists[0]}`, which its "
                "curators keep: an option they supersede gives records a new code, and "
                "this stops applying to them.",
                source,
                path,
                severity=Severity.WARNING,
                hint="A value a rule or guard reads should be the plugin's: make it an `enum` "
                "field. Reading an option's other facts is fine.",
            )

    def expressions(
        self,
        variables: Mapping[str, Any],
        asked: Sequence[tuple[Any, str, str, Path]],
        source: Document,
        *,
        fixed: tuple[EntityType, Mapping[str, Any]] | None = None,
    ) -> dict[Any, tuple[str, ...]]:
        """Type-checks each (key, source, kind, path), reporting those that don't; returns
        the record's fields each key's expressions read, and each (key, "check")'s own.
        ``fixed``: the type, and its variables without the facts of the lab lists it refers
        to that their curators can change. A condition that reads one is refused."""
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
        if fixed is not None:
            self.curated_reads(fixed, asked, checked, source)
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

    def curated_reads(
        self,
        fixed: tuple[EntityType, Mapping[str, Any]],
        asked: Sequence[tuple[Any, str, str, Path]],
        checked: Sequence[Checked],
        source: Document,
    ) -> None:
        """Refuses a condition that reads a fact a lab list's curators can change: what a
        state, guard or rule reads is the plugin's (ADR 0270), so it's fixed once the option
        is added (ADR 0143). A message may show one, as it says what's wrong."""
        entity_type, variables = fixed
        curated = self.curated(entity_type)
        if not curated:
            return
        conditions = [
            (each, result)
            for each, result in zip(asked, checked, strict=True)
            if each[2] == CONDITION and result.valid
        ]
        if not conditions:
            return
        again = type_check(variables, [(e, k) for (_, e, k, _), _ in conditions])
        for ((_, expression, _, path), _), result in zip(conditions, again, strict=True):
            if result.valid:
                continue
            named = [c for c in curated if c.split(".", 1)[1] in expression] or curated
            shown = ", ".join(f"`{c}`" for c in named)
            self.add(
                "reads-curated-fact",
                f"It reads {shown}, which the list's curators can change: what a rule or "
                "guard reads is the plugin's (ADR 0270).",
                source,
                path,
                hint="Declare the fact `immutable: true`, so it's fixed once an option is "
                "added and a wrong one is superseded (ADR 0143).",
            )


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
