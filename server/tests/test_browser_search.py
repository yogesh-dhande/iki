# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Search everything (ADR 0176) in real browsers: the ⌘K palette opens over the app's pages
and the server's own, finds what its person can read across their projects, is driven by
keyboard as a combobox, passes axe in both themes, and fits a phone's width."""

from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, expect
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

from iki_server.entities import services as entities
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"


def lysozymes(world: World) -> dict[str, Any]:
    """Lysozymes in two projects the admin reads, and one in a project they can't."""
    admin = world.users["admin"]
    enzymes = Project.objects.create(team=world.team, name="Enzymes")
    ProjectMembership.objects.create(project=enzymes, user=admin, role=ProjectRole.EDITOR)
    hidden = Project.objects.create(team=world.team, name="Hidden Screens")
    ProjectMembership.objects.create(
        project=hidden, user=world.users["editor"], role=ProjectRole.EDITOR
    )

    def protein(project: Project, sequence: str, name: str) -> Any:
        values = {"sequence": sequence, "name": name}
        return entities.create(
            admin if project != hidden else world.users["editor"],
            "acme/protein.Protein",
            project,
            values,
        ).entity

    return {
        "c": protein(world.project, "KVFGRCELAAAMKRHGLDNY", "Lysozyme C"),
        "b": protein(enzymes, "KVFERCELARTLKRLGMDGY", "Lysozyme B"),
        "hidden": protein(hidden, "KIFERCELARTLKRLGLDGY", "Lysozyme hidden"),
    }


def open_palette(page: Page) -> Any:
    page.keyboard.press("ControlOrMeta+k")
    dialog = page.get_by_role("dialog", name="Search everything")
    dialog.wait_for()
    return dialog


def search_for(page: Page, text: str) -> Any:
    dialog = open_palette(page)
    box = dialog.get_by_role("combobox")
    expect(box).to_be_focused()
    box.fill(text)
    dialog.get_by_role("group", name="Entities").wait_for()
    return dialog


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_palette_finds_what_you_read_across_projects_and_opens_it(
    open_as: Any, world: World, color_scheme: str
) -> None:
    made = lysozymes(world)
    page = open_as("admin", "/projects/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    trigger = page.get_by_role("link", name="Projects").first
    trigger.focus()
    dialog = search_for(page, "lysozyme")
    found = dialog.get_by_role("group", name="Entities").get_by_role("option")
    expect(found).to_have_count(2)
    expect(found.first).to_contain_text(made["b"].hrid)
    expect(found.first).to_contain_text("Enzymes")
    expect(found.nth(1)).to_contain_text("Kinase Atlas")
    expect(dialog).not_to_contain_text("Lysozyme hidden")
    expect(dialog.get_by_role("button", name="Entities 2")).to_be_visible()
    expect(dialog).to_contain_text("In all 3 projects you can read")
    assert_clean(page)
    # Esc closes it, and focus goes back where it was.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(trigger).to_be_focused()
    # ↓ then ↵ opens the second, in the app.
    dialog = search_for(page, "lysozyme")
    box = dialog.get_by_role("combobox")
    second = dialog.get_by_role("option", name=f"{made['c'].hrid} Lysozyme C", exact=False)
    page.keyboard.press("ArrowDown")
    expect(second).to_have_attribute("aria-selected", "true")
    assert box.get_attribute("aria-activedescendant") == second.get_attribute("id")
    page.keyboard.press("Enter")
    # The second one's page: both are called Lysozyme C, so its ID, beside the title, says.
    page.wait_for_url(f"**/entities/{made['c'].hrid}/**")
    page.get_by_role("heading", level=1, name="Lysozyme C").wait_for()
    page.locator(".record-title").get_by_text(made["c"].hrid, exact=True).wait_for()
    assert "/projects/" not in page.url
    assert_clean(page)


def test_the_palette_fits_a_phone(open_as: Any, world: World) -> None:
    lysozymes(world)
    page = open_as("admin", "/work/")
    page.set_viewport_size({"width": 390, "height": 844})
    dialog = search_for(page, "lysozyme")
    expect(dialog.get_by_role("option").first).to_be_visible()
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    box = dialog.bounding_box()
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= 390
    # On a phone, Esc is a button to press.
    assert_clean(page)
    dialog.get_by_role("button", name="Esc: close the search").click()
    expect(dialog).to_be_hidden()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_palette_opens_on_the_servers_own_pages(
    open_as: Any, world: World, color_scheme: str
) -> None:
    made = lysozymes(world)
    page = open_as("org_admin", "/teams/", color_scheme)
    # The org admin reads no project: searching finds the projects they see, and people.
    dialog = open_palette(page)
    dialog.get_by_role("combobox").fill("enzymes")
    option = dialog.get_by_role("group", name="Projects").get_by_role("option").first
    expect(option).to_contain_text("Enzymes")
    expect(dialog).not_to_contain_text(made["b"].hrid)
    assert_clean(page)
    page.keyboard.press("Enter")
    page.wait_for_url("**/projects/enzymes/")
