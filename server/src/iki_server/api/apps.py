"""Apps (ADR 0090): what's needed to write one, and checking one before saving it.

An app is an entity (``iki/core.App``): create, read, change and archive it at
``/entities``. Saving checks its spec as the person saving it; these endpoints say how
to write a spec, and say everything wrong with one without saving it, so an agent can
put it right first. They also list the apps about an entity, and choose the app a
project's page opens with.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import caller, project, require_scope
from iki_server.api.entities import _find
from iki_server.api.pagination import PageParams, page_by
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef, ProjectRef
from iki_server.entities import app_drafts, app_specs, dashboards
from iki_server.entities.models import Entity
from iki_server.org.permissions import Action

router = Router(tags=["apps"])


class AppComponentOut(Schema):
    name: str = Field(description="As a panel's `component` names it, such as iki.entities.")
    description: str
    props: dict[str, str] = Field(description="Each prop it takes, and what it is.")
    offers: dict[str, str] = Field(description="What it offers later panels' expressions.")


class AppPluginComponentOut(Schema):
    name: str = Field(description="As a panel's `component` names it, such as acme/lab.box_map.")
    label: str
    description: str | None
    tabs: list[str] = Field(
        description="The types it's a tab of: an `entity` prop is one of these, or extends one."
    )
    shows: str | None = Field(
        description="The type a search prop searches, or one extending it; null if it shows "
        "only one entity."
    )
    selects: str | None = Field(
        description="The type of what people pick in it, which it offers as `selection`; "
        "null if they pick nothing."
    )


class AppGuideOut(Schema):
    type: str = Field(description="The entity type an app is: iki/core.App.")
    spec_schema: dict[str, Any] = Field(description="The JSON Schema its spec follows (app/v1).")
    grid: str
    components: list[AppComponentOut]
    plugin_components: list[AppPluginComponentOut] = Field(
        description="Installed plugins' components a panel can be (ADR 0117)."
    )
    plugins: str = Field(description="How a panel is a plugin's component, and its props.")
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


@router.get("/entities/{ref}/apps", response=Page[EntityAppOut], summary="The apps about an entity")
def entity_apps(request: HttpRequest, ref: str, params: Query[PageParams]) -> dict[str, Any]:
    """Apps about its type in the projects it's in, that you can read, by name: its page
    shows each as a tab, about it."""
    me = caller(request)
    require_scope(me, "read")
    entity = _find(me, ref)
    found = page_by(app_specs.about(me, entity), params, by=app_specs.BY_NAME)
    return {**found, "items": [_app_out(app) for app in found["items"]]}


def _app_out(app: Entity) -> dict[str, Any]:
    return {"id": str(app.pk), "hrid": app.hrid, "name": app.name}  # type: ignore[attr-defined]


class DashboardOut(Schema):
    app: EntityAppOut | None = Field(
        description="The app the project's page opens with. Null if there's none, or you "
        "can't read it there now: it was archived, or taken out of the project."
    )
    set_by: PersonRef | None = Field(description="Who chose it, if anyone has.")
    set_at: datetime | None


class DashboardIn(Schema):
    app: str = Field(
        description="An app in the project, by ID or HRID, that isn't about an entity.",
        min_length=1,
        max_length=100,
    )


def _dashboard_out(me: Any, found: Any) -> dict[str, Any]:  # noqa: ANN401
    chosen, app = dashboards.of(me, found)
    return {
        "app": _app_out(app) if app is not None else None,
        "set_by": chosen.set_by if chosen is not None else None,
        "set_at": chosen.set_at if chosen is not None else None,
    }


@router.get(
    "/projects/{project_id}/dashboard", response=DashboardOut, summary="A project's dashboard"
)
def get_dashboard(request: HttpRequest, project_id: UUID) -> dict[str, Any]:
    """The app the project's page opens with, chosen by its Admins."""
    me = caller(request)
    return _dashboard_out(me, project(me, project_id, Action.PROJECT_READ))


