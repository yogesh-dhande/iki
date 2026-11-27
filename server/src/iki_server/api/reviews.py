"""Review procedures and who chose which (ADRs 0301, 0302).

- ``/review-procedures``: the installed procedures a plugin declares, with their slots
  and flows.
- ``/review-choices/{type}``: the procedure a type's new records are pinned to: the
  deployment's default, which an Org Admin sets or clears with a reason, and with
  ``project``, the project's own choice, which its Admin makes. ``effective`` says which
  applies to the project's next record, if any: without one, creating it is refused
  (409 ``procedure_not_chosen``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import ConfigDict

from iki_server.api import common
from iki_server.org.permissions import Action
from iki_server.reviews import choices, pins, sync
from iki_server.reviews.models import Choice
from iki_spec import procedures

router = Router(tags=["reviews"])


class ProcedureSlotOut(Schema):
    id: str
    label: str
    description: str | None = None


class ProcedureFlowOut(Schema):
    id: str
    label: str
    summary: str
    route: list[str] = Field(description="Its states in order: the first is where records start.")
    states: list[str] = Field(description="Every state its records can be in.")
    final: str


class ProcedureStateOut(Schema):
    id: str
    label: str
    description: str | None = None
    by: list[str] = Field(description="The slots whose holders set it.")
    locks: bool
    signed: bool
    signer: str | None = Field(
        default=None,
        description="Who may sign it, or who may not, in words: from its `maker` and "
        "`independent_of`.",
    )


class ProcedureOut(Schema):
    id: str = Field(description="`plugin.name@version`, such as `iki/core.result_review@1`.")
    label: str
    description: str | None = None
    slots: list[ProcedureSlotOut]
    states: list[ProcedureStateOut]
    flows: list[ProcedureFlowOut]


class ReviewChoiceOut(Schema):
    entity_type: str
    project: UUID | None = Field(description="Null for the deployment's default.")
    procedure: str
    flow: str
    binding: dict[str, str] = Field(description="Each slot, to the lab role that plays it.")
    starter: str | None = None
    reason: str | None = None
    chosen_by: UUID
    chosen_at: datetime


class ReviewChoicesOut(Schema):
    entity_type: str
    default: ReviewChoiceOut | None = Field(description="The deployment's default.")
    project: ReviewChoiceOut | None = Field(
        default=None, description="The project's own choice, when a project is given."
    )
    effective: ReviewChoiceOut | None = Field(
        default=None,
        description="What the project's next record is pinned to: its choice for the type "
        "or an ancestor, else the deployment's. Null: creating one is refused.",
    )
    fitting: dict[str, list[str]] = Field(
        description="Each installed procedure that fits the type, with the flows that do."
    )


class ReviewChoiceIn(Schema):
    model_config = ConfigDict(extra="forbid")
    procedure: str = Field(min_length=1, max_length=160)
    flow: str = Field(min_length=1, max_length=63)
    binding: dict[str, str] | None = Field(
        default=None,
        description="Lab roles for some of its slots; the rest play their `iki/lab_roles` role.",
    )
    reason: str = Field(
        default="",
        max_length=choices.MAX_REASON,
        description="Why. Required for the deployment's default.",
    )


class ReviewClearIn(Schema):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=choices.MAX_REASON)


def _procedure_out(qualified: str) -> dict[str, Any]:
    p = sync.declared()[qualified]
    labels = {n: s.label for n, s in p.states.items()}
    states = []
    for name, st in p.states.items():
        # Its edits window: the first state of the first flow it's in.
        window = next((f.open for f in p.flows.values() if name in f.states), "")
        signer = None
        if st.maker is not None:
            signer = procedures.maker_words(st.maker, window, labels)
        elif st.independent_of:
            signer = procedures.exclusion_words(st.independent_of, open=window, labels=labels)
        states.append(
            {
                "id": name,
                "label": st.label,
                "description": st.description,
                "by": list(st.by),
                "locks": st.locks,
                "signed": st.signature is not None,
                "signer": signer,
            }
        )
    return {
        "id": qualified,
        "label": p.label,
        "description": p.description,
        "slots": [
            {"id": n, "label": s.label, "description": s.description} for n, s in p.slots.items()
        ],
        "states": states,
        "flows": [
            {
                "id": n,
                "label": f.label,
                "summary": f.summary,
                "route": list(f.route),
                "states": list(f.states),
                "final": f.final,
            }
            for n, f in p.flows.items()
        ],
    }


def _choice_out(c: Choice | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {
        "entity_type": c.entity_type,
        "project": c.project_id,
        "procedure": c.procedure_id,
        "flow": c.flow,
        "binding": c.binding,
        "starter": c.starter or None,
        "reason": c.reason or None,
        "chosen_by": c.chosen_by_id,
        "chosen_at": c.chosen_at,
    }


@router.get(
    "/review-procedures", response=list[ProcedureOut], summary="List installed review procedures"
)
def list_procedures(request: HttpRequest) -> list[dict[str, Any]]:
    me = common.caller(request)
    common.require_scope(me, "read")
    return [_procedure_out(q) for q in sorted(sync.declared())]


def _current(project: Any, type_name: str) -> Choice | None:  # noqa: ANN401 - a Project or None
    return (
        Choice.objects.filter(project=project, entity_type=type_name, superseded_at__isnull=True)
        .select_related("procedure")
        .first()
    )


def _choices(type_name: str, project: Any) -> dict[str, Any]:  # noqa: ANN401 - a Project or None
    return {
        "entity_type": type_name,
        "default": _choice_out(_current(None, type_name)),
        "project": _choice_out(_current(project, type_name)) if project is not None else None,
        "effective": _choice_out(pins.chosen(project, type_name)) if project is not None else None,
        "fitting": choices.fitting(type_name),
    }


@router.get(
    "/review-choices/{qualified:name}",
    response=ReviewChoicesOut,
    summary="A type's review procedure choices",
)
def get_choices(request: HttpRequest, name: str, project: UUID | None = None) -> dict[str, Any]:
    me = common.caller(request)
    common.require_scope(me, "read")
    found = common.project(me, project, Action.PROJECT_READ) if project else None
    choices.fitting(name)  # refuses a type that isn't reviewed
    return _choices(name, found)


@router.put(
    "/review-choices/{qualified:name}",
    response=ReviewChoicesOut,
    summary="Choose a type's review procedure",
)
def put_choice(
    request: HttpRequest, name: str, payload: ReviewChoiceIn, project: UUID | None = None
) -> dict[str, Any]:
    """Without ``project``: the deployment's default, for Org Admins, with a reason. With
    it: the project's choice, for its Admins. Records already made keep their procedure."""
    me = common.caller(request)
    common.require_scope(me, "write")
    found = common.project(me, project, Action.PROJECT_MANAGE) if project else None
    choices.choose(
        me,
        project=found,
        type_name=name,
        procedure=payload.procedure,
        flow=payload.flow,
        binding=payload.binding,
        reason=payload.reason,
    )
    return _choices(name, found)


@router.post(
    "/review-choices/{qualified:name}/clear",
    response=ReviewChoicesOut,
    summary="Clear a type's review procedure choice",
)
def clear_choice(
    request: HttpRequest, name: str, payload: ReviewClearIn, project: UUID | None = None
) -> dict[str, Any]:
    me = common.caller(request)
    common.require_scope(me, "write")
    found = common.project(me, project, Action.PROJECT_MANAGE) if project else None
    choices.clear(me, project=found, type_name=name, reason=payload.reason)
    return _choices(name, found)
