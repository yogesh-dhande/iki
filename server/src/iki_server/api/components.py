"""Components: the Web Components installed plugins add to Iki's pages (ADR 0084).

``GET /components`` lists them, with where each appears. Their code runs only in a
sandboxed frame the web app makes, never in its page.
"""

from typing import Any, Literal

from django.http import HttpRequest
from ninja import Field, Router, Schema

from iki_server.api.common import caller
from iki_server.components.installed import components
from iki_spec.model import Component

router = Router(tags=["components"])


class SlotOut(Schema):
    slot: Literal["entity.tab"] = Field(
        description="`entity.tab`: a tab of its own on the pages of entities of `type`."
    )
    type: str = Field(description="The entity type whose pages show it, with its subtypes.")
    label: str = Field(description="What the tab is called.")


class ComponentOut(Schema):
    name: str = Field(description="Its qualified name, such as `protein.plate_map`.")
    plugin: str
    label: str
    description: str | None
    element: str = Field(description="The custom element its module defines.")
    slots: list[SlotOut]
    selects: str | None = Field(
        description="The type of the entities a person can select in it, or null."
    )
    sha256: str = Field(description="Its module's SHA-256: the code its frames run.")


def _out(component: Component) -> dict[str, Any]:
    return {
        "name": component.qualified_name,
        "plugin": component.plugin,
        "label": component.label,
        "description": component.description,
        "element": component.element,
        "slots": [{"slot": s.slot, "type": s.type, "label": s.label} for s in component.slots],
        "selects": component.selects or None,
        "sha256": component.sha256,
    }


@router.get("/components", response=list[ComponentOut], summary="Installed components")
def list_components(request: HttpRequest) -> list[dict[str, Any]]:
    caller(request)
    return [_out(c) for c in components().values()]
