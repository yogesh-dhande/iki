"""What an app's spec (ADR 0090) must be beyond its schema, checked on every save.

The schema (``app/v1``, in ``core.App``'s manifest) says which props each component
takes. This says the rest, run as the person saving, as they'd see the page:

- Panel IDs are unique.
- An entities grid names a type there is, and a search that works: its conditions,
  sort and columns are fields of that type.
- A viewer shows an entity: an HRID the person can read, whose type has the viewer
  asked for, or one expression, whose value is the entity.
- Each expression reads (with iki-wasm, as browsers will), and reads only panels before
  its own, and only what they offer: a grid its ``rows`` and ``selection``, a viewer its
  ``entity``. A grid's column also reads its ``row``.

- Its text and expressions are at most :data:`MAX_TEXT` characters in all, so checking
  them is quick however it's written.

Each problem says which panel and prop.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.db.models import Q

from iki_server import expressions
from iki_server.accounts.models import User
from iki_server.entities import access, registry, search

#: What each component offers the panels after it.
OFFERS: Mapping[str, tuple[str, ...]] = {
    "iki.entities": ("rows", "selection"),
    "iki.viewer": ("entity",),
    "iki.text": (),
}
#: What an entity is to an expression: a grid's rows and selection, a viewer's entity.
ENTITY: tuple[str, ...] = ("id", "hrid", "type", "title", "revision", "values")
#: Columns any entity has, besides its fields.
COLUMNS: tuple[str, ...] = ("hrid", "title", "type", "created_at", "updated_at")
#: The most characters an app's text and expressions may have in all.
MAX_TEXT = 50_000


@dataclass(frozen=True)
class _Template:
    #: Where it is, for a person: "Panel `proteins`, `columns.2.value`".
    where: str
    text: str
    #: The names it may read: panels before its own, and maybe ``row``.
    panels: Mapping[str, str]
    row: bool = False
    #: Whether it must be one expression and nothing else (an entity's prop).
    whole: bool = False


def problems(user: User, spec: Mapping[str, Any]) -> list[str]:
    """What's wrong with an app's spec, which already fits its schema: nothing if it's
    right."""
    out: list[str] = []
    before: dict[str, str] = {}
    templates: list[_Template] = []
    for panel in spec.get("panels", ()):
        name, component, props = panel["id"], panel["component"], panel["props"]
        at = f"Panel `{name}`"
        if name in before:
            out.append(f"{at}: another panel before it has that ID.")
        earlier = dict(before)
        if component == "iki.entities":
            out.extend(_entities(user, at, props))
            for i, column in enumerate(props.get("columns", ())):
                if "value" in column:
                    templates.append(
                        _Template(f"{at}, `columns.{i}.value`", column["value"], earlier, row=True)
                    )
        elif component == "iki.viewer":
            out.extend(_viewer(user, at, props))
            if "{{" in props["entity"]:
                templates.append(_Template(f"{at}, `entity`", props["entity"], earlier, whole=True))
        elif component == "iki.text":
            templates.append(_Template(f"{at}, `text`", props["text"], earlier))
        before.setdefault(name, component)
    total = sum(len(t.text) for t in templates)
    if total > MAX_TEXT:
        out.append(
            f"Its text and expressions are {total:,} characters; an app may have {MAX_TEXT:,}."
        )
        return out
    out.extend(_expressions(templates))
    return out


def _entities(user: User, at: str, props: Mapping[str, Any]) -> list[str]:
    type_name = props["type"]
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return [f"{at}, `type`: there’s no entity type {type_name}."]
    out = []
    try:
        search.queryset(
            user,
            search.Search(
                type=type_name,
                where=props.get("where"),
                q=props.get("q"),
                sort=props.get("sort"),
                descending=props.get("descending", False),
            ),
        )
    except search.SearchError as error:
        for pointer, detail in error.errors:
            where = pointer.removeprefix("#/").replace("/", ".")
            out.append(f"{at}, `{where}`: {detail}" if where else f"{at}: {detail}")
    form = search.form_of(type_name)
    fields = set(form.fields()) if form is not None else set()
    for i, column in enumerate(props.get("columns", ())):
        field = column.get("field")
        if field is not None and field not in fields and field not in COLUMNS:
            out.append(f"{at}, `columns.{i}.field`: {type_name} has no field {field}.")
    return out


def _viewer(user: User, at: str, props: Mapping[str, Any]) -> list[str]:
    entity = props["entity"]
    if "{{" in entity:
        return []  # An expression: checked with the others.
    try:
        lookup = Q(pk=uuid.UUID(entity))
    except ValueError:
        lookup = Q(hrid=entity)
    found = access.visible(user).filter(lookup).first()
    if found is None:
        return [f"{at}, `entity`: {entity} is no entity you can read."]
    entity_type = registry.types().get(found.type)
    shown = [v.viewer for v in registry.viewers(entity_type)] if entity_type else []
    viewer = props.get("viewer")
    if not shown:
        return [f"{at}, `entity`: {found.hrid}’s type has no viewer."]
    if viewer is not None and viewer not in shown:
        return [f"{at}, `viewer`: {found.hrid}’s type has no {viewer} viewer."]
    return []


def _expressions(templates: Sequence[_Template]) -> list[str]:
    if not templates:
        return []
    try:
        checked = expressions.check([t.text for t in templates])
    except expressions.UnavailableError:
        return ["Its expressions can’t be checked just now. Try saving it again shortly."]
    out = []
    for template, found in zip(templates, checked, strict=True):
        at = template.where
        if found.error is not None:
            out.append(f"{at}: {found.error}")
            continue
        if template.whole and not found.whole:
            out.append(
                f"{at}: an entity is an HRID, or one expression between `{{{{` and `}}}}` "
                "and nothing else."
            )
        readable = {**template.panels, **({"row": "row"} if template.row else {})}
        for name in found.names:
            if name not in readable:
                what = "its row or a panel before it" if template.row else "a panel before it"
                out.append(f"{at}: `{name}` isn’t {what}.")
        for path in found.fields:
            name, _, field = path.partition(".")
            if name not in readable:
                continue
            offered = ENTITY if name == "row" else OFFERS[readable[name]]
            if field not in offered:
                listed = " and ".join(f"`{o}`" for o in offered) or "nothing"
                out.append(f"{at}: `{name}` offers {listed}, not `{field}`.")
    return out
