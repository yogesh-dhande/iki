"""Workflows: reading their manifests, and the rules beyond them (ADR 0079).

A plugin's workflows live under `workflows/`. The schema checks each document's
shape; the rules here check it against the plugin's actions: that each step runs
one, that every input a step takes comes from somewhere of the right type and
number, that its params fit, and that no step comes before itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from iki_spec import actions, schemas
from iki_spec import validate as _validate
from iki_spec.model import Action, Field, Input, Workflow, WorkflowStep, qualify
from iki_spec.problems import Path

if TYPE_CHECKING:
    from iki_spec.yamlsource import Document

__all__ = ["Source", "check", "literal", "read_workflow", "source_of"]


def read_workflow(plugin: str, body: dict[str, Any], document: Document) -> Workflow:
    name = body["name"]
    return Workflow(
        plugin=plugin,
        name=name,
        label=body.get("label") or name.replace("_", " ").capitalize(),
        description=body.get("description"),
        inputs={
            n: Input(
                name=n,
                type=qualify(spec["type"], plugin),
                schema=spec.get("schema"),
                cardinality=spec.get("cardinality", "one"),
                max=spec.get("max"),
                optional=bool(spec.get("optional", False)),
                label=spec.get("label") or n.replace("_", " ").capitalize(),
                description=spec.get("description"),
            )
            for n, spec in (body.get("inputs") or {}).items()
        },
        params={
            n: Field.from_manifest(n, spec, plugin=plugin)
            for n, spec in (body.get("params") or {}).items()
        },
        steps={
            i: WorkflowStep(
                id=i,
                action=step["action"],
                label=step.get("label") or i.replace("_", " ").capitalize(),
                inputs={
                    n: _qualified_ref(ref, plugin) for n, ref in (step.get("inputs") or {}).items()
                },
                params=dict(step.get("params") or {}),
                needs=tuple(step.get("needs") or ()),
                each=step.get("each"),
            )
            for i, step in body["steps"].items()
        },
        file=document.file,
    )


def _qualified_ref(ref: str, plugin: str) -> str:
    """`$steps.<id>.created.<Type>` with its type qualified by the plugin."""
    parts = ref.split(".", 3)
    if len(parts) == 4 and parts[0] == "$steps" and parts[2] == "created":
        return f"{parts[0]}.{parts[1]}.created.{qualify(parts[3], plugin)}"
    return ref


def literal(value: Any) -> Any:  # noqa: ANN401 - manifest data is JSON-shaped
    """A step's param value as written, with a leading `$$` read as `$`."""
    if isinstance(value, str) and value.startswith("$$"):
        return value[1:]
    return value


@dataclass(frozen=True)
class Source:
    """What a step's input takes: entities of `type`, one or many, and the step that
    makes or last handles them, if a step does."""

    type: str
    cardinality: str
    step: str | None = None


def source_of(workflow: Workflow, actions_: dict[str, Action], ref: str) -> Source | None:
    """What `ref` names in the workflow, or None if it names nothing."""
    parts = ref.split(".", 3)
    if parts[0] == "$inputs" and len(parts) == 2:
        given = workflow.inputs.get(parts[1])
        return None if given is None else Source(given.type, given.cardinality)
    if parts[0] != "$steps" or len(parts) != 4:
        return None
    step = workflow.steps.get(parts[1])
    action = actions_.get(step.action) if step is not None else None
    if step is None or action is None:
        return None
    if parts[2] == "created":
        made = next((c for c in action.creates if c.type == parts[3]), None)
        if made is None:
            return None
        # A step run once for each entity makes many, however many each run makes.
        return Source(made.type, "many" if step.each else made.cardinality, step.id)
    if parts[2] == "inputs" and parts[3] in step.inputs:
        # What it was given, once it has run: as it came, from wherever it came.
        upstream = source_of(workflow, actions_, step.inputs[parts[3]])
        if upstream is None:
            return None
        return Source(upstream.type, upstream.cardinality, step.id)
    return None


def check(semantics: _validate._Semantics) -> None:
    """Check the plugin's workflows against its actions."""
    _Rules(semantics).run()


