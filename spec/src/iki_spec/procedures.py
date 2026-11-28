"""Review procedures (ADR 0301): reading and checking them, what a reviewed type needs of
one, and compiling a flow into the state field its records move through.

A procedure is declared once, by core or any plugin: its role slots, its states and the
flows through them a project can choose. A reviewed type says what it needs of one, and
any procedure that fits can be pinned on its records (ADR 0302). Who may sign, and whom a
signer must not be, are declared structure (``maker`` and ``independent_of``), compiled
here into the guards ADR 0128 evaluates, so the server enforces exactly what the starter
preview and the flow diagram describe.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from iki_spec.model import (
    EXCLUSIONS,
    GUARD_GROUPS,
    SIGNATURE_MEANINGS,
    Exclusion,
    Field,
    Guard,
    Maker,
    ReviewFlow,
    ReviewProcedure,
    ReviewSpec,
    ReviewState,
    Signature,
    Slot,
    State,
)

__all__ = [
    "ProcedureProblem",
    "Staffing",
    "can_staff",
    "check_procedure",
    "compile_flow",
    "default_binding",
    "exclusion_guard",
    "exclusion_words",
    "maker_guard",
    "maker_words",
    "misfits",
    "parse_reference",
    "read_procedure",
    "review_field",
    "staffing",
]

#: The shared lab roles a slot is bound to by default (ADR 0304).
SHARED_ROLES = "iki/lab_roles"
#: The guards a compiled state's own conditions are named, which a type's can't take.
COMPILED_GUARDS = ("route", "maker", "independent")


def parse_reference(reference: str) -> tuple[str, int] | None:
    """``iki/core.result_review@1`` → (``iki/core.result_review``, 1), or None."""
    name, at, version = reference.rpartition("@")
    if not at or "." not in name or not version.isdigit():
        return None
    return name, int(version)


# --- Reading ---------------------------------------------------------------------------------


def read_procedure(plugin: str, data: Mapping[str, Any], file: str | None) -> ReviewProcedure:
    """A schema-valid ``kind: ReviewProcedure`` manifest as Iki understands it."""
    slots: dict[str, Any] = data["slots"] if isinstance(data.get("slots"), dict) else {}
    states: dict[str, Any] = data["states"] if isinstance(data.get("states"), dict) else {}
    flows: dict[str, Any] = data["flows"] if isinstance(data.get("flows"), dict) else {}
    return ReviewProcedure(
        plugin=plugin,
        name=str(data["name"]),
        version=int(data["version"]),
        label=str(data.get("label") or data["name"]),
        description=data.get("description") if isinstance(data.get("description"), str) else None,
        slots={
            n: Slot(name=n, label=str(s.get("label") or n), description=s.get("description"))
            for n, s in slots.items()
            if isinstance(s, dict)
        },
        states={n: _state(n, s) for n, s in states.items() if isinstance(s, dict)},
        flows={
            n: ReviewFlow(
                name=n,
                label=str(f.get("label") or n),
                summary=str(f.get("summary") or ""),
                route=tuple(f.get("route") or ()),
                moves={k: tuple(v) for k, v in (f.get("moves") or {}).items()},
            )
            for n, f in flows.items()
            if isinstance(f, dict)
        },
        file=file,
    )


def _state(name: str, data: Mapping[str, Any]) -> ReviewState:
    by = data.get("by")
    sig = data.get("signature")
    signature = None
    if isinstance(sig, dict):
        meaning = str(sig.get("meaning") or "")
        known = SIGNATURE_MEANINGS.get(meaning)
        signature = Signature(
            meaning=meaning,
            label=str(sig.get("label") or (known.label if known else meaning)),
            statement=str(sig.get("statement") or (known.statement if known else "")),
        )
    return ReviewState(
        name=name,
        label=str(data.get("label") or name.replace("_", " ").capitalize()),
        description=data.get("description") if isinstance(data.get("description"), str) else None,
        by=(by,) if isinstance(by, str) else tuple(by) if isinstance(by, list) else (),
        locks=data.get("locks") is True,
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        on_edit=data.get("on_edit") if isinstance(data.get("on_edit"), str) else None,
        signature=signature,
        maker=Maker.read(data.get("maker")),
        independent_of=Exclusion.read_all(data.get("independent_of")),
    )


# --- Checking a procedure --------------------------------------------------------------------


@dataclass(frozen=True)
class ProcedureProblem:
    code: str
    message: str
    #: Where in the manifest, as keys from its root.
    path: tuple[str | int, ...]
    hint: str | None = None
    warning: bool = False


def check_procedure(procedure: ReviewProcedure) -> list[ProcedureProblem]:
    """What's wrong with a procedure on its own, before any type is paired with it."""
    out: list[ProcedureProblem] = []

    def add(code: str, message: str, *path: str | int, hint: str | None = None) -> None:
        out.append(ProcedureProblem(code, message, path, hint))

    used_slots: set[str] = set()
    for name, st in procedure.states.items():
        at = ("states", name)
        for slot in st.by:
            used_slots.add(slot)
            if slot not in procedure.slots:
                add(
                    "unknown-slot",
                    f"`{slot}` isn't one of this procedure's slots.",
                    *at,
                    "by",
                    hint="Declare it under `slots`, or name one that is: "
                    + ", ".join(f"`{s}`" for s in procedure.slots)
                    + ".",
                )
        if st.signature is not None:
            if st.maker is None and not st.independent_of:
                add(
                    "signer-undeclared",
                    f"`{name}` is signed, so it says who may sign: the work's maker "
                    "(`maker`) or someone independent of it (`independent_of`).",
                    *at,
                    hint="A signature attesting to someone else's work declares whom its "
                    "signer must not be, such as `independent_of: [creator, editors]`.",
                )
            elif st.maker is not None and st.independent_of:
                add(
                    "signer-both",
                    f"`{name}` says both that its signer is the maker and whom they must not "
                    "be. Keep one.",
                    *at,
                )
            if st.signature.meaning == "authorship" and st.maker is None:
                add(
                    "authorship-not-maker",
                    f"`{name}`'s authorship signature says “I made this record”, so "
                    "its signer is the work's maker.",
                    *at,
                    "signature",
                    hint="Add `maker: true`, or give the signature another meaning.",
                )
            if not st.by:
                add(
                    "signed-by-no-one",
                    f"No slot sets `{name}`, so no one could sign it.",
                    *at,
                    hint="Name the slots whose holders sign it under `by`.",
                )
        if st.on_edit is not None:
            if st.on_edit not in procedure.states:
                add("unknown-state", f"`{st.on_edit}` isn't one of its states.", *at, "on_edit")
            if st.signature is not None or not st.locks:
                add(
                    "invalid-on-edit",
                    f"Only an unsigned state that locks returns on an edit, and `{name}` "
                    + ("is signed." if st.signature is not None else "doesn't lock."),
                    *at,
                    "on_edit",
                )
        for index, ex in enumerate(st.independent_of):
            where = (*at, "independent_of", index)
            if ex.kind == "people":
                add(
                    "people-in-procedure",
                    "A procedure doesn't know a type's fields: the type adds the people its "
                    "fields name, in its `review: independent_of`.",
                    *where,
                )
            if ex.window:
                add(
                    "window-in-procedure",
                    "A procedure's editors are those since the record last entered its "
                    "flow's first state, so it names no window.",
                    *where,
                )
            if ex.when is not None:
                add(
                    "when-in-procedure",
                    "A procedure applies to any type that fits it, so its exclusions don't "
                    "read the record. A type adds conditional exclusions of its own.",
                    *where,
                )
            for s in ex.states:
                if s not in procedure.states:
                    add("unknown-state", f"`{s}` isn't one of its states.", *where, "signers")
                elif procedure.states[s].signature is None:
                    add(
                        "unsigned-signers",
                        f"`{s}` isn't signed, so it has no signer to exclude.",
                        *where,
                        "signers",
                    )
        if st.maker is not None and st.maker.person is not None:
            add(
                "people-in-procedure",
                "A procedure doesn't know a type's fields, so its maker is the record's creator "
                "or current editor (`maker: true`).",
                *at,
                "maker",
            )
    out.extend(
        ProcedureProblem(
            "unused-slot",
            f"No state is set by `{slot}`.",
            ("slots", slot),
            "Remove it, or name it in a state's `by`.",
            warning=True,
        )
        for slot in procedure.slots
        if slot not in used_slots
    )
    in_a_flow: set[str] = set()
    for name, flow in procedure.flows.items():
        at = ("flows", name)
        for index, s in enumerate(flow.route):
            if s not in procedure.states:
                add("unknown-state", f"`{s}` isn't one of its states.", *at, "route", index)
        if not flow.route or flow.route[0] not in procedure.states:
            continue
        first = procedure.states[flow.route[0]]
        if first.locks:
            add(
                "open-state-locks",
                f"`{first.name}` is where `{name}`'s records are recorded and edited, so it "
                "can't lock them.",
                *at,
                "route",
                0,
            )
        for target, sources in flow.moves.items():
            if target not in procedure.states:
                add("unknown-state", f"`{target}` isn't one of its states.", *at, "moves", target)
            for s in sources:
                if s not in procedure.states:
                    add("unknown-state", f"`{s}` isn't one of its states.", *at, "moves", target)
                elif s not in flow.states:
                    add(
                        "move-from-elsewhere",
                        f"`{s}` isn't in `{name}`, so no record of it moves from there.",
                        *at,
                        "moves",
                        target,
                    )
        for s in flow.states:
            in_a_flow.add(s)
            found = procedure.states.get(s)
            if found is not None and s != flow.open and not found.by:
                add(
                    "unreachable-state",
                    f"No slot sets `{s}`, so `{name}`'s records never reach it.",
                    *at,
                    hint=f"Name who sets it under `states: {s}: by`.",
                )
            if found is not None and found.on_edit is not None and found.on_edit not in flow.states:
                add(
                    "unknown-state",
                    f"`{s}` returns to `{found.on_edit}` on an edit, which isn't in `{name}`.",
                    *at,
                )
    out.extend(
        ProcedureProblem(
            "unused-state",
            f"No flow goes through `{s}`.",
            ("states", s),
            "Remove it, or add it to a flow's `route` or `moves`.",
            warning=True,
        )
        for s in procedure.states
        if s not in in_a_flow
    )
    return out


