# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Importing a spreadsheet (ADR 0178) in real browsers: a kinase lab's sample manifest, with
a title above its header, a total under its rows, a few bad rows and a repeated label, is
taken from the Entities tab through its sheet, its columns, how it writes numbers and dates,
and a preview, to one run and the records it wrote. Every step passes axe in both themes
and fits a phone's width."""

import re
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, Request, Route, expect
from real_server import Server
from test_browser import (  # noqa: F401 - fixtures
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    tasks,
    watch,
    world,
)
from workbook_files import XLSX_TYPE, Date, xlsx

from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.runs.models import ActionRun, ExternalOrigin
from iki_server.spreadsheets.models import PreviewRow, SpreadsheetImport

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


def choose_manifest(page: Page) -> None:
    page.get_by_label("Upload a workbook").set_input_files(
        {
            "name": "kinase-domains-batch-12.xlsx",
            "mimeType": XLSX_TYPE,
            "buffer": xlsx(("About", ABOUT), ("Samples", SAMPLES)),
        }
    )


@pytest.fixture
def sample_manifest(open_as: Any, world: World, tasks: Server) -> tuple[Page, SpreadsheetImport]:
    kinases(world)
    page: Page = open_as("editor", f"/projects/kinase-atlas/entities/import/?type={SAMPLE}", "dark")
    expect(page.get_by_role("combobox", name="Its rows are")).to_have_value(SAMPLE)
    choose_manifest(page)
    page.get_by_role("heading", name="Sheet and rows").wait_for(timeout=60_000)
    page.reload()
    page.get_by_role("heading", name="Sheet and rows").wait_for(timeout=60_000)
    return page, SpreadsheetImport.objects.get(project=world.project)


def test_row_edits_wait_for_the_chosen_sheet_and_a_shorter_sheet_explains_its_reset(
    sample_manifest: tuple[Page, SpreadsheetImport],
) -> None:
    page, imported = sample_manifest
    sheet = page.get_by_role("combobox", name="Sheet", exact=True)
    header = page.get_by_role("spinbutton", name="Header row")
    first = page.get_by_role("spinbutton", name="First row")
    next_step = page.get_by_role("button", name="Next: columns")
    endpoint = f"**/api/v1/spreadsheet-imports/{imported.pk}"
    held: list[Route] = []
    patches: list[Request] = []

    def record_patch(request: Request) -> None:
        if request.method == "PATCH" and request.url.endswith(str(imported.pk)):
            patches.append(request)

    page.on("request", record_patch)
    page.route(endpoint, lambda route: held.append(route), times=1)

    with page.expect_request(lambda r: r.method == "PATCH" and r.url.endswith(str(imported.pk))):
        sheet.select_option("Samples")
    expect(header).to_be_disabled()
    expect(first).to_be_disabled()
    expect(next_step).to_be_disabled()
    header.focus()
    expect(header).not_to_be_focused()
    page.keyboard.press("3")
    page.keyboard.press("Enter")
    expect(header).to_have_value("1")
    assert len(patches) == 1
    requested = patches[0].post_data_json
    assert isinstance(requested, dict) and requested["sheet"] == "Samples"
    imported.refresh_from_db()
    assert (imported.sheet, imported.header_row, imported.first_row) == ("About", 1, 2)
    held[0].continue_()
    expect(header).to_be_enabled()
    header.fill("3")
    with page.expect_response(
        lambda r: r.request.method == "PATCH" and r.url.endswith(str(imported.pk))
    ) as saving:
        header.press("Enter")
    assert saving.value.status == 200
    body = saving.value.request.post_data_json
    assert isinstance(body, dict) and body["header_row"] == 3
    assert (body.get("basis") or body)["sheet"] == "Samples"
    expect(first).to_have_value("4")
    imported.refresh_from_db()
    assert (imported.sheet, imported.header_row, imported.first_row) == ("Samples", 3, 4)

    sheet.select_option("About")
    notice = page.get_by_role("status").filter(has_text="About")
    expect(notice).to_contain_text(re.compile(r"header", re.I))
    expect(notice).to_contain_text(re.compile(r"\b3\b"))
    expect(notice).to_contain_text(re.compile(r"\b1\b"))
    expect(header).to_have_value("1")
    expect(first).to_have_value("2")
    imported.refresh_from_db()
    assert (imported.sheet, imported.header_row, imported.first_row) == ("About", 1, 2)


def test_a_rejected_header_cannot_advance_or_preview_until_its_saved_value_is_restored(
    sample_manifest: tuple[Page, SpreadsheetImport],
) -> None:
    page, imported = sample_manifest
    page.get_by_role("combobox", name="Sheet", exact=True).select_option("Samples")
    sample = page.get_by_role("table", name="Its first rows")
    expect(sample.get_by_text("ABL1-KD-001")).to_be_visible()
    header = page.get_by_role("spinbutton", name="Header row")
    first = page.get_by_role("spinbutton", name="First row")
    header.fill("3")
    header.press("Enter")
    expect(first).to_have_value("4")

    header.fill("99")
    header.press("Enter")
    expect(header).to_have_attribute("aria-invalid", "true")
    expect(header).to_have_accessible_description(re.compile(r"1 to 11"))
    expect(header).to_have_value("99")
    next_step = page.get_by_role("button", name="Next: columns")
    expect(next_step).to_be_disabled()
    next_step.click(force=True)
    expect(header).to_be_visible()
    expect(header).to_have_attribute("aria-invalid", "true")
    expect(page.get_by_role("button", name="Check every row")).to_have_count(0)
    imported.refresh_from_db()
    assert (imported.sheet, imported.header_row, imported.first_row) == ("Samples", 3, 4)
    assert imported.state == "mapping" and imported.round == 0 and imported.run_id is None

    header.press("Escape")
    expect(header).to_have_value("3")
    expect(header).not_to_have_attribute("aria-invalid", "true")
    expect(first).to_have_value("4")
    next_step.click()
    page.get_by_role("button", name="Check every row").click()
    page.get_by_role("heading", name="Rows of Samples").wait_for(timeout=60_000)
    imported.refresh_from_db()
    rows = PreviewRow.objects.filter(spreadsheet_import_id=imported.pk, round=imported.round)
    assert sorted(rows.values_list("row", flat=True)) == list(range(4, 12))
    assert (imported.sheet, imported.header_row, imported.first_row) == ("Samples", 3, 4)
    assert imported.state == "previewed" and imported.run_id is None


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
    choose_manifest(page)
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


