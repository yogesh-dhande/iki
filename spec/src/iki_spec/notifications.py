"""Notifications a type declares (ADR 0129): the rules beyond their schema.

A type's ``notifications`` say, each by name, when its records tell someone and whom:

- **when**: a record is made (``created: true``), or a field changes: one of the record's
  own, or ``ref.field``, a field of the record one of its references names (a booking's
  instrument's ``service``). A state field, an enum or a yes-or-no field can say ``to``
  which values, or ``from`` which, so a state field's notification is sent as it enters
  states or leaves them. A request (ADR 0132) can also tell its service it's been sent
  (``sent: true``), by the lab roles held there, and the project asking that one of its
  results was released (``released: true``) (ADR 0142);
- **where**: only for records these conditions find, as the search API takes them, on the
  type's own fields, compared with values or ``{{ now }}`` (dates and times);
- **tell**: the people its fields name, holders of lab roles in its project, whoever made
  it, whoever watches it, and whoever made the change, who isn't told otherwise;
- **message**: its ``title`` and ``body`` in Iki, written as a display title is, over the
  record's fields. Emails never carry them, only what it is and a link (ADR 0129).

Each field, state, value and lab role named must be one the type has, with a hint when
it's close to one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from iki_spec import conditions, schemas
from iki_spec.model import EntityType, Field, TypeNotification, names_people
from iki_spec.problems import Path, Severity

if TYPE_CHECKING:
    from iki_spec.validate import _Semantics
    from iki_spec.yamlsource import Document

__all__ = ["MESSAGE_TYPES", "NOW", "VALUED_TYPES", "check", "now_of"]

#: The kinds of field whose change can say ``to`` and ``from``: a few values, each named.
VALUED_TYPES = frozenset({"state", "enum", "bool"})
#: The kinds of field a message can show: a display title's, and a state's label, a
#: person's name, and the HRID of the record a reference names.
MESSAGE_TYPES = frozenset(
    {
        "string",
        "text",
        "int",
        "float",
        "enum",
        "date",
        "datetime",
        "quantity",
        "bool",
        "state",
        "person",
        "ref",
    }
)
#: The kinds of field a notification's conditions compare with values. Any field can be
#: ``is_set``.
_COMPARED = frozenset(
    {"string", "text", "int", "float", "quantity", "enum", "state", "date", "datetime", "bool"}
)
#: The one context a notification's condition can name: when it's sent.
NOW = "{{ now }}"


def now_of(value: Any) -> bool:  # noqa: ANN401 - JSON
    """Whether a condition's value is ``{{ now }}``, spaced any way."""
    return isinstance(value, str) and value.replace(" ", "") == NOW.replace(" ", "")


def check(s: _Semantics, entity_type: EntityType, source: Document) -> None:
    """Checks a type's notifications against its fields, its references' types, and the
    lab roles the plugin can name."""
    if not entity_type.notifications:
        return
    every = {n: f for n, (f, _) in s.all_fields(entity_type).items() if not f.removed}
    owners = {n: owner for n, (_, owner) in s.all_fields(entity_type).items()}
    inherited = {
        name: ancestor for ancestor in s.ancestors(entity_type) for name in ancestor.notifications
    }
    for name, notification in entity_type.notifications.items():
        at: Path = ("notifications", name)
        if name in inherited:
            s.add(
                "notification-shadows-parent",
                f"`{inherited[name].qualified_name}`, which this type extends, has a "
                f"notification `{name}` already, which its records send too.",
                source,
                at,
                key=True,
                hint="Name it differently.",
            )
        _Notification(s, entity_type, notification, source, at, every, owners).run()