# --- What a type needs ----------------------------------------------------------------------


def misfits(
    review: ReviewSpec, type_label: str, procedure: ReviewProcedure, flows: Iterable[str]
) -> list[str]:
    """Why ``procedure``'s ``flows`` can't review the type, one sentence each; empty if
    they fit what it needs (ADR 0301)."""
    out: list[str] = []
    needs = review.needs
    name = procedure.qualified_name

    def no(message: str) -> None:
        out.append(f"{name} can't review {type_label}: {message}")

    for s in needs.states:
        if s not in procedure.states:
            no(f"it has no `{s}` state, which {type_label} names.")
    for slot in needs.slots:
        if slot not in procedure.slots:
            no(f"it has no `{slot}` slot, which {type_label} names.")
    for guard in review.guards.values():
        for s in guard.on:
            if s not in GUARD_GROUPS and s not in procedure.states:
                no(f"it has no `{s}` state, which {type_label}'s `{guard.guard.name}` is on.")
    for flow_name in flows:
        flow = procedure.flows.get(flow_name)
        if flow is None:
            no(f"it has no `{flow_name}` flow.")
            continue
        route = _route(flow, review)
        if review.opens:
            continue
        if route[0] != needs.open:
            no(
                f"its flow `{flow_name}` starts in `{route[0]}`, and {type_label}'s actions "
                f"write in `{needs.open}`."
            )
        if needs.final and route[-1] not in needs.final:
            taken = _or([f"`{s}`" for s in needs.final])
            no(
                f"its flow `{flow_name}` ends in `{route[-1]}`, and {type_label}'s readers take "
                f"only {taken} as complete."
            )
        states = [procedure.states[s] for s in _flow_states(flow, review) if s in procedure.states]
        if needs.locks == "signed":
            for st in states:
                if st.signature is not None and not st.locks:
                    no(
                        f"`{st.name}` in its flow `{flow_name}` is signed and doesn't lock, and "
                        f"{type_label}'s signed records must not change."
                    )
        if needs.locks == "final":
            final = procedure.states.get(route[-1])
            if final is None or not final.locks or len(route) == 1:
                no(
                    f"its flow `{flow_name}` doesn't end in a state that locks, and "
                    f"{type_label}'s completed records must not change."
                )
        if review.derived:
            final = procedure.states.get(route[-1])
            if len(route) == 1 or final is None or final.signature is None:
                no(
                    f"its flow `{flow_name}` doesn't end in a signed state, and {type_label} "
                    "re-issues a signed record."
                )
    return out


