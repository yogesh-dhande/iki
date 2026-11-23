# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A stock manager checks usable inventory and receives the rest of a partial order.

The browser test installation has its own fixture plugins. Supply a synthetic catalogue,
its actual API shapes and two available actions, while retaining the real session,
project permissions, route, rendering, and action URLs.
"""

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from org_world import World
from playwright.sync_api import Locator, Page, Route, expect
from test_browser import (  # noqa: F401 - fixtures
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    world,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

ITEM: dict[str, Any] = {
    "id": str(UUID(int=1)),
    "hrid": "SCI-000001",
    "name": "Synthetic reference powder",
    "unit": "mg",
    "threshold": "25",
    "physical": "65",
    "stored": "45",
    "available": "15",
    "low": True,
    "outstanding_packs": 7,
}
OTHER = {
    **ITEM,
    "id": str(UUID(int=2)),
    "hrid": "SCI-000002",
    "name": "Synthetic spare powder",
    "physical": "55",
    "stored": "55",
    "available": "55",
    "low": False,
    "outstanding_packs": 0,
}
ORDER: dict[str, Any] = {
    "id": str(UUID(int=3)),
    "hrid": "ROL-000001",
    "order_reference": "TEST-PO-007",
    "item_id": ITEM["id"],
    "item_hrid": ITEM["hrid"],
    "item_name": ITEM["name"],
    "ordered_packs": 12,
    "received_packs": 5,
    "outstanding_packs": 7,
}


def synthetic_lot(
    index: int,
    number: str,
    quantity: str,
    usable: str,
    *,
    expires_on: str | None = None,
    held: str | None = None,
    checked_out: bool = False,
) -> dict[str, Any]:
    return {
        "id": str(UUID(int=20 + index)),
        "hrid": f"RMG-{index:06}",
        "lot_number": number,
        "unit": "mg",
        "amount": quantity,
        "available": usable,
        "expires_on": expires_on,
        "held": held,
        "expired": expires_on is not None,
        "checked_out": checked_out,
        "placed": not checked_out,
        "container_id": None if checked_out else str(UUID(int=10)),
        "container_hrid": None if checked_out else "SLC-000001",
        "row": None if checked_out else 1,
        "column": None if checked_out else index,
    }


LOTS = [
    synthetic_lot(1, "LOT-USABLE", "15", "15"),
    synthetic_lot(2, "LOT-HELD", "12", "0", held="Pending lot review"),
    synthetic_lot(3, "LOT-EXPIRED", "18", "0", expires_on="2000-01-01"),
    synthetic_lot(4, "LOT-CHECKED-OUT", "20", "0", checked_out=True),
]


def supply_catalogue(page: Page, world: World) -> None:
    base = f"/api/v1/projects/{world.project.pk}/stock"

    def stock(route: Route) -> None:
        url = urlsplit(route.request.url)
        if url.path == base:
            query = parse_qs(url.query)
            low_only = query.get("low_only") == ["true"]
            if low_only and "cursor" not in query:
                # The first scanned page contains no low-stock items. More remain.
                route.fulfill(json={"items": [], "next_cursor": "catalogue-page-two"})
                return
            rows = [ITEM] if low_only else [ITEM, OTHER]
        elif url.path == f"{base}/orders":
            rows = [ORDER]
        elif url.path == f"{base}/{ITEM['id']}/lots":
            rows = LOTS
        else:
            route.fallback()
            return
        route.fulfill(json={"items": rows, "next_cursor": None})

    def installed_actions(route: Route) -> None:
        actions = route.fetch().json()
        template = next(action for action in actions if action["tier"] == "backend")
        for name, label in [
            ("place_order", "Place order"),
            ("receive_delivery", "Receive delivery"),
        ]:
            actions.append(
                {
                    **template,
                    "name": f"iki/reagents.{name}",
                    "label": label,
                    "plugin": "iki/reagents",
                    "available": True,
                }
            )
        route.fulfill(json=actions)

    def installed_types(route: Route) -> None:
        types = route.fetch().json()
        template = next(t for t in types if t["name"] == "acme/protein.Sample")
        types.append(
            {
                **template,
                "name": "iki/reagents.StockItem",
                "label": "Stock item",
                "label_plural": "Stock items",
            }
        )
        route.fulfill(json=types)

    page.route(f"**{base}**", stock)
    page.route("**/api/v1/actions", installed_actions)
    page.route("**/api/v1/entity-types", installed_types)


def check_action(link: Locator, slug: str, action: str, hrid: str) -> None:
    href = link.get_attribute("href")
    assert href is not None
    url = urlsplit(href)
    assert url.path.rstrip("/") == f"/projects/{slug}/runs/new"
    query = parse_qs(url.query)
    assert query["action"] == [f"iki/reagents.{action}"]
    assert query["entities"] == [hrid]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_project_stock_and_outstanding_deliveries(
    open_as: Any,
    world: World,
    color_scheme: str,
) -> None:
    page: Page = open_as("admin", "/projects/", color_scheme)
    supply_catalogue(page, world)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"/projects/{world.project.slug}/")
    page.get_by_role("navigation", name="Project", exact=True).get_by_role(
        "link", name="Stock", exact=True
    ).click()
    expect(page.get_by_role("heading", level=2, name="Stock", exact=True)).to_be_visible()
    catalogue = page.get_by_role("table", name="Catalogue stock")
    item = (
        catalogue.get_by_role("row")
        .filter(has=page.get_by_role("link", name="Place order"))
        .filter(has_text=ITEM["name"])
    )
    expect(item.get_by_text("Low stock", exact=True)).to_be_visible()
    for column, amount in (
        ("Physical", "65"),
        ("In storage", "45"),
        ("Usable", "15"),
        ("Reorder level", "25"),
    ):
        index = catalogue.get_by_role("columnheader", name=column, exact=True).evaluate(
            "th => th.cellIndex"
        )
        expect(item.locator(":scope > td, :scope > th").nth(index)).to_contain_text(f"{amount} mg")
    spare = catalogue.get_by_role("link", name=OTHER["name"], exact=True)
    expect(spare).to_be_visible()
    low_only = page.get_by_role("checkbox", name="Low stock only")
    low_only.focus()
    low_only.press("Space")
    expect(low_only).to_be_checked()
    expect(item).to_be_visible()
    expect(spare).to_have_count(0)
    check_action(
        item.get_by_role("link", name="Place order"),
        world.project.slug,
        "place_order",
        str(ITEM["hrid"]),
    )
    expect(item.get_by_role("cell", name="7", exact=True)).to_be_visible()
    show = page.get_by_role("button", name=f"Show lots for {ITEM['name']}")
    show.click()

    lots = page.get_by_role("table", name=f"Lots for {ITEM['name']}")
    usable = lots.get_by_role("row").filter(has_text="LOT-USABLE")
    expect(usable.get_by_role("link", name="SLC-000001")).to_have_attribute(
        "href", "/storage/SLC-000001/"
    )
    expect(usable).to_contain_text("Row 1 · column 1")
    held = lots.get_by_role("row").filter(has_text="LOT-HELD")
    expect(held).to_contain_text("Pending lot review")
    expect(held.get_by_role("cell", name="12 mg", exact=True)).to_be_visible()
    expired = lots.get_by_role("row").filter(has_text="LOT-EXPIRED")
    expect(expired.get_by_role("cell", name="18 mg", exact=True)).to_be_visible()
    expect(expired.get_by_text("Expired", exact=True)).to_be_visible()
    expect(expired).to_contain_text("2000")
    checked_out = lots.get_by_role("row").filter(has_text="LOT-CHECKED-OUT")
    expect(checked_out.get_by_text("Checked out", exact=True)).to_be_visible()
    for excluded in (expired, held, checked_out):
        expect(excluded.get_by_role("cell", name=re.compile(r"^0\s*mg$"))).to_be_visible()

    orders = page.get_by_role("table", name="Open orders")
    order = orders.get_by_role("row").filter(has_text=str(ORDER["order_reference"]))
    for column, amount in (
        ("Ordered packs", "12"),
        ("Received packs", "5"),
        ("Outstanding packs", "7"),
    ):
        index = orders.get_by_role("columnheader", name=column, exact=True).evaluate(
            "th => th.cellIndex"
        )
        expect(order.locator(":scope > td, :scope > th").nth(index)).to_have_text(amount)
    check_action(
        order.get_by_role("link", name="Receive delivery"),
        world.project.slug,
        "receive_delivery",
        str(ORDER["hrid"]),
    )
    assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth <= 1")
    assert_clean(page, axe_in_firefox=False)
    page.context.close()
