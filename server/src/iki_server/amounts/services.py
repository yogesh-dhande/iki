"""Drawing, transferring, splitting and adjusting a record's amount (ADR 0139).

Each operation locks its records and then their amounts, in ID order, so two transfers
between the same records can't deadlock. It checks eligibility and the available balance,
then writes the new balance and a movement for each in one transaction. Two people drawing
from one tube at once take turns: the second sees what the first left. The database
refuses a balance below zero whatever writes it.

Amounts are exact decimals in their type's unit. What's asked for in another unit is
converted exactly (UCUM's factors are fractions), and refused if the record's 12 places
can't hold it exactly (0.4 pL of a record kept in L would be 0 L), so both sides of a
transfer are the same quantity. A mass asked of a volume, or a volume of a mass, goes
through the record's concentration, rounded to 12 places, and is refused if that's 0.

Pooling changes what a record holds, so a transfer into a record with a concentration
sets it to what the pool's is: the solute of both over their volume, when the record it
came from has a concentration too, and unknown when it hasn't.

A transfer can't change what a record is. Into a record whose type says what its amount
is of (``material``, such as a lot's molecule), only material of that entity goes, and an
aliquot split off it is of that entity too (ADR 0162). Into a record a state locks, such
as a released lot, only material in that same state goes: what's added to released
material has been released too (ADR 0157).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models.functions import Now

from iki_server.accounts.models import User
from iki_server.amounts import eligibility
from iki_server.amounts.models import (
    DIGITS,
    GIVEN_PLACES,
    PLACES,
    Amount,
    DrawPurpose,
    Movement,
    MovementKind,
)
from iki_server.audit import log
from iki_server.audit.log import current_agent, current_oauth_client, current_via
from iki_server.core.ids import uuid7
from iki_server.entities import access, registry, states
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org.services import Refused
from iki_server.storage import scan_identity
from iki_server.storage import services as storage
from iki_spec import units
from iki_spec.model import Amount as AmountSpec
from iki_spec.model import EntityType


class Wrong(Invalid):
    """What's wrong with a request to change an amount, by where it is in the request
    (``amount``, ``to``, ``values/0/lot``), or ``""`` for the request as a whole."""


#: At most this many aliquots in one split.
MAX_ALIQUOTS = 200
_QUANTUM = Decimal(1).scaleb(-PLACES)
#: The ledger's arithmetic, exact for its amounts: a sum, difference or product of two of
#: them (40 digits each, 12 after the point) fits in twice the digits, so nothing is
#: rounded as Python's default context (28 digits) would round it.
_EXACT = Context(prec=2 * DIGITS, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Given:
    """How much someone asked for: a positive amount (zero too, to adjust), in a unit."""

    value: Decimal
    unit: str

    @classmethod
    def of(cls, data: Any, name: str = "amount", *, zero: bool = False) -> Given:  # noqa: ANN401 - JSON
        if not isinstance(data, Mapping) or set(data) - {"value", "unit"}:
            raise Wrong({name: ["must be an object with a `value` and a `unit`."]})
        raw, unit = data.get("value"), data.get("unit")
        try:
            if isinstance(raw, bool) or not isinstance(raw, int | float | str):
                raise InvalidOperation
            value = Decimal(str(raw))
        except InvalidOperation:
            raise Wrong({name: ["needs a `value`: a number."]}) from None
        if not value.is_finite() or value < 0 or (value == 0 and not zero):
            raise Wrong({name: ["must be more than zero." if not zero else "can't be below zero."]})
        # Kept as it was given (ADR 0139), so refused if the ledger couldn't keep it all.
        _sign, digits, exponent = value.normalize(_EXACT).as_tuple()
        assert isinstance(exponent, int)  # noqa: S101 - it's finite
        if -exponent > GIVEN_PLACES:
            raise Wrong(
                {
                    name: [
                        f"has more than the {GIVEN_PLACES} decimal places a ledger keeps of "
                        "what's asked for."
                    ]
                }
            )
        if len(digits) + exponent > GIVEN_PLACES:
            raise Wrong(
                {
                    name: [
                        f"has more than the {GIVEN_PLACES} digits before its point a ledger "
                        "keeps of what's asked for."
                    ]
                }
            )
        if not isinstance(unit, str) or not unit:
            raise Wrong({name: ["needs a `unit`, in UCUM, such as uL or mg."]})
        try:
            units.parse(unit)
        except units.UnitError as error:
            raise Wrong({name: [str(error)]}) from None
        return cls(value, unit)


def spec(type_name: str) -> AmountSpec | None:
    """The amount ``type_name``'s records have, its own or a parent's."""
    entity_type = registry.types().get(type_name)
    if entity_type is None:
        return None
    for t in [entity_type, *registry.ancestors(entity_type)]:
        if t.amount is not None:
            return t.amount
    return None


def amounts_of(entities: Sequence[Entity]) -> dict[Any, Amount]:
    """The amounts recorded for ``entities``, by ID, in one query."""
    return {a.entity_id: a for a in Amount.objects.filter(entity_id__in=[e.pk for e in entities])}


def held(entity: Entity, values: Mapping[str, Any] | None = None) -> str | None:
    """The state field holding ``entity``'s amount, if one does: a state with
    ``holds_amount``, such as quarantine."""
    fields = states.state_fields(entity.type)
    if not fields:
        return None
    if values is None:
        from iki_server.entities import services

        values = services.current_values_of([entity])[entity.pk]
    for name, f in fields.items():
        state = f.states.get(values.get(name) or "")
        if state is not None and state.holds_amount:
            return name
    return None


def _whole_units(entity: Entity, wanted: AmountSpec, value: Fraction | Decimal) -> None:
    """Check indivisible quantities in their ledger unit, before any decimal rounding."""
    if wanted.whole_units and Fraction(value).denominator != 1:
        raise Wrong({"amount": [f"{entity.hrid} requires whole units of {wanted.unit}."]})


def in_unit(entity: Entity, given: Given, values: Mapping[str, Any] | None = None) -> Decimal:
    """``given`` in ``entity``'s amount's unit: converted, or through its concentration."""
    wanted = spec(entity.type)
    assert wanted is not None  # noqa: S101 - only records with an amount get here
    target, source = units.parse(wanted.unit), units.parse(given.unit)
    exact = Fraction(given.value)
    if source.is_commensurable(target):
        converted = exact * source.factor / target.factor
        _whole_units(entity, wanted, converted)
        return _exactly(entity, wanted.unit, converted, given)
    if wanted.concentration is not None:
        if values is None:
            from iki_server.entities import services

            values = services.current_values_of([entity])[entity.pk]
        entity_type = registry.types()[entity.type]
        f = _fields(entity_type).get(wanted.concentration)
        held_at = values.get(wanted.concentration)
        if (
            f is not None
            and f.unit is not None
            and isinstance(held_at, int | float)
            and held_at > 0
        ):
            per = units.parse(f.unit)
            concentration = Fraction(str(held_at)) * per.factor
            through: Fraction | None = None
            if units.parse(f"({given.unit}).({f.unit})").is_commensurable(target):
                through = exact * source.factor * concentration / target.factor
            elif units.parse(f"({given.unit})/({f.unit})").is_commensurable(target):
                through = exact * source.factor / concentration / target.factor
            if through is not None:
                _whole_units(entity, wanted, through)
                value = _rounded(through)
                if value == 0 and exact > 0:
                    raise Wrong(
                        {
                            "amount": [
                                f"is {shown(given.value)} {given.unit}, which at "
                                f"{entity.hrid}'s {f.label.lower()} is less than the "
                                f"{shown(_QUANTUM)} {wanted.unit} its amount is kept to."
                            ]
                        }
                    )
                return value
        if f is not None and f.unit is not None and not held_at:
            raise Wrong(
                {
                    "amount": [
                        f"is in {given.unit}, which {entity.hrid}'s amount is in only through "
                        f"its {f.label.lower()}, which it hasn't got."
                    ]
                }
            )
    raise Wrong(
        {
            "amount": [
                f"is in {given.unit}, which doesn't convert to {wanted.unit}, what "
                f"{entity.hrid}'s amount is kept in."
            ]
        }
    )


def record(
    user: User | None,
    entity: Entity,
    given: Given,
    *,
    reason: str = "",
    kind: str = MovementKind.RECORDED,
    counterpart: Entity | None = None,
    operation: Any = None,  # noqa: ANN401 - a UUID
) -> Movement:
    """Records how much a new record has: when it's created, or each aliquot of a split."""
    with transaction.atomic():
        # Its values as just saved: a mass given for a record kept by volume converts
        # through the concentration it was created with.
        value = in_unit(entity, given)
        wanted = spec(entity.type)
        assert wanted is not None  # noqa: S101
        Amount.objects.create(entity=entity, unit=wanted.unit, value=value)
        return _movement(
            user,
            entity,
            kind,
            value,
            value,
            wanted.unit,
            given,
            reason,
            counterpart=counterpart,
            operation=operation,
        )


