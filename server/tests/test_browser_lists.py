# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Lab lists (ADR 0270) in real browsers: a protein core's purification methods, curated on
the list's page by its registrar (an option added with its category, relabelled from the
revision read, moved up the list by keyboard, archived), each change with a reason; picked
from a dropdown on a prep's form, which keeps a method archived since; read by its label on
the record, in the grid, in a filter and in a sheet's dropdown cell, where a pasted code
names it too; and a mistyped method superseded, its records moved by their project. Every
step passes axe and fits a phone's width."""

import re
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Locator, Page, expect
from test_browser import (  # noqa: F401 - fixtures
    PAGE_ERRORS,
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    world,
)

from iki_server.entities import entries, lists
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.notifications.models import Notification
from iki_server.org import registries
from iki_server.org.models import Registry

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

METHOD = "acme/protein.PurificationMethod"
PREP = "acme/protein.Prep"
LIST_PAGE = f"/types/{METHOD}/"
NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"


def checked(page: Page) -> None:
    """axe, as on every page; Playwright's Firefox crashes in axe on pages with
    `aria-labelledby` (a dialog's, a menu's: see assert_clean), and the DOM is the same in
    every browser."""
    assert_clean(page, axe_in_firefox=False)


def fits_a_phone(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL), page.url
    checked(page)
    page.set_viewport_size({"width": 1440, "height": 900})


def registrar(world: World) -> Any:
    """The auditor, appointed the default registry's registrar: they curate its lists."""
    user = world.users["auditor"]
    registries.appoint(Registry.default(), user, by=world.users["org_admin"])
    return user


def option(code: str) -> lists.Option:
    found = lists.find(Registry.default().pk, METHOD, code)
    assert found is not None, code
    return found


def prep(world: World, name: str, **values: Any) -> Entity:
    saved = entities.create(world.users["editor"], PREP, world.project, {"name": name, **values})
    return Entity.objects.get(pk=saved.entity.pk)


def values_of(record: Entity) -> dict[str, Any]:
    """A record's values now, as the database has them."""
    return entities.current_values(entities.specific(Entity.objects.get(pk=record.pk)))


def reason(scope: Page | Locator) -> Locator:
    return scope.get_by_role("textbox", name=re.compile("^Reason"))


def options_table(page: Page) -> Locator:
    return page.get_by_role("table", name=re.compile("^Purification methods, in the order"))


def labels(page: Page) -> list[str]:
    """The table's options, by label, in its order: each row's header's first line."""
    return [
        text.strip()
        for text in options_table(page).get_by_role("rowheader").locator("a").all_text_contents()
    ]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_lists_registrar_curates_its_options_on_its_page(
    open_as: Any, world: World, color_scheme: str
) -> None:
    registrar(world)
    page = open_as("auditor", LIST_PAGE, color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(options_table(page)).to_be_visible()
    assert labels(page) == ["Protein A affinity", "Size exclusion", "Dialysis"]
    # Its curators see how many records name each option, never which.
    expect(page.get_by_role("columnheader", name="Records")).to_be_visible()
    checked(page)
    fits_a_phone(page)

    # Added, with a category from another list, at the end.
    page.get_by_role("button", name="Add an option").click()
    dialog = page.get_by_role("dialog", name="Add an option to purification methods")
    dialog.get_by_role("textbox", name="Code").fill("imac")
    dialog.get_by_role("textbox", name="Label").fill("Ni-NTA affinity")
    dialog.get_by_role("textbox", name=re.compile("^Description")).fill(
        "His-tagged protein captured on Ni-NTA resin and eluted with imidazole."
    )
    dialog.get_by_role("combobox", name=re.compile("^Category")).select_option(
        label="Chromatography"
    )
    checked(page)
    dialog.get_by_role("button", name="Add option").click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text(re.compile("Added Ni-NTA affinity"))).to_be_visible()
    assert labels(page)[-1] == "Ni-NTA affinity"

    # Relabelled, with a reason; its code stays.
    page.get_by_role("button", name="Change Size exclusion").click()
    page.get_by_role("menuitem", name="Edit…").click()
    dialog = page.get_by_role("dialog", name="Edit Size exclusion")
    expect(dialog.get_by_role("textbox", name="Code")).to_have_count(0)
    dialog.get_by_role("textbox", name="Label").fill("Gel filtration")
    dialog.get_by_role("button", name="Save").click()
    expect(reason(dialog)).to_have_attribute("aria-invalid", "true")
    expect(reason(dialog)).to_be_focused()
    reason(dialog).fill("What the core calls it")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    expect(options_table(page).get_by_role("link", name="Gel filtration")).to_be_visible()
    assert option("sec").label == "Gel filtration"

    # Moved up by keyboard, two places, saying where it went each time.
    page.get_by_role("button", name="Reorder").click()
    up = page.get_by_role("button", name="Move Ni-NTA affinity up")
    up.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Moved Ni-NTA affinity to position 3 of 4.")).to_be_attached()
    expect(up).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Moved Ni-NTA affinity to position 2 of 4.")).to_be_attached()
    checked(page)
    reason(page).fill("Most used first")
    page.get_by_role("button", name="Save order").click()
    expect(page.get_by_text(re.compile("Saved the order of purification methods"))).to_be_visible()
    assert labels(page) == ["Protein A affinity", "Ni-NTA affinity", "Gel filtration", "Dialysis"]

    # Archived, with a reason: it's off the list until asked for.
    page.get_by_role("button", name="Change Dialysis").click()
    page.get_by_role("menuitem", name="Archive…").click()
    dialog = page.get_by_role("dialog", name="Archive Dialysis?")
    reason(dialog).fill("Buffer exchange is by desalting column now")
    dialog.get_by_role("button", name="Archive").click()
    expect(dialog).to_be_hidden()
    expect(options_table(page).get_by_role("link", name="Dialysis")).to_have_count(0)
    page.get_by_role("checkbox", name=re.compile("^Show archived and superseded options")).check()
    expect(options_table(page).get_by_role("link", name="Dialysis")).to_be_visible()
    expect(options_table(page).get_by_text("Archived", exact=True)).to_be_visible()
    assert option("dialysis").archived
    checked(page)
    fits_a_phone(page)


