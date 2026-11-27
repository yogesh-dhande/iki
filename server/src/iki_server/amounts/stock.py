"""Current project stock from the amounts ledger and placement records (ADR 0190)."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from functools import cache
from typing import Any
from uuid import UUID

from django.db.models import F, Model, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from iki_server.accounts.models import User
from iki_server.amounts import eligibility
from iki_server.amounts import services as amounts
from iki_server.amounts.models import DIGITS, DrawPurpose
from iki_server.entities import access, registry, services
from iki_server.entities.models import Entity
from iki_server.org.models import Project
from iki_server.storage.models import Place
from iki_spec.model import StockCatalogue

BATCH = 100
ZERO = Decimal(0)


@cache
def catalogues() -> dict[str, StockCatalogue]:
    configured = {}
    for name, t in registry.types().items():
        if not t.abstract:
            cfg = registry.effective(name).stock_catalogue
            if cfg:
                configured[name] = cfg
    return configured


@cache
def _catalogues_for(*, orders: bool) -> dict[str, StockCatalogue]:
    result = {}
    for name in registry.types():
        declared = registry.effective(name)
        for cfg in catalogues().values():
            if declared.is_a(cfg.orders.type if orders else cfg.lots.type):
                result[name] = cfg
    return result


def check_item(type_name: str, project: Project, values: Mapping[str, Any]) -> dict[str, list[str]]:
    """A stock lot's ledger and its local catalogue item must agree on their unit."""
    lot = _catalogues_for(orders=False).get(type_name)
    cfg = lot or _catalogues_for(orders=True).get(type_name)
    if cfg is None:
        return {}
    field = cfg.lots.item if lot else cfg.orders.item
    item = items(project).filter(pk=values[field]).first()
    if item is None or catalogues()[item.type] != cfg:
        return {field: ["Choose an active catalogue item owned by this project."]}
    if lot is None:
        return {}
    unit = services.current_values_of([item])[item.pk][cfg.unit]
    spec = amounts.spec(type_name)
    if spec is None or unit != spec.unit:
        return {
            field: [
                f"The catalogue item is counted in {unit}. Choose a lot type with that ledger unit."
            ]
        }
    return {}


def items(project: Project) -> QuerySet[Entity]:
    return Entity.objects.filter(
        owner_project=project,
        type__in=catalogues(),
        archived_at__isnull=True,
        purged_at__isnull=True,
    )


def _rows(name: str, project: Project) -> QuerySet[Model]:
    return registry.model(name)._default_manager.filter(
        owner_project=project, purged_at__isnull=True
    )


def lots(project: Project, cfg: StockCatalogue, item_ids: Sequence[UUID]) -> QuerySet[Entity]:
    rows = _rows(cfg.lots.type, project).filter(**{f"{cfg.lots.item}_id__in": item_ids})
    return Entity.objects.filter(pk__in=rows.values("pk"))


def _open_orders(project: Project, cfg: StockCatalogue) -> QuerySet[Model]:
    order = cfg.orders
    return _rows(order.type, project).filter(
        archived_at__isnull=True,
        **{
            f"{order.item}_id__in": items(project)
            .filter(type__in=[t for t, c in catalogues().items() if c == cfg])
            .values("pk"),
            f"{order.ordered}__gt": Coalesce(F(order.received), 0),
        },
    )


def open_orders(project: Project) -> QuerySet[Entity]:
    condition = Q(pk__in=[])
    for cfg in set(catalogues().values()):
        condition |= Q(pk__in=_open_orders(project, cfg).values("pk"))
    return Entity.objects.filter(condition)


@dataclass
class StockSummary:
    id: UUID
    hrid: str
    name: str
    unit: str
    #: None for an item the lab doesn't reorder by its level: it's never low.
    threshold: str | None
    physical: str
    stored: str
    available: str
    low: bool
    outstanding_packs: int


@dataclass
class StockLot:
    id: UUID
    hrid: str
    lot_number: str | None
    expires_on: date | None
    expired: bool
    held: str | None
    unit: str
    amount: str
    available: str
    container_id: UUID | None
    container_hrid: str | None
    row: int | None
    column: int | None
    checked_out: bool
    placed: bool


@dataclass
class OpenOrder:
    id: UUID
    hrid: str
    order_reference: str | None
    item_id: UUID
    item_hrid: str
    item_name: str
    ordered_packs: int
    received_packs: int
    outstanding_packs: int


@dataclass
class _Balance:
    item_id: UUID
    lot: Entity
    number: str | None
    expiry: date | None
    expired: bool
    held: str | None
    unit: str
    physical: Decimal
    stored: Decimal
    available: Decimal
    place: Place | None


