"""The installed assay starters and their project-specific setup (ADR 0198)."""

from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.accounts.models import User
from iki_server.api import common
from iki_server.api.problems import Problem
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.setup import starters

router = Router(tags=["starters"])

StarterFlow = Literal["research", "regulated_qc"]


class StarterFlowOut(Schema):
    id: StarterFlow
    label: str
    result_type: str = Field(description="Qualified type for a requested result in this flow.")
    required_roles: list[str] = Field(description="Qualified lab roles needed to run this flow.")


class StarterRoleOut(Schema):
    id: str = Field(description="Qualified lab role.")
    label: str
    minimum_project_role: Literal["viewer", "editor"] = Field(
        description="The least project role a member needs to carry out this lab role."
    )


class AppliedStarterOut(Schema):
    version: str
    flow: StarterFlow
    requester_project: UUID | None = Field(
        description="Original requester for an assay project Admin who may edit that project."
    )


class StarterOut(Schema):
    id: str
    version: str
    label: str
    description: str
    flows: list[StarterFlowOut]
    roles: list[StarterRoleOut]
    types: list[str] = Field(description="Qualified types the starter uses.")
    applied: AppliedStarterOut | None = Field(
        description="Current choice in the requested project, if applied there."
    )


class StarterSetupIn(Schema):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, description="Installed starter version shown at setup.")
    project: UUID = Field(description="The assay service project. Requires its Admin role.")
    requester_project: UUID = Field(
        description="A different project in which the caller can create the example request."
    )
    flow: StarterFlow
    role_assignments: dict[str, list[UUID]] = Field(
        description="Each qualified lab role and the service project membership IDs receiving it."
    )


class StarterApplyIn(StarterSetupIn):
    plan: str = Field(
        pattern="^[0-9a-f]{64}$",
        description="The `plan` its preview returned: what you reviewed. If what applying does "
        "has changed since, nothing is applied (409 `starter_plan_changed`, with the `plan` "
        "it would apply now).",
    )


class StarterSetupOut(Schema):
    starter: str
    version: str
    project: UUID
    requester_project: UUID
    flow: StarterFlow
    result_type: str
    changed: bool = Field(
        description="Whether applying this choice would change anything (preview), or did (apply)."
    )
    role_assignments: dict[str, list[UUID]]
    grants_to_add: dict[str, list[UUID]] = Field(
        description="Lab role grants missing when the setup was checked, by membership ID."
    )
    examples_to_create: list[str] = Field(
        description="Names of examples missing when the setup was checked."
    )
    examples: dict[str, UUID] = Field(
        description="IDs of example records already present at preview or available after setup."
    )
    plan: str = Field(
        description="Identifies this plan: what the setup was, and every change applying makes. "
        "Send it to apply what was reviewed."
    )


@router.get("/starters", response=list[StarterOut], summary="List installed starters")
def list_starters(request: HttpRequest, project: UUID | None = None) -> list[dict[str, Any]]:
    """Include the current flow and version when the caller supplies a readable project."""
    me = common.caller(request)
    common.require_scope(me, "read")
    found = common.project(me, project, Action.PROJECT_READ) if project else None
    return starters.list_starters(found, by=me)


def _projects(me: User, payload: StarterSetupIn) -> tuple[Project, Project]:
    service = common.project(me, payload.project, Action.PROJECT_MANAGE)
    requester = common.project(me, payload.requester_project, Action.PROJECT_EDIT)
    return service, requester


@router.post(
    "/starters/{starter}/preview", response=StarterSetupOut, summary="Preview a starter choice"
)
def preview_starter(request: HttpRequest, starter: str, payload: StarterSetupIn) -> dict[str, Any]:
    """Check the installed version, projects, roles and flow without changing either project."""
    me = common.caller(request)
    service, requester = _projects(me, payload)
    return starters.preview(
        service,
        starter,
        payload.version,
        payload.flow,
        payload.role_assignments,
        requester_project=requester,
        by=me,
    )


@router.post(
    "/starters/{starter}/apply", response=StarterSetupOut, summary="Apply a starter to a project"
)
def apply_starter(request: HttpRequest, starter: str, payload: StarterApplyIn) -> dict[str, Any]:
    """Apply the plan a preview returned: create the examples and grant the chosen roles.
    If what applying does changed since the preview, as when another Admin removed a grant or
    applied it first, nothing changes and it answers 409 `starter_plan_changed` with `plan`,
    what applying would do now, for review (ADR 0207). Reapplying a setup that's in place
    changes nothing. Changing the flow selects the result type for future fits. Existing
    results keep their flow."""
    me = common.caller(request)
    service, requester = _projects(me, payload)
    try:
        return starters.apply(
            service,
            starter,
            payload.version,
            payload.flow,
            payload.role_assignments,
            requester_project=requester,
            by=me,
            plan=payload.plan,
        )
    except starters.PlanChanged as changed:
        raise Problem(
            409, str(changed), code="starter_plan_changed", extensions={"plan": changed.plan}
        ) from None