def test_a_lists_members_read_it_and_cant_change_it(open_as: Any, world: World) -> None:
    page = open_as("editor", LIST_PAGE)
    expect(options_table(page)).to_be_visible()
    expect(page.get_by_role("button", name="Add an option")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Change "))).to_have_count(0)
    expect(page.get_by_role("columnheader", name="Records")).to_have_count(0)
    checked(page)


def test_a_prep_picks_its_method_and_everyone_reads_it_by_its_label(
    open_as: Any, world: World
) -> None:
    curator = registrar(world)
    held = prep(world, "Anti-HER2 prep 11", method=str(option("dialysis").entry.pk))
    lists.archive(curator, option("dialysis").entry, reason="Desalting columns now")

    page = open_as("editor", f"/projects/kinase-atlas/entities/new/?type={PREP}")
    page.set_viewport_size({"width": 1440, "height": 900})
    method = page.get_by_role("combobox", name=re.compile("^Method"))
    # The list's active options, in its order, by label: an archived one isn't offered.
    expect(method.get_by_role("option")).to_have_text(
        ["Not set", "Protein A affinity", "Size exclusion"]
    )
    page.get_by_role("textbox", name="Name").fill("Anti-HER2 prep 12")
    method.select_option(label="Protein A affinity")
    page.get_by_role("button", name="Add Polishing").click()
    page.get_by_role("combobox", name="Polishing, item 1").select_option(label="Size exclusion")
    checked(page)
    page.get_by_role("button", name="Create prep").click()

    # The record reads it by its label, and leads to the list, at its option.
    expect(
        page.get_by_role("heading", level=1, name=re.compile("Anti-HER2 prep 12"))
    ).to_be_visible()
    link = page.get_by_role("link", name="Protein A affinity")
    expect(link).to_be_visible()
    checked(page)
    link.click()
    expect(page).to_have_url(
        re.compile(rf"/types/{re.escape(METHOD)}/\?project=kinase-atlas#option-protein_a$")
    )
    expect(options_table(page).locator("tr.is-target")).to_contain_text("Protein A affinity")

    # A record that names a method archived since keeps it, marked, when it's edited.
    page.goto(f"/entities/{held.hrid}/edit/")
    method = page.get_by_role("combobox", name=re.compile("^Method"))
    expect(method).to_have_value(str(option("dialysis").entry.pk))
    expect(method.locator("option:checked")).to_have_text("Dialysis (archived)")
    page.get_by_role("textbox", name="Name").fill("Anti-HER2 prep 11b")
    page.get_by_role("button", name=re.compile("^Save")).click()
    expect(
        page.get_by_role("heading", level=1, name=re.compile("Anti-HER2 prep 11b"))
    ).to_be_visible()
    assert str(values_of(held)["method"]) == str(option("dialysis").entry.pk)

    # In the grid, by label; filtered by it, every option is offered, archived ones too.
    page.goto(f"/projects/kinase-atlas/entities/?type={PREP}")
    grid = page.get_by_role("table", name="2 preps")
    expect(grid.get_by_role("cell", name="Dialysis (archived)")).to_be_visible()
    page.get_by_role("button", name="Filter", exact=True).click()
    page.get_by_role("menu", name="Filter").get_by_role(
        "menuitem", name=re.compile("^Method")
    ).click()
    editor = page.get_by_role("dialog", name=re.compile(r"^Filter 1: Method"))
    value = editor.get_by_role("combobox", name="Filter 1: value")
    expect(value.get_by_role("option")).to_have_text(
        ["Choose one", "Protein A affinity", "Size exclusion", "Dialysis (archived)"]
    )
    value.select_option(label="Dialysis (archived)")
    editor.get_by_role("button", name="Done").click()
    expect(
        page.get_by_role("group", name="Filters").get_by_role(
            "button", name="Filter 1: Method is Dialysis (archived)", exact=True
        )
    ).to_be_visible()
    expect(page.get_by_text("Anti-HER2 prep 11b")).to_be_visible()
    expect(page.get_by_text("Anti-HER2 prep 12", exact=True)).to_have_count(0)
    checked(page)


def sheet_cell(page: Page, row: int, column: str) -> Locator:
    """A sheet's cell, by its row (0 the first under the headings) and its column's name."""
    headers = page.get_by_role("columnheader").all_text_contents()
    return page.locator(f'[data-cell="{row}:{headers.index(column)}"]')


def settled(page: Page) -> None:
    """Every change is checked."""
    expect(page.get_by_role("status").filter(has_text="checking")).to_have_count(0, timeout=15_000)
    page.wait_for_timeout(500)
    expect(page.get_by_role("status").filter(has_text="checking")).to_have_count(0, timeout=15_000)


def test_a_sheets_method_cell_is_a_dropdown_and_takes_a_pasted_code(
    open_as: Any, world: World
) -> None:
    made = prep(world, "Anti-HER2 prep 12", method=str(option("sec").entry.pk))
    page = open_as("editor", f"/projects/kinase-atlas/entities/?type={PREP}")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="Edit as sheet").click()
    page.get_by_role("grid", name="Editing preps as a sheet").wait_for()
    method = sheet_cell(page, 0, "Method")
    expect(method).to_have_text("Size exclusion")

    # Alt+Down opens its options, as a spreadsheet's dropdown cell does; typed, they narrow.
    method.click()
    page.keyboard.press("Alt+ArrowDown")
    box = page.get_by_role("combobox", name=f"Method, {made.hrid}")
    expect(box).to_be_focused()
    options = page.get_by_role("listbox", name="Purification methods")
    expect(options.get_by_role("option")).to_have_count(3)
    checked(page)
    box.fill("affin")
    expect(options.get_by_role("option")).to_have_count(1)
    page.keyboard.press("Enter")
    expect(method).to_have_text("Protein A affinity")

    # A pasted code names its option, as its label does.
    polishing = sheet_cell(page, 0, "Polishing")
    browser = page.context.browser
    if browser is not None and browser.browser_type.name == "firefox":
        # Firefox hands a script's paste event no data: typed, as the sheet's test does.
        polishing.click()
        page.keyboard.type("dialysis")
        page.keyboard.press("Escape")
        page.keyboard.press("Tab")
    else:
        polishing.click()
        polishing.evaluate(
            """(el) => {
                const data = new DataTransfer();
                data.setData("text/plain", "dialysis");
                const init = { clipboardData: data, bubbles: true, cancelable: true };
                el.dispatchEvent(new ClipboardEvent("paste", init));
            }"""
        )
    settled(page)
    expect(page.get_by_role("region", name=re.compile("problems? to put right"))).to_have_count(0)
    page.get_by_role("button", name="Review and save").click()
    dialog = page.get_by_role("alertdialog", name="Save 1 row?")
    expect(dialog).to_contain_text("Size exclusion")
    expect(dialog).to_contain_text("Protein A affinity")
    expect(dialog).to_contain_text("Dialysis")
    dialog.get_by_role("textbox", name="Reason").fill("Polished by dialysis")
    dialog.get_by_role("button", name="Save 1 row").click()
    expect(dialog).to_be_hidden()
    assert str(values_of(made)["method"]) == str(option("protein_a").entry.pk)
    assert [str(i) for i in values_of(made)["polishing"]] == [str(option("dialysis").entry.pk)]


