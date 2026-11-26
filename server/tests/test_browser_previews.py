# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Previewing a file without downloading it (ADR 0226, ADR 0227), in real browsers, with
its bytes in the test store: a CSV file as a table, a large one read from its start in a
range, a PDF page by page with its text, an image, a Sanger trace with its calls, spectra,
and a tiled TIFF; on a file's page and from a record's file field. Each read is the
audited download link; each page keeps the policy and passes axe in both themes."""

import re
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import expect
from test_browser import (  # noqa: F401 - fixtures
    PAGE_ERRORS,
    _orm_beside_playwright,
    _stored_file,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    server,
    watch,
    world,
)

from iki_server.audit.models import AuditEvent
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.files import scan_state

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

DATA = Path(__file__).parent / "data" / "previews"
NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"

PLATE = (
    "well,compound,concentration_um,inhibition_pct\n"
    "A1,IKI-0001,10,12.5\n"
    "A2,IKI-0002,10,87.1\n"
    "A3,IKI-0003,10,45.0\n"
    "B1,DMSO,0,0.4\n"
)


def clean(page: Any) -> None:
    """assert_clean, with axe in Chromium and WebKit: Playwright's Firefox crashes its
    content process when axe runs on a file's page once a viewer has drawn the file. It
    isn't the preview's markup (taking it out of the page leaves the crash), nor its worker,
    its reads from the store or its code's loading, each tried alone on a file's page that
    axe passes in Firefox. axe checks the DOM, which is the same in every engine; Firefox
    still checks the policy and errors."""
    assert_clean(page, axe_in_firefox=False)


def no_errors(page: Any) -> None:
    """No CSP violations or errors since the page opened. axe runs once a test, before it
    finds anything by role and name: in Firefox, Playwright's role engine and then axe
    crash the page (see assert_clean)."""
    assert page.evaluate("window.__cspViolations") == [], page.url
    assert PAGE_ERRORS[page] == [], page.url


def links_issued(file: Any) -> int:
    return AuditEvent.objects.filter(
        action="file.download_link_issued", target_id=str(file.pk)
    ).count()


def two_page_pdf() -> bytes:
    """A two-page certificate of analysis: text in Helvetica, which PDFs needn't embed."""

    def page_text(lines: list[str]) -> bytes:
        ops = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL"]
        ops += [f"({line}) Tj T*" for line in lines]
        ops.append("ET")
        return "\n".join(ops).encode()

    contents = [
        page_text(["Certificate of analysis", "Lot 12, imatinib mesylate", "Purity (HPLC): 99.6%"]),
        page_text(["Residual solvents", "Ethanol: 210 ppm", "Released by QA"]),
    ]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R"
        b" /Resources << /Font << /F1 7 0 R >> >> >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R"
        b" /Resources << /Font << /F1 7 0 R >> >> >>",
        *(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(c), c) for c in contents),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def gel_png(width: int = 64, height: int = 48) -> bytes:
    """A small grey PNG: a gel's bands as darker rows."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = b"".join(
        b"\x00" + bytes([40 if y % 12 in (4, 5) else 220] * width) for y in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def stored(
    world: World,
    endpoint: str,
    bucket: str,
    tmp: Path,
    name: str,
    data: bytes,
    media: str,
    *,
    scanned: bool = True,
) -> Any:
    path = tmp / name
    path.write_bytes(data)
    return _stored_file(world, endpoint, bucket, path, media, scanned=scanned)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_csv_file_is_a_table_on_its_page(
    open_as: Any,
    world: World,
    color_scheme: str,
    tmp_path: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    file = stored(
        world,
        _s3_endpoint,
        _s3_bucket,
        tmp_path,
        "plate-07-inhibition.csv",
        PLATE.encode(),
        "text/csv",
    )
    page = open_as("admin", f"/files/{file.pk}/", color_scheme)
    expect(
        page.get_by_text("5 rows. Read as UTF-8. Read whole and checked against its SHA-256.")
    ).to_be_visible()
    clean(page)
    table = page.get_by_role("table", name="plate-07-inhibition.csv")
    expect(table.get_by_role("columnheader", name="inhibition_pct")).to_be_visible()
    # Each row is numbered as the file's line is: the header is line 1.
    expect(table.get_by_role("row", name="3 A2 IKI-0002 10 87.1")).to_be_visible()
    # The table scrolls by keyboard: it's reached with Tab.
    grid = page.locator(".preview-grid")
    grid.focus()
    expect(grid).to_be_focused()
    assert links_issued(file) == 1
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    no_errors(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_file_the_scan_holds_says_why_and_is_never_read(
    open_as: Any,
    world: World,
    color_scheme: str,
    tmp_path: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    """A quarantined workbook's preview says what the scanner found, with no download, and
    asks the store for nothing (ADR 0216): the link a preview reads through isn't issued."""
    file = stored(
        world,
        _s3_endpoint,
        _s3_bucket,
        tmp_path,
        "EnVision_plate-07.csv",
        PLATE.encode(),
        "text/csv",
        scanned=False,
    )
    scan_state.record_result(
        file.blob,
        state="infected",
        scanner="ClamAV 1.4.3",
        signature_version="27453",
        reason="Xls.Downloader.Agent-7372345-0",
    )
    page = open_as("admin", f"/files/{file.pk}/", color_scheme)
    preview = page.get_by_role("region", name="Table")
    expect(preview.get_by_text("It isn’t shown or downloaded", exact=False)).to_be_visible()
    clean(page)
    expect(preview.get_by_text("Xls.Downloader.Agent-7372345-0")).to_be_visible()
    expect(preview.get_by_role("table")).to_have_count(0)
    expect(page.get_by_role("link", name=re.compile("^Download"))).to_have_count(0)
    assert links_issued(file) == 0
    assert page.evaluate(NO_SIDEWAYS_SCROLL)


