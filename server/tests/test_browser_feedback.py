# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""What a page says late (a confirmation, a form's problems, a list that answers after the
card it's in is drawn) never moves a control that's being pressed, in real browsers.

Each test presses a control where it was when it could first be pressed, has the late
content come while the press is on, and lets go once it's in: the click lands on what was
pressed, and the click-stability check (conftest.py) finds the press and its release on the
same element. By role, never by label (Firefox; see test_browser.py)."""

import json
import re
from typing import Any, cast

import pytest
from org_world import World
from playwright.sync_api import Page, Request, Route
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

from iki_server.accounts.models import User
from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

#: Two frames: whatever the late content moves has been laid out and painted.
FRAMES = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"


def press(page: Page, target: Any) -> None:
    """Puts the pointer down at the middle of `target`, where it is now."""
    box = target.bounding_box()
    assert box, "it isn't drawn"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()


TAKEN = "A project in Structural Biology is called Kinase Atlas already."


def refuse(route: Route) -> None:
    """Answers New project as the API does when the name's taken."""
    route.fulfill(
        status=422,
        content_type="application/problem+json",
        body=json.dumps(
            {
                "type": "about:blank",
                "title": "Validation failed",
                "status": 422,
                "code": "validation_failed",
                "detail": "Check the highlighted fields.",
                "errors": [{"pointer": "#/name", "detail": TAKEN}],
            }
        ),
    )


def creating(request: Request) -> bool:
    return request.method == "POST" and request.url.endswith("/api/v1/projects")


def test_a_forms_problem_comes_without_moving_whats_pressed(open_as: Any) -> None:
    """New project's answer, a problem with its name, comes as the person presses Cancel:
    the alert at the top of the form and the name's error wait for the press to end, so it
    ends on Cancel. Staying instead, they're told, and focus is on the name."""
    page: Page = open_as("team_member", "/projects/new/")
    page.get_by_role("heading", name="New project", level=1).wait_for()
    held: list[Route] = []
    page.route(
        "**/api/v1/projects",
        lambda route: held.append(route) if creating(route.request) else route.fallback(),
    )
    name = page.get_by_role("textbox", name="Project name")
    name.fill("Kinase Atlas")
    # Sent by its key: the pointer's free for Cancel while the answer comes.
    with page.expect_request(creating):
        name.press("Enter")
    press(page, page.get_by_role("link", name="Cancel"))
    with page.expect_response(lambda r: creating(r.request)):
        refuse(held[0])
    # The answer's in: the name is invalid, though what it says waits for the press.
    page.wait_for_function(
        "id => document.getElementById(id)?.getAttribute('aria-invalid') === 'true'",
        arg=name.get_attribute("id"),
    )
    page.evaluate(FRAMES)
    page.mouse.up()
    page.wait_for_url(re.compile(r"/projects/$"))
    page.get_by_role("heading", name="Projects", level=1).wait_for()

    page.go_back()
    page.get_by_role("heading", name="New project", level=1).wait_for()
    page.unroute("**/api/v1/projects")
    page.route(
        "**/api/v1/projects",
        lambda route: refuse(route) if creating(route.request) else route.fallback(),
    )
    name.fill("Kinase Atlas")
    page.get_by_role("button", name="Create project").click()
    page.get_by_role("alert").get_by_text("Check the highlighted fields.").wait_for()
    page.get_by_text(TAKEN).wait_for()
    page.wait_for_function("id => document.activeElement?.id === id", arg=name.get_attribute("id"))
    assert_clean(page)


def test_a_confirmation_comes_without_moving_whats_pressed(open_as: Any, world: World) -> None:
    """My work's Take answers as the person presses Assigned to me: its confirmation is said
    at the foot of the page, not above the tabs, so the press ends on the tab."""
    from iki_server.entities import services as entities
    from iki_server.org import permissions
    from iki_server.org.models import LabRoleGrant

    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["editor"]),
        role="acme/protein.analyst",
        granted_by=world.users["admin"],
    )
    permissions.forget(world.users["editor"])
    hrid, other = (
        cast(
            "Entity",
            entities.create(
                world.users["admin"], "acme/protein.Sample", world.project, {"label": label}
            ).entity,
        ).hrid
        for label in ("Lot 9", "Lot 10")
    )

    page: Page = open_as("editor", "/work/?open=true")
    page.get_by_role("list", name="Open work").wait_for()
    held: list[Route] = []
    page.route("**/api/v1/entities/*/take", held.append)
    take = page.get_by_role("button", name=f"Take {hrid} as Analyst")
    with page.expect_request("**/api/v1/entities/*/take"):
        take.click()
    tab = page.get_by_role("navigation", name="Show").get_by_role("link", name="Assigned to me")
    press(page, tab)
    with page.expect_response("**/api/v1/entities/*/take"):
        held[0].continue_()
    # Taken, and the list refreshed: what's left is what says so.
    take.wait_for(state="detached")
    page.evaluate(FRAMES)
    page.mouse.up()
    page.wait_for_url(re.compile(r"/work/$"))
    page.get_by_role("list", name="Your work").get_by_role("link", name=hrid, exact=True).wait_for()
    assert_clean(page)

    # Said where it moves nothing: after the page, at the foot of the window.
    page.goto("/work/?open=true")
    page.get_by_role("list", name="Open work").wait_for()
    tab_before = tab.bounding_box()
    page.get_by_role("button", name=f"Take {other} as Analyst").click()
    said = page.get_by_role("status").filter(has_text=f"{other} is yours, as Analyst.")
    said.wait_for()
    assert tab.bounding_box() == tab_before
    viewport = page.viewport_size
    box = said.bounding_box()
    assert viewport and box and box["y"] + box["height"] <= viewport["height"]
    assert_clean(page)


def test_the_requests_shortcut_holds_still_as_the_latest_requests_come(
    open_as: Any, world: World
) -> None:
    """A core's Requests card draws its shortcut to the requests sent to it, then its latest
    requests answer: they come below it, so a press on the shortcut as they come ends on it,
    and opens them. (Firefox lost this click when they came above it.)"""
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

    page: Page = open_as("tech", f"/projects/{core.slug}/")
    heading = page.get_by_role("heading", name="Requests", exact=True)
    heading.wait_for()
    card = page.locator("section", has=heading)
    card.get_by_role("list", name="Latest requests").wait_for()

    def latest(request: Request) -> bool:
        if request.method != "POST" or not request.url.endswith("/api/v1/entities/search"):
            return False
        body = request.post_data_json or {}
        return bool(body.get("sort")) and (body.get("where") or {}).get("field") == "sent_to"

    held: list[Route] = []
    page.route(
        "**/api/v1/entities/search",
        lambda route: held.append(route) if latest(route.request) else route.fallback(),
    )
    with page.expect_request(latest):
        page.reload()
    shortcut = card.get_by_role("link", name="Requests sent to Protein Core")
    shortcut.wait_for()
    assert card.get_by_role("list", name="Latest requests").count() == 0
    press(page, shortcut)
    with page.expect_response(lambda r: latest(r.request)):
        for route in held:
            route.continue_()
    card.get_by_role("list", name="Latest requests").get_by_role(
        "link", name=request.hrid
    ).wait_for()
    page.evaluate(FRAMES)
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
