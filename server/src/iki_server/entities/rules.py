"""Rules some core types' values follow beyond what a manifest can say.

A manifest checks each field on its own. Core types can need more, checked
on every create and edit, so they hold for the web, the API, MCP and runs alike:

- ``core.Collection`` lists its ``members`` or has a ``search``, not both. Its
  search must work for the person saving it, in a project they can read.
- ``core.View`` (ADR 0086) shows a revision its entity has, in a viewer its
  entity's type has, with that viewer's state.

Subtypes follow their parents' rules.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from iki_server.accounts.models import User
from iki_server.entities import access, registry, search
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


#: What each viewer keeps in a view's state, besides its version.
VIEW_STATE = {"sequence": {"feature"}, "structure": {"camera"}}


def _view(user: User, values: Mapping[str, Any]) -> Errors:
    target = (
        access.visible(user).filter(pk=values["target"]).first() if values.get("target") else None
    )
    if target is None:
        return {}  # The reference's own check says so.
    errors: Errors = {}
    revision = values.get("target_revision")
    revisions = Revision.objects.filter(entity=target)
    if revision is not None and not revisions.filter(number=revision).exists():
        errors["target_revision"] = [f"{target.hrid} has no revision {revision}."]
    viewer = values.get("viewer")
    entity_type = registry.types().get(target.type)
    shown = {v.viewer for v in registry.viewers(entity_type)} if entity_type else set()
    if viewer is not None and viewer not in shown:
        errors["viewer"] = [f"{target.hrid}’s type has no {viewer} viewer."]
    state = values.get("state")
    if viewer in VIEW_STATE and isinstance(state, Mapping):
        others = sorted(set(state) - {"version"} - VIEW_STATE[viewer])
        if others:
            listed = ", ".join(f"`{o}`" for o in others)
            errors["state"] = [f"The {viewer} viewer doesn’t keep {listed}."]
    return errors


RULES: dict[str, Rule] = {
    "core.Collection": Rule(("members", "search"), _collection),
    "core.View": Rule(("target", "target_revision", "viewer", "state"), _view),
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
