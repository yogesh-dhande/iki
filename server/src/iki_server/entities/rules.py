"""Rules some core types' values follow beyond what a manifest can say.

A manifest checks each field on its own. Core types can need more, checked
on every create and edit, so they hold for the web, the API, MCP and runs alike:

- ``iki/core.Collection`` lists its ``members`` or has a ``search``, not both. Its
  search must work for the person saving it, in a project they can read.
- ``iki/core.View`` (ADR 0086, ADR 0110) shows, for each of its entities, a revision
  that entity has, in a viewer its type has, with what that viewer keeps.
- ``iki/core.App`` (ADR 0090) has a spec whose panels, props and expressions work for
  the person saving it (see :mod:`iki_server.entities.app_specs`).

Subtypes follow their parents' rules.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from iki_server.accounts.models import User
from iki_server.entities import access, app_specs, registry, search
from iki_server.entities.models import Revision
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_spec.model import EntityType

Errors = dict[str, list[str]]


@dataclass(frozen=True)
class Rule:
    #: The fields it reads. On an edit it sees their new values, or their current ones.
    fields: tuple[str, ...]
    check: Callable[[User, Mapping[str, Any]], Errors]


def _collection(user: User, values: Mapping[str, Any]) -> Errors:
    data = values.get("search")
    if data is None:
        return {}
    if values.get("members"):
        return {"search": ["A collection lists its members or has a search, not both."]}
    errors = search_errors(user, data)
    return {"search": errors} if errors else {}


def search_errors(user: User, data: Mapping[str, Any]) -> list[str]:
    """What's wrong with a collection's search, run as ``user``: nothing if it works."""
    try:
        found = search.stored(data)
        if found.project is not None:
            project = Project.objects.filter(pk=found.project).first()
            if project is None or not permissions.can(user, Action.PROJECT_READ, project):
                raise search.SearchError([("#/project", "No such project, or you can’t see it.")])
        search.queryset(user, found)
    except search.SearchError as error:
        return [_pointed(p, d) for p, d in error.errors]
    return []


def _pointed(pointer: str, detail: str) -> str:
    where = pointer.removeprefix("#/").replace("/", ".")
    return f"`{where}`: {detail}" if where else detail


#: What each viewer keeps in a version 1 view's state (of one entity), besides its version.
VIEW_STATE = {"sequence": {"feature"}, "structure": {"camera"}, "alignment": {"position"}}
#: What each viewer keeps of each entity in a version 2 view's ``state.targets``.
TARGET_STATE = {
    "sequence": {"label", "feature"},
    "structure": {"label", "colour", "hidden", "transform", "superposed", "selection"},
    "alignment": {"label", "position"},
}


def _view(user: User, values: Mapping[str, Any]) -> Errors:
    targets: list[Any] = list(values.get("targets") or [])
    found = {e.pk: e for e in access.visible(user).filter(pk__in=targets)}
    if not targets or any(t not in found for t in targets):
        return {}  # The references' own check says so, naming nothing they can't read.
    shown = [found[t] for t in targets]
    revisions = values.get("target_revisions")
    viewers = values.get("viewers")
    errors: Errors = {}
    for name, given in (("target_revisions", revisions), ("viewers", viewers)):
        if given is not None and len(given) != len(targets):
            errors[name] = [
                f"has {len(given)} for {len(targets)} entities: give one for each, in the "
                "same order as `targets`."
            ]
    if errors:
        return errors
    if revisions is not None:
        had = set(
            Revision.objects.filter(entity_id__in=targets, number__in=set(revisions)).values_list(
                "entity_id", "number"
            )
        )
        missing = [
            f"{e.hrid} has no revision {n}."
            for e, n in zip(shown, revisions, strict=True)
            if (e.pk, n) not in had
        ]
        if missing:
            errors["target_revisions"] = missing
    if viewers is not None:
        problems = _viewers(shown, viewers)
        if problems:
            errors["viewers"] = problems
        state = values.get("state")
        if isinstance(state, Mapping):
            problems = _view_state(shown, viewers, state)
            if problems:
                errors["state"] = problems
    return errors


def _viewers(shown: list[Any], viewers: list[str]) -> list[str]:
    problems = []
    seen: set[tuple[Any, str]] = set()
    for entity, viewer in zip(shown, viewers, strict=True):
        entity_type = registry.types().get(entity.type)
        has = {v.viewer for v in registry.viewers(entity_type)} if entity_type else set()
        if viewer not in has:
            problems.append(f"{entity.hrid}’s type has no {viewer} viewer.")
        if (entity.pk, viewer) in seen:
            problems.append(f"It shows {entity.hrid} in the {viewer} viewer twice.")
        seen.add((entity.pk, viewer))
    return problems


def _view_state(shown: list[Any], viewers: list[str], state: Mapping[str, Any]) -> list[str]:
    """What's wrong with a view's state for its entities and viewers, beyond its schema."""
    if state.get("version") == 1:
        if len(shown) > 1:
            return ["A view of several entities keeps their state as version 2, in `targets`."]
        others = sorted(set(state) - {"version"} - VIEW_STATE[viewers[0]])
        listed = ", ".join(f"`{o}`" for o in others)
        return [f"The {viewers[0]} viewer doesn’t keep {listed}."] if others else []
    problems = []
    if "camera" in state and "structure" not in viewers:
        problems.append("Only the structure viewer keeps `camera`, and it shows none of them.")
    kept = state.get("targets")
    if not isinstance(kept, list):
        return problems  # The schema says so.
    if len(kept) != len(shown):
        return [
            *problems,
            f"`targets` has {len(kept)} for {len(shown)} entities: give one for each, in the "
            "same order.",
        ]
    for index, (entity, viewer, item) in enumerate(zip(shown, viewers, kept, strict=True)):
        if not isinstance(item, Mapping):
            continue
        others = sorted(set(item) - TARGET_STATE[viewer])
        if others:
            listed = ", ".join(f"`{o}`" for o in others)
            problems.append(f"The {viewer} viewer doesn’t keep {listed} for {entity.hrid}.")
        onto = (item.get("superposed") or {}).get("onto")
        if isinstance(onto, int) and (
            onto == index or onto >= len(shown) or viewers[onto] != "structure"
        ):
            problems.append(
                f"{entity.hrid} is superposed on entity {onto + 1}, which isn’t another "
                "entity in the structure viewer."
            )
        spans = [s for s in item.get("selection") or [] if isinstance(s, Mapping)]
        if any(s.get("start", 0) > s.get("end", 0) for s in spans):
            problems.append(f"{entity.hrid}’s selection has a range that ends before it starts.")
    return problems


def _app(user: User, values: Mapping[str, Any]) -> Errors:
    spec = values.get("spec")
    if not isinstance(spec, Mapping):
        return {}  # The field's schema says so.
    found = app_specs.problems(user, spec)
    return {"spec": found} if found else {}


RULES: dict[str, Rule] = {
    "iki/core.App": Rule(("spec",), _app),
    "iki/core.Collection": Rule(("members", "search"), _collection),
    "iki/core.View": Rule(("targets", "target_revisions", "viewers", "state"), _view),
}


def rules_for(entity_type: EntityType) -> list[Rule]:
    """The rules for a type: its own and its parents'."""
    chain = [entity_type, *registry.ancestors(entity_type)]
    return [RULES[t.qualified_name] for t in chain if t.qualified_name in RULES]


def check(
    user: User,
    entity_type: EntityType,
    values: Callable[[str], Any],
    changed: Collection[str] | None = None,
) -> Errors:
    """Every rule's problems. ``values`` gives a field's value as it will be saved.

    On an edit, ``changed`` names the fields it changes, and only rules that
    read one of them run: renaming a collection doesn't re-check its search.
    """
    errors: Errors = {}
    for rule in rules_for(entity_type):
        if changed is not None and not set(rule.fields) & set(changed):
            continue
        for name, messages in rule.check(user, {f: values(f) for f in rule.fields}).items():
            errors.setdefault(name, []).extend(messages)
    return errors
