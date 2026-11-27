# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Search everything (ADR 0176) in real browsers: the ⌘K palette opens over the app's pages
and the server's own, finds what its person can read across their projects, is driven by
keyboard as a combobox, passes axe in both themes, and fits a phone's width."""

import re
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, Route, expect
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
from iki_server.searches.models import SavedSearch

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
    page.get_by_role("heading", level=1, name="Lysozyme C").wait_for()
    page.get_by_text(made["c"].hrid, exact=True).first.wait_for()
    assert "/projects/" not in page.url
    assert_clean(page)


def test_enter_on_a_pasted_hrid_opens_that_record_not_what_the_text_before_found(
    open_as: Any, world: World
) -> None:
    """↵ pressed at once after pasting, while the last text's results still show and the
    answer for the new text is on its way, waits for that answer and opens what it found;
    text that finds nothing opens nothing, however quickly ↵ follows it."""
    made = lysozymes(world)
    page = open_as("admin", "/projects/")
    dialog = search_for(page, "lysozyme")
    found = dialog.get_by_role("group", name="Entities").get_by_role("option")
    expect(found.first).to_contain_text(made["b"].hrid)
    held: list[Route] = []
    asked = re.compile(r"/api/v1/search\?.*q=" + re.escape(made["c"].hrid))
    page.route(asked, lambda route: held.append(route))
    with page.expect_request(asked):
        dialog.get_by_role("combobox").fill(made["c"].hrid)
        page.keyboard.press("Enter")
    # Lysozyme B, which "lysozyme" found first, still shows, isn't active, and ↵ didn't
    # open it.
    expect(found.first).to_contain_text(made["b"].hrid)
    expect(found.first).to_have_attribute("aria-selected", "false")
    expect(dialog).to_be_visible()
    assert "/entities/" not in page.url
    assert len(held) == 1
    held[0].continue_()
    page.get_by_role("heading", level=1, name="Lysozyme C").wait_for()
    page.get_by_text(made["c"].hrid, exact=True).first.wait_for()
    assert_clean(page)
    page.unroute(asked)
    dialog = search_for(page, "lysozyme")
    dialog.get_by_role("combobox").fill("nothing like it")
    page.keyboard.press("Enter")
    expect(dialog).to_contain_text("Nothing you can read matches “nothing like it”.")
    expect(dialog).to_be_visible()
    assert made["c"].hrid in page.url


def test_a_saved_view_the_grid_cant_show_opens_on_its_own_page(open_as: Any, world: World) -> None:
    """Saved with the API for an exact type alone, it can't open in the grid: found by
    search, it opens where the sidebar's does, on the page that says so (ADR 0176)."""
    SavedSearch.objects.create(
        project=world.project,
        name="Proteins of this type only",
        query={"type": "acme/protein.Protein", "exact_type": True},
        created_by=world.users["admin"],
    )
    page = open_as("admin", "/projects/")
    dialog = open_palette(page)
    dialog.get_by_role("combobox").fill("this type only")
    view = dialog.get_by_role("group", name="Saved views").get_by_role("option")
    expect(view).to_contain_text("Proteins of this type only")
    page.keyboard.press("Enter")
    page.get_by_role("heading", level=1, name="Proteins of this type only").wait_for()
    explained = page.get_by_text("conditions the grid can’t show, so it can’t open there")
    expect(explained).to_be_visible()
    assert "/saved-searches/" in page.url


@pytest.mark.parametrize("focus_link", [False, True], ids=["disclosure", "child-link"])
def test_open_sidebar_section_keeps_focus_when_saved_views_arrive(
    open_as: Any, world: World, focus_link: bool
) -> None:
    saved = SavedSearch.objects.create(
        project=world.project, name="My recent work", query={}, created_by=world.users["admin"]
    )
    page = open_as("admin", "/projects/")
    nav = page.get_by_role("navigation", name="Main")
    saved_link = nav.get_by_role("link", name=saved.name)
    saved_link.wait_for()
    deferred: list[Route] = []
    saved_views = "**/api/v1/saved-searches?*"
    page.route(saved_views, lambda route: deferred.append(route))
    with page.expect_request(saved_views):
        page.reload()

    section = nav.get_by_role("button", name="Entities")
    section.focus()
    page.keyboard.press("Enter")
    expect(section).to_have_attribute("aria-expanded", "true")
    child = nav.get_by_role("link", name="All entities")
    expect(child).to_be_visible()
    chevron = section.locator("svg:last-child path")
    original_chevron = chevron.evaluate_handle("path => path")
    focused = child if focus_link else section
    focused.focus()
    expect(focused).to_be_focused()

    assert len(deferred) == 1
    deferred[0].continue_()
    expect(saved_link).to_be_visible()
    expect(section).to_have_attribute("aria-expanded", "true")
    expect(child).to_be_visible()
    expect(focused).to_be_focused()
    assert chevron.evaluate("(path, original) => path === original", original_chevron)
    assert_clean(page)


