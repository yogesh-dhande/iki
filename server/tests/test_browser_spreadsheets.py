# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Importing a spreadsheet (ADR 0178) in real browsers: a kinase lab's sample manifest, with
a title above its header, a total under its rows, a few bad rows and a repeated label, is
taken from the Entities tab through its sheet, its columns, how it writes numbers and dates,
and a preview, to one run and the records it wrote. Every step passes axe in both themes
and fits a phone's width."""

import re
import time
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, expect
from real_server import Server
from test_browser import (  # noqa: F401 - fixtures
    PAGE_ERRORS,
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    tasks,
    world,
)
from workbook_files import Date, xlsx

from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.runs.models import ActionRun, ExternalOrigin

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
SAMPLE = "acme/protein.Sample"

# The kinase domains of human ABL1 (UniProt P00519, residues 229-500, in part) and EGFR
# (P00533, 696-1022, in part), as a structural biology lab expresses them.
ABL1 = "GPSGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKKYSLTVAVKTLKEDTMEVEEFLKEAAVMKEIKH"
EGFR = "GSGEAPNQALLRILKETEFKKIKVLGSGAFGTVYKGLWIPEGEKVKIPVAIKELREATSPKANKEILDEAYV"

# What a lab's manifest looks like: a sheet about it first, then the samples, under a title
# and a blank row, with a total at the foot. Rows 8 to 10 are wrong: a protein the project
# hasn't, a label row 5 has already, and a purity over 100%. Row 5's due date and row 7's
# concentration were typed as text, and read two ways until the sheet's locale is said.
ABOUT = [
    ["Expression core: kinase domain constructs"],
    ["Batch 12, purified November 2026. Concentrations by A280."],
]
SAMPLES: list[list[Any]] = [
    ["Kinase domains, batch 12"],
    [],
    ["Label", "Protein", "Purity (%)", "Conc. (ug/mL)", "Tags", "Due on"],
    ["ABL1-KD-001", "ABL1", 98.5, 1250, "kinase; crystallography", Date(46388)],
    ["ABL1-KD-002", "abl1", 97.2, 980, "kinase", "03/04/2027"],
    ["EGFR-KD-001", "EGFR", 95.0, "2.1 mg/mL", "kinase; screening", "2027-01-15"],
    ["EGFR-KD-002", "EGFR", 96.4, "1,250", "kinase", "15/02/2027"],
    ["BRAF-KD-001", "BRAF", 94.1, 1100, "kinase", Date(46400)],
    ["ABL1-KD-002", "ABL1", 96.0, 1020, "kinase", Date(46401)],
    ["EGFR-KD-003", "EGFR", 120, 870, "kinase", Date(46402)],
    ["Total", "", "", 6470, "", ""],
]


def kinases(world: World) -> None:
    for name, sequence in (("ABL1", ABL1), ("EGFR", EGFR)):
        entities.create(
            world.users["editor"],
            "acme/protein.Protein",
            world.project,
            {"name": name, "sequence": sequence},
        )


def checked(page: Page) -> None:
    """axe, as on every page. Playwright's Firefox crashes in axe on an import's pages, as
    on others named by `aria-labelledby` (see assert_clean): Chromium and WebKit check
    them, and the DOM is the same in every browser."""
    assert_clean(page, axe_in_firefox=False)