def test_a_projects_records_move_off_a_superseded_option(open_as: Any, world: World) -> None:
    curator = registrar(world)
    registry = Registry.default()
    wrong = lists.add(curator, registry, METHOD, {"code": "protien_g", "label": "Protein G (typo)"})
    right = lists.add(curator, registry, METHOD, {"code": "protein_g", "label": "Protein G"})
    made = prep(world, "Anti-TNF prep 3", method=str(wrong.entry.pk))
    entries.supersede(curator, wrong.entry, right.entry, reason="Added twice, once misspelled")

    # The project's Admins are told, and the notice opens the option's page for the project.
    notice = Notification.objects.get(recipient=world.users["admin"])
    assert notice.title == "Purification method “Protein G (typo)” was superseded by “Protein G”"
    page = open_as("admin", notice.path)
    expect(
        page.get_by_role("heading", level=1, name=re.compile(r"Protein G \(typo\)"))
    ).to_be_visible()
    expect(page.get_by_role("note")).to_contain_text("Dropdowns offer Protein G instead")
    checked(page)
    move = page.get_by_role("button", name="Move Kinase Atlas’s records")
    move.click()
    expect(reason(page)).to_be_focused()
    reason(page).fill("Protein G (typo) was a duplicate")
    move.click()
    expect(
        page.get_by_text(re.compile("Moved 1 of Kinase Atlas’s records to Protein G"))
    ).to_be_visible()
    assert str(values_of(made)["method"]) == str(right.entry.pk)
    checked(page)