def test_a_file_still_being_scanned_is_shown_once_its_scan_finds_it_clean(
    open_as: Any, world: World, tmp_path: Path, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = stored(
        world,
        _s3_endpoint,
        _s3_bucket,
        tmp_path,
        "plate-07-inhibition.csv",
        PLATE.encode(),
        "text/csv",
        scanned=False,
    )
    page = open_as("admin", f"/files/{file.pk}/")
    preview = page.get_by_role("region", name="Table")
    expect(preview.get_by_text("Scanning…", exact=False)).to_be_visible()
    clean(page)
    assert links_issued(file) == 0
    scan_state.record_result(file.blob, state="clean", scanner="ClamAV 1.4.3")
    # The page asks again while the file is scanned, and reads it once it may.
    expect(preview.get_by_role("table", name="plate-07-inhibition.csv")).to_be_visible()
    assert links_issued(file) == 1
    no_errors(page)


def test_a_large_csv_file_is_read_from_its_start_in_a_range(
    open_as: Any, world: World, tmp_path: Path, _s3_endpoint: str, _s3_bucket: str
) -> None:
    rows = ["well,compound,concentration_um,inhibition_pct"]
    rows += [f"P{n},IKI-{n:06d},10,{n % 100}.5" for n in range(700_000)]
    data = ("\n".join(rows) + "\n").encode()
    assert len(data) > 16 * 1024 * 1024
    file = stored(world, _s3_endpoint, _s3_bucket, tmp_path, "screen-2026-11.csv", data, "text/csv")
    page = open_as("admin", f"/files/{file.pk}/")
    # The store let the page read a range and see which bytes it sent (setup_storage's CORS).
    expect(page.get_by_text("Showing the 50,000 rows in its first 16 MiB")).to_be_visible()
    expect(page.get_by_text("Read in parts")).to_be_visible()
    clean(page)
    table = page.get_by_role("table", name="screen-2026-11.csv")
    expect(table.get_by_role("row", name="2 P0 IKI-000000 10 0.5")).to_be_visible()
    assert links_issued(file) == 1
    no_errors(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_pdf_is_shown_page_by_page_with_its_text(
    open_as: Any,
    world: World,
    color_scheme: str,
    tmp_path: Path,
    _s3_endpoint: str,
    _s3_bucket: str,
) -> None:
    file = stored(
        world,
        _s3_endpoint,
        _s3_bucket,
        tmp_path,
        "coa-lot-12.pdf",
        two_page_pdf(),
        "application/pdf",
    )
    page = open_as("admin", f"/files/{file.pk}/", color_scheme)
    expect(page.locator(".preview-pdf-text").get_by_text("Purity (HPLC): 99.6%")).to_be_attached()
    expect(page.locator(".preview-pdf-frame [role=status]")).to_have_count(0)
    clean(page)
    expect(page.get_by_role("button", name="Previous page")).to_be_disabled()
    page.get_by_role("button", name="Next page").click()
    expect(
        page.get_by_role("group", name="Page 2 of 2").get_by_text("Released by QA")
    ).to_be_attached()
    # The keyboard turns pages from the page itself.
    page.get_by_role("group", name="Page 2 of 2").press("PageUp")
    expect(page.get_by_role("group", name="Page 1 of 2")).to_be_focused()
    page.get_by_role("spinbutton", name="Page").fill("2")
    expect(page.get_by_role("group", name="Page 2 of 2")).to_be_visible()
    assert links_issued(file) == 1
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    no_errors(page)


def test_an_image_is_shown_from_its_checked_bytes(
    open_as: Any, world: World, tmp_path: Path, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = stored(
        world, _s3_endpoint, _s3_bucket, tmp_path, "gel-2026-11-20.png", gel_png(), "image/png"
    )
    page = open_as("admin", f"/files/{file.pk}/")
    expect(page.get_by_text("64 × 48 pixels")).to_be_visible()
    clean(page)
    image = page.get_by_role("img", name="gel-2026-11-20.png")
    assert image.evaluate("i => i.src.startsWith('blob:') && i.naturalWidth") == 64
    page.get_by_role("combobox", name="Zoom").select_option("2")
    assert image.evaluate("i => i.getBoundingClientRect().width") == 128
    no_errors(page)


def test_a_record_s_file_opens_its_preview_in_a_dialog(
    open_as: Any, world: World, tmp_path: Path, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = stored(
        world,
        _s3_endpoint,
        _s3_bucket,
        tmp_path,
        "plate-07-inhibition.csv",
        PLATE.encode(),
        "text/csv",
    )
    admin = world.users["admin"]
    made = entities.create(
        admin, "acme/protein.Lot", world.project, {"label": "Lot 12", "report": file.pk}
    )
    hrid = Entity.objects.get(pk=made.entity.pk).hrid
    page = open_as("admin", f"/entities/{hrid}/")
    opener = page.get_by_role("button", name="Preview plate-07-inhibition.csv")
    # From the keyboard, so the focus has somewhere to go back to (WebKit doesn't focus a
    # button that's clicked).
    opener.focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("5 rows. Read as UTF-8.", exact=False)).to_be_visible()
    # In a dialog on a record's page, Firefox's axe runs too.
    assert_clean(page)
    dialog = page.get_by_role("dialog", name="plate-07-inhibition.csv")
    expect(dialog.get_by_role("table", name="plate-07-inhibition.csv")).to_be_visible()
    expect(dialog.get_by_role("link", name="Open its page")).to_have_attribute(
        "href", f"/files/{file.pk}/"
    )
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(opener).to_be_focused()
    assert links_issued(file) == 1


def sample(world: World, endpoint: str, bucket: str, name: str, media: str) -> Any:
    return _stored_file(world, endpoint, bucket, DATA / name, media)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_sanger_trace_shows_its_calls_and_their_quality(
    open_as: Any, world: World, color_scheme: str, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = sample(world, _s3_endpoint, _s3_bucket, "3730.ab1", "application/x-abi")
    page = open_as("admin", f"/files/{file.pk}/", color_scheme)
    expect(page.get_by_text("Q20 or better")).to_be_visible()
    clean(page)
    trace = page.locator(".preview-scroll")
    expect(trace).to_have_attribute("aria-label", re.compile("^Chromatogram of 3730.ab1"))
    # The calls as text, for reading without the picture.
    page.get_by_text("bases as text").click()
    expect(page.locator(".preview-bases")).to_contain_text("     1  ")
    # The trace scrolls from the keyboard, and jumps to a base.
    trace.focus()
    trace.press("End")
    assert trace.evaluate("t => t.scrollLeft > 0")
    page.get_by_role("spinbutton", name="Go to base").fill("1")
    page.get_by_role("button", name="Go", exact=True).click()
    assert trace.evaluate("t => t.scrollLeft") == 0
    assert links_issued(file) == 1
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    no_errors(page)


def test_a_jcamp_dx_spectrum_is_a_plot_with_its_highest_points(
    open_as: Any, world: World, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = sample(world, _s3_endpoint, _s3_bucket, "jcamp-difdup.dx", "chemical/x-jcamp-dx")
    page = open_as("admin", f"/files/{file.pk}/")
    plot = page.locator(".preview-plot")
    expect(plot).to_have_attribute("role", "img")
    clean(page)
    label = plot.get_attribute("aria-label") or ""
    assert "points" in label and "Highest shown" in label, label
    plot.focus()
    plot.press("+")
    expect(page.get_by_role("button", name="Show all")).to_be_enabled()
    assert "zoomed to" in (plot.get_attribute("aria-label") or "")
    page.get_by_role("button", name="Show all").click()
    page.get_by_text("highest points").click()
    expect(page.locator(".preview-data table tbody tr")).to_have_count(20)
    no_errors(page)


def test_an_mzml_file_is_read_a_spectrum_at_a_time(
    open_as: Any, world: World, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = sample(world, _s3_endpoint, _s3_bucket, "tiny.pwiz.1.1.mzML", "application/x-mzml")
    page = open_as("admin", f"/files/{file.pk}/")
    expect(page.get_by_text("MS level")).to_be_visible()
    clean(page)
    first = page.locator(".preview-facts dd").first.inner_text()
    page.get_by_role("button", name="Next spectrum").click()
    expect(page.locator(".preview-facts dd").first).not_to_have_text(first)
    expect(page.get_by_role("spinbutton", name="Spectrum")).to_have_value("2")
    page.get_by_role("button", name="Chromatograms", exact=False).click()
    expect(page.get_by_text("Time (min)", exact=False).first).to_be_attached()
    no_errors(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_an_ome_tiff_is_drawn_in_tiles_by_channel_and_plane(
    open_as: Any, world: World, color_scheme: str, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = sample(world, _s3_endpoint, _s3_bucket, "ome_pyramid.ome.tif", "image/tiff")
    page = open_as("admin", f"/files/{file.pk}/", color_scheme)
    image = page.locator(".preview-tiff [role=img]")
    expect(image).to_be_visible()
    clean(page)
    assert "128 × 96 pixels" in (image.get_attribute("aria-label") or "")
    channel = page.get_by_role("combobox", name="Channel")
    expect(channel.locator("option")).to_have_text(["DAPI", "GFP"])
    channel.select_option("GFP")
    expect(image).to_have_attribute("aria-label", re.compile("GFP"))
    # Pixels were drawn: the canvas isn't blank.
    page.wait_for_function(
        """() => {
          const c = document.querySelector('.preview-tiff canvas');
          if (!c || !c.width) return false;
          const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
          for (let i = 0; i < d.length; i += 4) if (d[i] || d[i + 1] || d[i + 2]) return true;
          return false;
        }"""
    )
    image.focus()
    image.press("+")
    assert links_issued(file) == 1
    no_errors(page)


def test_a_workbook_shows_its_sheets(
    open_as: Any, world: World, _s3_endpoint: str, _s3_bucket: str
) -> None:
    file = sample(
        world,
        _s3_endpoint,
        _s3_bucket,
        "qpcr_plate_results.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    page = open_as("admin", f"/files/{file.pk}/")
    expect(page.locator(".preview-grid .data-grid-body [role=row]").first).to_be_visible()
    clean(page)
    table = page.get_by_role("table", name=re.compile("^qpcr_plate_results.xlsx, sheet "))
    # A workbook's columns are its letters; this sheet's data starts in column B.
    expect(table.get_by_role("columnheader", name="B", exact=True)).to_be_visible()
    no_errors(page)
