# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A project's Files (ADR 0177) in real browsers: found by part of a name, format and when
their data was acquired; Used by links to the records and revisions that held a file; the
file picker in an entity form. Each passes axe in both themes and fits a phone's width."""

import re
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
from test_project_files import add

from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.files.models import File

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
STRUCTURE = "acme/protein.ProteinStructure"


def a_plate(world: World) -> dict[str, File]:
    """Two Sanger traces whose instrument recorded their run start, a certificate that
    records nothing, and a structure a record holds."""
    admin = world.users["admin"]
    return {
        "a01": add(
            world.project,
            "A01_clone-17-F.ab1",
            "application/x-abi",
            size=212_480,
            acquired="2026-03-14T09:21:07",
            source="abif",
            by=admin,
        ),
        "b02": add(
            world.project,
            "B02_clone-18-R.ab1",
            "application/x-abi",
            size=198_656,
            acquired="2026-02-02T16:40:11",
            source="abif",
            by=admin,
        ),
        "coa": add(world.project, "coa-lot-12.pdf", "application/pdf", size=88_064, by=admin),
        "pdb": add(world.project, "1abc.pdb", "chemical/x-pdb", size=40_960, by=admin),
    }


def held(world: World, file: File) -> Entity:
    """A structure that held the file at revision 1 and still does at 2."""
    admin = world.users["admin"]
    saved = entities.create(admin, STRUCTURE, world.project, {"structure": str(file.pk)})
    record = Entity.objects.get(pk=saved.entity.pk)
    entities.update(admin, record, {"method": "xray"}, expected_revision=1)
    return record