def draw(
    user: User,
    entity: Entity,
    given: Given,
    *,
    purpose: str = DrawPurpose.ROUTINE,
    reason: str = "",
    used_in: Entity | None = None,
    confirm: str | storage.Confirmation = "",
) -> Movement:
    """Takes ``given`` from ``entity``'s amount: used up, in an assay or a reaction.
    ``used_in`` is what it went into (ADR 0180), which depends on it from then on; ``confirm``
    the code scanned, for a type that asks for one before a draw."""
    if purpose not in DrawPurpose.values:
        raise Wrong({"purpose": ["Choose routine use or QA sampling."]})
    if used_in is not None and used_in.pk == entity.pk:
        raise Wrong({"used_in": [f"is {entity.hrid}, what it's drawn from."]})
    receipt = registry.draws_received(used_in.type) if used_in is not None else None
    with transaction.atomic():
        from iki_server.entities import services

        # Take source, destination and followed record locks in one global order. A
        # concurrent edit to the tube's sources is checked again after taking the locks.
        parents = [p for _, p in eligibility.followed(entity)] if receipt else []
        records = [entity, used_in] if used_in is not None and receipt else [entity]
        locked = _lock_records(records, related=parents)
        entity = locked[entity.pk]
        if not access.can_edit(user, entity) and not (
            access.can_read(user, entity) and storage.holds_material(user, entity)
        ):
            raise PermissionDenied(
                "Drawing material needs Editor in its owning or storing project."
            )
        if used_in is not None:
            used_in = locked.get(used_in.pk, used_in)
            if not access.can_edit(user, used_in):
                raise PermissionDenied(
                    "Recording material used needs Editor in the receiving project."
                )
        confirmed = _confirmed(user, entity, confirm)

        current = services.current_values_of([entity])[entity.pk]
        followed = eligibility.followed(entity, current)
        if receipt and any(parent.pk not in locked for _, parent in followed):
            raise Wrong({"used_in": ["The material's source changed during the draw. Retry it."]})
        source = followed[0][1] if followed else None
        refused = eligibility.purpose_refusals(user, entity, DrawPurpose(purpose), current)
        if refused is not None:
            raise PermissionDenied(refused)
        (amount,) = _locked([entity])
        change = _taking(entity, amount, given, operation="draw", purpose=purpose, values=current)
        movement = _change(
            user,
            entity,
            amount,
            MovementKind.DRAWN,
            change.copy_negate(),
            given,
            reason,
            purpose=purpose,
            used_in=used_in,
            source=source,
            confirmed=confirmed,
        )
        if receipt and used_in is not None:
            _receive_draw(user, used_in, receipt, movement, entity, source)
        return movement