def route(flow: ReviewFlow, review: ReviewSpec | None) -> tuple[str, ...]:
    """The states a type's records pass through in ``flow``, in order: for a derived
    record, the flow's first and final states (ADR 0301)."""
    return _route(flow, review)


def _route(flow: ReviewFlow, review: ReviewSpec | None) -> tuple[str, ...]:
    if review is not None and review.derived and len(flow.route) > 1:
        return (flow.route[0], flow.route[-1])
    return flow.route


def _flow_states(flow: ReviewFlow, review: ReviewSpec | None) -> tuple[str, ...]:
    if review is not None and review.derived:
        return _route(flow, review)
    return flow.states


def _sources(flow: ReviewFlow, review: ReviewSpec | None, state: str) -> tuple[str, ...]:
    if review is not None and review.derived:
        route = _route(flow, review)
        return (route[0],) if len(route) > 1 and state == route[-1] else ()
    return flow.sources(state)


# --- Compiling a flow ------------------------------------------------------------------------


#: Whether a person path names one person (False) or a list of them (True); None if it
#: doesn't name people.
PeopleKind = Callable[[str], bool | None]


def default_binding(procedure: ReviewProcedure, roles: Collection[str]) -> dict[str, str]:
    """Each slot's default lab role: the shared ``iki/lab_roles`` role of its name, where
    one is declared (ADR 0304)."""
    return {
        slot: f"{SHARED_ROLES}.{slot}"
        for slot in procedure.slots
        if f"{SHARED_ROLES}.{slot}" in roles
    }


