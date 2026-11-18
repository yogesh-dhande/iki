"""Outside APIs (ADR 0130): every installed one, where each is approved, approving and
revoking, and the callbacks APIs make when a run's job is done.

An Org Admin approves an API for a project, for the host and purpose its plugin names, in
the web app only: no token can, so no agent can approve where its runs send data. Revoking
works from the API too.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from ninja import Field, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api.common import caller, project, require
from iki_server.api.problems import Problem
from iki_server.api.schemas import PersonRef
from iki_server.entities import registry
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.runs import catalog, outside, secrets
from iki_server.runs.models import ApiApproval
from iki_spec.model import OutsideApi

router = Router(tags=["Outside APIs"])


class ApiCredentialOut(Schema):
    key: str = Field(description="Where administrators set it: its key in the run secrets.")
    set: bool = Field(description="Whether this deployment has set it. Its value is never shown.")
    header: str = Field(description="The header the egress proxy sends it in.")
    description: str


class ApiActionOut(Schema):
    action: str
    label: str
    statement: str = Field(description="What people launching it are told is sent.")


class ApiPriceOut(Schema):
    per_call: float | None
    usage: dict[str, float] = Field(description="The price of each unit of usage it reports.")
    currency: str


class ApiApprovalOut(Schema):
    project: UUID
    project_name: str
    host: str = Field(description="The host approved.")
    purpose: str = Field(description="The purpose approved.")
    note: str
    approved_by: PersonRef
    approved_at: datetime
    current: bool = Field(
        description="Whether it still counts: the API names the host and purpose approved. "
        "Once it names others, it must be approved again."
    )


class OutsideApiOut(Schema):
    api: str = Field(description="By qualified name, such as `iki/protein.fold`.")
    plugin: str
    label: str
    host: str = Field(description="Empty while the deployment hasn't set it.")
    purpose: str
    credential: ApiCredentialOut | None
    jobs: bool = Field(description="Runs can park on its jobs, polled without a worker.")
    webhook: bool = Field(description="It calls back when a job is done, signed.")
    price: ApiPriceOut | None = Field(description="This deployment's price for its calls.")
    actions: list[ApiActionOut] = Field(description="The actions that call it.")
    approvals: list[ApiApprovalOut] = Field(
        description="The projects it's approved for, that you can see."
    )


def _apis() -> dict[str, OutsideApi]:
    return {
        api.qualified_name: api
        for name, plugin in registry.plugins().items()
        if name not in registry.detached()
        for api in plugin.apis.values()
    }


def _api(name: str) -> OutsideApi:
    found = _apis().get(name)
    if found is None:
        raise Problem(404, f"No outside API `{name}` is installed.")
    return found


def _out(api: OutsideApi, approvals: list[ApiApproval]) -> dict[str, Any]:
    price = catalog.compute().prices.get(api.qualified_name)
    credential = api.credential
    return {
        "api": api.qualified_name,
        "plugin": api.plugin,
        "label": api.label,
        "host": api.host,
        "purpose": api.purpose,
        "credential": {
            "key": secrets.key(api.plugin, credential.secret),
            "set": secrets.is_set(api.plugin, credential.secret),
            "header": credential.header,
            "description": credential.description,
        }
        if credential is not None
        else None,
        "jobs": api.jobs is not None,
        "webhook": api.jobs is not None and api.jobs.webhook is not None,
        "price": {"per_call": price.per_call, "usage": price.usage, "currency": price.currency}
        if price is not None
        else None,
        "actions": [
            {
                "action": action.qualified_name,
                "label": action.label or action.name,
                "statement": outside.statement(action, use, found),
            }
            for action in catalog.actions().values()
            for use, found in outside.used(action)
            if found.qualified_name == api.qualified_name
        ],
        "approvals": [
            {
                "project": a.project_id,
                "project_name": a.project.name,
                "host": a.host,
                "purpose": a.purpose,
                "note": a.note,
                "approved_by": a.approved_by,
                "approved_at": a.approved_at,
                "current": a.host == api.host and a.purpose == api.purpose,
            }
            for a in approvals
            if a.api == api.qualified_name
        ],
    }


def _approvals(projects: Any = None) -> list[ApiApproval]:  # noqa: ANN401 - a QuerySet
    found = ApiApproval.objects.filter(revoked_at__isnull=True)
    if projects is not None:
        found = found.filter(project__in=projects)
    return list(found.select_related("project", "approved_by").order_by("project__name"))


@router.get("/outside-apis", response=list[OutsideApiOut], summary="Outside APIs")
def list_outside_apis(request: HttpRequest) -> list[dict[str, Any]]:
    """Every outside API installed plugins call, with where data goes, why, the actions that
    send it and what each sends, the key's state, the price, and each project's approval.
    Org Admins see every project's approvals."""
    me = caller(request)
    require(me, Action.OUTSIDE_APIS_MANAGE)
    approvals = _approvals()
    return [
        _out(api, approvals) for api in sorted(_apis().values(), key=lambda a: a.qualified_name)
    ]


