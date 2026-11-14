"""Triggers: reading their manifests, and the rules beyond them (ADR 0080).

A plugin's triggers live under `triggers/`. The schema checks each document's shape; the
rules here check it against the plugin's types and actions: that the type exists and the
fields it watches are the type's, that the action it runs takes an entity of that type and
needs nothing else, that its params fit, and that it doesn't run on the client tier. A
scheduled trigger's schedule must read, come round, and run at most every five minutes,
and its action must need no inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from iki_spec import cron, schemas
from iki_spec import validate as _validate
from iki_spec.model import Action, Trigger, qualify
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
        label=body.get("label") or name.replace("_", " ").capitalize(),
        description=body.get("description"),
        on=body["on"],
        type=qualify(body["type"], plugin) if body.get("type") else "",
        schedule=body.get("schedule") or "",
        timezone=body.get("timezone") or "",
        fields=tuple(body.get("fields") or ()),
        run=body["run"],
        # Filled in by `check` when it's left out and one input takes the type.
        input=body.get("input") or "",
        params=dict(body.get("params") or {}),
        file=document.file,
    )


def taking(trigger: Trigger, action: Action, is_a: Any) -> list[str]:  # noqa: ANN401 - a predicate
    """The action's inputs that can take an entity of the trigger's type."""
    return [n for n, put in action.inputs.items() if is_a(trigger.type, put.type)]


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
        if action.tier == "client":
            self.add(
                "trigger-client-tier",
                f"`{trigger.run}` runs on the client tier: on a person's own computer, where "
                "nobody may be when the trigger fires.",
                source,
                ("run",),
                hint="Run it on the backend, container or VM tier.",
            )
        if scheduled:
            for name, put in action.inputs.items():
                if not put.optional:
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
        if given:
            for name, put in action.inputs.items():
                if name != given and not put.optional:
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

    def scheduled(self, trigger: Trigger, source: Document) -> None:
        for name in ("type", "fields", "input"):
            if getattr(trigger, name):
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
