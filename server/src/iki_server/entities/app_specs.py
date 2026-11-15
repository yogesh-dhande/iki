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

Each problem says which panel and prop. :func:`check` also says where a spec doesn't fit
its schema, for agents checking one before saving it, and :func:`guide` describes the
components for them.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from jsonschema import Draft202012Validator

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
#: The most schema problems :func:`check` lists: past the first few, they repeat.
MAX_SCHEMA_PROBLEMS = 20


@dataclass(frozen=True)
class Component:
    """One of Iki's components, as agents are told about it."""

    description: str
    #: Each prop, and what it is.
    props: Mapping[str, str]
    #: What it offers later panels, and what each is.
    offers: Mapping[str, str]


ENTITY_VALUE = (
    "an entity: {id, hrid, type, title, revision, values}, `values` holding its fields by name"
)

COMPONENTS: Mapping[str, Component] = {
    "iki.entities": Component(
        "A grid of the entities a search finds, as the person viewing the app may read them.",
        {
            "type": "The entity type to search, such as protein.Protein (list_entity_types).",
            "scope": "`project` (the default): only the app's project. `all`: every project "
            "the person viewing it can read.",
            "where": "Conditions on the type's fields, as search_entities takes them.",
            "q": "Text in their HRID or title.",
            "sort": "The field they're in order of.",
            "descending": "Whether in reverse order.",
            "limit": "How many it shows, 1 to 100 (25 if not given).",
            "select": "How many people may pick: `none`, `one` (the default) or `many`.",
            "columns": "After the ID, each column is {field} (one of the type's fields, or "
            "hrid, title, type, created_at, updated_at) or {label, value}, value being text "
            "with expressions that read `row`, the row's entity. Without columns, its title.",
        },
        {"rows": f"the rows it shows, each {ENTITY_VALUE}", "selection": "the rows picked"},
    ),
    "iki.viewer": Component(
        "One of Iki's viewers (sequence, structure, alignment) on an entity, as its type "
        "configures them.",
        {
            "entity": "An HRID, or one expression and nothing else whose value is an entity, "
            "an HRID, or a list (its first is shown), such as `{{ proteins.selection }}`. "
            "Until there's one, it says there's nothing to show yet.",
            "viewer": "Which of the entity's viewers: sequence, structure or alignment. Its "
            "type's first if not given.",
        },
        {"entity": f"the entity it shows, {ENTITY_VALUE}, or null"},
    ),
    "iki.text": Component(
        "Text, with expressions in it. It's shown as written, line breaks and all: never as "
        "HTML or Markdown.",
        {"text": "The text, up to 10,000 characters."},
        {},
    ),
}

EXPRESSIONS = (
    "Text props (a text's `text`, a column's `value`, a viewer's `entity`) can hold "
    "expressions between {{ and }}, in Google's Common Expression Language (CEL): "
    "`size(proteins.selection)`, `proteins.rows.filter(r, r.values.length > 300)`, "
    "`row.values.name + ' (' + row.hrid + ')'`, `x > 0 ? 'yes' : 'no'`. An expression reads "
    "only panels before its own, by their IDs, and what they offer; a column's also reads "
    "`row`. In text, each value is written in: text as it is, numbers as written, yes or "
    "no, a list's items with commas, an entity by its title. An expression is at most 1,000 "
    "characters and nests at most 32 deep; an app's text and expressions are at most "
    "50,000 characters in all."
)


def guide() -> dict[str, Any]:
    """What agents need to write an app: its schema, the components, and expressions."""
    schema = registry.types()["core.App"].fields["spec"].schema
    return {
        "type": "core.App",
        "spec_schema": schema,
        "grid": "Panels are laid out in order on a grid twelve columns wide: `width` is how "
        "many each spans (12 if not given), and a panel that doesn't fit starts a new row. "
        "On a phone, each is the whole width.",
        "components": [
            {"name": name, "description": c.description, "props": c.props, "offers": c.offers}
            for name, c in COMPONENTS.items()
        ],
        "expressions": EXPRESSIONS,
        "example": {
            "version": 1,
            "panels": [
                {
                    "id": "proteins",
                    "title": "Proteins",
                    "width": 6,
                    "component": "iki.entities",
                    "props": {
                        "type": "protein.Protein",
                        "sort": "name",
                        "columns": [
                            {"field": "name"},
                            {"label": "Length", "value": "{{ size(row.values.sequence) }} aa"},
                        ],
                    },
                },
                {
                    "id": "picked",
                    "title": "Picked",
                    "width": 6,
                    "component": "iki.viewer",
                    "props": {"entity": "{{ proteins.selection }}"},
                },
                {
                    "id": "summary",
                    "component": "iki.text",
                    "props": {
                        "text": "{{ size(proteins.selection) }} of "
                        "{{ size(proteins.rows) }} picked."
                    },
                },
            ],
        },
    }


def check(user: User, spec: Any) -> list[str]:  # noqa: ANN401 - JSON
    """Everything wrong with a spec, as saving it would say, but each schema problem on its
    own with where it is. Nothing if it would save."""
    schema = registry.types()["core.App"].fields["spec"].schema or {}
    errors = sorted(
        Draft202012Validator(schema).iter_errors(spec),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if errors:
        out = []
        for error in errors[:MAX_SCHEMA_PROBLEMS]:
            where = ".".join(str(p) for p in error.absolute_path)
            out.append(f"At `{where}`: {error.message}." if where else f"{error.message}.")
        if len(errors) > MAX_SCHEMA_PROBLEMS:
            out.append(f"And {len(errors) - MAX_SCHEMA_PROBLEMS} more.")
        return out
    return problems(user, spec)


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