class _Rules:
    def __init__(self, semantics: _validate._Semantics) -> None:
        self.s = semantics
        self.add = semantics.add
        self.plugin = semantics.plugin
        self.loaded = semantics.loaded
        self.actions = actions._Rules(semantics)

    def run(self) -> None:
        for name, workflow in self.plugin.workflows.items():
            self.workflow(workflow, self.loaded.workflow_sources[name])

    def is_a(self, child: str, parent: str) -> bool:
        if child == parent:
            return True
        found = self.s.types.get(child)
        return found is not None and any(
            each.qualified_name == parent for each in self.s.ancestors(found)
        )

    def workflow(self, workflow: Workflow, source: Document) -> None:
        for name, put in workflow.inputs.items():
            self.actions.input(put, source, ("inputs", name))
        for name, param in workflow.params.items():
            path: Path = ("params", name)
            if name in workflow.inputs:
                self.add(
                    "duplicate-name",
                    f"`{name}` is both an input and a param.",
                    source,
                    path,
                    key=True,
                    hint="People choose both by name, so give them different ones.",
                )
            self.actions.param(param, source, path)
        for step in workflow.steps.values():
            self.step(workflow, step, source)
        if workflow.order() is None:
            looped = sorted(i for i in workflow.steps if i not in _reachable_order(workflow))
            self.add(
                "workflow-cycle",
                "These steps each wait for another of them, so none could start: "
                + ", ".join(f"`{i}`" for i in looped)
                + ".",
                source,
                ("steps",),
                hint="A step can only take from, or need, steps that come before it.",
            )

    def step(self, workflow: Workflow, step: WorkflowStep, source: Document) -> None:
        path: Path = ("steps", step.id)
        action = self.plugin.actions.get(step.action)
        if action is None:
            self.add(
                "workflow-unknown-action",
                f"This plugin has no action `{step.action}`.",
                source,
                (*path, "action"),
                hint=schemas._did_you_mean(step.action, list(self.plugin.actions)),
            )
            return
        if step.each is not None:
            self.step_each(step, action, source, (*path, "each"))
        for name, ref in step.inputs.items():
            self.step_input(workflow, step, action, name, ref, source, (*path, "inputs", name))
        for name, put in action.inputs.items():
            if name not in step.inputs and not put.optional:
                self.add(
                    "workflow-input-missing",
                    f"`{step.action}` needs `{name}`, and this step doesn't say where it comes "
                    "from.",
                    source,
                    (*path, "inputs") if step.inputs else path,
                    hint=f"Add `{name}: $inputs.<name>`, or a step's created entities.",
                )
        for name, value in step.params.items():
            self.step_param(workflow, action, name, value, source, (*path, "params", name))
        for name, param in action.params.items():
            if name not in step.params and param.required:
                self.add(
                    "workflow-param-missing",
                    f"`{step.action}` needs the param `{name}`, and this step doesn't give it.",
                    source,
                    (*path, "params") if step.params else path,
                    hint=f"Add `{name}:` with a value, or `$params.<name>` for the workflow's.",
                )
        for index, other in enumerate(step.needs):
            if other == step.id or other not in workflow.steps:
                self.add(
                    "workflow-unknown-step",
                    f"`{other}` isn't another step of this workflow.",
                    source,
                    (*path, "needs", index),
                )

    def step_each(self, step: WorkflowStep, action: Action, source: Document, path: Path) -> None:
        put = action.inputs.get(step.each or "")
        if put is None:
            self.add(
                "workflow-unknown-input",
                f"`{step.action}` has no input `{step.each}`.",
                source,
                path,
                hint=schemas._did_you_mean(step.each or "", list(action.inputs)),
            )
        elif put.cardinality != "one":
            self.add(
                "workflow-each-takes-many",
                f"`{step.action}`'s `{step.each}` takes many entities, so one run takes them all.",
                source,
                path,
                hint="Leave `each` out: the step runs once, on every entity.",
            )
        elif step.each not in step.inputs and put.optional:  # a required one is reported anyway
            self.add(
                "workflow-input-missing",
                f"The step runs once for each `{step.each}`, and doesn't say where they come from.",
                source,
                path,
                hint=f"Add `{step.each}:` under its `inputs`.",
            )

    def step_input(
        self,
        workflow: Workflow,
        step: WorkflowStep,
        action: Action,
        name: str,
        ref: str,
        source: Document,
        path: Path,
    ) -> None:
        wanted = action.inputs.get(name)
        if wanted is None:
            self.add(
                "workflow-unknown-input",
                f"`{step.action}` has no input `{name}`.",
                source,
                path,
                key=True,
                hint=schemas._did_you_mean(name, list(action.inputs)),
            )
            return
        parts = ref.split(".")
        if parts[0] == "$steps" and parts[1] == step.id:
            self.add(
                "workflow-unknown-step",
                "A step can't take from itself.",
                source,
                path,
            )
            return
        found = source_of(workflow, self.plugin.actions, ref)
        if found is None:
            why = _why_nothing(workflow, self.plugin.actions, ref)
            self.add(
                "workflow-unknown-reference",
                f"`{ref}` names nothing in this workflow{why}.",
                source,
                path,
            )
            return
        if not self.is_a(found.type, wanted.type):
            self.add(
                "workflow-wrong-type",
                f"`{ref}` is `{found.type}`, and `{step.action}`'s `{name}` takes `{wanted.type}`.",
                source,
                path,
            )
        if found.cardinality == "many" and wanted.cardinality == "one" and name != step.each:
            self.add(
                "workflow-many-into-one",
                f"`{ref}` can be many entities, and `{step.action}`'s `{name}` takes one.",
                source,
                path,
                hint=f"Add `each: {name}` to run the step once for each of them.",
            )

    def step_param(
        self,
        workflow: Workflow,
        action: Action,
        name: str,
        value: Any,  # noqa: ANN401 - manifest data is JSON-shaped
        source: Document,
        path: Path,
    ) -> None:
        wanted = action.params.get(name)
        if wanted is None:
            self.add(
                "workflow-unknown-param",
                f"`{action.name}` has no param `{name}`.",
                source,
                path,
                key=True,
                hint=schemas._did_you_mean(name, list(action.params)),
            )
            return
        if isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
            given = (
                workflow.params.get(value.removeprefix("$params."))
                if value.startswith("$params.")
                else None
            )
            if given is None:
                self.add(
                    "workflow-unknown-reference",
                    f"`{value}` isn't one of the workflow's params. A value starting with `$` "
                    "is written `$$`.",
                    source,
                    path,
                )
                return
            if not _fits(given, wanted):
                self.add(
                    "workflow-wrong-type",
                    f"`{value}` is {_kind(given)}, and `{action.name}`'s `{name}` takes "
                    f"{_kind(wanted)}.",
                    source,
                    path,
                )
            return
        for message in _validate.value_problems(wanted, literal(value)):
            self.add("workflow-invalid-param", f"The value {message}", source, path)