def fits_a_phone(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL), page.url
    checked(page)
    page.set_viewport_size({"width": 1440, "height": 900})


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_sample_manifest_is_imported_from_the_entities_tab(
    open_as: Any, world: World, tasks: Server, color_scheme: str
) -> None:
    kinases(world)
    page = open_as("editor", f"/projects/kinase-atlas/entities/?type={SAMPLE}", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})

    # 1. The workbook: the type shown is what its rows are.
    page.get_by_role("link", name="Import", exact=True).click()
    page.get_by_role("heading", level=2, name="Import a spreadsheet").wait_for()
    expect(page.get_by_role("combobox", name="Its rows are")).to_have_value(SAMPLE)
    checked(page)
    fits_a_phone(page)
    page.locator("input[type=file]").set_input_files(
        {
            "name": "kinase-domains-batch-12.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": xlsx(("About", ABOUT), ("Samples", SAMPLES)),
        }
    )
    page.wait_for_url(re.compile(r"/entities/import/[0-9a-f-]+/$"), timeout=60_000)
    # An import has an address of its own, to come back to.
    page.reload()

    # 2. Its sheet and rows: the samples, their header under the title, the total left out.
    page.get_by_role("heading", level=3, name="Sheet and rows").wait_for(timeout=60_000)
    page.get_by_role("combobox", name="Sheet").select_option("Samples")
    header = page.get_by_role("spinbutton", name="Header row")
    header.fill("3")
    header.press("Enter")
    expect(page.get_by_role("spinbutton", name="First row")).to_have_value("4")
    skip = page.get_by_role("textbox", name="Rows to leave out")
    skip.fill("11")
    skip.press("Enter")
    sample = page.get_by_role("table", name="Its first rows")
    expect(sample.get_by_role("rowheader", name=re.compile(r"^3\s*\(header\)$"))).to_be_visible()
    expect(
        sample.get_by_role("rowheader", name=re.compile(r"^11\s*\(not imported\)$"))
    ).to_be_visible()
    checked(page)
    fits_a_phone(page)

    # 3. Its columns, each matched to the field its header names.
    page.get_by_role("button", name="Next: columns").click()
    columns = page.get_by_role("table", name="Each column, and the field it sets")
    for column, field in (
        ("A: Label", "label"),
        ("B: Protein", "protein"),
        ("C: Purity (%)", "purity"),
        ("D: Conc. (ug/mL)", "concentration"),
        ("E: Tags", "tags"),
        ("F: Due on", "due_on"),
    ):
        expect(columns.get_by_role("combobox", name=f"Field for {column}")).to_have_value(field)
    # The header's unit, converted to the field's.
    expect(columns.get_by_role("combobox", name="Unit of D: Conc. (ug/mL)’s cells")).to_have_value(
        "ug/mL"
    )

    # How the sheet writes numbers and dates: nothing's guessed until it's said.
    page.get_by_text("2 cells below read two ways").wait_for()
    readings = page.get_by_role("table", name=re.compile("How numbers and dates"))
    expect(readings.get_by_text("Reads two ways", exact=True)).to_have_count(2)
    checked(page)
    fits_a_phone(page)
    # Each choice is the import's once Iki has it.
    for choice in ("A decimal point: 1,234.5", "Day first: 31/12/2026"):
        page.get_by_role("radio", name=choice).click()
        expect(page.get_by_role("radio", name=choice)).to_be_checked()
    expect(readings.get_by_text("Reads two ways", exact=True)).to_have_count(0)
    expect(readings.get_by_role("row").filter(has_text="03/04/2027")).to_contain_text("2027-04-03")
    expect(readings.get_by_role("row").filter(has_text="1,250")).to_contain_text(
        "1\xa0250 ug/mL = 1.25 mg/mL"
    )

    # 4. The preview: every row checked as it would be written.
    page.get_by_role("button", name="Check every row").click()
    page.get_by_text("3 rows have problems").wait_for(timeout=60_000)
    expect(page.get_by_text("4 new records")).to_be_visible()
    problems = page.get_by_role("table", name=re.compile("^Has problems"))
    for row in ("8", "9", "10"):
        expect(problems.get_by_role("rowheader", name=row, exact=True)).to_be_visible()
    expect(problems.get_by_role("row", name=re.compile("^9 "))).to_contain_text("row 5")
    checked(page)
    fits_a_phone(page)

    # 5. One run imports the rest, all or none, and links to what it wrote.
    importing = page.get_by_role("button", name="Import 4 rows")
    expect(importing).to_have_attribute("aria-disabled", "true")
    page.get_by_role("checkbox", name="Leave out the 3 rows with problems").check()
    page.get_by_role("textbox", name="Why").fill("Batch 12 from the expression core")
    importing.click()
    page.get_by_text("Imported 4 rows of kinase-domains-batch-12.xlsx into", exact=False).wait_for(
        timeout=90_000
    )
    checked(page)
    fits_a_phone(page)
    page.get_by_role("link", name="Show the imported records").click()
    page.get_by_text("Records written by run").wait_for()
    expect(page.get_by_role("link", name=re.compile(r"^SMP-\d+$"))).to_have_count(4)
    checked(page)

    # Each record names the workbook, its sheet and row; the run, who imported, and how.
    made = Entity.objects.filter(type=SAMPLE, owner_project=world.project).order_by("hrid")
    assert made.count() == 4
    origins = {
        o.identifier: o
        for o in ExternalOrigin.objects.filter(entity__in=made).select_related("file")
    }
    assert sorted(origins) == [f"Samples, row {n}" for n in (4, 5, 6, 7)]
    workbook = origins["Samples, row 4"].file
    assert workbook is not None and workbook.name == "kinase-domains-batch-12.xlsx"
    run = ActionRun.objects.get(pk=origins["Samples, row 4"].run_id)
    assert run.requested_by == world.users["editor"]
    assert run.params["locale"] == {"decimal": "point", "dates": "day_first", "serial_dates": False}
    by_label = {
        entities.current_values(entities.specific(e))["label"]: entities.current_values(
            entities.specific(e)
        )
        for e in made
    }
    assert sorted(by_label) == ["ABL1-KD-001", "ABL1-KD-002", "EGFR-KD-001", "EGFR-KD-002"]
    # "1,250" µg/mL with a decimal point, in the field's mg/mL; 03/04/2027 day first.
    assert by_label["EGFR-KD-002"]["concentration"] == pytest.approx(1.25)
    assert str(by_label["ABL1-KD-002"]["due_on"]) == "2027-04-03"