def _receive_draw(
    user: User,
    destination: Entity,
    field: str,
    movement: Movement,
    record: Entity,
    source: Entity | None,
) -> None:
    """Append immutable facts from the debit to the destination's revision (ADR 0189).
    The destination and source records are locked until the entire draw commits."""
    from iki_server.entities import services

    project = destination.owner_project
    assert project is not None  # noqa: S101 - checked as a usable record
    readable = set(
        access.shareable_into(user, project)
        .filter(pk__in=[record.pk, movement.source_id])
        .values_list("pk", flat=True)
    )
    if record.pk not in readable:
        raise PermissionDenied("The drawn record cannot be referenced from the receiving project.")
    visible_source = source if source is not None and source.pk in readable else None
    entry = {
        "movement": str(movement.pk),
        "record": str(record.pk),
        "record_revision": record.revision,
        "source": str(visible_source.pk) if visible_source else None,
        "source_revision": visible_source.revision if visible_source else None,
        "amount": shown(movement.change.copy_abs()),
        "unit": movement.unit,
        "at": movement.created_at.isoformat(),
        "by": str(user.pk),
    }
    previous = services.current_values_of([destination])[destination.pk][field]
    if previous is not None and not isinstance(previous, list):
        raise Wrong({"used_in": ["Its material draw field must contain a list of receipts."]})
    try:
        services.update(
            user,
            destination,
            {field: [*(previous or []), entry]},
            expected_revision=destination.revision,
            reason=movement.reason,
            _recording_draw=True,
        )
    except Refused as error:
        raise Wrong({"used_in": [str(error)]}) from None


