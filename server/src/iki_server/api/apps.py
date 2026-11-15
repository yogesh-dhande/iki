"""Apps (ADR 0090): what's needed to write one, and checking one before saving it.

An app is an entity (``core.App``): create, read, change and archive it at
``/entities``. Saving checks its spec as the person saving it; these endpoints say how
to write a spec, and say everything wrong with one without saving it, so an agent can
put it right first.
"""

from typing import Any

from django.http import HttpRequest
from ninja import Field, Router, Schema

from iki_server.api.common import caller, require_scope
from iki_server.api.entities import _find
from iki_server.entities import app_specs

router = Router(tags=["apps"])


class AppComponentOut(Schema):
    name: str = Field(description="As a panel's `component` names it, such as iki.entities.")
    description: str
    props: dict[str, str] = Field(description="Each prop it takes, and what it is.")
    offers: dict[str, str] = Field(description="What it offers later panels' expressions.")


class AppGuideOut(Schema):
    type: str = Field(description="The entity type an app is: core.App.")
    spec_schema: dict[str, Any] = Field(description="The JSON Schema its spec follows (app/v1).")
    grid: str
    components: list[AppComponentOut]
    expressions: str = Field(description="How expressions between {{ and }} work.")
    about: str = Field(description="How an app is about an entity, and shown on its page.")
    example: dict[str, Any] = Field(description="A spec that saves.")


class AppCheckIn(Schema):
    spec: Any = Field(description="A spec, as an app's `spec` value would be.")


class AppCheckOut(Schema):
    problems: list[str] = Field(
        description="Everything saving it would refuse, each saying which panel and prop, "
        "or where it doesn't fit the schema. Empty if it would save."
    )


@router.get("/apps/guide", response=AppGuideOut, summary="How to write an app")
def app_guide(request: HttpRequest) -> dict[str, Any]:
    """The spec's schema, Iki's components (what each takes and offers), how expressions
    work, and an example."""
    require_scope(caller(request), "read")
    return app_specs.guide()


@router.post("/apps/check", response=AppCheckOut, summary="Check an app's spec")
def check_app(request: HttpRequest, payload: AppCheckIn) -> dict[str, Any]:
    """Everything wrong with a spec, checked as you, as saving it would be: its schema,
    its panels' searches and entities, and its expressions. Nothing is saved."""
    me = caller(request)
    require_scope(me, "read")
    return {"problems": app_specs.check(me, payload.spec)}


class EntityAppOut(Schema):
    id: str
    hrid: str
    name: str


@router.get("/entities/{ref}/apps", response=list[EntityAppOut], summary="The apps about an entity")
def entity_apps(request: HttpRequest, ref: str) -> list[dict[str, Any]]:
    """Apps about its type in the projects it's in, that you can read, by name: its page
    shows each as a tab, about it."""
    me = caller(request)
    require_scope(me, "read")
    entity = _find(me, ref)
    return [
        {"id": str(app.pk), "hrid": app.hrid, "name": app.name}  # type: ignore[attr-defined]
        for app in app_specs.about(me, entity)
    ]