def test_a_workbook_already_in_the_project_is_picked_from_its_files(
    open_as: Any, world: World, tasks: Server
) -> None:
    from iki_server.files.models import File
    from iki_server.spreadsheets.models import SpreadsheetImport

    kinases(world)
    page = open_as("editor", f"/projects/kinase-atlas/entities/import/?type={SAMPLE}")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", level=2, name="Import a spreadsheet").wait_for()
    # Someone uploaded the core's manifest once, and left that import.
    page.locator("input[type=file]").first.set_input_files(
        {
            "name": "kinase-domains-batch-12.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": xlsx(("About", ABOUT), ("Samples", SAMPLES)),
        }
    )
    page.get_by_role("heading", level=3, name="Sheet and rows").wait_for(timeout=60_000)
    page.get_by_role("button", name="Discard", exact=True).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Discard", exact=True).click()
    page.wait_for_url(re.compile(r"/projects/kinase-atlas/entities/$"))

    # Importing it again starts from the project's Files: the picker lists workbooks only.
    page.get_by_role("link", name="Import", exact=True).click()
    choose = page.get_by_role("button", name="Or choose one already in Kinase Atlas")
    # What its rows are is asked first, as the import needs it.
    choose.click()
    page.get_by_text("Choose what its rows are first.").wait_for()
    page.get_by_role("combobox", name="Its rows are").select_option(SAMPLE)
    choose.click()
    picker = page.get_by_role("dialog", name="Choose a file for the import")
    picker.get_by_text("It takes Excel (.xlsx) or CSV (.csv) files, up to 100 MiB.").wait_for()
    workbook = picker.get_by_role("radio", name="kinase-domains-batch-12.xlsx")
    workbook.wait_for()
    checked(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL), page.url
    checked(page)
    page.set_viewport_size({"width": 1440, "height": 900})
    workbook.click()
    page.wait_for_url(re.compile(r"/entities/import/[0-9a-f-]+/$"), timeout=60_000)
    page.get_by_role("heading", level=3, name="Sheet and rows").wait_for(timeout=60_000)
    expect(
        page.get_by_role("heading", level=2, name=re.compile("^Import kinase-domains"))
    ).to_be_visible()
    checked(page)

    # One file, kept as it was: both imports name it, and nothing was uploaded again.
    (kept,) = File.objects.filter(project=world.project, name="kinase-domains-batch-12.xlsx")
    imports = SpreadsheetImport.objects.filter(project=world.project).order_by("created_at")
    assert [i.file_id for i in imports] == [kept.pk, kept.pk]