def compile_flow(
    procedure: ReviewProcedure,
    flow_name: str,
    *,
    field: str,
    binding: Mapping[str, str],
    review: ReviewSpec | None = None,
    people: PeopleKind | None = None,
) -> dict[str, State]:
    """The states a record pinned to ``flow_name`` moves through, in the procedure's order,
    as the state field ``field`` of its type: who sets each (``binding`` from slot to lab
    role), its lock, reason and signature, and its guards: the route, ``maker`` and
    ``independent_of`` compiled, then the type's own (ADR 0301)."""
    flow = procedure.flows[flow_name]
    names = _flow_states(flow, review)
    labels = {s: procedure.states[s].label for s in names}
    final = _route(flow, review)[-1]
    out: dict[str, State] = {}
    for name in procedure.states:
        if name not in names:
            continue
        st = procedure.states[name]
        signed = st.signature is not None
        if review is not None and review.derived:
            independence: tuple[Exclusion, ...] = ()
            maker = None
        else:
            independence = st.independent_of
            if independence and review is not None:
                independence = (*independence, *review.independent_of)
            maker = st.maker
        guards: dict[str, Guard] = {}
        sources = _sources(flow, review, name)
        # A flow's first state is set again only by its moves, such as a return for
        # correction; where it has none, records only start in it.
        reentered = name != flow.open or bool(sources)
        if reentered:
            guards["route"] = _route_guard(name, sources, labels)
        if maker is not None:
            guards["maker"] = maker_guard(field, maker, flow.open, labels)
        if independence:
            compiled = exclusion_guard(
                field, independence, open=flow.open, labels=labels, people=people
            )
            if review is not None and review.independent_of:
                compiled = replace(compiled, reads=review.reads)
            guards["independent"] = compiled
        if review is not None:
            for gname, extra in review.guards.items():
                on = set(extra.on)
                if (
                    name in on
                    or ("signed" in on and signed)
                    or ("final" in on and name == final and name != flow.open)
                ):
                    guards[gname] = extra.guard
        out[name] = State(
            name=name,
            label=st.label,
            description=st.description,
            set_by=tuple(binding[s] for s in st.by if s in binding) if reentered else (),
            locks=st.locks,
            reason=st.reason if reentered else None,
            on_edit=st.on_edit if st.on_edit in names else None,
            signatures=(st.signature,) if st.signature is not None else (),
            guards=guards,
            maker=maker,
            independent_of=independence,
        )
    return out


