"""Requests between projects (ADR 0132): what a service project takes, sending a request to
one, and a request's two sides.

- ``/projects/{id}/intake`` is what a project takes as a service: the request types it
  accepts, and from which projects. Its Admins set it.
- ``/projects/{id}/services`` lists the services a project may send requests to, and
  ``/projects/{id}/sent-to`` those it has sent requests to; ``/projects/{id}/sent-from``
  the projects that have sent requests to it.
- ``POST /entities/{ref}/submit`` sends a request to one, with the records it sends.
- ``/entities/{ref}/request`` is a request's two sides: what it sent the service, and
  the results the service released back.

A service's queue is the requests sent to it: search with ``sent_to``, or the grid.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from ninja import Field, HeaderEx, P, Query, Router, Schema

from iki_server.accounts.models import User
from iki_server.api.common import caller, project
from iki_server.api.entities import (
    EntityOut,
    _expected,
    _find,
    _one,
    _out,
)
from iki_server.api.problems import Problem
from iki_server.api.schemas import ProjectRef, UserOut
from iki_server.audit.models import AuditEvent
from iki_server.entities import access, registry, requests, services
from iki_server.entities.models import Entity, ProjectLink, Request
from iki_server.entities.values import Invalid, shape
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action

router = Router(tags=["requests"])

#: The most projects one intake names.
MAX_SOURCES = 200


class RequestTypeRef(Schema):
    name: str = Field(description="Qualified: plugin.Type.")
    label: str


class IntakeOut(Schema):
    types: list[RequestTypeRef] = Field(
        description="The request types it accepts, and their subtypes. Empty: it takes none."
    )
    from_registry: bool = Field(
        description="Every project in its registry may send it these. Otherwise only `projects`."
    )
    projects: list[ProjectRef] = Field(
        description="The projects that may send it these, besides the whole registry."
    )


class IntakeIn(Schema):
    types: list[str] = Field(
        default_factory=list, max_length=50, description="Request types, qualified."
    )
    from_registry: bool = False
    projects: list[UUID] = Field(default_factory=list, max_length=MAX_SOURCES)


class ServiceOut(Schema):
    project: ProjectRef
    description: str
    types: list[RequestTypeRef] = Field(
        description="The request types it takes from the project asking."
    )


class ServicesFilters(Schema):
    type: str | None = Field(
        None, description="Only services taking this request type (qualified), or a parent."
    )


class SubmitIn(Schema):
    service: UUID = Field(description="The service's project: one `/services` lists.")
    reason: str = Field("", max_length=2000)


class ResultReleaseOut(Schema):
    """Receive the exact versions at GET /entities/{entity}/revisions/{number}."""

    entity: UUID
    reviewed_revision: int | None = Field(
        description="The revision the reviewer signed, if required."
    )
    signed_revision: int | None = Field(description="The revision signed for release, if signed.")
    released_revision: int = Field(description="The revision that released the result.")


class RequestOut(Schema):
    """A request's two sides (ADR 0132)."""

    request: EntityOut
    asking_project: ProjectRef | None = Field(description="The project that owns and sent it.")
    service: ProjectRef | None = Field(description="The project serving it; null before it's sent.")
    submitted_at: datetime | None
    submitted_by: UserOut | None
    sent: list[EntityOut] = Field(
        description="What it sent the service with it, that you can read: the records its "
        "`sends` fields named, now or when they were sent."
    )
    result_releases: list[ResultReleaseOut] = Field(
        description="The revisions delivered to this request."
    )
    results: list[EntityOut] = Field(
        description="Its results that you can read: for the asking project, those the service "
        "released back; for the service, all of them."
    )


def _type_ref(name: str) -> dict[str, str]:
    found = registry.types().get(name)
    return {"name": name, "label": (found.label if found else "") or name}


def _intake_out(found: requests.IntakeSettings, me: User) -> dict[str, Any]:
    return {
        "types": [_type_ref(t) for t in found.types],
        "from_registry": found.from_registry,
        "projects": found.sources,
    }