@router.get(
    "/projects/{project_id}/outside-apis",
    response=list[OutsideApiOut],
    summary="Outside APIs, and whether each is approved for a project",
)
def list_project_outside_apis(request: HttpRequest, project_id: UUID) -> list[dict[str, Any]]:
    """Every installed outside API, with the project's approval if it has one: what its
    members see before launching an action that sends data outside."""
    me = caller(request)
    found = project(me, project_id)
    approvals = _approvals([found.pk])
    return [
        _out(api, approvals) for api in sorted(_apis().values(), key=lambda a: a.qualified_name)
    ]


class ApiApprovalIn(Schema):
    project: UUID
    note: str = Field("", max_length=2000, description="Why, for the record.")


def _manage(me: User, target: Project, *, web: bool) -> None:
    require(me, Action.OUTSIDE_APIS_MANAGE)
    if web and permissions.token_limits(me) is not None:
        raise Problem(
            403,
            "Approve outside APIs in Iki’s web app. Tokens can’t, so no agent can approve "
            "where its runs send data.",
            code="web_only",
        )
    if target.archived_at is not None:
        raise Problem(409, f"{target.name} is archived.", code="refused")


@router.post(
    "/outside-apis/{qualified:name}/approvals",
    response={201: ApiApprovalOut},
    summary="Approve an outside API for a project",
)
def approve_outside_api(
    request: HttpRequest, name: str, payload: ApiApprovalIn
) -> Status[dict[str, Any]]:
    """Lets runs in the project send data to the API, for the host and purpose it names now.
    Only Org Admins, in the web app. Audited as `outside_api.approved`."""
    me = caller(request)
    api = _api(name)
    target = project(me, payload.project)
    _manage(me, target, web=True)
    made = outside.approve(me, target, api, note=payload.note)
    return Status(201, _out(api, [made])["approvals"][0])


@router.delete(
    "/outside-apis/{qualified:name}/approvals/{project_id}",
    response={204: None},
    summary="Revoke an outside API's approval for a project",
)
def revoke_outside_api(request: HttpRequest, name: str, project_id: UUID) -> Status[None]:
    """Runs in the project can no longer send data to the API: launches are refused,
    running attempts' next calls too, and parked runs end, their jobs cancelled at the API
    where it says how. Audited as `outside_api.revoked`."""
    me = caller(request)
    target = project(me, project_id)
    _manage(me, target, web=False)
    outside.revoke(me, target, name)
    return Status(204, None)


@router.post(
    "/outside-apis/callbacks/{job_id}/{api}/{token}",
    auth=None,
    include_in_schema=False,
    response={202: None},
)
def outside_api_callback(
    request: HttpRequest, job_id: UUID, api: str, token: str
) -> HttpResponse | Status[None]:
    """An outside API saying a run's job may be done (ADR 0130). Signed with its webhook
    secret, it makes Iki poll the job now; what the run resumes with is what the poll
    answers, never this body."""
    headers = dict(request.headers.items())
    try:
        outside.callback(job_id, api, token, headers, request.body)
    except outside.BadCallback as bad:
        raise Problem(401 if "signature" in str(bad) else 404, str(bad)) from None
    return Status(202, None)
