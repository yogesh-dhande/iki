# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Editing records as a sheet (ADR 0228) in real browsers: a kinase lab's batch of samples
typed into, pasted over from a spreadsheet, filled down, undone, and added to, with each
cell checked as it would be saved; then reviewed and saved with a reason, all at once.
New tubes are put away in a box with their volumes, and a record changed by someone else
meanwhile is brought in before saving. Every step passes axe in both themes and fits a
phone's width."""

import os
from datetime import date
from pathlib import Path
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

from iki_server.amounts.models import Amount
from iki_server.entities import services as entities
from iki_server.entities.models import Entity, Revision
from iki_server.storage import services as storage
from iki_server.storage.models import Place

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
SAMPLE = "acme/protein.Sample"
TUBE = "acme/protein.Tube"
#: Where screenshots go, when a run asks for them.
SHOTS = os.environ.get("IKI_SHEET_SCREENSHOTS")

# The kinase domains of human ABL1 (UniProt P00519, in part) and EGFR (P00533, in part).
ABL1 = "GPSGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKKYSLTVAVKTLKEDTMEVEEFLKEAAVMKEIKH"
EGFR = "GSGEAPNQALLRILKETEFKKIKVLGSGAFGTVYKGLWIPEGEKVKIPVAIKELREATSPKANKEILDEAYV"

#: The columns of a sample's sheet, as a paste fills them: its ID, then its fields.
LABEL, PROTEIN, PURITY, CONCENTRATION = 1, 2, 5, 6
DUE_ON = 13


def batch(world: World) -> dict[str, Entity]:
    """Kinase domains, and three samples of batch 12, oldest first."""
    made: dict[str, Entity] = {}
    for name, sequence in (("ABL1", ABL1), ("EGFR", EGFR)):
        saved = entities.create(
            world.users["editor"],
            "acme/protein.Protein",
            world.project,
            {"name": name, "sequence": sequence},
        )
        made[name] = Entity.objects.get(pk=saved.entity.pk)
    for label, protein in (
        ("ABL1-KD-001", "ABL1"),
        ("ABL1-KD-002", "ABL1"),
        ("EGFR-KD-001", "EGFR"),
    ):
        saved = entities.create(
            world.users["editor"],
            SAMPLE,
            world.project,
            {"label": label, "protein": str(made[protein].pk)},
        )
        made[label] = Entity.objects.get(pk=saved.entity.pk)
    return made


def checked(page: Page) -> None:
    """axe and the page's errors, as on every page."""
    assert_clean(page)


def fits_a_phone(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL), page.url
    checked(page)
    page.set_viewport_size({"width": 1440, "height": 900})


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        # The caret as it is: hiding it adds a style the page's CSP refuses.
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), caret="initial")


def sheet(page: Page) -> Locator:
    return page.get_by_role("grid", name="Editing samples as a sheet")


def cell(page: Page, row: int, col: int) -> Locator:
    """The sheet's cell at a row (0 the first under the headings) and column."""
    return page.locator(f'[data-cell="{row}:{col}"]')


def row_of(page: Page, hrid: str) -> int:
    """The sheet's row of a record: the table lists the newest first."""
    index = page.locator('[role="row"]').filter(has=page.get_by_text(hrid, exact=True))
    return int(index.first.get_attribute("aria-rowindex") or "0") - 2


def type_into(page: Page, at: Locator, text: str) -> None:
    at.click()
    page.keyboard.type(text)
    page.keyboard.press("Enter")


def paste(page: Page, row: int, col: int, text: str, *, adds: int = 0) -> None:
    """Pastes `text` at a cell, as a spreadsheet's copy hands it over: tab-separated.
    Firefox hands a script's paste event no data, so there its cells are typed instead, in
    `adds` new rows more: Chromium and WebKit paste them, and the unit tests place them."""
    at = cell(page, row, col)
    browser = page.context.browser
    if browser is None or browser.browser_type.name != "firefox":
        at.click()
        at.evaluate(
            """(el, text) => {
                const data = new DataTransfer();
                data.setData("text/plain", text);
                const init = { clipboardData: data, bubbles: true, cancelable: true };
                el.dispatchEvent(new ClipboardEvent("paste", init));
            }""",
            text,
        )
        return
    for _ in range(adds):
        page.get_by_role("button", name="Add row").click()
    for r, line in enumerate(text.split("\n")):
        for c, value in enumerate(line.split("\t")):
            if value:
                type_into(page, cell(page, row + r, col + c), value)