# --- Choices saved in order -------------------------------------------------------------------


def mapping_step(open_as: Any, color_scheme: str = "light") -> tuple[Page, str]:
    """The kinase manifest uploaded, at its sheet and rows: About chosen, as Iki first
    chooses the first sheet with rows. Returns the page and the import's ID."""
    page = open_as("editor", f"/projects/kinase-atlas/entities/import/?type={SAMPLE}", color_scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", level=2, name="Import a spreadsheet").wait_for()
    page.locator("input[type=file]").first.set_input_files(
        {
            "name": "kinase-domains-batch-12.xlsx",
            "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "buffer": xlsx(("About", ABOUT), ("Samples", SAMPLES)),
        }
    )
    page.wait_for_url(re.compile(r"/entities/import/[0-9a-f-]+/$"), timeout=60_000)
    page.get_by_role("heading", level=3, name="Sheet and rows").wait_for(timeout=60_000)
    found = re.search(r"/entities/import/([0-9a-f-]+)/$", page.url)
    assert found is not None
    expect(page.get_by_role("combobox", name="Sheet")).to_have_value("About")
    return page, found.group(1)


def on_screen(page: Page) -> dict[str, Any]:
    """The sheet and rows as the page shows them, and what it says is wrong."""
    return {
        "sheet": page.get_by_role("combobox", name="Sheet").input_value(),
        "header_row": page.get_by_role("spinbutton", name="Header row").input_value(),
        "first_row": page.get_by_role("spinbutton", name="First row").input_value(),
        "skip": page.get_by_role("textbox", name=re.compile("Rows to leave out")).input_value(),
        "errors": page.locator(".sheet-card .field-error").all_inner_texts(),
    }


def saved(page: Page, import_id: str) -> dict[str, Any]:
    """The sheet and rows as the server has them, read apart from the page's own reads."""
    answer = page.context.request.get(f"/api/v1/spreadsheet-imports/{import_id}")
    assert answer.ok, answer.text()
    found = answer.json()
    return {
        "sheet": found["sheet"],
        "header_row": str(found["header_row"]),
        "first_row": str(found["first_row"]),
        "skip": ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in found["skip"]),
        "errors": [],
    }


def press_header_row(page: Page, row: str) -> None:
    """Types a header row as people do: into the box, then Enter."""
    header = page.get_by_role("spinbutton", name="Header row")
    header.click()
    header.press("ControlOrMeta+a")
    header.press(row)
    header.press("Enter")


