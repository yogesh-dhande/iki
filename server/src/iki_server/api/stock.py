"""Live project inventory, lot locations and open reorder lines (ADR 0190)."""

from typing import Any
from uuid import UUID

from django.http import HttpRequest
from ninja import Query, Router

from iki_server.amounts import stock
from iki_server.api.common import caller, project
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.core.snapshots import snapshot
from iki_server.org.permissions import Action

router = Router(tags=["stock"])


class StockFilters(PageParams):
    low_only: bool = False


@router.get(
    "/projects/{project_id}/stock",
    response=Page[stock.StockSummary],
    summary="Current stock and reorder thresholds",
)
@snapshot()
def project_stock(
    request: HttpRequest, project_id: UUID, filters: Query[StockFilters]
) -> dict[str, Any]:
    """All active catalogue items owned by the project, including items with no lots.
    Physical is the amount on hand. Stored excludes unplaced and checked-out material.
    Available counts stored material eligible for routine use. Amounts are exact decimal
    strings in the item's unit. A low-only page can be empty and have a next cursor."""
    found = project(caller(request), project_id, Action.PROJECT_READ)
    result = page(stock.items(found), filters)
    result["items"] = [
        item for item in stock.summaries(found, result["items"]) if not filters.low_only or item.low
    ]
    return result


@router.get(
    "/projects/{project_id}/stock/orders",
    response=Page[stock.OpenOrder],
    summary="Outstanding stock reorders",
)
@snapshot()
def stock_orders(
    request: HttpRequest, project_id: UUID, filters: Query[PageParams]
) -> dict[str, Any]:
    found = project(caller(request), project_id, Action.PROJECT_READ)
    result = page(stock.open_orders(found), filters)
    result["items"] = stock.order_details(found, result["items"])
    return result


@router.get(
    "/projects/{project_id}/stock/{item_id}/lots",
    response=Page[stock.StockLot],
    summary="A catalogue item's lots and storage positions",
)
@snapshot()
def stock_lots(
    request: HttpRequest, project_id: UUID, item_id: UUID, filters: Query[PageParams]
) -> dict[str, Any]:
    user = caller(request)
    found = project(user, project_id, Action.PROJECT_READ)
    item = stock.items(found).filter(pk=item_id).first()
    if item is None:
        raise Problem(404, "No such stock item in this project.")
    cfg = stock.catalogues()[item.type]
    result = page(stock.lots(found, cfg, [item.pk]), filters)
    result["items"] = stock.lot_details(user, item, result["items"])
    return result