def settled(page: Page) -> None:
    """Every change is checked."""
    expect(page.get_by_role("status").filter(has_text="checking")).to_have_count(0, timeout=15_000)
    page.wait_for_timeout(500)
    expect(page.get_by_role("status").filter(has_text="checking")).to_have_count(0, timeout=15_000)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_batch_of_samples_is_edited_and_added_to_as_a_sheet(
    open_as: Any, world: World, color_scheme: str
) -> None:
    made = batch(world)
    page = open_as("editor", f"/projects/kinase-atlas/entities/?type={SAMPLE}", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="Edit as sheet").click()
    grid = sheet(page)
    grid.wait_for()
    expect(page.get_by_role("columnheader", name="Concentration (mg/mL)")).to_be_visible()
    checked(page)
    fits_a_phone(page)

    kd1, kd2, kd3 = (
        row_of(page, made[k].hrid) for k in ("ABL1-KD-001", "ABL1-KD-002", "EGFR-KD-001")
    )
    # The ID can't change, and says so.
    expect(cell(page, kd1, 0)).to_have_attribute("aria-readonly", "true")

    # Typed: a purity, as the sheet's European colleague writes it.
    type_into(page, cell(page, kd1, PURITY), "98,5")
    expect(cell(page, kd1, PURITY)).to_have_text("98,5")
    # Pasted from a spreadsheet: purity and concentration for the other two, one over 100.
    top = min(kd2, kd3)
    paste(page, top, PURITY, "97.2\t1.1\n140\t0.9")
    settled(page)
    expect(cell(page, top + 1, PURITY)).to_have_attribute("aria-invalid", "true")
    problems = page.get_by_role("region", name="1 problem to put right")
    expect(problems).to_contain_text("Purity must be at most 100.")
    # A date that reads two ways is amber until the sheet says how it writes dates.
    type_into(page, cell(page, kd1, DUE_ON), "03/04/2027")
    settled(page)
    expect(page.get_by_role("region", name="2 problems to put right")).to_contain_text(
        "reads two ways"
    )
    checked(page)
    cell(page, top + 1, PURITY).click()
    shot(page, f"sheet-problems-{color_scheme}")
    page.get_by_role("combobox", name="Dates written as numbers").select_option("day_first")
    settled(page)
    # The wrong purity, put right; undone and redone.
    type_into(page, cell(page, top + 1, PURITY), "96.4")
    page.keyboard.press("Control+z")
    expect(cell(page, top + 1, PURITY)).to_have_text("140")
    page.keyboard.press("Control+Shift+z")
    expect(cell(page, top + 1, PURITY)).to_have_text("96.4")
    settled(page)
    expect(page.get_by_role("region", name="problems to put right")).to_have_count(0)

    # Two new samples, the second's protein filled down from the first.
    page.get_by_role("button", name="Add row").click()
    page.get_by_role("button", name="Add row").click()
    paste(page, 0, LABEL, "EGFR-KD-002\tEGFR\nEGFR-KD-003")
    cell(page, 0, PROTEIN).click()
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Control+d")
    expect(cell(page, 1, PROTEIN)).to_have_text("EGFR")
    settled(page)

    # Saving shows every change, and needs a reason.
    page.get_by_role("button", name="Review and save").click()
    dialog = page.get_by_role("alertdialog", name="Save 5 rows?")
    expect(dialog).to_contain_text("3 samples change, and 2 new samples are added")
    expect(dialog.get_by_role("row", name="Due on Empty 2027-04-03")).to_be_visible()
    expect(dialog.get_by_role("button", name="Save 5 rows")).to_have_attribute(
        "aria-disabled", "true"
    )
    dialog.get_by_role("textbox", name="Reason").fill("Purity by SEC, batch 12")
    checked(page)
    shot(page, f"sheet-review-{color_scheme}")
    dialog.get_by_role("button", name="Save 5 rows").click()
    expect(page.get_by_text("3 samples changed, 2 samples added.")).to_be_visible()
    expect(grid).to_have_count(0)

    first = entities.current_values(
        entities.specific(Entity.objects.get(pk=made["ABL1-KD-001"].pk))
    )
    assert (first["purity"], first["due_on"]) == (98.5, date(2027, 4, 3))
    second = entities.current_values(
        entities.specific(Entity.objects.get(pk=made["ABL1-KD-002"].pk))
    )
    third = entities.current_values(
        entities.specific(Entity.objects.get(pk=made["EGFR-KD-001"].pk))
    )
    assert sorted([second["purity"], third["purity"]]) == [96.4, 97.2]
    added = Entity.objects.filter(type=SAMPLE).exclude(pk__in=[made[k].pk for k in made])
    assert sorted(entities.current_values(entities.specific(e))["label"] for e in added) == [
        "EGFR-KD-002",
        "EGFR-KD-003",
    ]
    reasons = set(
        Revision.objects.filter(entity__type=SAMPLE, number__gt=1).values_list("reason", flat=True)
    )
    assert reasons == {"Purity by SEC, batch 12"}


