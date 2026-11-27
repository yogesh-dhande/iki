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
from iki_server.api.graph import FollowedName
from iki_server.api.pagination import PageParams, page, page_sorted
from iki_server.api.problems import Problem
from iki_server.api.runs import Tier
from iki_server.api.schemas import Page, PersonRef
from iki_server.entities import access
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.runs import catalog, triggers
from iki_server.runs.models import ProjectTrigger, TriggerFiring
from iki_spec.model import ACCOUNT_EVENTS, Trigger

router = Router(tags=["triggers"])

Event = Literal[
    "entity.created",
    "entity.registered",
    "entity.state_changed",
    "entity.updated",
    "request.received",
    "schedule",
]


class RelatedOut(Schema):
    direction: Literal["downstream", "upstream"] = Field(
        description="`downstream`: what was derived from it; `upstream`: what it was derived from."
    )
    depth: int = Field(description="How many edges away, at most.")
    edges: list[FollowedName] = Field(description="The kinds of edge it walks; empty for all.")


class TriggerOut(Schema):
    name: str = Field(description="Its qualified name, such as `protein.weigh_new_proteins`.")
    plugin: str
    label: str
    description: str | None
    on: Event = Field(
        description="What fires it: an entity of its type created, registered (ADR 0115), "
        "set to another state in one of its state fields (ADR 0125) or changed, a request of "
        "its type sent to the project as a service (ADR 0232), or its schedule."
    )
    runs_as_account: bool = Field(
        False,
        description="Its runs are launched as a service account the Admin who turns it on "
        "chooses, on their authority: on a schedule, or as a request arrives, since nobody in "
        "the project made it happen (ADR 0081, ADR 0232).",
    )
    type: str = Field(
        description="The entity type it fires for, with its subtypes; empty on a schedule."
    )
    schedule: str = Field(
        description="On a schedule: when it runs, in cron's five fields. Empty otherwise."
    )
    timezone: str = Field(description="On a schedule: the timezone of its times.")
    fields: list[str] = Field(
        description="For `entity.updated`: it fires only when one of these changed. Empty for "
        "any change."
    )
    field: str = Field(
        description="For `entity.state_changed`: the state field it watches. Empty otherwise."
    )
    states: list[str] = Field(
        description="For `entity.state_changed`: it fires only when the field is set to one "
        "of these. Empty for any of its states."
    )
    action: str = Field(description="The qualified name of the action it runs.")
    action_label: str
    recheck: bool = Field(False, description="Recheck an edited record on the editor’s authority.")
    input: str = Field(description="The action's input that takes the entity; empty on a schedule.")
    related: dict[str, RelatedOut] = Field(
        description="Other inputs it gives the entities related to the one that fired it "
        "(ADR 0123), by input: each, every one of the input's type in the project that way of "
        "it. All of them or none: past the most an input takes, it fails."
    )
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
        "runs_as_account": spec.on in ACCOUNT_EVENTS,
        "type": spec.type,
        "schedule": spec.schedule,
        "timezone": spec.timezone or ("UTC" if spec.schedule else ""),
        "fields": list(spec.fields),
        "field": spec.state_field,
        "states": list(spec.states),
        "action": f"{spec.plugin}.{spec.run}",
        "recheck": bool(spec.recheck),
        "action_label": action.label if action is not None else spec.run,
        "input": spec.input,
        "related": {
            name: {"direction": r.direction, "depth": r.depth, "edges": list(r.edges)}
            for name, r in spec.related.items()
        },
        "params": spec.params,
        "tier": action.tier if action is not None else None,
    }


def _page(params: PageParams) -> dict[str, Any]:
    return page_sorted(triggers.triggers().items(), params, by=("name",), key=lambda t: (t[0],))


@router.get("/triggers", response=Page[TriggerOut], summary="Installed triggers")
def list_triggers(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """By name."""
    caller(request)
    found = _page(params)
    return {**found, "items": [_trigger_out(t) for _, t in found["items"]]}


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
        description="Who turned it on last: whom an entity trigger's runs are launched as, and on "
        "whose authority a service account's are. Null if nobody ever has."
    )
    enabled_at: datetime | None
    run_as: PersonRef | None = Field(
        description="On a schedule or a request's arrival: the service account its runs are "
        "launched as."
    )
    next_run_at: datetime | None = Field(
        description="On a schedule, while it's on: when it next runs."
    )
    run_as_choices: list[PersonRef] = Field(
        description="For one whose runs are launched as a service account, for the project's "
        "Admins: the accounts it may run as, members who can run its action here."
    )
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


