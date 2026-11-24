# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A project's Overview holds still under a press as its late reads answer, in real
browsers: its cards are drawn together once each has what decides its lines, and what's
left to come (a service's latest requests) comes after its links, at the foot of the last
card. By role, never by label (Firefox; see test_browser.py)."""

import re
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, Request, Route
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


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_press_on_the_overview_ends_where_it_began_as_its_reads_answer(
    open_as: Any, world: World, color_scheme: str, width: int
) -> None:
    """A core's technician opens its Overview. Its cards wait for what changed lately, so
    none is drawn, and nothing can be pressed, till it's in. Then the shortcut to its queue
    is pressed while its latest requests are still to come: they come below it, and the
    press ends on it, opening the queue. (Firefox lost this click when they came above.)"""
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

    page: Page = open_as("tech", f"/projects/{core.slug}/", color_scheme)
    page.set_viewport_size({"width": width, "height": 844})
    heading = page.get_by_role("heading", name="Requests", exact=True)
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_role("list", name="Latest requests").wait_for()

    held: dict[str, list[Route]] = {"changes": [], "latest": []}

    def hold(route: Route) -> None:
        if latest_requests(route.request):
            held["latest"].append(route)
        elif recent_changes(route.request):
            held["changes"].append(route)
        else:
            route.fallback()

    page.route("**/api/v1/**", hold)
    with page.expect_request(recent_changes), page.expect_request(latest_requests):
        page.reload()
    page.get_by_text("Opening its overview…").wait_for()
    shortcut = card.get_by_role("link", name="Requests sent to Protein Core")
    # Nothing on the Overview is drawn while a card's answer is still to come.
    assert not shortcut.is_visible()
    assert not page.get_by_role(
        "link", name="Open in Entities, filtered to this project"
    ).is_visible()
    for route in held["changes"]:
        route.continue_()
    page.get_by_role("list", name="Recent changes").wait_for()
    shortcut.wait_for()
    assert page.get_by_text("Opening its overview…").count() == 0
    assert card.get_by_role("list", name="Latest requests").count() == 0

    # Pressed where it's drawn, as the latest requests come.
    shortcut.scroll_into_view_if_needed()
    page.evaluate(NEXT_FRAMES)
    before = shortcut.bounding_box()
    assert before, "it isn't drawn"
    page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
    page.mouse.down()
    with page.expect_response(lambda r: latest_requests(r.request)):
        for route in held["latest"]:
            route.continue_()
    card.get_by_role("list", name="Latest requests").get_by_role(
        "link", name=request.hrid
    ).wait_for()
    page.evaluate(NEXT_FRAMES)
    assert shortcut.bounding_box() == before
    page.mouse.up()

    page.wait_for_url(re.compile(r"/requests/\?side=received$"))
    tabs = page.get_by_role("navigation", name="Project")
    tab = tabs.get_by_role("link", name=re.compile(r"^Requests\b"))
    tab.wait_for()
    assert tab.get_attribute("aria-current") == "page"
    page.get_by_role("table", name="1 assay request", exact=True).get_by_role(
        "link", name=request.hrid
    ).wait_for()
    assert_clean(page)

    # The Overview, as it's drawn, passes axe too.
    page.unroute("**/api/v1/**")
    page.go_back()
    card.get_by_role("list", name="Latest requests").get_by_role(
        "link", name=request.hrid
    ).wait_for()
    assert_clean(page)
