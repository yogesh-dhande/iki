"""How much there is of a record (ADR 0139), and what changes it.

- ``GET /entities/{ref}/amount``: how much there is now, and whether a state holds it.
- ``GET /entities/{ref}/amount/movements``: its ledger, newest first.
- ``POST /entities/{ref}/amount/draw``: use some up.
- ``POST /entities/{ref}/amount/transfer``: move some to another record.
- ``POST /entities/{ref}/amount/adjust``: set it to what it was found to be, saying why.
- ``POST /entities/{ref}/aliquots``: split it into aliquots, each a new record.

Each change needs Editor in the record's project (and the other record's, for a transfer).
Amounts are exact decimals, given and answered as strings or numbers: ``"12.5"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.amounts import services
from iki_server.amounts.models import Amount, Movement, MovementKind
from iki_server.api.common import caller
from iki_server.api.entities import (
    AmountIn,
    AmountOut,
    RefOut,
    _amount,
    _find,
    _invalid,
    _lookup,
    _plain,
)
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import AgentOut, Page, UserOut, agent_out
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid

router = Router(tags=["amounts"])


QuantityIn = AmountIn


class GivenOut(Schema):
    value: str
    unit: str


class MovementOut(Schema):
    id: UUID
    kind: Literal[
        "recorded", "drawn", "transferred_out", "transferred_in", "aliquoted", "aliquot", "adjusted"
    ] = Field(
        description="`recorded`: how much it had to start with; `drawn`: used up; "
        "`transferred_out` and `transferred_in`: moved to or from `counterpart`; "
        "`aliquoted`: split into aliquots, and `aliquot`: an aliquot's first, split from "
        "`counterpart`; `adjusted`: set to what it was found to be."
    )
    change: str = Field(
        pattern=r"^-?\d+(\.\d+)?$", description="What it took (below zero) or gave, in `unit`."
    )
    balance: str = Field(pattern=r"^\d+(\.\d+)?$", description="How much it had after.")
    unit: str
    given: GivenOut | None = Field(description="What was asked for, in the unit it was asked in.")
    counterpart: UUID | None = Field(description="The other record, if there is one.")
    operation: UUID = Field(
        description="One per request: a transfer's two movements, and a split's and its "
        "aliquots', share it."
    )
    user: UserOut | None
    via: str
    client: str
    agent: AgentOut | None
    reason: str
    created_at: datetime


class MovementsOut(Page[MovementOut]):
    refs: dict[str, RefOut] = Field(
        description="The counterparts you can read, by ID.", default_factory=dict
    )


class DrawIn(Schema):
    amount: QuantityIn = Field(
        description="How much to draw: in its unit, one that converts to it, or (for a type "
        "with a concentration) a mass of a volume or a volume of a mass."
    )
    reason: str = Field("", max_length=2000, description="What it was used for.")


class TransferIn(DrawIn):
    to: str = Field(max_length=64, description="The record it goes to, by ID or HRID.")


class AdjustIn(Schema):
    amount: QuantityIn = Field(description="How much there is: what it was found to be.")
    reason: str = Field(min_length=1, max_length=2000, description="Why: a recount, a spill.")


class SplitIn(Schema):
    count: int = Field(ge=1, le=services.MAX_ALIQUOTS, description="How many aliquots.")
    each: QuantityIn = Field(description="How much each holds.")
    values: list[dict[str, Any]] | dict[str, Any] | None = Field(
        None,
        description="The aliquots' own values: one object for all, or a list, one each. An "
        "aliquot of its own type has the record's values, but not its unique ones or states.",
    )
    reason: str = Field("", max_length=2000)


class AmountMovedOut(Schema):
    amount: AmountOut
    movement: MovementOut


class AliquotsOut(AmountMovedOut):
    aliquots: list[RefOut] = Field(description="The new records, in order.")
    ids: list[UUID] = Field(description="Their IDs, in the same order.")


def amount_out(
    entity: Entity, amount: Amount | None, values: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """``entity``'s amount as ``AmountOut``: None for a type without one, or a record with
    none recorded."""
    if amount is None:
        return None
    if values is None:
        from iki_server.entities import services as entities

        values = entities.current_values_of([entity])[entity.pk]
    return _amount(entity, amount, values)


def movement_out(m: Movement) -> dict[str, Any]:
    return {
        "id": m.id,
        "kind": m.kind,
        "change": _plain(m.change),
        "balance": _plain(m.balance),
        "unit": m.unit,
        "given": {"value": _plain(m.given_value), "unit": m.given_unit}
        if m.given_value is not None
        else None,
        "counterpart": m.counterpart_id,
        "operation": m.operation,
        "user": m.user,
        "via": m.via,
        "client": m.client,
        "agent": agent_out(m.agent, m.client),
        "reason": m.reason,
        "created_at": m.created_at,
    }


def _refs_out(me: User, ids: set[Any]) -> dict[str, dict[str, Any]]:
    refs, _ = _lookup(me, {"ref": ids, "file": set()})
    return {str(pk): ref for pk, ref in refs.items()}


def _record(me: User, ref: str, *, edit: bool = True) -> Entity:
    entity = _find(me, ref)
    if services.spec(entity.type) is None:
        raise Problem(404, f"{entity.hrid} is a {entity.type}, which has no amount.")
    if edit and not access.can_edit(me, entity):
        raise Problem(403, f"Changing {entity.hrid}'s amount needs Editor in its project.")
    return entity


def _given(q: QuantityIn, name: str = "amount", *, zero: bool = False) -> services.Given:
    try:
        return services.Given.of({"value": str(q.value), "unit": q.unit}, name, zero=zero)
    except Invalid as error:
        raise _invalid(error) from None


def _moved(entity: Entity, movement: Movement) -> dict[str, Any]:
    amount = Amount.objects.get(pk=entity.pk)
    return {"amount": amount_out(entity, amount), "movement": movement_out(movement)}


@router.get("/entities/{ref}/amount", response=AmountOut, summary="How much there is of it")
def get_amount(request: HttpRequest, ref: str) -> dict[str, Any]:
    """404 for a type without an amount, or a record with none recorded yet: adjust it to
    record one."""
    me = caller(request)
    entity = _record(me, ref, edit=False)
    found = Amount.objects.filter(pk=entity.pk).first()
    out = amount_out(entity, found)
    if out is None:
        raise Problem(404, f"{entity.hrid} has no amount recorded.")
    return out


@router.get(
    "/entities/{ref}/amount/movements",
    response=MovementsOut,
    summary="Its amount's ledger",
)
def list_movements(request: HttpRequest, ref: str, params: Query[PageParams]) -> dict[str, Any]:
    """Newest first: each change to its amount, who made it, how, when and why."""
    me = caller(request)
    entity = _record(me, ref, edit=False)
    movements = Movement.objects.filter(entity=entity).select_related("user", "agent")
    result = page(movements, params, key="id", newest_first=True)
    items: list[Movement] = result["items"]
    result["items"] = [movement_out(m) for m in items]
    result["refs"] = _refs_out(me, {m.counterpart_id for m in items if m.counterpart_id})
    return result


@router.post("/entities/{ref}/amount/draw", response=AmountMovedOut, summary="Draw from its amount")
def draw(request: HttpRequest, ref: str, payload: DrawIn) -> dict[str, Any]:
    """Takes some of it: used up, in an assay or a reaction. Refused (422) if it hasn't that
    much, or a state holds it. Two people drawing at once take turns."""
    me = caller(request)
    entity = _record(me, ref)
    try:
        movement = services.draw(me, entity, _given(payload.amount), reason=payload.reason)
    except Invalid as error:
        raise _invalid(error) from None
    return _moved(entity, movement)


@router.post(
    "/entities/{ref}/amount/transfer",
    response=AmountMovedOut,
    summary="Transfer some of its amount",
)
def transfer(request: HttpRequest, ref: str, payload: TransferIn) -> dict[str, Any]:
    """Moves some of it to another record with an amount: pooled, or topped up. Each is
    changed, and each keeps a movement naming the other. Needs Editor in both projects."""
    me = caller(request)
    entity = _record(me, ref)
    target = _record(me, payload.to)
    try:
        sent, _ = services.transfer(
            me, entity, target, _given(payload.amount), reason=payload.reason
        )
    except Invalid as error:
        raise _invalid(error) from None
    return _moved(entity, sent)


@router.post("/entities/{ref}/amount/adjust", response=AmountMovedOut, summary="Adjust its amount")
def adjust(request: HttpRequest, ref: str, payload: AdjustIn) -> dict[str, Any]:
    """Sets it to what it was found to be, saying why: a recount, a spill, a correction. A
    record with no amount recorded gets one."""
    me = caller(request)
    entity = _record(me, ref)
    try:
        movement = services.adjust(
            me, entity, _given(payload.amount, zero=True), reason=payload.reason
        )
    except Invalid as error:
        raise _invalid(error) from None
    return _moved(entity, movement)


@router.post(
    "/entities/{ref}/aliquots", response={201: AliquotsOut}, summary="Split it into aliquots"
)
def split(request: HttpRequest, ref: str, payload: SplitIn) -> Status[dict[str, Any]]:
    """Makes ``count`` new records in its project, each holding ``each`` of its amount and
    split from it (an edge of the graph), and takes exactly that much from it: nothing is
    made or lost. All of them, or none."""
    me = caller(request)
    entity = _record(me, ref)
    try:
        done = services.split(
            me,
            entity,
            payload.count,
            _given(payload.each, "each"),
            values=payload.values,
            reason=payload.reason,
        )
    except Invalid as error:
        raise _invalid(error) from None
    refs = _refs_out(me, {a.pk for a in done.aliquots})
    return Status(
        201,
        {
            **_moved(entity, done.source),
            "aliquots": [refs[str(a.pk)] for a in done.aliquots],
            "ids": [a.pk for a in done.aliquots],
        },
    )


__all__ = ["AmountOut", "MovementKind", "QuantityIn", "amount_out", "router"]