def _confirmed(user: User, entity: Entity, value: str | storage.Confirmation) -> str:
    """Return the physical proof to record with this debit, while the source is locked."""
    proof = storage.Confirmation(value) if isinstance(value, str) else value
    code = proof.code.strip()
    if not code:
        found = storage.spec(entity.type)
        if found is None or "draw" not in found.confirm:
            if value == "":
                return ""
            raise Wrong({"confirm": [str(scan_identity.ScanRefused())]})
        raise Wrong(
            {"confirm": [f"Scan {entity.hrid} to confirm it's the one you're drawing from."]}
        )
    if len(proof.code) > 200:
        raise Wrong({"confirm": ["The confirmation must be at most 200 characters."]})
    try:
        scope = storage.ConfirmationScope(user, [entity], [code])
        if scope.resolve(code, mode=proof.mode).record != entity:
            raise scan_identity.ScanRefused()
    except scan_identity.ScanRefused as refusal:
        raise Wrong({"confirm": [str(refusal)]}) from None
    return code


def transfer(
    user: User, source: Entity, target: Entity, given: Given, *, reason: str = ""
) -> tuple[Movement, Movement]:
    """Moves ``given`` from ``source``'s amount to ``target``'s: pooled, or topped up."""
    if source.pk == target.pk:
        raise Wrong({"to": ["is the record it's from."]})
    wanted = spec(target.type)
    if wanted is None:
        raise Wrong({"to": [f"{target.hrid} is a {target.type}, which has no amount."]})
    with transaction.atomic():
        _lock_records([source, target])
        _ensure(target, wanted.unit)
        locked = dict(zip((source.pk, target.pk), _locked([source, target]), strict=True))
        _what_it_takes(source, target)
        out = _taking(source, locked[source.pk], given, operation="transfer")
        here = units.parse(locked[source.pk].unit)
        there = units.parse(locked[target.pk].unit)
        if not here.is_commensurable(there):
            raise Wrong(
                {
                    "to": [
                        f"{target.hrid}'s amount is in {there.code}, which {source.hrid}'s, in "
                        f"{here.code}, doesn't convert to."
                    ]
                }
            )
        moved = Fraction(out) * here.factor / there.factor
        _whole_units(target, wanted, moved)
        into = _rounded(moved)
        if Fraction(into) != moved:
            raise Wrong(
                {
                    "amount": [
                        f"is {shown(out)} {here.code} of {source.hrid}, which is "
                        f"{_decimal(moved)} {there.code}: {target.hrid}'s amount, kept in "
                        f"{there.code} to {PLACES} decimal places, can't hold that exactly, so "
                        "neither was changed."
                    ]
                }
            )
        _pool(user, source, target, out, here, locked[target.pk], reason)
        operation = uuid7()
        sent = _change(
            user,
            source,
            locked[source.pk],
            MovementKind.TRANSFERRED_OUT,
            out.copy_negate(),
            given,
            reason,
            counterpart=target,
            operation=operation,
        )
        came = _change(
            user,
            target,
            locked[target.pk],
            MovementKind.TRANSFERRED_IN,
            into,
            given,
            reason,
            counterpart=source,
            operation=operation,
        )
        return sent, came


def adjust(user: User, entity: Entity, given: Given, *, reason: str) -> Movement:
    """Sets ``entity``'s amount to what it was found to be: a recount, or a correction.
    It always says why."""
    if not reason.strip():
        raise Wrong({"reason": ["is required: say why it's adjusted."]})
    wanted = spec(entity.type)
    assert wanted is not None  # noqa: S101
    with transaction.atomic():
        _lock_records([entity])
        fresh = not Amount.objects.filter(entity=entity).exists()
        _ensure(entity, wanted.unit)
        (amount,) = _locked([entity])
        value = in_unit(entity, given)
        kind = MovementKind.RECORDED if fresh else MovementKind.ADJUSTED
        change = _EXACT.subtract(value, amount.value)
        return _change(user, entity, amount, kind, change, given, reason)


@dataclass(frozen=True)
class Split:
    source: Movement
    aliquots: list[Entity]


