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

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from ninja import Field, HeaderEx, P, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.amounts import services
from iki_server.amounts.models import Amount, DrawPurpose, Movement, MovementKind
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
from iki_server.api.storage import ConfirmationIn
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.storage import services as storage

router = Router(tags=["amounts"])


QuantityIn = AmountIn


class GivenOut(Schema):
    value: str
    unit: str


class MovementOut(Schema):
    id: UUID
    purpose: DrawPurpose | None = Field(description="The purpose recorded for a draw.")
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
    used_in: UUID | None = Field(
        None, description="What a draw went into, such as an analysis or an experiment."
    )
    source: UUID | None = Field(
        None,
        description="Where a container's material came from, as its type's `follows` says: "
        "its lot (ADR 0180).",
    )
    confirmed: str = Field("", description="The code scanned to confirm it, if one was.")
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
        description="The counterparts, sources and what draws went into that you can read, by ID.",
        default_factory=dict,
    )


class AmountTakeIn(Schema):
    amount: QuantityIn = Field(
        description="How much to draw: in its unit, one that converts to it, or (for a type "
        "with a concentration) a mass of a volume or a volume of a mass."
    )
    reason: str = Field("", max_length=2000, description="What it was used for.")


class DrawIn(AmountTakeIn):
    purpose: DrawPurpose = Field(DrawPurpose.ROUTINE, description="Why material is being drawn.")
    used_in: str | None = Field(
        None,
        max_length=64,
        description="What it went into, by ID or HRID: a record in a project you edit, such as "
        "an analysis or an experiment, which depends on it from then on (ADR 0180).",
    )
    confirm: ConfirmationIn = Field(
        "",
        description="For a type that requires a scan: its unique label or printed Iki code, "
        "a link to the record, or its HRID explicitly entered from the tube.",
    )


class TransferIn(AmountTakeIn):
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
        "purpose": m.purpose,
        "change": _plain(m.change),
        "balance": _plain(m.balance),
        "unit": m.unit,
        "given": {"value": _plain(m.given_value), "unit": m.given_unit}
        if m.given_value is not None
        else None,
        "counterpart": m.counterpart_id,
        "used_in": m.used_in_id,
        "source": m.source_id,
        "confirmed": m.confirmed,
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


def _record(me: User, ref: str, *, edit: bool = True, keeper: bool = False) -> Entity:
    """The record, if it has an amount the caller may change: Editor in its project, or,
    for a draw (``keeper``), in the project whose storage keeps it (ADR 0180)."""
    entity = _find(me, ref)
    if services.spec(entity.type) is None:
        raise Problem(404, f"{entity.hrid} is a {entity.type}, which has no amount.")
    if edit and not access.can_edit(me, entity):
        if keeper and storage.holds_material(me, entity):
            return entity
        raise Problem(
            403,
            f"Drawing from {entity.hrid} needs Editor in its project, or in the project keeping it."
            if keeper
            else f"Changing {entity.hrid}'s amount needs Editor in its project.",
        )
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
    result["refs"] = _refs_out(
        me,
        {
            ref
            for m in items
            for ref in (m.counterpart_id, m.used_in_id, m.source_id)
            if ref is not None
        },
    )
    return result


@router.post("/entities/{ref}/amount/draw", response=AmountMovedOut, summary="Draw from its amount")
def draw(request: HttpRequest, ref: str, payload: DrawIn) -> dict[str, Any]:
    """Takes some of it for the given purpose, saying what it went into. Editors of its
    project may draw, and so may those of the project keeping it (ADR 0180). A purpose you
    cannot use answers 403.
    An insufficient balance, a state hold or a draw guard refusing the draw answers 422.
    Two people drawing at once take turns."""
    me = caller(request)
    entity = _record(me, ref, keeper=True)
    used_in = None
    if payload.used_in:
        try:
            used_in = _find(me, payload.used_in)
        except Problem:
            raise _invalid(services.Wrong({"used_in": ["isn't a record you can read."]})) from None
        if not access.can_edit(me, used_in):
            raise Problem(403, f"Saying {used_in.hrid} used material needs Editor in its project.")
    try:
        movement = services.draw(
            me,
            entity,
            _given(payload.amount),
            purpose=payload.purpose,
            reason=payload.reason,
            used_in=used_in,
            confirm=payload.confirm
            if isinstance(payload.confirm, str)
            else storage.Confirmation(payload.confirm.code, payload.confirm.mode),
        )
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
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
def adjust(
    request: HttpRequest,
    ref: str,
    payload: AdjustIn,
    revision: HeaderEx[str | None, P(alias="If-Balance-Revision")] = None,
) -> dict[str, Any]:
    """Records an absolute count, with a reason. Send the observed amount's exact
    `updated_at` string in `If-Balance-Revision`, or `unrecorded` if it had no amount.
    A missing header answers 428. A changed balance answers 409 and must be recounted."""
    me = caller(request)
    entity = _record(me, ref)
    if not revision:
        raise Problem(428, "Send If-Balance-Revision with the balance revision you observed.")
    try:
        movement = services.adjust(
            me,
            entity,
            _given(payload.amount, zero=True),
            reason=payload.reason,
            expected_revision=revision,
        )
    except services.StaleBalanceError:
        raise Problem(
            409,
            "The stock balance changed. Refresh it and recount before submitting.",
            code="stale_balance",
        ) from None
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
