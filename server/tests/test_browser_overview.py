# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A project's Overview holds still under a press as its late reads answer, in real
browsers. Its cards are drawn together once each has what decides its lines, or once its
budget is spent: a card still waiting then holds its slot, where its answer comes, and
what can only grow (a service's latest requests) comes below everything that can be
pressed. By role, never by label (Firefox; see test_browser.py)."""

import re
from collections.abc import Callable
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Locator, Page, Request, Route
from test_browser import (  # noqa: F401 - fixtures
    NEXT_FRAMES,
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    world,
)

from iki_server.accounts.models import User
from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]


def latest_requests(request: Request) -> bool:
    """The Requests card's search for the latest requests sent to the service (sorted, as
    the search for whether any were sent isn't)."""
    if request.method != "POST" or not request.url.endswith("/api/v1/entities/search"):
        return False
    body = request.post_data_json or {}
    return bool(body.get("sort")) and (body.get("where") or {}).get("field") == "sent_to"


def recent_changes(request: Request) -> bool:
    return request.method == "GET" and bool(
        re.search(r"/api/v1/projects/[^/]+/changes", request.url)
    )


def a_core(world: World) -> tuple[Project, Entity]:
    """A core facility that takes assay requests, its technician, and one sent to it."""
    from iki_server.entities import requests
    from iki_server.entities import services as entities

    core = Project.objects.create(team=world.team, name="Protein Core", description="Assays")
    tech = User.objects.create_user("tech@example.com", "Tech")
    world.users["tech"] = tech
    ProjectMembership.objects.create(project=core, user=tech, role=ProjectRole.EDITOR)
    requests.set_intake(
        core,
        types=["acme/protein.AssayRequest"],
        from_registry=True,
        sources=[],
        by=world.users["admin"],
    )
    editor = world.users["editor"]
    sent = entities.create(
        editor, "acme/protein.AssayRequest", world.project, {"title": "Purity of the kinases"}
    )
    request = Entity.objects.get(pk=sent.entity.pk)
    requests.submit(editor, request, core, expected_revision=request.revision)
    # Its own records, so it has changes and counts of its own.
    entities.create(world.users["admin"], "acme/protein.Sample", core, {"label": "Lot 7"})
    return core, request


def opened(open_as: Any, core: Project, color_scheme: str, width: int) -> Page:
    """The core's Overview, as its technician sees it, every card in."""
    page: Page = open_as("tech", f"/projects/{core.slug}/", color_scheme)
    page.set_viewport_size({"width": width, "height": 844})
    page.get_by_role("list", name="Latest requests").wait_for()
    page.get_by_role("list", name="Recent changes").wait_for()
    return page


def hold(page: Page, which: Callable[[Request], bool]) -> list[Route]:
    """Holds back the reads `which` picks, on the next load of the page."""
    held: list[Route] = []
    page.route(
        "**/api/v1/**",
        lambda route: held.append(route) if which(route.request) else route.fallback(),
    )
    with page.expect_request(which):
        page.reload()
    return held


def press_while(page: Page, target: Locator, held: list[Route], came: Locator) -> None:
    """Presses `target` where it's drawn, lets the held reads answer, and lets go once
    what they bring (`came`) is drawn: `target` hasn't moved meanwhile."""
    target.scroll_into_view_if_needed()
    page.evaluate(NEXT_FRAMES)
    before = target.bounding_box()
    assert before, "it isn't drawn"
    page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
    page.mouse.down()
    for route in held:
        route.continue_()
    came.wait_for()
    page.evaluate(NEXT_FRAMES)
    assert target.bounding_box() == before
    page.mouse.up()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_press_on_the_overview_ends_where_it_began_as_its_reads_answer(
    open_as: Any, world: World, color_scheme: str, width: int
) -> None:
    """A core's technician presses the shortcut to its queue while its latest requests are
    still to come: they come below it, and the press ends on it, opening the queue.
    (Firefox lost this click when they came above.)"""
    core, request = a_core(world)
    page = opened(open_as, core, color_scheme, width)
    held = hold(page, latest_requests)
    card = page.locator("section", has=page.get_by_role("heading", name="Requests", exact=True))
    shortcut = card.get_by_role("link", name="Requests sent to Protein Core")
    # Drawn when the budget's spent, without them: they're said to be coming, below it.
    shortcut.wait_for()
    coming = card.get_by_text("Finding its latest requests…")
    assert coming.is_visible()
    assert card.get_by_role("list", name="Latest requests").count() == 0
    box, line = shortcut.bounding_box(), coming.bounding_box()
    assert box and line and line["y"] >= box["y"] + box["height"]

    latest = card.get_by_role("list", name="Latest requests")
    press_while(page, shortcut, held, latest.get_by_role("link", name=request.hrid))
    page.wait_for_url(re.compile(r"/requests/\?side=received$"))
    tabs = page.get_by_role("navigation", name="Project")
    tab = tabs.get_by_role("link", name=re.compile(r"^Requests\b"))
    tab.wait_for()
    assert tab.get_attribute("aria-current") == "page"
    page.get_by_role("table", name="1 assay request", exact=True).get_by_role(
        "link", name=request.hrid
    ).wait_for()
    assert_clean(page)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_press_in_one_card_ends_where_it_began_as_a_slow_card_fills_in(
    open_as: Any, world: World, color_scheme: str, width: int
) -> None:
    """What changed lately is slow: the Overview is drawn without it once its budget is
    spent, with its place held and said to be loading. A link in another card, below it on a
    phone, is pressed as it fills in: nothing moves, and the press opens the members."""
    core, _request = a_core(world)
    page = opened(open_as, core, color_scheme, width)
    held = hold(page, recent_changes)
    changes = page.locator(
        "section", has=page.get_by_role("heading", name="Recent changes", exact=True)
    )
    loading = changes.get_by_text("Finding what changed…")
    loading.wait_for()
    members = page.locator("section", has=page.get_by_role("heading", name="Members", exact=True))
    link = members.get_by_role("link", name="All members")
    assert link.is_visible()
    assert page.get_by_text("Opening its overview…").count() == 0
    assert_clean(page)

    press_while(page, link, held, changes.get_by_role("list", name="Recent changes"))
    page.wait_for_url(re.compile(rf"/projects/{core.slug}/members/$"))
    tabs = page.get_by_role("navigation", name="Project")
    tab = tabs.get_by_role("link", name=re.compile(r"^Members\b"))
    tab.wait_for()
    assert tab.get_attribute("aria-current") == "page"
    assert_clean(page)

    # Back on the Overview, with everything in: drawn whole, no place held.
    page.unroute("**/api/v1/**")
    page.go_back()
    changes.get_by_role("list", name="Recent changes").wait_for()
    assert page.locator(".reserve").count() == 0
    assert_clean(page)