def review_field(
    review: ReviewSpec,
    procedure: ReviewProcedure,
    flow_name: str,
    *,
    binding: Mapping[str, str],
    people: PeopleKind | None = None,
) -> Field:
    """A reviewed type's review field as a record pinned to ``flow_name`` has it."""
    flow = procedure.flows[flow_name]
    return Field(
        name=review.field,
        type="state",
        label=review.label,
        states=compile_flow(
            procedure,
            flow_name,
            field=review.field,
            binding=binding,
            review=review,
            people=people,
        ),
        initial=flow.open,
        procedure=True,
    )


def _route_guard(name: str, sources: Sequence[str], labels: Mapping[str, str]) -> Guard:
    if not sources:
        check = "false"
        message = f"A record doesn't return to {labels[name]} in this flow."
    elif len(sources) == 1:
        check = f"state.from == {json.dumps(sources[0])}"
        message = f"A record moves to {labels[name]} only from {labels[sources[0]]}."
    else:
        check = f"state.from in {json.dumps(list(sources))}"
        message = f"A record moves to {labels[name]} only from {_or([labels[s] for s in sources])}."
    return Guard(name="route", check=check, message=message)


def maker_guard(field: str, maker: Maker, window: str, labels: Mapping[str, str]) -> Guard:
    """The ``maker`` guard: the signer made the work (ADR 0301)."""
    if maker.person is not None:
        check = f"record.{maker.person} == actor.person"
    else:
        check = (
            f"history.{field}[0].by == actor.person || "
            f"edits.{field}.{window}.exists(e, e.by == actor.person)"
        )
    return Guard(name="maker", check=check, message=maker_words(maker, window, labels))


def maker_words(maker: Maker, window: str, labels: Mapping[str, str]) -> str:
    if maker.person is not None:
        return f"Only the person its {maker.person.replace('_', ' ')} names can sign it."
    return (
        "Only whoever created this record, or edited it since it was "
        f"{labels.get(window, window)}, can sign it."
    )


def exclusion_guard(
    field: str,
    exclusions: Sequence[Exclusion],
    *,
    open: str | None,
    labels: Mapping[str, str],
    people: PeopleKind | None = None,
) -> Guard:
    """The ``independent`` guard: the signer is none of ``exclusions`` (ADR 0301). On a
    procedure's flow, editors are those since the record last entered ``open``."""
    parts = [_exclusion_check(field, ex, open, labels, people) for ex in exclusions]
    return Guard(
        name="independent",
        check=" && ".join(p for p in parts if p),
        message=exclusion_words(exclusions, open=open, labels=labels),
    )


def _exclusion_check(
    field: str,
    ex: Exclusion,
    open: str | None,
    labels: Mapping[str, str],
    people: PeopleKind | None,
) -> str:
    if ex.kind == "creator":
        check = f"history.{field}[0].by != actor.person"
    elif ex.kind == "editors":
        window = ex.window or ((open,) if open else ())
        edits = " + ".join(f"edits.{field}.{s}" for s in window) or "[]"
        if len(window) > 1:
            edits = f"({edits})"
        if ex.fields:
            names = json.dumps(list(ex.fields))
            check = f"{edits}.all(e, e.by != actor.person || !e.fields.exists(f, f in {names}))"
        else:
            check = f"{edits}.all(e, e.by != actor.person)"
    elif ex.kind == "signers":
        named = [s for s in ex.states if s in labels]
        if not named:
            return ""
        check = " && ".join(
            f"(last.{field}.{s} == null || last.{field}.{s}.by != actor.person)" for s in named
        )
    elif ex.kind == "people":
        check = " && ".join(_person_check(p, people) for p in ex.people)
    else:  # pragma: no cover - the schema allows only EXCLUSIONS
        raise ValueError(f"unknown exclusion {ex.kind!r}; one of {EXCLUSIONS}")
    if ex.when is not None:
        return f"(!({ex.when}) || ({check}))"
    return check