def test_a_second_tabs_preview_requires_review_before_importing_its_rows(
    open_as: Any,
    world: World,
    tasks: Server,
) -> None:
    first: Page = open_as("editor", f"/projects/kinase-atlas/entities/import/?type={SAMPLE}")
    expect(first.get_by_role("combobox", name="Its rows are")).to_have_value(SAMPLE)
    first.get_by_label("Upload a workbook").set_input_files(
        {
            "name": "two-batches.xlsx",
            "mimeType": XLSX_TYPE,
            "buffer": xlsx(("A", [["Label"], ["From A"]]), ("B", [["Label"], ["From B"]])),
        }
    )

    def preview(tab: Page, sheet: str) -> None:
        tab.get_by_role("button", name="Next: columns").click()
        tab.get_by_role("button", name="Check every row").click()
        tab.get_by_role("heading", name=f"Rows of {sheet}").wait_for(timeout=60_000)
        expect(tab.get_by_role("button", name="Import 1 row", exact=True)).to_be_enabled()

    preview(first, "A")
    imported = SpreadsheetImport.objects.get(project=world.project)
    old_round = imported.round
    why = first.get_by_role("textbox", name="Why")
    why.fill("Reviewed batch A")
    # The first tab has seen only A. Keep a focus-triggered background read from
    # revealing B until its old approval has actually reached the server.
    snapshot_url = f"**/api/v1/spreadsheet-imports/{imported.pk}"
    delayed_reads: list[Route] = []
    first.route(snapshot_url, lambda route: delayed_reads.append(route))

    second = watch(first.context.new_page())
    second.goto(first.url)
    second.get_by_role("button", name="Back to the columns").click()
    second.get_by_role("button", name="Back", exact=True).click()
    second.get_by_role("combobox", name="Sheet", exact=True).select_option("B")
    expect(second.get_by_role("table", name="Its first rows").get_by_text("From B")).to_be_visible()
    preview(second, "B")
    imported.refresh_from_db()
    new_round = imported.round
    assert new_round > old_round
    expect(first.get_by_role("heading", name="Rows of A")).to_be_visible()

    endpoint = f"**/api/v1/spreadsheet-imports/{imported.pk}/commit"
    approve = first.get_by_role("button", name="Import 1 row", exact=True)
    with first.expect_response(endpoint) as stale:
        approve.click()
    assert stale.value.status == 409
    stale_body = stale.value.request.post_data_json
    assert isinstance(stale_body, dict) and stale_body["preview_round"] == old_round
    assert stale_body["reason"] == "Reviewed batch A"
    imported.refresh_from_db()
    assert imported.state == "previewed" and imported.run_id is None
    samples = Entity.objects.filter(type=SAMPLE, owner_project=world.project)
    assert not samples.exists()
    assert not ActionRun.objects.filter(project=world.project).exists()

    review = first.get_by_role("button", name="Review latest preview")
    expect(review).to_be_visible()
    expect(approve).to_have_count(0)
    first.unroute(snapshot_url)
    for read in delayed_reads:
        read.continue_()
    review.click()
    expect(first.get_by_role("heading", name="Rows of B")).to_be_visible()
    expect(why).to_have_value("")
    why.fill("Reviewed batch B")
    with first.expect_response(endpoint) as fresh:
        approve.click()
    assert fresh.value.status == 202
    fresh_body = fresh.value.request.post_data_json
    assert isinstance(fresh_body, dict) and fresh_body["preview_round"] == new_round
    assert fresh_body["reason"] == "Reviewed batch B"
    first.get_by_text("Imported 1 row of two-batches.xlsx into", exact=False).wait_for(
        timeout=90_000
    )
    imported.refresh_from_db()
    sample = samples.get()
    assert entities.current_values(entities.specific(sample))["label"] == "From B"
    origin = ExternalOrigin.objects.get(entity=sample)
    assert origin.identifier == "B, row 2" and origin.run_id == imported.run_id
    run = ActionRun.objects.get(project=world.project)
    assert (
        run.pk == imported.run_id
        and run.params["sheet"] == "B"
        and run.reason == "Reviewed batch B"
    )


def test_a_workbook_already_in_the_project_is_picked_from_its_files(
    open_as: Any, world: World, tasks: Server
) -> None:
    from iki_server.files.models import File

    kinases(world)
    page = open_as("editor", f"/projects/kinase-atlas/entities/import/?type={SAMPLE}")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.get_by_role("heading", level=2, name="Import a spreadsheet").wait_for()
    # Someone uploaded the core's manifest once, and left that import.
    choose_manifest(page)
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
