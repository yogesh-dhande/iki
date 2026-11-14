"""Triggers, and what they fired (ADR 0080).

``GET /triggers`` lists the triggers installed plugins provide. None runs until a project's
Admin turns it on there (``POST /projects/{id}/triggers/{name}/enable``), in the web app:
its runs are then launched as them, so no token, and so no agent, can turn one on. Anyone
who can read the project sees which are on, and what each change fired.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Max
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema

from iki_server.accounts.models import User
from iki_server.api.common import caller, project
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.runs import Tier
from iki_server.api.schemas import Page, PersonRef
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import catalog, triggers
from iki_server.runs.models import ProjectTrigger, TriggerFiring
from iki_spec.model import Trigger

router = Router(tags=["triggers"])

Event = Literal["entity.created", "entity.updated"]


class TriggerOut(Schema):
    name: str = Field(description="Its qualified name, such as `protein.weigh_new_proteins`.")
    plugin: str
    label: str
    description: str | None
    on: Event = Field(description="What fires it: an entity of its type created, or changed.")
    type: str = Field(description="The entity type it fires for, with its subtypes.")
    fields: list[str] = Field(
        description="For `entity.updated`: it fires only when one of these changed. Empty for "
        "any change."
    )
    action: str = Field(description="The qualified name of the action it runs on the entity.")
    action_label: str
    input: str = Field(description="The action's input that takes the entity.")
    params: dict[str, Any]
    tier: Tier | None = Field(
        description="Where its runs run; on the container and VM tiers each waits for the "
        "approval of the Admin who turned it on. Null once its action isn't installed."
    )


def _trigger_out(spec: Trigger) -> dict[str, Any]:
    action = catalog.action(f"{spec.plugin}.{spec.run}")
    return {
        "name": spec.qualified_name,
        "plugin": spec.plugin,
        "label": spec.label,
        "description": spec.description,
        "on": spec.on,
        "type": spec.type,
        "fields": list(spec.fields),
        "action": f"{spec.plugin}.{spec.run}",
        "action_label": action.label if action is not None else spec.run,
        "input": spec.input,
        "params": spec.params,
        "tier": action.tier if action is not None else None,
    }


@router.get("/triggers", response=list[TriggerOut], summary="Installed triggers")
def list_triggers(request: HttpRequest) -> list[dict[str, Any]]:
    caller(request)
    return [_trigger_out(t) for t in triggers.triggers().values()]


# --- In a project ------------------------------------------------------------------------------


class ProjectTriggerCan(Schema):
    enable: bool = Field(
        description="You may turn it on, or on as you: you're an Admin who can run its action "
        "here, in the web app."
    )
    disable: bool = Field(description="You may turn it off: you're one of the project's Admins.")


class ProjectTriggerOut(TriggerOut):
    enabled: bool
    enabled_by: PersonRef | None = Field(
        description="Who its runs are launched as: whoever turned it on last. Null if nobody "
        "ever has."
    )
    enabled_at: datetime | None
    disabled_by: PersonRef | None = Field(
        description="Who turned it off. Null while it's on, or when Iki did (see "
        "`disabled_reason`)."
    )
    disabled_at: datetime | None
    disabled_reason: str = Field(
        description="Why Iki turned it off, if it did: its runs could no longer be launched as "
        "the Admin who turned it on."
    )
    last_fired_at: datetime | None
    why_not_enable: str | None = Field(
        description="Why you can't turn it on, for one of the project's Admins who can't."
    )
    can: ProjectTriggerCan


def _project_triggers_out(me: User, found: Project) -> list[dict[str, Any]]:
    rows = triggers.in_project(found)
    last = _last_fired(found)
    web = permissions.token_limits(me) is None
    admin = permissions.can(me, permissions.Action.PROJECT_TRIGGERS_MANAGE, found)
    out = []
    for name, spec in triggers.triggers().items():
        row = rows.get(name)
        why = triggers.why_not_enable(me, found, spec) if admin else None
        mine = row is not None and row.enabled and row.enabled_by_id == me.pk
        out.append(
            {
                **_trigger_out(spec),
                **_state(row),
                "last_fired_at": last.get(name),
                "why_not_enable": why,
                "can": {
                    "enable": admin and web and why is None and not mine,
                    "disable": admin and row is not None and row.enabled,
                },
            }
        )
    return out


def _last_fired(found: Project) -> dict[str, datetime]:
    return dict(
        TriggerFiring.objects.filter(trigger__project=found)
        .values("trigger__trigger")
        .annotate(last=Max("created_at"))
        .values_list("trigger__trigger", "last")
    )


def _state(row: ProjectTrigger | None) -> dict[str, Any]:
    if row is None:
        return {
            "enabled": False,
            "enabled_by": None,
            "enabled_at": None,
            "disabled_by": None,
            "disabled_at": None,
            "disabled_reason": "",
        }
    return {
        "enabled": row.enabled,
        "enabled_by": row.enabled_by,
        "enabled_at": row.enabled_at,
        "disabled_by": None if row.enabled else row.disabled_by,
        "disabled_at": None if row.enabled else row.disabled_at,
        "disabled_reason": "" if row.enabled else row.disabled_reason,
    }


@router.get(
    "/projects/{project_id}/triggers",
    response=list[ProjectTriggerOut],
    summary="Triggers in a project",
)
def project_triggers(request: HttpRequest, project_id: UUID) -> list[dict[str, Any]]:
    """Every installed trigger, and whether it's on in the project, as whom."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_READ)
    return _project_triggers_out(me, found)