class _Notification:
    def __init__(
        self,
        s: _Semantics,
        entity_type: EntityType,
        n: TypeNotification,
        source: Document,
        at: Path,
        every: dict[str, Field],
        owners: Mapping[str, EntityType],
    ) -> None:
        self.s = s
        self.entity_type = entity_type
        self.n = n
        self.source = source
        self.at = at
        self.every = every
        self.owners = owners

    def add(
        self,
        code: str,
        message: str,
        path: Path,
        *,
        hint: str | None = None,
        severity: Severity = Severity.ERROR,
    ) -> None:
        self.s.add(code, message, self.source, (*self.at, *path), hint=hint, severity=severity)

    def run(self) -> None:
        changed = self.when()
        self.tell()
        if self.n.where is not None:
            self.where(self.n.where)
        for part in ("title", "body"):
            template = getattr(self.n, part)
            if template is not None:
                self.message(part, template)
        if changed is not None:
            self.repeats_ready(changed)

    # When -----------------------------------------------------------------------------------

    def own_field(self, name: str, path: Path) -> Field | None:
        """A field of the record's own, which changes with its revisions."""
        f = self.every.get(name)
        if f is None:
            self.add(
                "unknown-field",
                f"`{name}` isn't a field of `{self.entity_type.name}`.",
                path,
                hint=schemas._did_you_mean(name, sorted(self.every)),
            )
            return None
        owner = self.owners[name]
        if f.registry is not None or name in owner.identity:
            self.add(
                "notification-in-registry",
                f"`{name}` is its registry entry's, every project's: it changes in the "
                "registry, not with a project's record, so a record can't tell of it.",
                path,
            )
            return None
        return f

    def when(self) -> Field | None:
        """The field whose change sends it, checked, or None."""
        n = self.n
        path: Path = ("when",)
        events = [k for k in ("created", "sent", "released") if getattr(n, k)]
        if n.field or n.to_values or n.from_values:
            events.append("field")
        if len(events) > 1:
            self.add(
                "notification-when",
                "It's sent on one thing: a record made, a field changing, or a request sent or "
                f"its result released, not {' and '.join(f'`{e}`' for e in events)}.",
                path,
                hint="Keep one, and declare another notification for the other.",
            )
            return None
        if n.sent or n.released:
            self.request_event(path)
            return None
        if n.created:
            return None
        if not n.field:
            self.add(
                "notification-when",
                "Say when it's sent: `created: true`, or the `field` whose change sends it.",
                path,
            )
            return None
        if n.ref is None:
            changed = self.own_field(n.field, (*path, "field"))
            if changed is None:
                return None
        else:
            changed = self.referenced(n.ref, n.target, (*path, "field"))
            if changed is None:
                return None
        for key, values in (("to", n.to_values), ("from", n.from_values)):
            if values:
                self.values(changed, values, (*path, key))
        return changed

    def request_event(self, path: Path) -> None:
        """``sent`` and ``released`` are a request's (ADR 0132, 0142): sent, it tells the
        service, by the lab roles its people hold there, and nobody of the project asking."""
        n = self.n
        key = "sent" if n.sent else "released"
        if not any(
            t.request is not None for t in [self.entity_type, *self.s.ancestors(self.entity_type)]
        ):
            self.add(
                "notification-when",
                f"`{key}` is a request's, and `{self.entity_type.name}` isn't a request type.",
                (*path, key),
                hint="Declare `request:` on the type, or say another `when`.",
            )
            return
        if not n.sent:
            return
        others = [
            k
            for k, given in (
                ("fields", n.tell_fields),
                ("creator", n.tell_creator),
                ("watchers", n.tell_watchers),
                ("actor", n.tell_actor),
            )
            if given
        ]
        if others or not n.tell_roles:
            self.add(
                "notification-recipients",
                "A request sent tells its service, by the lab roles its people hold there: "
                "name them in `roles`"
                + (f", not {', '.join(f'`{k}`' for k in others)}." if others else "."),
                ("tell",),
                hint="The project asking knows it sent it: its people hear of it when its "
                "results are released (`released: true`).",
            )

    def referenced(self, ref: str, name: str, path: Path) -> Field | None:
        """``ref.name``: a field of the record a reference of the record names."""
        f = self.own_field(ref, path)
        if f is None:
            return None
        if f.type != "ref":
            self.add(
                "notification-field",
                f"`{ref}` is a {f.type} field: `{ref}.{name}` needs `{ref}` to be a reference "
                "to one record.",
                path,
            )
            return None
        if len(f.to) != 1:
            # A reference to one of several types (ADR 0136) has no one set of fields.
            self.add(
                "notification-field",
                f"`{ref}` refers to one of several types ({', '.join(f.to)}): `{ref}.{name}` "
                f"needs `{ref}` to be a reference to one record of one type.",
                path,
            )
            return None
        target = self.s.types.get(f.to[0])
        if target is None:
            return None  # its `to` is reported where it's declared
        fields = {n: (g, o) for n, (g, o) in self.s.all_fields(target).items() if not g.removed}
        found = fields.get(name)
        if found is None:
            self.add(
                "unknown-field",
                f"`{name}` isn't a field of `{target.qualified_name}`, which `{ref}` refers to.",
                path,
                hint=schemas._did_you_mean(name, sorted(fields)),
            )
            return None
        g, owner = found
        if g.registry is not None or name in owner.identity:
            self.add(
                "notification-in-registry",
                f"`{name}` is its registry entry's: it changes in the registry, not with a "
                "project's record, so a record can't tell of it.",
                path,
            )
            return None
        return g

    def values(self, f: Field, values: tuple[Any, ...], path: Path) -> None:
        if f.type not in VALUED_TYPES:
            self.add(
                "notification-values",
                f"`{f.name}` is a {f.type} field: only a state field, an enum or a yes-or-no "
                "field says which values it's sent for.",
                path,
                hint="Remove it: any change sends it.",
            )
            return
        allowed: list[Any] = (
            list(f.live_states)
            if f.type == "state"
            else list(f.values)
            if f.type == "enum"
            else [True, False]
        )
        for index, value in enumerate(values):
            if value in allowed and (f.type == "bool") == isinstance(value, bool):
                continue
            what = "states" if f.type == "state" else "values"
            self.add(
                "unknown-state" if f.type == "state" else "notification-values",
                f"`{value}` isn't one of `{f.name}`'s {what}"
                + (", or it's removed." if f.type == "state" else "."),
                (*path, index) if len(values) > 1 else path,
                hint=schemas._did_you_mean(str(value), [str(a) for a in allowed])
                or f"It's one of {', '.join(f'`{a}`' for a in allowed)}.",
            )

    # Tell -----------------------------------------------------------------------------------

    def tell(self) -> None:
        n = self.n
        path: Path = ("tell",)
        if not (n.tell_fields or n.tell_roles or n.tell_creator or n.tell_watchers or n.tell_actor):
            self.add(
                "notification-recipients",
                "It tells nobody: name `fields`, `roles`, `creator`, `watchers` or `actor`.",
                path,
            )
        for index, name in enumerate(n.tell_fields):
            at: Path = (*path, "fields", index)
            f = self.own_field(name, at)
            if f is not None and not names_people(f):
                people = sorted(k for k, g in self.every.items() if names_people(g))
                self.add(
                    "notification-recipients",
                    f"`{name}` is a {f.type} field: it names nobody to tell.",
                    at,
                    hint=schemas._did_you_mean(name, people)
                    or (
                        "Name a field of `type: person`, or a list of them."
                        if not people
                        else f"Fields naming people: {', '.join(f'`{p}`' for p in people)}."
                    ),
                )
        for index, role in enumerate(n.tell_roles):
            self.s.lab_role(role, self.source, (*self.at, *path, "roles", index))

    def repeats_ready(self, changed: Field) -> None:
        """A field the record assigns, told as its state field enters the states it counts
        in, is told "it's ready for you" already (ADR 0122)."""
        n = self.n
        if n.ref is not None or changed.type != "state" or not n.to_values:
            return
        for name in n.tell_fields:
            f = self.every.get(name)
            if (
                f is not None
                and f.assignment
                and f.assignment_field == n.field
                and set(n.to_values) <= set(f.assignment_states)
            ):
                self.add(
                    "notification-repeats-ready",
                    f"`{name}` is assigned the record in those states, so whoever it names is "
                    "told it's ready for them already.",
                    ("tell", "fields", n.tell_fields.index(name)),
                    severity=Severity.WARNING,
                    hint="Leave it out, or tell them something else.",
                )

    # Where ----------------------------------------------------------------------------------

    def where(self, where: Mapping[str, Any]) -> None:
        depth, count = _size(where)
        if depth > conditions.MAX_DEPTH or count > conditions.MAX_CONDITIONS:
            self.add(
                "search-too-big",
                f"A notification's conditions may be at most {conditions.MAX_CONDITIONS}, "
                f"{conditions.MAX_DEPTH} levels deep: these are {count}, {depth} deep.",
                ("where",),
            )
            return
        self.condition(where, ("where",))

    def condition(self, node: Mapping[str, Any], path: Path) -> None:
        for key in ("all", "any"):
            if key in node:
                for index, child in enumerate(node[key]):
                    self.condition(child, (*path, key, index))
                return
        if "not" in node:
            self.condition(node["not"], (*path, "not"))
            return
        name, op = node["field"], node["op"]
        f = self.own_field(name, (*path, "field"))
        if f is None:
            return
        if op == "is_set":
            return
        if f.type not in _COMPARED:
            self.add(
                "notification-where",
                f"`{name}` is a {f.type} field: a notification's conditions compare it only "
                "with `is_set`.",
                (*path, "op"),
            )
            return
        kind = "enum" if f.type == "state" else f.type
        allowed = ("is_set", *conditions.operators(kind, f.format, searchable=f.searchable))
        if op not in allowed:
            self.add(
                "unknown-operator",
                f"`{name}` takes {', '.join(f'`{o}`' for o in allowed)}, not `{op}`.",
                (*path, "op"),
            )
            return
        value = node["value"]
        for index, item in enumerate(value if op == "in" and isinstance(value, list) else [value]):
            at: Path = (*path, "value", index) if op == "in" else (*path, "value")
            problem = _value_problem(f, item)
            if problem is not None:
                self.add("notification-where", f"`{name}` {problem}", at)

    # Message --------------------------------------------------------------------------------

    def message(self, part: str, template: str) -> None:
        from iki_spec.validate import _title_problems

        for problem, hint in _title_problems(
            template, self.every, noun=f"The message's {part}", kinds=MESSAGE_TYPES, constant=True
        ):
            self.add("notification-message", problem, ("message", part), hint=hint)