def _reachable_order(workflow: Workflow) -> list[str]:
    """The steps that could start, in order, leaving out those in a cycle and after."""
    done: list[str] = []
    left = dict(workflow.steps)
    while True:
        ready = [i for i, s in left.items() if s.after() & workflow.steps.keys() <= set(done)]
        if not ready:
            return done
        done.extend(ready)
        for i in ready:
            del left[i]


def _why_nothing(workflow: Workflow, actions_: dict[str, Action], ref: str) -> str:
    parts = ref.split(".", 3)
    if parts[0] == "$inputs":
        return ": it has no input `" + parts[1] + "`"
    step = workflow.steps.get(parts[1])
    if step is None:
        return f": it has no step `{parts[1]}`"
    action = actions_.get(step.action)
    if action is None:
        return ""
    if parts[2] == "created":
        made = ", ".join(f"`{c.type}`" for c in action.creates) or "nothing"
        return f": `{step.action}` creates {made}"
    return f": step `{step.id}` isn't given `{parts[3]}`"


def _kind(param: Field) -> str:
    if param.type == "enum":
        return "one of " + ", ".join(f"`{v}`" for v in param.values or ())
    bounds = [
        f"{word} {bound:g}"
        for word, bound in (("from", param.min), ("to", param.max))
        if bound is not None
    ]
    article = "an" if param.type[0] in "aeiou" else "a"
    return f"{article} `{param.type}`" + (" " + " ".join(bounds) if bounds else "")


def _fits(given: Field, wanted: Field) -> bool:
    """Whether every value of the workflow's param is one the step's param takes."""
    if given.type != wanted.type and not (given.type == "int" and wanted.type == "float"):
        return False
    if given.type == "enum":
        return set(given.values or ()) <= set(wanted.values or ())
    if wanted.min is not None and (given.min is None or given.min < wanted.min):
        return False
    return wanted.max is None or (given.max is not None and given.max <= wanted.max)