@router.get(
    "/projects/{project_id}/intake",
    response=IntakeOut,
    summary="What a project takes as a service",
)
def get_intake(request: HttpRequest, project_id: UUID) -> dict[str, Any]:
    """The request types it accepts, and who from (ADR 0132). Its members can read it."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_READ)
    return _intake_out(requests.intake_of(found), me)


@router.put(
    "/projects/{project_id}/intake",
    response=IntakeOut,
    summary="Choose what a project takes as a service",
)
def put_intake(request: HttpRequest, project_id: UUID, payload: IntakeIn) -> dict[str, Any]:
    """Its Admins choose the request types it accepts, from every project in its registry
    (`from_registry`) or from the projects they name, which they must be able to see.
    Requests already sent stay. Audited as `project.intake_changed`."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_MANAGE)
    seen = permissions.visible_projects(me)
    wanted = list(dict.fromkeys(payload.projects))
    sources = {p.pk: p for p in seen.filter(pk__in=wanted)}
    errors = [
        {"pointer": f"#/projects/{i}", "detail": "is no project you can see."}
        for i, pk in enumerate(wanted)
        if pk not in sources
    ]
    if errors:
        raise Problem(
            422, "Those aren’t projects you can see.", code="validation_failed", errors=errors
        )
    try:
        saved = requests.set_intake(
            found,
            types=payload.types,
            from_registry=payload.from_registry,
            sources=[sources[pk] for pk in wanted],
            by=me,
        )
    except Invalid as error:
        raise _invalid(error) from None
    return _intake_out(saved, me)


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "Those values aren’t valid.",
        code="validation_failed",
        errors=[
            {"pointer": f"#/{key}", "detail": " ".join(messages)}
            for key, messages in error.errors.items()
        ],
    )


@router.get(
    "/projects/{project_id}/services",
    response=list[ServiceOut],
    summary="The services a project may send requests to",
)
def list_services(
    request: HttpRequest, project_id: UUID, filters: Query[ServicesFilters]
) -> list[dict[str, Any]]:
    """Each project that takes requests from this one (ADR 0132), of `type` if given, with
    the request types it takes. Its members can read them: a service's name and description
    are what it shows those it serves."""
    me = caller(request)
    asking = project(me, project_id, Action.PROJECT_READ)
    wanted = filters.type
    if wanted is not None and (wanted not in registry.types() or requests.spec_of(wanted) is None):
        return []
    out = []
    for found in services_list(asking, wanted):
        intake = requests.intake_of(found)
        out.append(
            {
                "project": found,
                "description": found.description,
                "types": [
                    _type_ref(t)
                    for t in intake.types
                    if wanted is None or t in requests.accepted_by(wanted)
                ],
            }
        )
    return out


@router.get(
    "/projects/{project_id}/sent-to",
    response=list[ProjectRef],
    summary="The services a project has sent requests to",
)
def list_sent_to(request: HttpRequest, project_id: UUID) -> list[Project]:
    """Each project this one has sent a request to (ADR 0132), by name, whether or not it
    still takes them: what its requests' `service` says, and the `sent_to` condition
    finds. Its members can read them; nothing else of those projects shows."""
    me = caller(request)
    asking = project(me, project_id, Action.PROJECT_READ)
    sent = Request.objects.filter(entity__owner_project=asking).values("service_id")
    return list(Project.objects.filter(pk__in=sent).order_by("name", "pk")[:200])


@router.get(
    "/projects/{project_id}/sent-from",
    response=list[ProjectRef],
    summary="The projects that have sent a project requests",
)
def list_sent_from(request: HttpRequest, project_id: UUID) -> list[Project]:
    """Each project that has sent this one a request (ADR 0132), by name: what its queue's
    requests' `asking_project` says, which its `owner_project` condition finds. Its members
    can read them, though they can't see those projects; nothing else of theirs shows."""
    me = caller(request)
    service = project(me, project_id, Action.PROJECT_READ)
    asking = Request.objects.filter(service=service).values("entity__owner_project_id")
    return list(Project.objects.filter(pk__in=asking).order_by("name", "pk")[:200])


def services_list(asking: Project, type_name: str | None) -> list[Project]:
    return list(requests.services_for(asking, type_name).order_by("name", "pk")[:200])