def split(
    user: User,
    source: Entity,
    count: int,
    each: Given,
    *,
    values: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    reason: str = "",
) -> Split:
    """Splits ``count`` aliquots of ``each`` off ``source``, each a new record in its
    project (of its type, or the type its amount's ``aliquots`` names) with that amount,
    split from it. ``source`` loses exactly what they hold: nothing is made or lost.
    ``values``: each aliquot's own values (a list, one each), or all of theirs."""
    from iki_server.entities import services

    wanted = spec(source.type)
    assert wanted is not None  # noqa: S101
    if not 1 <= count <= MAX_ALIQUOTS:
        raise Wrong({"count": [f"must be from 1 to {MAX_ALIQUOTS}."]})
    given = list(values) if isinstance(values, Sequence) else [values or {}] * count
    if len(given) != count:
        raise Wrong({"values": [f"gives {len(given)} aliquots' values for {count} aliquots."]})
    project = source.owner_project
    assert project is not None  # noqa: S101 - only records have amounts
    kind = wanted.aliquots or source.type
    with transaction.atomic():
        _lock_records([source])
        (amount,) = _locked([source])
        current = services.current_values_of([source])[source.pk]
        one = in_unit(source, each, current)
        total = _EXACT.multiply(one, count)
        _taking(source, amount, Given(total, amount.unit), operation="split", values=current)
        base = _copied(source, current) if wanted.aliquots is None else {}
        theirs = spec(kind)
        of = _material_of(source, current)
        operation = uuid7()
        made: list[Entity] = []
        for index, own in enumerate(given):
            mine = {**base, **own}
            if wanted.aliquots_of is not None:
                mine[wanted.aliquots_of] = str(source.pk)
            if theirs is not None and theirs.material is not None and of is not None:
                # An aliquot is of what it was split from: it can't be given another.
                named = own.get(theirs.material)
                if named is not None and _entry(named) != _entry(of):
                    raise Wrong(
                        {
                            f"values/{index}/{theirs.material}": [
                                f"is what it's split from's: an aliquot of {source.hrid} is "
                                f"of {_hrid(of)}, as it is."
                            ]
                        }
                    )
                mine[theirs.material] = str(of)
            try:
                saved = services.create(user, kind, project, mine, reason=reason)
            except Invalid as error:
                raise Wrong({f"values/{index}/{k}": v for k, v in error.errors.items()}) from None
            aliquot = Entity.objects.get(pk=saved.entity.pk)
            record(
                user,
                aliquot,
                Given(one, amount.unit),
                reason=reason,
                kind=MovementKind.ALIQUOT,
                counterpart=source,
                operation=operation,
            )
            made.append(aliquot)
        moved = _change(
            user,
            source,
            amount,
            MovementKind.ALIQUOTED,
            total.copy_negate(),
            Given(_EXACT.multiply(each.value, count), each.unit),
            reason,
            operation=operation,
        )
        return Split(moved, made)


# --- The parts ----------------------------------------------------------------------------------


def _material_of(entity: Entity, values: Mapping[str, Any] | None = None) -> Any:  # noqa: ANN401 - an ID
    """What ``entity``'s amount is of, by its type's ``material``: the entity its reference
    names, or None if its type doesn't say."""
    wanted = spec(entity.type)
    if wanted is None or wanted.material is None:
        return None
    if values is None:
        from iki_server.entities import services

        values = services.current_values_of([entity])[entity.pk]
    return values.get(wanted.material)


def _entry(ref: Any) -> str:  # noqa: ANN401 - an ID
    """The entity ``ref`` names, as its registry entry if it has one: a reference to a
    molecule may name its entry or a project's record of it, and they're one molecule."""
    found = Entity.objects.filter(pk=ref).values_list("entry_id", flat=True).first()
    return str(found or ref)


def _hrid(ref: Any) -> str:  # noqa: ANN401 - an ID
    found = Entity.objects.filter(pk=ref).values_list("hrid", flat=True).first()
    return str(found or ref)