@pytest.mark.parametrize("slow", ["request", "answer"])
def test_a_sheet_and_its_header_row_chosen_in_quick_succession_are_saved_as_chosen(
    open_as: Any, world: World, tasks: Server, slow: str
) -> None:
    """The sheet's request is slow to reach Iki, or its answer slow to come back, and the
    header row is typed meanwhile: what's saved is what the person chose, and the page
    shows what's saved."""
    page, import_id = mapping_step(open_as)
    held: list[tuple[Any, Any]] = []
    sent: list[dict[str, Any]] = []
    answered: list[int] = []

    def intercept(route: Any) -> None:
        if route.request.method != "PATCH":
            route.fallback()
            return
        body = route.request.post_data_json
        sent.append(body)
        if "sheet" in body and not held:
            # Held until the header row's been typed: unsent, or sent and its answer held.
            held.append((route, route.fetch() if slow == "answer" else None))
            return
        route.fallback()

    def answer(response: Any) -> None:
        if response.request.method == "PATCH" and response.url.endswith(import_id):
            answered.append(response.status)

    page.on("response", answer)
    page.route(f"**/api/v1/spreadsheet-imports/{import_id}", intercept)

    # Samples, from the keyboard: S, on the sheet box.
    sheet = page.get_by_role("combobox", name="Sheet")
    sheet.focus()
    sheet.press("s")
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert held, sent
    press_header_row(page, "3")
    # Time for the header row's request to go, if it goes before the sheet's answer.
    page.wait_for_timeout(1_000)
    before_the_sheet = list(sent)
    route, response = held[0]
    if response is None:
        route.fallback()
    else:
        route.fulfill(response=response)
    deadline = time.monotonic() + 15
    while len(answered) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert len(answered) == 2, (sent, answered)
    page.wait_for_timeout(300)

    # The page shows what's saved, and what's saved is what was chosen.
    shown = on_screen(page)
    assert shown == saved(page, import_id), (slow, sent, answered)
    assert shown == {
        "sheet": "Samples",
        "header_row": "3",
        "first_row": "4",
        "skip": "",
        "errors": [],
    }
    # The header row waited for the sheet: each was sent from the revision before it.
    assert before_the_sheet == [{"sheet": "Samples"}]
    assert answered == [200, 200]
    # Opened afresh, it's the same.
    again = open_as("editor", f"/projects/kinase-atlas/entities/import/{import_id}/")
    again.get_by_role("heading", level=3, name="Sheet and rows").wait_for()
    assert on_screen(again) == shown
    checked(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_change_made_in_another_tab_is_shown_not_overwritten(
    open_as: Any, world: World, tasks: Server, color_scheme: str
) -> None:
    page, import_id = mapping_step(open_as, color_scheme)
    page.get_by_role("combobox", name="Sheet").select_option("Samples")
    press_header_row(page, "3")
    expect(page.get_by_role("spinbutton", name="First row")).to_have_value("4")
    expect(page.get_by_text("Saving…")).to_have_count(0)

    # In another tab, the first row is moved on.
    other = open_as("editor", f"/projects/kinase-atlas/entities/import/{import_id}/", color_scheme)
    first = other.get_by_role("spinbutton", name="First row")
    expect(first).to_have_value("4")
    first.click()
    first.press("ControlOrMeta+a")
    first.press("5")
    first.press("Enter")
    expect(other.get_by_text("Saving…")).to_have_count(0)
    deadline = time.monotonic() + 10
    while saved(other, import_id)["first_row"] != "5" and time.monotonic() < deadline:
        other.wait_for_timeout(50)

    # This tab still shows the first row it had, and leaves a row out.
    expect(page.get_by_role("spinbutton", name="First row")).to_have_value("4")
    skip = page.get_by_role("textbox", name=re.compile("Rows to leave out"))
    skip.click()
    skip.press("1")
    skip.press("1")
    skip.press("Enter")
    alert = page.get_by_role("alert").filter(has_text="changed in another tab or session")
    alert.wait_for()
    expect(alert).to_contain_text("with its first row as changed there")
    expect(alert.get_by_role("listitem")).to_have_text(["Rows to leave out: 11"])
    # What's saved, and what the person chose that wasn't, still in its box.
    expect(page.get_by_role("spinbutton", name="First row")).to_have_value("5")
    expect(skip).to_have_value("11")
    assert saved(page, import_id)["skip"] == ""
    # The API answers the stale change with a 412, which Chromium and WebKit log as a failed
    # load: that, and nothing else.
    errors = PAGE_ERRORS[page]
    assert sum("status of 412 (Precondition Failed)" in e for e in errors) <= 1, errors
    errors[:] = [e for e in errors if "status of 412 (Precondition Failed)" not in e]
    checked(page)
    fits_a_phone(page)

    alert.get_by_role("button", name="Save my change").click()
    expect(alert).to_have_count(0)
    expect(page.get_by_text("Saving…")).to_have_count(0)
    shown = on_screen(page)
    assert shown == saved(page, import_id)
    assert (shown["first_row"], shown["skip"]) == ("5", "11")