@router.post(
    "/entities/{ref}/submit",
    response=EntityOut,
    summary="Send a request to a service",
)
def submit_request(
    request: HttpRequest,
    ref: str,
    payload: SubmitIn,
    response: HttpResponse,
    project: UUID | None = None,
    if_match: HeaderEx[str | None, P(alias="If-Match")] = None,
) -> dict[str, Any]:
    """Sends the request (ADR 0132) to a service that takes it from its project: it's linked
    into the service's project, with the records its `sends` fields name, which the service
    can then read, and only those. It goes to one service, for good. It needs you as an
    Editor in its project, which must allow sharing its entities (ADR 0067). A service that
    doesn't take it answers 404, as one that doesn't exist does. Send If-Match."""
    me = caller(request)
    entity = _find(me, ref, project)
    expected = _expected(if_match)
    service = Project.objects.select_related("team", "registry").filter(pk=payload.service).first()
    try:
        if service is None:
            raise requests.NoSuchService("")
        requests.submit(me, entity, service, expected_revision=expected, reason=payload.reason)
    except requests.NoSuchService:
        type_name = registry.types().get(entity.type)
        owner = services.owner_of(entity)
        label = (type_name.label_plural if type_name else "") or "requests"
        raise Problem(404, requests.not_a_service(owner, label)) from None
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except services.Stale as stale:
        raise Problem(
            412,
            f"Someone changed it since you read it: it’s at revision {stale.current} now. "
            "Read it again, then send it.",
            code="stale",
            headers={"ETag": f'"{stale.current}"'},
        ) from None
    return _one(me, _find(me, str(entity.pk)), response)


@router.get(
    "/entities/{ref}/request",
    response=RequestOut,
    summary="A request's two sides",
)
def get_request(request: HttpRequest, ref: str, project: UUID | None = None) -> dict[str, Any]:
    """Who asked and who serves it, what it sent the service, and the results that came of
    it (ADR 0132): each only as far as you can read them. A record that isn't a request
    answers 404."""
    me = caller(request)
    entity = _find(me, ref, project)
    if requests.spec_of(entity.type) is None or entity.owner_project_id is None:
        raise Problem(404, f"{entity.hrid} isn’t a request.")
    sent = Request.objects.select_related("service", "submitted_by").filter(entity=entity).first()
    out = _out(me, [entity])[0]
    readable = access.visible(me)
    sent_ids = (
        list(
            ProjectLink.objects.filter(via_request=entity, project_id=sent.service_id)
            .exclude(entity=entity)
            .order_by("linked_at", "pk")
            .values_list("entity_id", flat=True)
        )
        if sent is not None
        else []
    )
    shown = {e.pk: e for e in readable.filter(pk__in=sent_ids).select_related("created_by")}
    results = _results(me, entity)
    return {
        "request": out,
        "asking_project": out["request"]["asking_project"],
        "service": out["request"]["service"],
        "submitted_at": sent.submitted_at if sent is not None else None,
        "submitted_by": sent.submitted_by if sent is not None else None,
        "sent": _out(me, [shown[i] for i in sent_ids if i in shown]),
        "results": _out(me, results),
        "result_releases": [
            {"entity": ref, **data}
            for ref, data in AuditEvent.objects.filter(
                action="request.result_released",
                target_type="entity",
                target_id__in=[str(result.pk) for result in results],
                project_id=entity.owner_project_id,
                data__request=str(entity.pk),
                data__has_key="released_revision",
            )
            .order_by("id")
            .values_list("target_id", "data")
        ],
    }


def _results(me: User, entity: Entity) -> list[Entity]:
    """The results of the request ``me`` can read, oldest first."""
    found: list[Entity] = []
    readable = access.visible(me)
    for name in sorted(registry.types()):
        spec = requests.result_spec(name)
        entity_type = registry.types()[name]
        if spec is None or entity_type.abstract:
            continue
        form = shape(name)
        f = form.fields().get(spec.of)
        if f is None or not any(entity.type in _subtypes_of(t) for t in f.to):
            continue
        rows = form.model._default_manager.filter(type=name, **{f"{spec.of}_id": entity.pk})
        found.extend(
            readable.filter(pk__in=rows.values("pk"), archived_at__isnull=True).select_related(
                "created_by"
            )
        )
    return sorted(found, key=lambda e: (e.created_at, e.pk))[:200]


def _subtypes_of(name: str) -> set[str]:
    from iki_server.entities.values import subtypes

    return subtypes(name)