def _project_triggers_out(
    me: User, found: Project, specs: list[tuple[str, Trigger]]
) -> list[dict[str, Any]]:
    rows = triggers.in_project(found)
    last = _last_fired(found)
    web = permissions.token_limits(me) is None
    admin = permissions.can(me, permissions.Action.PROJECT_TRIGGERS_MANAGE, found)
    out = []
    for name, spec in specs:
        row = rows.get(name)
        why = triggers.why_not_enable(me, found, spec) if admin else None
        # One run as a service account can be turned on again to choose another account.
        mine = (
            row is not None
            and row.enabled
            and row.enabled_by_id == me.pk
            and spec.on not in ACCOUNT_EVENTS
        )
        enabled = row.enabled if row is not None else bool(spec.recheck)
        out.append(
            {
                **_trigger_out(spec),
                **_state(row),
                "enabled": enabled,
                "last_fired_at": last.get(name),
                "run_as_choices": triggers.run_as_choices(found, spec) if admin else [],
                "why_not_enable": why,
                "can": {
                    "enable": admin
                    and web
                    and why is None
                    and not mine
                    and not (spec.recheck and enabled),
                    "disable": admin and enabled,
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
            "run_as": None,
            "next_run_at": None,
        }
    return {
        "enabled": row.enabled,
        "enabled_by": row.enabled_by,
        "enabled_at": row.enabled_at,
        "run_as": row.run_as,
        "next_run_at": row.next_run_at if row.enabled else None,
        "disabled_by": None if row.enabled else row.disabled_by,
        "disabled_at": None if row.enabled else row.disabled_at,
        "disabled_reason": "" if row.enabled else row.disabled_reason,
    }


@router.get(
    "/projects/{project_id}/triggers",
    response=Page[ProjectTriggerOut],
    summary="Triggers in a project",
)
def project_triggers(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    """Every installed trigger, by name, and whether it's on in the project, as whom."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_READ)
    paged = _page(params)
    return {**paged, "items": _project_triggers_out(me, found, paged["items"])}


def _one(me: User, found: Project, name: str) -> dict[str, Any]:
    spec = triggers.trigger(name)
    if spec is None:
        raise Problem(404, f"There’s no trigger {name}.")
    return _project_triggers_out(me, found, [(name, spec)])[0]


def _installed(name: str) -> Trigger:
    spec = triggers.trigger(name)
    if spec is None:
        raise Problem(404, f"No trigger `{name}` is installed.")
    return spec


class EnableIn(Schema):
    run_as: UUID | None = Field(
        None,
        description="For a trigger whose runs are launched as a service account (on a schedule, "
        "or as a request arrives): the account, one of its `run_as_choices`.",
    )


@router.post(
    "/projects/{project_id}/triggers/{qualified:name}/enable",
    response=ProjectTriggerOut,
    summary="Turn a trigger on in a project",
)
def enable_trigger(
    request: HttpRequest, project_id: UUID, name: str, payload: EnableIn | None = None
) -> dict[str, Any]:
    """From now on an entity trigger's runs are launched as you, and a scheduled one's, or one
    on a request's arrival, as the service account you choose (`run_as`), on your authority:
    either way you must be able to run its action here. Only in the web app: no token, and so
    no agent, can turn one on."""
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
    run_as = None
    if payload is not None and payload.run_as is not None:
        run_as = User.objects.filter(pk=payload.run_as).first()
        if run_as is None:
            raise Problem(
                422,
                "Some fields are invalid.",
                errors=[{"pointer": "#/run_as", "detail": "No such service account."}],
            )
    try:
        triggers.enable(me, found, name, run_as=run_as)
    except PermissionDenied:
        raise Problem(403) from None
    except Refused as error:
        raise Problem(409, str(error), code="refused") from None
    return _one(me, found, name)


class DisableIn(Schema):
    reason: str = Field("", max_length=2000)


@router.post(
    "/projects/{project_id}/triggers/{qualified:name}/disable",
    response=ProjectTriggerOut,
    summary="Turn a trigger off in a project",
)
def disable_trigger(
    request: HttpRequest, project_id: UUID, name: str, payload: DisableIn | None = None
) -> dict[str, Any]:
    """Changes after this fire nothing; runs it already launched carry on."""
    me = caller(request)
    found = project(me, project_id, permissions.Action.PROJECT_TRIGGERS_MANAGE)
    _installed(name)
    try:
        triggers.disable(me, found, name, reason=payload.reason if payload else "")
    except Refused as error:
        raise Problem(
            422, "Some fields are invalid.", errors=[{"pointer": "#/reason", "detail": str(error)}]
        ) from None
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
    editor: PersonRef | None = None
    id: UUID
    trigger: str = Field(description="The trigger's qualified name.")
    label: str
    event: Event
    entity: FiredEntityOut | None = Field(description="The visible entity whose change fired it.")
    revision: int | None = Field(
        description="The revision of the visible entity whose change fired it."
    )
    scheduled_for: datetime | None = Field(description="On a schedule: the time it was for.")
    since: datetime | None = Field(
        description="On a schedule: the time the last run that succeeded was for, which its "
        "command carries on from. Null for the first."
    )
    state: Literal["pending", "launched", "failed", "skipped"] = Field(
        description="`pending` until its run is launched, just after the change or the time; "
        "`failed` if it couldn't be, or its related entities were too many to give an input "
        "or took too long to find; `skipped` if the trigger was turned off or the entity "
        "archived first, if nothing was related for an input that needs some, or, on a "
        "schedule, while its last run hadn't finished."
    )
    related: dict[str, int] | None = Field(
        description="For a trigger that gives inputs related entities (ADR 0123): how many "
        "each was given, by input, once found."
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
    result = page(rows, params, newest_first=True)
    firings = result["items"]
    readable = set(
        access.traced(me).filter(pk__in=[f.entity_id for f in firings]).values_list("pk", flat=True)
    )
    for firing in firings:
        if firing.entity_id and firing.entity_id not in readable:
            firing.entity = None
            firing.revision = None
            if firing.error:
                firing.error = {
                    **firing.error,
                    "message": "The trigger concerned an entity you can’t read.",
                }
    return result