def _what_it_takes(source: Entity, target: Entity) -> None:
    """Refuses a transfer that would change what ``target`` is: material of another entity
    than its type says it's of, or material that isn't in the state a state locking it
    is in (a quarantined lot's into a released one). Neither is changed."""
    from iki_server.entities import services

    values = services.current_values_of([source, target])
    theirs, mine = values[source.pk], values[target.pk]
    wanted = spec(target.type)
    assert wanted is not None  # noqa: S101 - only records with an amount get here
    if wanted.material is not None:
        of = mine.get(wanted.material)
        given = _material_of(source, theirs)
        if given is None or of is None or _entry(given) != _entry(of):
            said = (
                f"{source.hrid}'s amount isn't of one entity, as its type doesn't say"
                if given is None
                else f"{source.hrid} is of {_hrid(given)}"
            )
            raise Wrong(
                {
                    "to": [
                        f"{target.hrid} is of {_hrid(of) if of is not None else 'nothing yet'}, "
                        f"and {said}: pooling them would put another material in it, so "
                        "neither was changed. Record a mixture as a record of its own."
                    ]
                }
            )
    held_in = states.state_fields(source.type)
    for name, f in states.state_fields(target.type).items():
        state = f.states.get(mine.get(name) or "")
        if state is None or not state.locks:
            continue
        if name in held_in and theirs.get(name) == mine.get(name):
            continue
        other = held_in.get(name)
        now = other.states.get(theirs.get(name) or "") if other is not None else None
        raise Wrong(
            {
                "to": [
                    f"{target.hrid} is {state.label}, which locks it, so only material that's "
                    f"{state.label} too goes into it, and {source.hrid} "
                    + (f"is {now.label}" if now is not None else "isn't")
                    + ": neither was changed."
                ]
            }
        )


def _usable(entity: Entity) -> None:
    if entity.owner_project_id is None:
        raise Wrong({"": [f"{entity.hrid} is a registry entry, which has no amount."]})
    if entity.archived_at is not None:
        raise Wrong({"": [f"{entity.hrid} is archived: restore it to change its amount."]})


def _ensure(entity: Entity, unit: str) -> None:
    """An amount row to lock, of nothing, for a record that has none recorded yet."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO iki_amount (entity_id, unit, value) VALUES (%s, %s, 0) "
            "ON CONFLICT (entity_id) DO NOTHING",
            [entity.pk, unit],
        )


def _lock_records(
    entities: Sequence[Entity],
    *,
    related: Sequence[Entity] = (),
) -> dict[Any, Entity]:
    """Keep state and eligibility values stable through a withdrawal and its movement.
    Lock records before touching amounts, including a transfer's new destination balance."""
    locked = {
        record.pk: record
        for record in Entity.objects.select_for_update(no_key=True)
        .filter(pk__in=[entity.pk for entity in (*entities, *related)])
        .order_by("pk")
    }
    for entity in entities:
        _usable(locked[entity.pk])
    return locked


def _locked(entities: Sequence[Entity]) -> list[Amount]:
    """Their amounts, locked until the transaction ends, in the order given. Locks are
    taken in ID order, whatever that is, so two requests can't wait on each other."""
    found = {
        a.entity_id: a
        for a in Amount.objects.select_for_update()
        .filter(entity_id__in=[e.pk for e in entities])
        .order_by("entity_id")
    }
    missing = [e for e in entities if e.pk not in found]
    if missing:
        raise Wrong(
            {"amount": [f"{missing[0].hrid} has no amount recorded: adjust it to how much it has."]}
        )
    return [found[e.pk] for e in entities]


def _taking(
    entity: Entity,
    amount: Amount,
    given: Given,
    *,
    operation: str,
    purpose: str = DrawPurpose.ROUTINE,
    values: Mapping[str, Any] | None = None,
) -> Decimal:
    """The withdrawal in the record's unit after checking holds, draw guards and balance."""
    if values is None and (
        eligibility.draw_guards(entity.type) or eligibility.follows(entity.type)
    ):
        from iki_server.entities import services

        values = services.current_values_of([entity])[entity.pk]
    name = held(entity, values)
    if name is not None:
        f = states.state_fields(entity.type)[name]
        raise Wrong(
            {
                "amount": [
                    f"{entity.hrid} is held by its {f.label.lower()}: none of it can be taken "
                    "until that changes."
                ]
            }
        )
    refused = eligibility.refusals(entity, [purpose], values, operation=operation)
    if purpose in refused:
        raise Wrong({"amount": [refused[purpose]]})
    change = in_unit(entity, given, values)
    if change > amount.value:
        raise Wrong(
            {
                "amount": [
                    f"is more than {entity.hrid} has: {shown(change)} {amount.unit} of "
                    f"{shown(amount.value)} {amount.unit}."
                ]
            }
        )
    return change