def test_new_tubes_are_put_away_with_their_volumes_and_a_changed_record_is_brought_in(
    open_as: Any, world: World
) -> None:
    editor = world.users["editor"]
    box = Entity.objects.get(
        pk=entities.create(
            editor, "acme/protein.Box", world.project, {"name": "Batch 12 retains"}
        ).entity.pk
    )
    old = Entity.objects.get(
        pk=entities.create(editor, TUBE, world.project, {"barcode": "TB-0001"}).entity.pk
    )
    storage.move(editor, [storage.Wanted(old, box, "A1")], reason="Filed")
    page = open_as("editor", f"/projects/kinase-atlas/entities/?type={TUBE}")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("button", name="Edit as sheet").click()
    grid = page.get_by_role("grid", name="Editing tubes as a sheet")
    grid.wait_for()
    headings = page.locator('[role="columnheader"]').all_inner_texts()
    location, volume = headings.index("Location"), headings.index("Volume (uL)")
    barcode = headings.index("Barcode")
    # An existing tube's place and volume change on its page, and its cell says so.
    expect(cell(page, 0, location)).to_have_attribute("aria-readonly", "true")
    expect(cell(page, 0, location)).to_have_attribute(
        "title", "Move a record from its page or in Storage."
    )

    # Two new tubes, pasted with where they go and how much each holds.
    page.get_by_role("button", name="Add row").click()
    paste(page, 0, location, f"{box.hrid} B3\t500\nBatch 12 retains\t0.25 mL", adds=1)
    expect(page.locator('[role="row"]')).to_have_count(4)
    paste(page, 0, barcode, "TB-0002\nTB-0003")
    # Its old tube, changed meanwhile by someone else.
    type_into(page, cell(page, 2, barcode), "TB-0001-R")
    settled(page)
    expect(cell(page, 0, volume)).to_have_text("500")
    entities.update(
        world.users["admin"],
        old,
        {"barcode": "TB-0001-A"},
        expected_revision=old.revision,
        reason="Relabelled",
    )
    page.get_by_role("button", name="Review and save").click()
    dialog = page.get_by_role("alertdialog", name="Save 3 rows?")
    expect(dialog).to_contain_text(f"{box.hrid} B3")
    expect(dialog).to_contain_text(f"{box.hrid} A2")
    dialog.get_by_role("textbox", name="Reason").fill("Aliquoted for stability")
    dialog.get_by_role("button", name="Save 3 rows").click()
    expect(dialog).to_contain_text("changed since you opened the sheet")
    # The 412 the browser logs is the refusal the page shows.
    PAGE_ERRORS[page][:] = [e for e in PAGE_ERRORS[page] if "status of 412" not in e]
    checked(page)
    shot(page, "sheet-stale-light")
    # Nothing was saved, and bringing it in keeps what was typed.
    assert Entity.objects.filter(type=TUBE).count() == 1
    dialog.get_by_role("button", name="Bring in the latest").click()
    expect(cell(page, 2, barcode)).to_have_text("TB-0001-R")
    settled(page)
    page.get_by_role("button", name="Review and save").click()
    dialog = page.get_by_role("alertdialog", name="Save 3 rows?")
    expect(dialog).to_contain_text("TB-0001-A")
    dialog.get_by_role("textbox", name="Reason").fill("Aliquoted for stability")
    dialog.get_by_role("button", name="Save 3 rows").click()
    expect(grid).to_have_count(0)

    tubes = {
        entities.current_values(entities.specific(e))["barcode"]: e
        for e in Entity.objects.filter(type=TUBE)
    }
    assert set(tubes) == {"TB-0001-R", "TB-0002", "TB-0003"}
    for code, (row, column), volume_ul in (("TB-0002", (2, 3), 500), ("TB-0003", (1, 2), 250)):
        place = Place.objects.get(pk=tubes[code].pk)
        assert (place.container_id, place.row, place.column) == (box.pk, row, column)
        assert Amount.objects.get(entity=tubes[code]).value == volume_ul
