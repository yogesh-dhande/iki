# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Admin → Quarantine in real browsers (ADR 0217): a plate-reader export ClamAV flags and
a sequencing archive too large to scan, held in Kinase Atlas. A Quarantine Admin finds
them, opens one from the keyboard, and releases the false positive with a reason; the
archive can't be released. A held file says so in its project's list and on its page, and
sends a Quarantine Admin who can read it to review it. Each passes axe in both themes and
fits a phone's width; nobody else sees any of it or can read it from the API."""

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

from iki_server.audit.models import AuditEvent
from iki_server.files import scan_state
from iki_server.files.models import Blob, File, ScanRelease
from iki_server.org.models import OrgRole, OrgRoleAssignment

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
SIZES = [(390, 844), (1440, 900)]

MACRO = "Xls.Downloader.Agent-7372345-0"
EXPORT = "EnVision_plate-07.xlsm"
ARCHIVE = "NovaSeq_221103_A01234.tar"
REASON = "Compared byte for byte with the EnVision export on the instrument PC."


def held(world: World) -> dict[str, File]:
    """The export, flagged by a macro heuristic, then the archive, too large to scan; and
    a clean certificate that isn't held."""
    editor = world.users["editor"]

    def add(n: int, name: str, media_type: str, size: int) -> File:
        blob = Blob.objects.create(sha256=f"{n:064x}", size=size, storage_uri=f"s3://iki/q{n}")
        return File.objects.create(
            blob=blob, name=name, media_type=media_type, project=world.project, created_by=editor
        )

    export = add(1, EXPORT, "application/vnd.ms-excel.sheet.macroenabled.12", 48_213)
    scan_state.record_result(
        export.blob,
        state="infected",
        scanner="ClamAV 1.4.3",
        signature_version="27453",
        reason=MACRO,
    )
    archive = add(2, ARCHIVE, "application/x-tar", 412_000_000_000)
    scan_state.record_result(
        archive.blob, state="not_scanned", scanner="ClamAV", reason="too_large"
    )
    certificate = add(3, "coa-lot-12.pdf", "application/pdf", 88_064)
    scan_state.record_result(
        certificate.blob, state="clean", scanner="ClamAV 1.4.3", signature_version="27453"
    )
    return {"export": export, "archive": archive, "certificate": certificate}


def decision(file: File) -> str:
    found = scan_state.status_for(file.blob).decision_id
    assert found is not None
    return str(found)


def names(page: Page, caption: str) -> list[str]:
    table = page.get_by_role("table", name=caption)
    return [n.strip() for n in table.locator("tbody tr td:first-child a").all_inner_texts()]


