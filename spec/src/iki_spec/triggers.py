"""Triggers: reading their manifests, and the rules beyond them (ADR 0080).

A plugin's triggers live under `triggers/`. The schema checks each document's shape; the
rules here check it against the plugin's types and actions: that the type exists and the
fields it watches are the type's, that the action it runs takes an entity of that type and
needs nothing else, and that its params fit. A scheduled trigger's schedule must read,
come round, and run at most every five minutes, and its action must need no inputs.

A trigger may give other inputs the entities related to the one that fired it (ADR 0123):
``related: {aliquots: {direction: downstream}}`` gives ``aliquots`` every Aliquot in the
project derived from it. Each such input takes many, and isn't the one the entity goes to.
An input ``from_project`` (ADR 0232) is filled by the run itself, so a trigger never gives
it the entity, nor needs to give it anything.

``request.received`` fires in a service's project as a request of its type is sent there
(ADR 0232): its type must be a request type.

A calculation, a trigger ``on: entity.updated`` whose action updates the entity it's given,
says what it reads through the references its input includes (ADR 0080, amended
2026-11-24), with ``watch_included``: a change to one of those fields flags each record it
was worked out for as needing it again. So each included reference whose entities have a
value that can change is watched, naming the fields of them it reads; a fixed one (required
and immutable, or a lab list's immutable fact) never changes in place, and naming it only
says the calculation reads it. The reference itself is watched too, among `fields`, fixed
or not: pointed at another entity, the record is worked out from that one, and a move to a
superseded entry's or option's replacement re-points even a fixed one (ADR 0115, ADR 0270).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from iki_spec import cron, schemas
from iki_spec import validate as _validate
from iki_spec.model import Action, EntityType, Field, Related, Trigger, included_reference, qualify
from iki_spec.problems import Path
from iki_spec.workflows import literal

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = ["check", "read_trigger"]

#: From when a schedule is checked to come round.
_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def read_trigger(plugin: str, body: dict[str, Any], document: Document) -> Trigger:
    name = body["name"]
    return Trigger(
        plugin=plugin,
        name=name,
        annotations=dict(body.get("annotations") or {}),
        label=body.get("label") or name.replace("_", " ").capitalize(),
        description=body.get("description"),
        on=body["on"],
        type=qualify(body["type"], plugin) if body.get("type") else "",
        schedule=body.get("schedule") or "",
        timezone=body.get("timezone") or "",
        fields=tuple(body.get("fields") or ()),
        state_field=body.get("field") or "",
        states=tuple(body.get("states") or ()),
        run=body["run"],
        # Filled in by `check` when it's left out and one input takes the type.
        input=body.get("input") or "",
        params=dict(body.get("params") or {}),
        file=document.file,
        recheck=body.get("recheck", ""),
        watch_included={k: tuple(v) for k, v in (body.get("watch_included") or {}).items()},
        related={
            name: Related(
                direction=r["direction"],
                depth=int(r.get("depth", 10)),
                edges=tuple(r.get("edges") or ()),
            )
            for name, r in (body.get("related") or {}).items()
        },
    )


def taking(trigger: Trigger, action: Action, is_a: Any) -> list[str]:  # noqa: ANN401 - a predicate
    """The action's inputs that can take an entity of the trigger's type, besides those it
    gives related entities."""
    return [
        n
        for n, put in action.inputs.items()
        if n not in trigger.related and not put.from_project and is_a(trigger.type, put.type)
    ]


def check(semantics: _validate._Semantics) -> dict[str, Trigger]:
    """Check the plugin's triggers against its types and actions. Returns them with the
    input each gives its entity to filled in, where it was left out and one input fits."""
    return _Rules(semantics).run()


class _Rules:
    def __init__(self, semantics: _validate._Semantics) -> None:
        self.s = semantics
        self.add = semantics.add
        self.plugin = semantics.plugin
        self.loaded = semantics.loaded

    def run(self) -> dict[str, Trigger]:
        out: dict[str, Trigger] = {}
        for name, trigger in self.plugin.triggers.items():
            out[name] = self.trigger(trigger, self.loaded.trigger_sources[name])
        return out

    def is_a(self, child: str, parent: str) -> bool:
        if child == parent:
            return True
        found = self.s.types.get(child)
        return found is not None and any(
            each.qualified_name == parent for each in self.s.ancestors(found)
        )

    def trigger(self, trigger: Trigger, source: Document) -> Trigger:
        scheduled = trigger.on == "schedule"
        if scheduled:
            self.scheduled(trigger, source)
        elif not trigger.type:
            self.add(
                "trigger-type-missing",
                f"A trigger `on: {trigger.on}` says which entities it fires for, with `type`.",
                source,
                (),
                hint="Add `type:`, such as `type: Protein`.",
            )
        else:
            if trigger.schedule or trigger.timezone:
                self.add(
                    "trigger-schedule-not-scheduled",
                    "`schedule` and `timezone` are for `on: schedule`.",
                    source,
                    ("schedule",) if trigger.schedule else ("timezone",),
                    key=True,
                )
            target = self.s.reference(trigger.type, source, ("type",))
            if target is not None:
                self.watched(trigger, source)
                self.entered(trigger, source)
            if (
                target is not None
                and trigger.on == "request.received"
                and self.s.request_of(target) is None
            ):
                self.add(
                    "trigger-received-not-a-request",
                    f"`{trigger.type}` isn't a request type, so its records are never sent to "
                    "a service.",
                    source,
                    ("type",),
                    hint="Name a request type, such as one that extends `iki/requests.Request`.",
                )
            if (
                target is not None
                and trigger.on == "entity.registered"
                and not any(t.identity for t in [target, *self.s.ancestors(target)])
            ):
                self.add(
                    "trigger-registered-without-identity",
                    f"`{trigger.type}` has no identity, so its entities are never registered.",
                    source,
                    ("on",),
                    hint="Use `on: entity.created`, which fires for each new entity.",
                )
        action = self.plugin.actions.get(trigger.run)
        if action is None:
            self.add(
                "trigger-unknown-action",
                f"This plugin has no action `{trigger.run}`.",
                source,
                ("run",),
                hint=schemas._did_you_mean(trigger.run, list(self.plugin.actions)),
            )
            return trigger
        if scheduled:
            for name, put in action.inputs.items():
                if not put.optional and not put.from_project:
                    self.add(
                        "trigger-input-missing",
                        f"`{trigger.run}` needs `{name}`, and a scheduled trigger gives it no "
                        "entities.",
                        source,
                        ("run",),
                        hint="Run an action that needs no inputs, such as an import.",
                    )
            given = ""
        else:
            given = self.given(trigger, action, source)
        self.related(trigger, action, given, source)
        if trigger.watch_included and (
            not given or set(trigger.watch_included) - set(action.inputs[given].include)
        ):
            self.add(
                "trigger-reference",
                "Watch only references included by the action's input.",
                source,
                ("watch_included",),
            )
        elif given:
            self.reads(trigger, action, given, source)
        if trigger.recheck and trigger.watch_included:
            self.add(
                "trigger-recheck-watch-included",
                "A recheck runs with the credential of whoever edited the record, so only an "
                "edit of the record itself can start it: a change to what its references name "
                "has no editor to run it as.",
                source,
                ("watch_included",),
                key=True,
                hint="Drop `watch_included`, or keep the calculation in a trigger without "
                "`recheck`.",
            )
        if trigger.recheck:
            outputs = {n for update in action.updates for n in update.fields}
            target = self.s.types.get(trigger.type)
            fields = self.s.all_fields(target) if target else {}
            if (
                trigger.on != "entity.updated"
                or not trigger.fields
                or not given
                or action.tier != "backend"
                or not action.deterministic
                or action.creates
                or action.apis
                or action.network
                or action.secrets
                or trigger.related
                or any(update.input != given for update in action.updates)
                or trigger.recheck not in outputs
                or any(
                    n not in fields
                    or not fields[n][0].set_by_runs
                    or fields[n][0].required
                    or fields[n][0].immutable
                    or fields[n][0].registry
                    for n in outputs
                )
                or trigger.recheck not in fields
                or fields[trigger.recheck][0].type != "text"
            ):
                self.add(
                    "trigger-recheck",
                    "A recheck watches an edit and runs a deterministic "
                    "backend calculation updating only optional, computed fields of the edited "
                    "record, including its text status.",
                    source,
                    ("recheck",),
                )
        if given:
            for name, put in action.inputs.items():
                if (
                    name != given
                    and name not in trigger.related
                    and not put.optional
                    and not put.from_project
                ):
                    self.add(
                        "trigger-input-missing",
                        f"`{trigger.run}` also needs `{name}`, and a trigger gives it only the "
                        "entity that changed.",
                        source,
                        ("run",),
                        hint="Run an action that needs only that entity, or make the rest "
                        "optional.",
                    )
        for name, value in trigger.params.items():
            self.param(action, name, value, source, ("params", name))
        for name, param in action.params.items():
            if name not in trigger.params and param.required:
                self.add(
                    "trigger-param-missing",
                    f"`{trigger.run}` needs the param `{name}`, and this trigger doesn't give it.",
                    source,
                    ("params",) if trigger.params else (),
                    hint=f"Add `{name}:` under `params`, with a value.",
                )
        if given and given != trigger.input:
            return Trigger(**{**trigger.__dict__, "input": given})
        return trigger

    def reads(self, trigger: Trigger, action: Action, given: str, source: Document) -> None:
        """What the action reads through the references its input includes: that each field
        watched of them is one their entities have; and, for a calculation, that each
        reference whose entities can change, and each reference field that can, is
        watched (see the module's docstring)."""
        target = self.s.types.get(trigger.type)
        if target is None:
            return
        own = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        for name, watched in trigger.watch_included.items():
            ref = included_reference(own, name)
            if ref is None:
                continue
            theirs = self.fields_of(self.targets(ref))
            for index, field in enumerate(watched):
                f = theirs.get(field)
                path = ("watch_included", name, index)
                if f is None or f.removed:
                    self.add(
                        "unknown-field",
                        f"What `{name}` names has no field `{field}`"
                        + (" any more (it was removed)." if f is not None else "."),
                        source,
                        path,
                        hint=schemas._did_you_mean(
                            field, [n for n, x in theirs.items() if not x.removed]
                        ),
                    )
                elif f.type == "state":
                    self.add(
                        "trigger-field-is-state",
                        f"`{field}` is a state field: setting it isn't a change of the record's "
                        "values, so it never wakes what reads it.",
                        source,
                        path,
                    )
        calculation = (
            trigger.on == "entity.updated"
            and not trigger.recheck
            and any(update.input == given for update in action.updates)
        )
        if not calculation:
            return
        for name in action.inputs[given].include:
            ref = included_reference(own, name)
            if ref is None:
                continue
            if name not in trigger.watch_included:
                changing = sorted(
                    {
                        n
                        for t in self.targets(ref)
                        for n, f in self.fields_of([t]).items()
                        if _changes(f, t)
                    }
                )
                if changing:
                    shown = ", ".join(f"`{n}`" for n in changing[:4])
                    self.add(
                        "trigger-reads-unwatched",
                        f"`{trigger.run}` reads what `{name}` names, whose values can change "
                        f"({shown}{', …' if len(changing) > 4 else ''}), and this trigger "
                        f"doesn't watch it: a record worked out from one would stay as it was "
                        "when it changes, with no notice that it needs working out again.",
                        source,
                        ("watch_included",) if trigger.watch_included else ("run",),
                        hint=f"Add `watch_included: {{ {name}: [...] }}`, naming the fields of "
                        "it the calculation reads; a fixed one never changes, and naming it "
                        "says the calculation reads it.",
                    )
            field_name = name.partition(".")[0]
            f = own.get(field_name)
            if trigger.fields and field_name not in trigger.fields and f and not f.removed:
                self.add(
                    "trigger-reference-unwatched",
                    f"`{field_name}` can be pointed at another record, if only by a move to a "
                    "superseded one's replacement, and this trigger doesn't watch it: the "
                    "record would stay worked out from the one before.",
                    source,
                    ("fields",),
                    hint=f"Add `{field_name}` to `fields`.",
                )

    def targets(self, ref: Field) -> list[EntityType]:
        """The types a reference, or a list of them, can name."""
        names = ref.items.to if ref.type == "list" and ref.items is not None else ref.to
        return [t for n in names if (t := self.s.types.get(n)) is not None]

    def fields_of(self, targets: list[EntityType]) -> dict[str, Field]:
        """Every field an entity of ``targets`` can have: theirs, their parents', their
        subtypes', and what this plugin's extensions add to any of them."""
        kinds = {t.qualified_name for t in targets}
        every = [
            t for t in self.s.types.values() if any(self.is_a(t.qualified_name, k) for k in kinds)
        ]
        chain = {t.qualified_name for t in every} | {
            a.qualified_name for t in targets for a in self.s.ancestors(t)
        }
        out: dict[str, Field] = {}
        for t in every:
            for n, (f, _) in self.s.all_fields(t).items():
                out.setdefault(n, f)
        for extension in self.plugin.extensions:
            if extension.extends_type in chain:
                for n, f in extension.fields.items():
                    out.setdefault(n, f)
        return out

    def related(self, trigger: Trigger, action: Action, given: str, source: Document) -> None:
        """That each input given related entities is one, other than the entity's, that
        takes many."""
        for name in trigger.related:
            path = ("related", name)
            put = action.inputs.get(name)
            if put is None:
                self.add(
                    "trigger-unknown-input",
                    f"`{trigger.run}` has no input `{name}`.",
                    source,
                    path,
                    key=True,
                    hint=schemas._did_you_mean(name, list(action.inputs)),
                )
            elif put.from_project:
                self.add(
                    "trigger-input-from-project",
                    f"`{name}` is given every record of its type the project owns, so it "
                    "can't take related ones.",
                    source,
                    path,
                    key=True,
                    hint="Give related entities to another input.",
                )
            elif name == given:
                self.add(
                    "trigger-related-is-input",
                    f"`{name}` takes the entity that fired the trigger, so it can't take "
                    "related ones too.",
                    source,
                    path,
                    key=True,
                    hint="Give related entities to another input.",
                )
            elif put.cardinality != "many":
                self.add(
                    "trigger-related-one",
                    f"`{trigger.run}`'s `{name}` takes one entity, and related ones can be any "
                    "number.",
                    source,
                    path,
                    key=True,
                    hint=f"Make `{name}` `cardinality: many`.",
                )

    def scheduled(self, trigger: Trigger, source: Document) -> None:
        for name, attribute in (
            ("type", "type"),
            ("fields", "fields"),
            ("field", "state_field"),
            ("states", "states"),
            ("input", "input"),
            ("related", "related"),
        ):
            if getattr(trigger, attribute):
                self.add(
                    "trigger-schedule-entity",
                    f"A scheduled trigger fires for no entity, so it has no `{name}`.",
                    source,
                    (name,),
                    key=True,
                )
        if not trigger.schedule:
            self.add(
                "trigger-schedule-missing",
                "A trigger `on: schedule` says when it runs, with `schedule`.",
                source,
                (),
                hint="Add `schedule:` in cron's five fields, such as `0 6 * * 1-5`.",
            )
            return
        tz = None
        if trigger.timezone:
            try:
                tz = cron.zone(trigger.timezone)
            except cron.ScheduleError as error:
                self.add("trigger-timezone-unknown", str(error), source, ("timezone",))
                return
        try:
            schedule = cron.parse(trigger.schedule)
        except cron.ScheduleError as error:
            self.add("trigger-schedule-invalid", str(error), source, ("schedule",))
            return
        if schedule.next_after(_EPOCH, tz or cron.zone("UTC")) is None:
            self.add(
                "trigger-schedule-never",
                f"`{trigger.schedule}` names no day that comes round, so it never runs.",
                source,
                ("schedule",),
            )

    def watched(self, trigger: Trigger, source: Document) -> None:
        if not trigger.fields:
            return
        if trigger.on != "entity.updated":
            self.add(
                "trigger-fields-not-updated",
                "Only a change has fields that changed: `fields` is for `on: entity.updated`.",
                source,
                ("fields",),
                key=True,
            )
            return
        target = self.s.types[trigger.type]
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        chain = {trigger.type, *(t.qualified_name for t in self.s.ancestors(target))}
        for extension in self.plugin.extensions:
            if extension.extends_type in chain:
                fields.update(extension.fields)
        for index, name in enumerate(trigger.fields):
            f = fields.get(name)
            if f is None or f.removed:
                self.add(
                    "unknown-field",
                    f"`{trigger.type}` has no field `{name}`"
                    + (" any more (it was removed)." if f is not None else "."),
                    source,
                    ("fields", index),
                    hint=schemas._did_you_mean(
                        name, [n for n, x in fields.items() if not x.removed]
                    ),
                )
            elif f.type == "state":
                self.add(
                    "trigger-field-is-state",
                    f"`{name}` is a state field: setting it isn't a change of the record's "
                    "values, so it never fires `entity.updated`.",
                    source,
                    ("fields", index),
                    hint=(
                        f"Use another trigger with `on: entity.state_changed` and `field: {name}`."
                    ),
                )

    def entered(self, trigger: Trigger, source: Document) -> None:
        """A trigger on a state field being set (ADR 0125): its type has the field, and the
        states it names are the field's."""
        if trigger.on != "entity.state_changed":
            for key, value in (("field", trigger.state_field), ("states", trigger.states)):
                if value:
                    self.add(
                        "trigger-state-not-state-changed",
                        f"`{key}` is for `on: entity.state_changed`: only setting a state field "
                        "enters a state.",
                        source,
                        (key,),
                        key=True,
                    )
            return
        target = self.s.types[trigger.type]
        fields = {n: f for n, (f, _) in self.s.all_fields(target).items()}
        stateful = [n for n, f in fields.items() if f.type == "state" and not f.removed]
        if not trigger.state_field:
            self.add(
                "trigger-state-field-missing",
                "A trigger `on: entity.state_changed` names the state field it watches, with "
                "`field`.",
                source,
                (),
                hint=f"Add `field: {stateful[0]}`."
                if stateful
                else f"`{trigger.type}` has no state field: give it one, or use another `on:`.",
            )
            return
        f = fields.get(trigger.state_field)
        if f is None or f.removed or f.type != "state":
            self.add(
                "trigger-not-a-state-field",
                f"`{trigger.type}` has no state field `{trigger.state_field}`"
                + (
                    " any more (it was removed)."
                    if f is not None and f.removed
                    else (f": it's a {f.type} field." if f is not None else ".")
                ),
                source,
                ("field",),
                hint=schemas._did_you_mean(trigger.state_field, stateful),
            )
            return
        live = f.live_states
        for index, name in enumerate(trigger.states):
            if name not in live:
                self.add(
                    "unknown-state",
                    f"`{name}` isn't one of `{f.name}`'s states, or it's removed.",
                    source,
                    ("states", index),
                    hint=schemas._did_you_mean(name, list(live)),
                )

    def given(self, trigger: Trigger, action: Action, source: Document) -> str:
        """The input the entity goes to, once checked; empty if none can take it."""
        if trigger.input:
            put = action.inputs.get(trigger.input)
            if put is None:
                self.add(
                    "trigger-unknown-input",
                    f"`{trigger.run}` has no input `{trigger.input}`.",
                    source,
                    ("input",),
                    hint=schemas._did_you_mean(trigger.input, list(action.inputs)),
                )
                return ""
            if put.from_project:
                self.add(
                    "trigger-input-from-project",
                    f"`{trigger.input}` is given every record of its type the project owns, so "
                    "it can't take the entity that fired the trigger.",
                    source,
                    ("input",),
                    hint="Give the entity to another input.",
                )
                return ""
            if not self.is_a(trigger.type, put.type):
                self.add(
                    "trigger-wrong-type",
                    f"The trigger fires for `{trigger.type}`, and `{trigger.run}`'s "
                    f"`{trigger.input}` takes `{put.type}`.",
                    source,
                    ("input",),
                )
                return ""
            return trigger.input
        fits = taking(trigger, action, self.is_a)
        if len(fits) == 1:
            return fits[0]
        if not fits:
            self.add(
                "trigger-wrong-type",
                f"None of `{trigger.run}`'s inputs takes `{trigger.type}`, which the trigger "
                "fires for.",
                source,
                ("run",),
            )
        else:
            self.add(
                "trigger-input-ambiguous",
                f"More than one of `{trigger.run}`'s inputs takes `{trigger.type}`: "
                + ", ".join(f"`{n}`" for n in fits)
                + ".",
                source,
                ("run",),
                hint="Say which with `input:`.",
            )
        return ""

    def param(
        self,
        action: Action,
        name: str,
        value: Any,  # noqa: ANN401 - manifest data is JSON-shaped
        source: Document,
        path: Path,
    ) -> None:
        wanted = action.params.get(name)
        if wanted is None:
            self.add(
                "trigger-unknown-param",
                f"`{action.name}` has no param `{name}`.",
                source,
                path,
                key=True,
                hint=schemas._did_you_mean(name, list(action.params)),
            )
            return
        if isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
            self.add(
                "trigger-invalid-param",
                "A trigger's params are values. A value starting with `$` is written `$$`.",
                source,
                path,
            )
            return
        for message in _validate.value_problems(wanted, literal(value)):
            self.add("trigger-invalid-param", f"The value {message}", source, path)


def _changes(f: Field, owner: EntityType) -> bool:
    """Whether ``f`` of an entity of ``owner`` can change once the entity exists, as an
    edit, a run or a curator changes it. A required, immutable value is fixed; so is a lab
    list option's immutable one, set as the option is added (ADR 0270), as no one changes
    it after. An optional immutable value elsewhere can still be filled once. A state isn't
    one of its values, and setting it is no edit."""
    if f.removed or f.type == "state":
        return False
    return not (f.immutable and (f.required or owner.is_lab_list))