def _balances(cfg: StockCatalogue, records: list[Entity]) -> list[_Balance]:
    amounts_by_id = amounts.amounts_of(records)
    values = services.current_values_of(records)
    places = {p.entity_id: p for p in Place.objects.filter(entity_id__in=[r.pk for r in records])}
    today = datetime.now(UTC).date()
    result = []
    for lot in records:
        fields = values[lot.pk]
        amount = amounts_by_id.get(lot.pk)
        unit = amount.unit if amount else amounts.spec(lot.type).unit  # type: ignore[union-attr]
        value = amount.value if amount else ZERO
        place = places.get(lot.pk)
        stored = value if place and place.container_id and not place.custodian_id else ZERO
        raw_expiry = fields[cfg.lots.expiry]
        expiry = date.fromisoformat(str(raw_expiry)) if raw_expiry else None
        expired = expiry is not None and expiry <= today
        hold = amounts.held(lot, fields)
        reason = f"Stock is held by its {hold} state." if hold else None
        if lot.archived_at is not None:
            reason = "The lot is archived."
        if reason is None:
            reason = eligibility.refusals(lot, [DrawPurpose.ROUTINE], fields).get(
                DrawPurpose.ROUTINE
            )
            # Followed records may live in another project. Their identifiers stay private.
            if reason and eligibility.follows(lot.type):
                reason = "The lot or its source is ineligible for routine use."
        if expired and reason is None:
            reason = "The lot has expired."
        result.append(
            _Balance(
                UUID(str(fields[cfg.lots.item])),
                lot,
                fields[cfg.lots.number],
                expiry,
                expired,
                reason,
                unit,
                value,
                stored,
                ZERO if reason else stored,
                place,
            )
        )
    return result


def lot_details(user: User, item: Entity, records: list[Entity]) -> list[StockLot]:
    cfg = catalogues()[item.type]
    unit = services.current_values_of([item])[item.pk][cfg.unit]
    balances = _balances(cfg, records)
    containers = access.visible(user).in_bulk(
        {b.place.container_id for b in balances if b.place and b.place.container_id}
    )
    result = []
    for b in balances:
        if b.unit != unit:
            b.available = ZERO
            b.held = f"The ledger unit differs from the catalogue unit ({unit})."
        place = b.place
        container = containers.get(place.container_id) if place else None
        result.append(
            StockLot(
                b.lot.pk,
                b.lot.hrid,
                b.number,
                b.expiry,
                b.expired,
                b.held,
                b.unit,
                amounts.shown(b.physical),
                amounts.shown(b.available),
                container.pk if container else None,
                container.hrid if container else None,
                place.row if place and container else None,
                place.column if place and container else None,
                bool(place and place.custodian_id),
                bool(place and place.container_id),
            )
        )
    return result


def summaries(project: Project, records: list[Entity]) -> list[StockSummary]:
    """Stream a catalogue page's lots in keyset windows, including items with no lots."""
    if any(item.owner_project_id != project.pk for item in records):
        raise ValueError("Stock items must belong to the requested project.")
    by_config: dict[StockCatalogue, list[UUID]] = defaultdict(list)
    values = services.current_values_of(records)
    totals = {r.pk: [ZERO, ZERO, ZERO] for r in records}
    pending: dict[UUID, int] = {}
    for item in records:
        by_config[catalogues()[item.type]].append(item.pk)
    with localcontext() as ctx:
        ctx.prec = 2 * DIGITS
        for cfg, ids in by_config.items():
            selected = lots(project, cfg, ids).order_by("pk")
            after = None
            while batch := list((selected.filter(pk__gt=after) if after else selected)[:BATCH]):
                for b in _balances(cfg, batch):
                    if b.unit != values[b.item_id][cfg.unit]:
                        continue
                    total = totals[b.item_id]
                    total[0] += b.physical
                    total[1] += b.stored
                    total[2] += b.available
                after = batch[-1].pk
            order = cfg.orders
            grouped = (
                _open_orders(project, cfg)
                .filter(**{f"{order.item}_id__in": ids})
                .values(order.item)
                .annotate(remaining=Sum(F(order.ordered) - Coalesce(F(order.received), 0)))
            )
            pending.update({r[order.item]: r["remaining"] for r in grouped})
    out = []
    for item in records:
        cfg = catalogues()[item.type]
        fields = values[item.pk]
        given = fields[cfg.threshold]
        threshold = None if given is None else Decimal(str(given))
        physical, stored, available = totals[item.pk]
        out.append(
            StockSummary(
                item.pk,
                item.hrid,
                fields[cfg.name] or item.hrid,
                fields[cfg.unit],
                None if threshold is None else amounts.shown(threshold),
                amounts.shown(physical),
                amounts.shown(stored),
                amounts.shown(available),
                threshold is not None and available <= threshold,
                pending.get(item.pk, 0),
            )
        )
    return out


def summary(project: Project, item: Entity) -> StockSummary:
    return summaries(project, [item])[0]


def order_details(project: Project, records: list[Entity]) -> list[OpenOrder]:
    by_type = _catalogues_for(orders=True)
    values = services.current_values_of(records)
    item_ids = [UUID(str(values[r.pk][by_type[r.type].orders.item])) for r in records]
    found = items(project).in_bulk(item_ids)
    item_values = services.current_values_of(list(found.values()))
    result = []
    for record, item_id in zip(records, item_ids, strict=True):
        if item_id not in found:
            continue
        cfg = by_type[record.type]
        item = found[item_id]
        fields = values[record.pk]
        ordered = int(fields[cfg.orders.ordered])
        received = int(fields[cfg.orders.received] or 0)
        result.append(
            OpenOrder(
                record.pk,
                record.hrid,
                fields[cfg.orders.reference],
                item.pk,
                item.hrid,
                item_values[item.pk][cfg.name] or item.hrid,
                ordered,
                received,
                ordered - received,
            )
        )
    return result