@router.put(
    "/projects/{project_id}/dashboard",
    response=DashboardOut,
    summary="Choose a project's dashboard",
)
def put_dashboard(request: HttpRequest, project_id: UUID, payload: DashboardIn) -> dict[str, Any]:
    """Project Admins choose one of the project's apps for its page to open with, for
    everyone who can read it. An app about an entity can't be one: it's a tab on their
    pages instead. This is recorded."""
    me = caller(request)
    found = project(me, project_id, Action.DASHBOARD_MANAGE)
    dashboards.choose(found, payload.app, by=me)
    return _dashboard_out(me, found)


@router.delete(
    "/projects/{project_id}/dashboard",
    response={204: None},
    summary="Remove a project's dashboard",
)
def delete_dashboard(request: HttpRequest, project_id: UUID) -> Status[None]:
    """The project's page opens without an app. The app itself stays. This is recorded."""
    me = caller(request)
    dashboards.clear(project(me, project_id, Action.DASHBOARD_MANAGE), by=me)
    return Status(204, None)


@router.get(
    "/projects/{project_id}/dashboard/choices",
    response=Page[EntityAppOut],
    summary="The apps that could be a project's dashboard",
)
def dashboard_choices(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """The project's apps you can read that aren't archived or about an entity, by name."""
    me = caller(request)
    found = project(me, project_id, Action.DASHBOARD_CHOICES)
    choices = page_by(dashboards.choices(me, found), params, by=app_specs.BY_NAME)
    return {**choices, "items": [_app_out(app) for app in choices["items"]]}


class AppDraftIn(Schema):
    project: UUID = Field(description="The project it's for. It needs you as Editor there.")
    name: str = Field(min_length=1, max_length=200)
    spec: dict[str, Any] = Field(description="Its spec, which check_app found nothing wrong with.")
    entity: str | None = Field(
        default=None,
        description="For an app about an entity: one in the project, by ID or HRID, to show it on.",
        max_length=100,
    )


class DraftEntityOut(Schema):
    id: UUID
    hrid: str


class AppDraftOut(Schema):
    id: UUID
    project: ProjectRef
    name: str
    spec: dict[str, Any]
    entity: DraftEntityOut | None = Field(description="The entity it's shown on, if any.")
    agent: str | None = Field(description="The agent that drafted it for you, if one did.")
    created_at: datetime
    expires_at: datetime = Field(description="When it can no longer be opened.")


def _draft_out(draft: Any) -> dict[str, Any]:  # noqa: ANN401 - an AppDraft
    return {
        "id": draft.pk,
        "project": draft.project,
        "name": draft.name,
        "spec": draft.spec,
        "entity": draft.about,
        "agent": draft.agent.label() if draft.agent is not None else None,
        "created_at": draft.created_at,
        "expires_at": draft.expires_at,
    }


@router.post("/apps/drafts", response={201: AppDraftOut}, summary="Draft an app, to look at first")
def create_app_draft(request: HttpRequest, payload: AppDraftIn) -> Status[dict[str, Any]]:
    """An app to open and look at before saving it, checked as saving it would be. Only
    you can open it, for a day; nothing is recorded. Open it at
    ``/projects/{slug}/apps/drafts/{id}``."""
    me = caller(request)
    found = project(me, payload.project, Action.APP_EDIT)
    problems = app_specs.check(me, payload.spec)
    if problems:
        raise Problem(
            422,
            "The app has problems: put each right, and check it again.",
            code="validation_failed",
            errors=[{"pointer": "#/spec", "detail": p} for p in problems],
        )
    draft = app_drafts.create(me, found, payload.name.strip(), payload.spec, payload.entity)
    draft.refresh_from_db(fields=["created_at"])
    return Status(201, _draft_out(draft))


@router.get("/apps/drafts/{draft_id}", response=AppDraftOut, summary="An app's draft")
def get_app_draft(request: HttpRequest, draft_id: UUID) -> dict[str, Any]:
    """Your draft, while it lasts. No one else can open it."""
    me = caller(request)
    require_scope(me, "read")
    draft = app_drafts.find(me, draft_id)
    if draft is None:
        raise Problem(404, "No such draft of yours, or it’s expired.")
    return _draft_out(draft)