def test_escape_restores_the_sidebar_link_after_its_saved_views_arrive(
    open_as: Any, world: World
) -> None:
    saved = SavedSearch.objects.create(
        project=world.project, name="My recent work", query={}, created_by=world.users["admin"]
    )
    page = open_as("admin", "/projects/")
    saved_link = page.locator("#sidebar").get_by_text(saved.name, exact=True)
    saved_link.wait_for()
    deferred: list[Route] = []
    saved_views = "**/api/v1/saved-searches?*"
    page.route(saved_views, lambda route: deferred.append(route))
    # Hold the real saved-views response until focus is inside the palette.
    with page.expect_request(saved_views):
        page.reload()
    opener = page.get_by_role("link", name="All projects", exact=True)
    opener.focus()
    dialog = open_palette(page)
    expect(dialog.get_by_role("combobox")).to_be_focused()

    assert len(deferred) == 1
    deferred[0].continue_()
    saved_link.wait_for(state="attached")
    expect(dialog.get_by_role("combobox")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()


# What the palette's parts measure, as the reader sees them: the input row, the first
# result, its group's title and its project's swatch.
MEASURE = """dialog => {
    const box = dialog.querySelector('[role="combobox"]');
    const row = box.parentElement;
    const esc = row.querySelector("button");
    const option = dialog.querySelector('[role="group"] [role="option"]');
    const title = option.closest('[role="group"]').firstElementChild;
    const mark = option.querySelector("[data-swatch]");
    const rect = (el) => el.getBoundingClientRect();
    const style = (el) => getComputedStyle(el);
    return {
        dialog: rect(dialog).toJSON(),
        row: rect(row).toJSON(),
        box: rect(box).toJSON(),
        boxFont: style(box).fontSize,
        esc: rect(esc).toJSON(),
        icons: row.querySelectorAll("svg").length,
        option: rect(option).toJSON(),
        optionFont: style(option).fontSize,
        titleFont: style(title).fontSize,
        mark: mark && style(mark).backgroundColor,
    };
}"""


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_palette_is_sized_for_the_13px_root(
    open_as: Any, world: World, color_scheme: str
) -> None:
    """The mockup's sizes: a 50px input row at 15px with Esc at its end, 13px results in
    34px rows, 11.5px group titles, and each project in its own color."""
    lysozymes(world)
    page = open_as("admin", "/entities/", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    dialog = search_for(page, "lysozyme")
    m = dialog.evaluate(MEASURE)
    assert abs(m["row"]["height"] - 50) <= 1
    assert m["boxFont"] == "15px"
    assert m["icons"] == 1
    # The text box takes the row, and Esc sits at its end, centred on it.
    # 680px, less the modal's border.
    assert m["dialog"]["width"] == pytest.approx(678, abs=1)
    assert m["box"]["width"] >= m["dialog"]["width"] - 120
    assert m["dialog"]["right"] - m["esc"]["right"] <= 20
    row_middle = m["row"]["top"] + m["row"]["height"] / 2
    assert abs(m["esc"]["top"] + m["esc"]["height"] / 2 - row_middle) <= 1
    assert abs(m["option"]["height"] - 34) <= 1
    assert m["optionFont"] == "13px"
    assert m["titleFont"] == "11.5px"
    assert m["mark"] not in (None, "rgba(0, 0, 0, 0)", "transparent")
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
    m = dialog.evaluate(MEASURE)
    assert abs(m["row"]["height"] - 50) <= 1
    assert m["boxFont"] == "15px"
    assert m["optionFont"] == "13px"
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
