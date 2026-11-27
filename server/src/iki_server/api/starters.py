"""The installed starters and their project-specific setup (ADRs 0198 and 0247)."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.api import common
from iki_server.api.problems import Problem
from iki_server.org.permissions import Action
from iki_server.setup import declared_starters, starters

router = Router(tags=["starters"])

StarterFlow = str


class StarterStepOut(Schema):
    state: str
    label: str
    slots: list[str] = Field(description="The slots whose roles may set it: any one of them.")


class StarterFlowOut(Schema):
    id: StarterFlow
    label: str
    result_type: str = Field(
        description="Qualified result type whose states this flow's records pass through."
    )
    states: list[str] | None = Field(
        default=None,
        description="The result type's states in this flow, where its one state field has a "
        "route per flow. Null: all of them.",
    )
    required_roles: list[str] = Field(description="Qualified lab roles needed to run this flow.")
    independent_roles: list[str] = Field(
        description="Required roles whose signatures must come from different people: each "
        "group of them needs at least as many people as it has roles."
    )
    summary: str = Field(description="What the flow asks of people, in a sentence or two.")
    steps: list[StarterStepOut] = Field(
        description="For a plugin's starter: each state a record must be moved to, after "
        "the first, with the slots whose roles may set it.",
    )
    apart: list[list[str]] = Field(
        description="For a plugin's starter: the pairs of `steps` whose people must differ, "
        "as the procedure's `independent_of` says (ADR 0301). The people chosen must be "
        "able to sign every step with each pair apart.",
    )


class StarterRoleOut(Schema):
    id: str = Field(description="Qualified lab role.")
    label: str
    minimum_project_role: Literal["viewer", "editor"] = Field(
        description="The least project role a member needs to carry out this lab role."
    )


class StarterSlotOut(Schema):
    id: str = Field(description="The procedure's slot, such as `reviewer` (ADR 0304).")
    label: str
    description: str
    role: str = Field(
        description="The qualified lab role that plays it: in the project, as its current "
        "choice bound it, else as the starter does."
    )
    starter_role: str = Field(description="The role the starter binds it to.")
    minimum_project_role: Literal["viewer", "editor"]


class AppliedStarterOut(Schema):
    version: str
    flow: StarterFlow


class StarterChoiceOut(Schema):
    procedure: str = Field(description="The review procedure chosen, as `plugin.name@version`.")
    flow: StarterFlow
    version: str | None = Field(
        description="The version of the starter that made it; null if it was chosen "
        "through `PUT /review-choices` or by another starter."
    )
    slots: dict[str, str] = Field(description="Each slot to the lab role that plays it.")
    chosen_by: str
    chosen_at: datetime
    reason: str


class StarterOut(Schema):
    id: str = Field(
        description="A plugin's starter, qualified (`iki/proteomics.proteomics_search_review`), "
        "or one of the server's own recipes (`dose-response`)."
    )
    version: str
    label: str
    description: str
    procedure: str | None = Field(
        description="The review procedure it applies, as `plugin.name@version` (ADR 0303).",
    )
    suggested: str | None = Field(
        description="The flow its plugin suggests, shown chosen at first."
    )
    governs: str = Field(
        description="The records the project's choice applies to from now on, such as "
        "`release cases`. Those already opened keep the flow they were opened with."
    )
    flows: list[StarterFlowOut]
    roles: list[StarterRoleOut]
    types: list[str] = Field(description="Qualified types the starter uses.")
    applied: AppliedStarterOut | None = Field(
        description="Current choice in the requested project, if applied there."
    )
    slots: list[StarterSlotOut] = Field(
        description="For a plugin's starter: the parts its flows have. None for a recipe."
    )
    chosen: StarterChoiceOut | None = Field(
        description="For a plugin's starter: the project's current choice for its type, "
        "whether this starter, another or `PUT /review-choices` made it. `applied` is set "
        "when this starter did."
    )
    default: StarterChoiceOut | None = Field(
        description="For a plugin's starter: the deployment's default for its type, whoever "
        "set it. It applies in every project that hasn't chosen."
    )


class StarterSetupIn(Schema):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, description="Installed starter version shown at setup.")
    project: UUID = Field(description="The project to set up. Requires its Admin role.")
    flow: StarterFlow
    role_assignments: dict[str, list[UUID]] = Field(
        description="Each qualified lab role and the project membership IDs receiving it."
    )
    slots: dict[str, str] = Field(
        default_factory=dict,
        description="For a plugin's starter: each slot to the qualified lab role that plays "
        "it in this project, where not the starter's (ADR 0304).",
    )


class StarterDefaultIn(Schema):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, description="Installed starter version shown at setup.")
    flow: StarterFlow
    slots: dict[str, str] = Field(
        default_factory=dict,
        description="Each slot to the qualified lab role that plays it, where not the "
        "starter's (ADR 0304).",
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="Why: a deployment's default is a controlled procedure change.",
    )


class StarterDefaultClearIn(Schema):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class StarterDefaultOut(Schema):
    starter: str
    default: StarterChoiceOut | None


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
    flow: StarterFlow
    result_type: str
    changed: bool = Field(
        description="Whether applying this choice would change anything (preview), or did (apply)."
    )
    role_assignments: dict[str, list[UUID]]
    slots: dict[str, str] = Field(
        default_factory=dict,
        description="For a plugin's starter: each slot to the lab role that plays it.",
    )
    grants_to_add: dict[str, list[UUID]] = Field(
        description="Lab role grants missing when the setup was checked, by membership ID."
    )
    plan: str = Field(
        description="Identifies this plan: what the setup was, and every change applying makes. "
        "Send it to apply what was reviewed."
    )
    replaces: dict[str, Any] | None = Field(
        default=None,
        description="The project's current choice for the type, which applying replaces, "
        "where another starter or the API made it.",
    )


@router.get("/starters", response=list[StarterOut], summary="List installed starters")
def list_starters(request: HttpRequest, project: UUID | None = None) -> list[dict[str, Any]]:
    """Include the current flow and version when the caller supplies a readable project."""
    me = common.caller(request)
    common.require_scope(me, "read")
    found = common.project(me, project, Action.PROJECT_READ) if project else None
    return starters.list_starters(found)


def _recipe(starter: str, payload: StarterSetupIn) -> bool:
    """Whether ``starter`` is one of the server's own recipes rather than a plugin's starter
    (a qualified name), which alone has slots to bind (ADR 0304)."""
    if "." in starter:
        return False
    if payload.slots:
        raise Problem(
            422,
            "Only a plugin's starter has slots to bind: leave out `slots`.",
            code="validation_failed",
            errors=[{"pointer": "/slots", "detail": "This starter has no slots."}],
        )
    return True


@router.post(
    "/starters/{path:name}/preview", response=StarterSetupOut, summary="Preview a starter choice"
)
def preview_starter(request: HttpRequest, name: str, payload: StarterSetupIn) -> dict[str, Any]:
    """Preview the review flow, who plays each of its parts and the role grants for the
    project, and which current choice for its type it replaces."""
    me = common.caller(request)
    project = common.project(me, payload.project, Action.PROJECT_MANAGE)
    args = (project, name, payload.version, payload.flow, payload.role_assignments)
    if _recipe(name, payload):
        return starters.preview(*args, by=me)
    return declared_starters.preview(*args, by=me, slots=payload.slots)


@router.post(
    "/starters/{path:name}/apply",
    response=StarterSetupOut,
    summary="Apply a starter to a project",
)
def apply_starter(request: HttpRequest, name: str, payload: StarterApplyIn) -> dict[str, Any]:
    """Apply the reviewed flow and role grants to the project.
    If what applying does changed since the preview, as when another Admin removed a grant or
    applied it first, nothing changes and it answers 409 `starter_plan_changed` with `plan`,
    what applying would do now, for review (ADR 0207). Reapplying a setup that's in place
    changes nothing. A plugin's starter is the project's choice for its type: new records
    are pinned to it, and records already made keep their procedure (ADR 0302)."""
    me = common.caller(request)
    project = common.project(me, payload.project, Action.PROJECT_MANAGE)
    args = (project, name, payload.version, payload.flow, payload.role_assignments)
    try:
        if _recipe(name, payload):
            return starters.apply(*args, by=me, plan=payload.plan)
        return declared_starters.apply(*args, by=me, plan=payload.plan, slots=payload.slots)
    except starters.PlanChanged as changed:
        raise Problem(
            409, str(changed), code="starter_plan_changed", extensions={"plan": changed.plan}
        ) from None


@router.post(
    "/starters/{path:name}/default",
    response=StarterDefaultOut,
    summary="Set a starter as the deployment's default",
)
def set_default(request: HttpRequest, name: str, payload: StarterDefaultIn) -> dict[str, Any]:
    """For Org Admins: every project that hasn't chosen pins new records of the starter's
    type to this flow, its slots played by these roles (ADR 0302). Needs a reason, kept
    with the change in the audit log. Projects that chose keep their choice, and records
    already made keep their procedure."""
    me = common.caller(request)
    common.require_scope(me, "write")
    return declared_starters.set_default(
        me, name, payload.version, payload.flow, slots=payload.slots, reason=payload.reason
    )


@router.post(
    "/starters/{path:name}/default/clear",
    response=StarterDefaultOut,
    summary="Clear the deployment's default a starter set",
)
def clear_default(
    request: HttpRequest, name: str, payload: StarterDefaultClearIn
) -> dict[str, Any]:
    """For Org Admins, with a reason. Projects that haven't chosen can't create the type's
    records until they do (409 `procedure_not_chosen`)."""
    me = common.caller(request)
    common.require_scope(me, "write")
    return declared_starters.clear_default(me, name, reason=payload.reason)