def _size(node: Mapping[str, Any]) -> tuple[int, int]:
    for key in ("all", "any"):
        if key in node:
            sizes = [_size(child) for child in node[key]]
            return 1 + max((d for d, _ in sizes), default=0), sum(c for _, c in sizes)
    if "not" in node:
        depth, count = _size(node["not"])
        return depth + 1, count
    return 1, 1


def _value_problem(f: Field, value: Any) -> str | None:  # noqa: ANN401 - JSON
    """What's wrong with a value a condition compares ``f`` with, or None."""
    if isinstance(value, str) and "{{" in value:
        if now_of(value) and f.type in ("date", "datetime"):
            return None
        return (
            "is compared with `{{ now }}`, when it's sent, only if it's a date or a time."
            if now_of(value)
            else f"can't be compared with `{value}`: a value, or `{{{{ now }}}}` for a date "
            "or a time, is all a notification's condition takes."
        )
    if f.type == "state":
        if value not in f.live_states:
            return f"has no state `{value}`: it's one of {', '.join(f.live_states)}."
        return None
    if f.type == "enum":
        return None if value in f.values else f"has no value `{value}`."
    if f.type == "bool":
        return None if isinstance(value, bool) else "is true or false."
    if f.type in ("int", "float"):
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return None if ok else "is a number."
    if f.type == "date":
        return None if _parses(dt.date.fromisoformat, value) else "is a date, such as 2026-03-01."
    if f.type == "datetime":
        return (
            None
            if _parses(dt.datetime.fromisoformat, value)
            else "is a date and time, such as 2026-03-01T09:30:00Z."
        )
    if f.type in ("string", "text"):
        return None if isinstance(value, str) else "is text."
    return None


def _parses(parse: Any, value: Any) -> bool:  # noqa: ANN401 - a parser, and JSON
    try:
        parse(value)
    except (TypeError, ValueError):
        return False
    return True