def _person_check(path: str, people: PeopleKind | None) -> str:
    many = people(path) if people is not None else False
    head, _, tail = path.partition(".")
    nulls = [f"record.{head} == null"]
    if tail:
        nulls.append(f"record.{path} == null")
    test = f"!(actor.person in record.{path})" if many else f"record.{path} != actor.person"
    return "(" + " || ".join([*nulls, test]) + ")"


def exclusion_words(
    exclusions: Sequence[Exclusion], *, open: str | None, labels: Mapping[str, str]
) -> str:
    """Whom a signer must not be, as a sentence: what the flow diagram and the refusal say."""
    who: list[str] = []
    for ex in exclusions:
        if ex.kind == "creator":
            words = "whoever created the record"
        elif ex.kind == "editors":
            window = ex.window or ((open,) if open else ())
            since = _or([labels.get(s, s) for s in window])
            changed = (
                f" its {_and([f.replace('_', ' ') for f in ex.fields])}" if ex.fields else " it"
            )
            words = f"whoever changed{changed} since it was {since}" if since else "its editors"
        elif ex.kind == "signers":
            named = [labels[s] for s in ex.states if s in labels]
            if not named:
                continue
            words = f"whoever signed it {_and(named)}"
        else:
            words = _and(
                [f"the person its {p.replace('.', ' ').replace('_', ' ')} names" for p in ex.people]
            )
        if ex.when is not None:
            words += " (where it applies)"
        if words not in who:
            who.append(words)
    return f"It must be signed by someone other than {_and(who)}."


def _and(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _or(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


# --- Who setup asks for ----------------------------------------------------------------------


@dataclass(frozen=True)
class Staffing:
    """Who a flow needs (ADR 0303): for each state a record must pass to complete it, the
    slots one of whose holders sets it; and the pairs of those states whose signers must be
    different people, read from ``independent_of``."""

    #: Each state after the first on the route, with the slots that may set it.
    steps: dict[str, tuple[str, ...]]
    #: Pairs of steps whose people must differ.
    apart: frozenset[frozenset[str]]

    @property
    def slots(self) -> tuple[str, ...]:
        out: list[str] = []
        for group in self.steps.values():
            for slot in group:
                if slot not in out:
                    out.append(slot)
        return tuple(out)

    @property
    def distinct_slots(self) -> tuple[str, ...]:
        """The slots of the steps that must be different people, in order."""
        out: list[str] = []
        for pair in self.apart:
            for step in sorted(pair, key=list(self.steps).index):
                for slot in self.steps[step]:
                    if slot not in out:
                        out.append(slot)
        return tuple(sorted(out, key=list(self.slots).index))


def staffing(
    procedure: ReviewProcedure, flow_name: str, review: ReviewSpec | None = None
) -> Staffing:
    flow = procedure.flows[flow_name]
    route = _route(flow, review)
    steps = {s: procedure.states[s].by for s in route[1:]}
    apart: set[frozenset[str]] = set()
    if review is None or not review.derived:
        for s in steps:
            for ex in procedure.states[s].independent_of:
                if ex.kind == "signers":
                    for t in ex.states:
                        if t in steps and t != s:
                            apart.add(frozenset((s, t)))
    return Staffing(steps=steps, apart=frozenset(apart))


def can_staff(plan: Staffing, people: Mapping[str, Collection[object]]) -> bool:
    """Whether the people chosen for each slot can sign every step, with each pair of steps
    that must be apart signed by different people. Brute force: a flow has a handful of
    steps."""
    order = list(plan.steps)
    chosen: dict[str, object] = {}

    def candidates(step: str) -> list[object]:
        seen: list[object] = []
        for slot in plan.steps[step]:
            for person in people.get(slot, ()):
                if person not in seen:
                    seen.append(person)
        return seen

    def place(index: int) -> bool:
        if index == len(order):
            return True
        step = order[index]
        for person in candidates(step):
            if any(
                chosen.get(other) == person
                for other in order[:index]
                if frozenset((step, other)) in plan.apart
            ):
                continue
            chosen[step] = person
            if place(index + 1):
                return True
            del chosen[step]
        return False

    return place(0)