def rows(page: Page) -> list[str]:
    table = page.get_by_role("table", name="Files in Kinase Atlas")
    names = table.locator("tbody tr td:first-child")
    return [n.strip() for n in names.all_inner_texts()]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_finding_a_projects_files_by_name_format_and_acquisition(
    open_as: Any, world: World, color_scheme: str
) -> None:
    files = a_plate(world)
    held(world, files["pdb"])
    page = open_as("admin", "/projects/kinase-atlas/files/", color_scheme)
    table = page.get_by_role("table", name="Files in Kinase Atlas")
    table.get_by_role("link", name="1abc.pdb").wait_for()
    # Newest added first.
    assert rows(page) == ["1abc.pdb", "coa-lot-12.pdf", "B02_clone-18-R.ab1", "A01_clone-17-F.ab1"]
    expect(table.get_by_role("row", name=re.compile(r"^coa-lot-12\.pdf"))).to_contain_text(
        "Unknown"
    )
    assert_clean(page)

    # Part of the name, in any case.
    page.get_by_role("searchbox", name="Search").fill("CLONE")
    page.wait_for_url(re.compile(r"q=CLONE"))
    expect(table.locator("tbody tr")).to_have_count(2)
    page.get_by_role("combobox", name="Sort").select_option(label="Earliest acquired")
    expect(table.locator("tbody tr").first).to_contain_text("B02_clone-18-R.ab1")

    # Acquired from March on: the instrument's date, not when it was added.
    page.get_by_role("button", name="Filters").click()
    filters = page.get_by_role("group", name="Filters")
    # Found by role and text, not label: Firefox crashes on labels after axe.
    filters.locator("label", has_text="Acquired from").locator("input").fill("2026-03-01")
    page.wait_for_url(re.compile(r"acquired_from=2026-03-01"))
    expect(table.locator("tbody tr")).to_have_count(1)
    expect(table.locator("tbody tr")).to_contain_text("A01_clone-17-F.ab1")
    assert_clean(page)

    # The address holds it all: a reload shows the same.
    page.reload()
    page.get_by_role("table", name="Files in Kinase Atlas").get_by_role(
        "link", name="A01_clone-17-F.ab1"
    ).wait_for()
    expect(
        page.get_by_role("table", name="Files in Kinase Atlas").locator("tbody tr")
    ).to_have_count(1)

    # By format, from the formats there are.
    page.goto("/projects/kinase-atlas/files/")
    page.get_by_role("combobox", name="Format").select_option("application/pdf")
    page.wait_for_url(re.compile(r"format=application%2Fpdf|format=application/pdf"))
    expect(
        page.get_by_role("table", name="Files in Kinase Atlas").locator("tbody tr")
    ).to_have_count(1)

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_used_by_links_to_the_records_and_revisions_that_held_a_file(
    open_as: Any, world: World, color_scheme: str
) -> None:
    files = a_plate(world)
    record = held(world, files["pdb"])
    page = open_as("admin", "/projects/kinase-atlas/files/", color_scheme)
    page.get_by_role("link", name="1 record").click()
    page.wait_for_url(re.compile(rf"/files/{files['pdb'].pk}/#used-by$"))
    page.get_by_role("heading", level=1, name="1abc.pdb").wait_for()
    facts = page.locator(".file-facts")
    expect(facts).to_contain_text(files["pdb"].blob.sha256)
    expect(facts).to_contain_text("Unknown")
    records = page.get_by_role("table", name="Records that hold 1abc.pdb")
    revisions = records.get_by_role("link", name=f"{record.hrid} revisions r1 on")
    revisions.wait_for()
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    revisions.click()
    page.wait_for_url(re.compile(rf"/entities/{record.pk}/audit/\?field=structure$"))
    page.go_back()
    records.locator("a.list-name").click()
    page.wait_for_url(re.compile(rf"/entities/{record.pk}/$"))

    # A trace whose format records it says when, and how that's known.
    page.goto(f"/files/{files['a01'].pk}/")
    page.get_by_role("heading", level=1, name="A01_clone-17-F.ab1").wait_for()
    expect(page.locator(".file-facts")).to_contain_text(
        "2026-03-14 09:21:07 from the ABIF trace’s run start (RUND and RUNT)"
    )
    page.get_by_text("No record you can read holds it.").wait_for()
    assert_clean(page)

    # Search everything finds files by part of their name, and opens one.
    page.keyboard.press("ControlOrMeta+k")
    palette = page.get_by_role("dialog", name="Search everything")
    palette.get_by_role("combobox").fill("clone-18")
    found = palette.get_by_role("group", name="Files").get_by_role("option")
    expect(found).to_have_count(1)
    found.click()
    page.get_by_role("heading", level=1, name="B02_clone-18-R.ab1").wait_for()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_choosing_a_file_for_an_entity_form_with_the_picker(
    open_as: Any, world: World, color_scheme: str
) -> None:
    files = a_plate(world)
    second = add(world.project, "2xyz.pdb", "chemical/x-pdb", by=world.users["admin"])
    page = open_as("admin", f"/projects/kinase-atlas/entities/new/?type={STRUCTURE}", color_scheme)
    page.get_by_role("heading", level=2, name="New protein structure").wait_for()
    structure = page.get_by_role("group", name="Structure")
    structure.get_by_role("button", name="Or choose one already here").click()
    picker = page.get_by_role("dialog", name="Choose a file for Structure")
    table = picker.get_by_role("table", name="Files you can choose for Structure")
    # Only what the field takes: the structures, not the traces or the certificate.
    table.get_by_role("radio", name="1abc.pdb").wait_for()
    expect(table.locator("tbody tr")).to_have_count(2)
    assert_clean(page)

    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)
    page.set_viewport_size({"width": 1440, "height": 900})

    picker.get_by_role("searchbox", name="Search").fill("xyz")
    expect(table.locator("tbody tr")).to_have_count(1)
    # Choosing one closes the picker.
    table.get_by_role("radio", name="2xyz.pdb").click()
    expect(picker).to_be_hidden()
    structure.get_by_role("button", name="Remove 2xyz.pdb").wait_for()
    assert_clean(page)

    page.get_by_role("button", name="Create protein structure").click()
    page.wait_for_url(re.compile(r"/entities/PST-\d+/$"))
    made = Entity.objects.get(type=STRUCTURE)
    assert entities.current_values_of([made])[made.pk]["structure"] == second.pk
    assert files["pdb"].pk != second.pk