def _change(
    user: User | None,
    entity: Entity,
    amount: Amount,
    kind: str,
    change: Decimal,
    given: Given,
    reason: str,
    *,
    purpose: str | None = None,
    counterpart: Entity | None = None,
    operation: Any = None,  # noqa: ANN401 - a UUID
    used_in: Entity | None = None,
    source: Entity | None = None,
    confirmed: str = "",
) -> Movement:
    amount.value = _EXACT.add(amount.value, change)
    wanted = spec(entity.type)
    assert wanted is not None  # noqa: S101 - only records with an amount get here
    _whole_units(entity, wanted, amount.value)
    Amount.objects.filter(pk=amount.pk).update(value=amount.value, updated_at=Now())
    return _movement(
        user,
        entity,
        kind,
        change,
        amount.value,
        amount.unit,
        given,
        reason,
        purpose=purpose,
        counterpart=counterpart,
        operation=operation,
        used_in=used_in,
        source=source,
        confirmed=confirmed,
    )


def _movement(
    user: User | None,
    entity: Entity,
    kind: str,
    change: Decimal,
    balance: Decimal,
    unit: str,
    given: Given,
    reason: str,
    *,
    purpose: str | None = None,
    counterpart: Entity | None = None,
    operation: Any = None,  # noqa: ANN401 - a UUID
    used_in: Entity | None = None,
    source: Entity | None = None,
    confirmed: str = "",
) -> Movement:
    movement = Movement.objects.create(
        entity=entity,
        kind=kind,
        purpose=purpose,
        change=change,
        balance=balance,
        unit=unit,
        given_value=given.value,
        given_unit=given.unit,
        counterpart=counterpart,
        used_in=used_in,
        source=source,
        confirmed=confirmed.strip(),
        operation=operation or uuid7(),
        user=user,
        via=current_via(),
        client=current_oauth_client(),
        agent=current_agent(),
        reason=reason.strip(),
    )
    log.record(
        "entity.amount_moved",
        actor=user,
        target=entity,
        project=entity.owner_project,
        data={
            "kind": kind,
            "purpose": purpose,
            "change": shown(change),
            "balance": shown(balance),
            "unit": unit,
            "given": {"value": shown(given.value), "unit": given.unit},
            "counterpart": counterpart.hrid if counterpart is not None else None,
            "operation": str(movement.operation),
            "reason": movement.reason,
            **({"used_in": used_in.hrid} if used_in is not None else {}),
            **({"source": source.hrid} if source is not None else {}),
            **({"confirmed": movement.confirmed} if movement.confirmed else {}),
        },
    )
    return movement


def _copied(source: Entity, current: Mapping[str, Any]) -> dict[str, Any]:
    """What an aliquot of its own type takes from ``source``: its values, but not those
    that are each record's own (unique ones, states, computed ones) or its identity's."""
    from iki_server.entities.values import shape, to_json

    form = shape(source.type)
    out: dict[str, Any] = {}
    for name, f in form.fields().items():
        if f.removed or f.unique or f.computed or f.type == "state":
            continue
        value = current.get(name)
        if value is None or value == []:
            continue
        out[name] = to_json(f, value)
    return out


def _fields(entity_type: EntityType) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for t in reversed([entity_type, *registry.ancestors(entity_type)]):
        out.update(t.fields)
    return out


def _pool(
    user: User,
    source: Entity,
    target: Entity,
    out: Decimal,
    here: units.Unit,
    amount: Amount,
    reason: str,
) -> None:
    """Sets ``target``'s concentration to its pool's, as ``out`` (in ``here``) of
    ``source`` is moved into it: the solute in both over the volume of both, which a
    mass draw converts through. Unknown (empty) when ``source``'s concentration isn't
    recorded, or the amounts can't be put in volumes; ``source``'s, into an empty one.
    Refused if ``target`` can't take the change, such as when it's locked."""
    from iki_server.entities import services

    wanted = spec(target.type)
    assert wanted is not None  # noqa: S101 - only records with an amount get here
    if wanted.concentration is None:
        return
    field = _fields(registry.types()[target.type])[wanted.concentration]
    assert field.unit is not None  # noqa: S101 - a concentration is a quantity
    per = units.parse(field.unit)
    values = services.current_values_of([target])[target.pk]
    current = values.get(wanted.concentration)
    theirs = _concentration(source, per)
    held = _solution(Fraction(amount.value), units.parse(amount.unit), current, per)
    added = _solution(Fraction(out), here, theirs, per)
    pooled: float | None = None
    if amount.value == 0:
        pooled = theirs
    elif held is not None and added is not None:
        (volume, solute), (more, more_solute) = held, added
        pooled = float((solute + more_solute) / (volume + more) / per.factor)
    if pooled == current or (pooled is None and current is None):
        return
    unit = field.unit
    if pooled is None:
        why = (
            f"Pooled with {shown(out)} {here.code} of {source.hrid}, whose "
            f"{field.label.lower()} isn't recorded, so its own isn't known"
        )
    else:
        why = (
            f"Pooled with {shown(out)} {here.code} of {source.hrid} at {theirs:g} {unit}: "
            f"{pooled:g} {unit}"
        )
    if reason.strip():
        why = f"{why}. {reason.strip()}"
    locked = Entity.objects.get(pk=target.pk)
    try:
        services.update(
            user,
            locked,
            {wanted.concentration: None if pooled is None else {"value": pooled, "unit": unit}},
            expected_revision=locked.revision,
            reason=why,
        )
    except (Refused, Invalid) as error:
        said = (
            " ".join(m for ms in error.errors.values() for m in ms)
            if isinstance(error, Invalid)
            else str(error)
        )
        raise Wrong(
            {
                "to": [
                    f"would change {target.hrid}'s {field.label.lower()} to "
                    f"{'not known' if pooled is None else f'{pooled:g} {unit}'}, the pool's, "
                    f"which it can't take: {said}"
                ]
            }
        ) from None