def fits(page: Page) -> None:
    for width, height in SIZES:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => done(true)))")
        assert page.evaluate(NO_SIDEWAYS_SCROLL), (width, height)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_quarantine_admin_finds_a_false_positive_and_releases_it(
    open_as: Any, world: World, color_scheme: str
) -> None:
    files = held(world)
    export = decision(files["export"])
    page = open_as("quarantine_admin", "/admin/", color_scheme)
    page.wait_for_url("**/admin/quarantine/")
    sidebar = page.get_by_role("navigation", name="Admin")
    expect(sidebar.get_by_role("link", name="Quarantine", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    caption = "Files held in quarantine"
    expect(page.get_by_role("table", name=caption)).to_be_visible()
    assert names(page, caption) == [ARCHIVE, EXPORT]
    row = page.get_by_role("table", name=caption).get_by_role("row", name=re.compile(EXPORT))
    expect(row).to_contain_text("Kinase Atlas")
    expect(row).to_contain_text("Malware found")
    expect(row).to_contain_text(MACRO)
    expect(row).to_contain_text("by Editor")
    fits(page)
    assert_clean(page)

    page.get_by_role("combobox", name="Why").select_option("infected")
    page.wait_for_url(re.compile(r"state=infected"))
    expect(page.get_by_role("table", name=caption).get_by_role("row")).to_have_count(2)
    assert names(page, caption) == [EXPORT]

    # From the keyboard: its link, then Enter.
    page.get_by_role("link", name=EXPORT, exact=True).focus()
    page.keyboard.press("Enter")
    page.wait_for_url(f"**/admin/quarantine/{export}/")
    expect(page.get_by_role("heading", level=2, name=f"{EXPORT} Held")).to_be_visible()
    finding = page.get_by_role("region", name="Finding")
    expect(finding).to_contain_text(MACRO)
    expect(finding).to_contain_text("ClamAV 1.4.3, signatures 27453")
    where = page.get_by_role("region", name="Where it is")
    expect(where.get_by_role("row", name=re.compile(EXPORT))).to_contain_text("Kinase Atlas")
    # The reviewer isn't a member of Kinase Atlas: the file's page isn't offered.
    expect(where.get_by_role("link")).to_have_count(0)
    fits(page)
    assert_clean(page)

    page.get_by_role("button", name="Release…").click()
    dialog = page.get_by_role("dialog", name=f"Release {EXPORT}?")
    expect(dialog).to_contain_text("It can’t be undone.")
    field = dialog.get_by_role("textbox", name="Why is it a false positive?")
    dialog.get_by_role("button", name="Release", exact=True).click()
    expect(dialog.get_by_text("Say why it’s a false positive.")).to_be_visible()
    # The field that needs it has the focus, to type the reason straight away.
    expect(field).to_be_focused()
    assert not ScanRelease.objects.exists()
    assert_clean(page)
    field.fill(REASON)
    dialog.get_by_role("button", name="Release", exact=True).click()
    expect(dialog).to_be_hidden()
    release = page.get_by_role("region", name="Release")
    expect(release).to_contain_text("Released by Quarantine Admin (quarantine_admin@example.com)")
    expect(release).to_contain_text(REASON)
    expect(release.get_by_role("heading", name="Release")).to_be_focused()
    expect(page.get_by_role("region", name="History")).to_contain_text(
        f"By Quarantine Admin: {REASON}"
    )
    recorded = ScanRelease.objects.get()
    assert (str(recorded.decision_id), recorded.reason) == (export, REASON)
    event = AuditEvent.objects.get(action="file.quarantine_released")
    assert event.actor_id == world.users["quarantine_admin"].pk and event.data["reason"] == REASON
    assert scan_state.status_for(files["export"].blob).usable
    assert_clean(page)

    page.get_by_role("link", name="All quarantined files").click()
    page.wait_for_url("**/admin/quarantine/")
    # It's no longer held: the list doesn't show it, even for a moment.
    expect(page.get_by_role("table", name=caption).get_by_role("row")).to_have_count(2)
    assert names(page, caption) == [ARCHIVE]
    page.get_by_role("combobox", name="Status").select_option("released")
    released = page.get_by_role("table", name="Files released from quarantine")
    expect(released.get_by_role("row", name=re.compile(EXPORT))).to_contain_text(
        "by Quarantine Admin"
    )
    assert_clean(page)
    page.context.close()


def test_a_scan_that_couldnt_finish_is_held_and_cant_be_released(
    open_as: Any, world: World
) -> None:
    files = held(world)
    page = open_as("quarantine_admin", f"/admin/quarantine/{decision(files['archive'])}/")
    expect(page.get_by_role("heading", level=2, name=f"{ARCHIVE} Held")).to_be_visible()
    expect(page.get_by_role("region", name="Finding")).to_contain_text("Too large to scan")
    expect(page.get_by_role("region", name="Release")).to_contain_text("It can’t be released.")
    expect(page.get_by_role("button", name="Release…")).to_have_count(0)
    assert_clean(page)
    page.context.close()


def test_a_held_file_says_so_in_its_list_and_on_its_page(open_as: Any, world: World) -> None:
    """For a project member, and with a way to review it for one who's also a Quarantine
    Admin."""
    files = held(world)
    export = files["export"]
    page = open_as("editor", "/projects/kinase-atlas/files/")
    table = page.get_by_role("table", name="Files in Kinase Atlas")
    expect(table.get_by_role("row", name=re.compile(EXPORT))).to_contain_text("Quarantined")
    expect(table.get_by_role("row", name=re.compile(ARCHIVE))).to_contain_text("Held: not scanned")
    expect(table.get_by_role("row", name=re.compile("coa-lot-12"))).not_to_contain_text("Held")
    assert_clean(page)
    table.get_by_role("link", name=EXPORT, exact=True).click()
    page.wait_for_url(f"**/files/{export.pk}/")
    facts = page.locator("dt", has_text="Malware scan").locator("xpath=following-sibling::dd[1]")
    expect(facts).to_contain_text(
        f"Quarantined: the scanner found {MACRO}. It can’t be downloaded or used unless a "
        "Quarantine Admin finds it a false positive."
    )
    expect(facts.get_by_role("link")).to_have_count(0)
    expect(page.get_by_role("link", name="Download")).to_have_count(0)
    assert_clean(page)
    page.context.close()

    OrgRoleAssignment.objects.create(user=world.users["admin"], role=OrgRole.QUARANTINE_ADMIN)
    page = open_as("admin", f"/files/{export.pk}/")
    facts = page.locator("dt", has_text="Malware scan").locator("xpath=following-sibling::dd[1]")
    facts.get_by_role("link", name="Review it in Admin → Quarantine").click()
    page.wait_for_url(f"**/admin/quarantine/{decision(export)}/")
    # They read Kinase Atlas, so its file's page is offered, back where they came from.
    where = page.get_by_role("region", name="Where it is")
    expect(where.get_by_role("link", name=EXPORT)).to_have_attribute("href", f"/files/{export.pk}/")
    assert_clean(page)
    page.context.close()


def test_nobody_else_sees_or_reads_the_quarantine(open_as: Any, world: World) -> None:
    held(world)
    for principal in ("viewer", "org_admin", "auditor"):
        page = open_as(principal, "/admin/quarantine/")
        expect(page.get_by_role("heading", level=1).filter(has_text="Not found")).to_be_visible()
        expect(page.get_by_role("link", name="Quarantine", exact=True)).to_have_count(0)
        expect(page.get_by_text(EXPORT)).to_have_count(0)
        status = page.evaluate("() => fetch('/api/v1/quarantine').then((r) => r.status)")
        assert status == 403, principal
        page.context.close()