def _one(me: User, found: Project, name: str) -> dict[str, Any]:
    return next(t for t in _project_triggers_out(me, found) if t["name"] == name)


def _installed(name: str) -> Trigger:
    spec = triggers.trigger(name)
    if spec is None:
        raise Problem(404, f"No trigger `{name}` is installed.")
    return spec


@router.post(
    "/projects/{project_id}/triggers/{name}/enable",
    response=ProjectTriggerOut,
    summary="Turn a trigger on in a project",
)
def enable_trigger(request: HttpRequest, project_id: UUID, name: str) -> dict[str, Any]:
    """From now on its runs are launched as you, so you must be able to run its action here.
    Only in the web app: no token, and so no agent, can turn one on."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_TRIGGERS_MANAGE)
    _installed(name)
    if permissions.token_limits(me) is not None:
        raise Problem(
            403,
            "Turn triggers on in Iki’s web app. Their runs are launched as you, so tokens "
            "can’t, and no agent can turn one on for you.",
            code="web_only",
        )
    try:
        triggers.enable(me, found, name)
    except PermissionDenied:
        raise Problem(403) from None
    except Refused as error:
        raise Problem(409, str(error), code="not_allowed") from None
    return _one(me, found, name)


@router.post(
    "/projects/{project_id}/triggers/{name}/disable",
    response=ProjectTriggerOut,
    summary="Turn a trigger off in a project",
)
def disable_trigger(request: HttpRequest, project_id: UUID, name: str) -> dict[str, Any]:
    """Changes after this fire nothing; runs it already launched carry on."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_TRIGGERS_MANAGE)
    _installed(name)
    triggers.disable(me, found, name)
    return _one(me, found, name)


# --- Firings -----------------------------------------------------------------------------------


class FiredEntityOut(Schema):
    id: UUID
    hrid: str
    type: str


class FiredRunOut(Schema):
    id: UUID
    hrid: str
    status: str


class FiringErrorOut(Schema):
    code: str
    message: str


class FiringOut(Schema):
    id: UUID
    trigger: str = Field(description="The trigger's qualified name.")
    label: str
    event: Event
    entity: FiredEntityOut
    revision: int = Field(description="The revision the change made.")
    state: Literal["pending", "launched", "failed", "skipped"] = Field(
        description="`pending` until its run is launched, just after the change; `failed` if "
        "it couldn't be; `skipped` if the trigger was turned off or the entity archived first."
    )
    run: FiredRunOut | None
    error: FiringErrorOut | None
    created_at: datetime
    fired_at: datetime | None

    @staticmethod
    def resolve_trigger(firing: TriggerFiring) -> str:
        return firing.trigger.trigger

    @staticmethod
    def resolve_label(firing: TriggerFiring) -> str:
        return triggers.label(firing.trigger)


@router.get(
    "/projects/{project_id}/trigger-firings",
    response=Page[FiringOut],
    summary="What a project's triggers fired",
)
def trigger_firings(
    request: HttpRequest,
    project_id: UUID,
    params: Query[PageParams],
    trigger: str | None = None,
) -> dict[str, Any]:
    """Newest first. `trigger` narrows it to one trigger's."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_READ)
    rows = triggers.firings(found)
    if trigger is not None:
        rows = rows.filter(trigger__trigger=trigger)
    return page(rows, params, newest_first=True)