def _concentration(entity: Entity, per: units.Unit) -> float | None:
    """``entity``'s concentration, in ``per``'s unit, if its type has one, it's recorded,
    and it measures what ``per`` does."""
    from iki_server.entities import services

    wanted = spec(entity.type)
    if wanted is None or wanted.concentration is None:
        return None
    field = _fields(registry.types()[entity.type]).get(wanted.concentration)
    value = services.current_values_of([entity])[entity.pk].get(wanted.concentration)
    if field is None or field.unit is None or not isinstance(value, int | float):
        return None
    theirs = units.parse(field.unit)
    if not theirs.is_commensurable(per):
        return None
    return float(Fraction(str(value)) * theirs.factor / per.factor)


_LITRE = units.parse("L")


def _solution(
    value: Fraction, unit: units.Unit, concentration: float | None, per: units.Unit
) -> tuple[Fraction, Fraction] | None:
    """An amount as its volume and its solute, in base units, at ``concentration`` (in
    ``per``): None when they aren't known."""
    if concentration is None:
        return None
    each = Fraction(str(concentration)) * per.factor
    if unit.is_commensurable(_LITRE):
        volume = value * unit.factor
        return volume, volume * each
    if each > 0 and units.parse(f"({per.code}).L").is_commensurable(unit):
        solute = value * unit.factor
        return solute / each, solute
    return None


def _exactly(entity: Entity, unit: str, exact: Fraction, given: Given) -> Decimal:
    """``exact``, in ``entity``'s amount's ``unit``, refused if its 12 places can't hold it:
    0.4 pL of a record kept in L would be 0 L, and 2.5 pL 2 pL."""
    value = _rounded(exact)
    if Fraction(value) != exact:
        raise Wrong(
            {
                "amount": [
                    f"is {shown(given.value)} {given.unit}, which is {_decimal(exact)} {unit}: "
                    f"{entity.hrid}'s amount, kept in {unit} to {PLACES} decimal places, can't "
                    "hold that exactly."
                ]
            }
        )
    return value


def _decimal(exact: Fraction) -> str:
    """An exact quantity as people read it, to the ledger's 80 digits."""
    return shown(_EXACT.divide(Decimal(exact.numerator), Decimal(exact.denominator)))


def _rounded(exact: Fraction) -> Decimal:
    value = _EXACT.divide(Decimal(exact.numerator), Decimal(exact.denominator))
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN, context=_EXACT)


def displayed(wanted: AmountSpec, value: Decimal) -> str | None:
    """``value`` in its type's ``display_unit``, as people read it: ``0.25 mL``. None if
    it's shown in the unit it's kept in."""
    if wanted.display_unit is None:
        return None
    kept, display = units.parse(wanted.unit), units.parse(wanted.display_unit)
    return (
        f"{shown(_rounded(Fraction(value) * kept.factor / display.factor))} {wanted.display_unit}"
    )


def shown(value: Decimal) -> str:
    """An amount as people read it, and as the API gives it: 25, not 25.000000000000 or
    2.5E+1."""
    text = format(value.normalize(_EXACT), "f")
    return "0" if text in ("-0", "") else text
