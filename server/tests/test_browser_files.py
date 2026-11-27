# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A project's Files (ADR 0177) in real browsers: found by part of a name, format and when
their data was acquired, and by the instrument and operator a file states (ADR 0224); a
file's page with what it states and its read groups; Used by links to the records and
revisions that held a file; uploading files with when their data was acquired; the file
picker in an entity form. Each passes axe in both themes and fits a phone's width."""

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page, expect
from test_browser import (  # noqa: F401 - fixtures
    _orm_beside_playwright,
    _stored_file,
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
from test_project_files import add

from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.files import scan_state
from iki_server.files.models import File

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

ABIF = Path(__file__).parent / "data" / "abif"
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


def test_a_late_record_response_cannot_start_a_watch_when_leaving_for_a_file(
    open_as: Any, world: World
) -> None:
    file = add(world.project, "record.pdb", by=world.users["admin"])
    record = held(world, file)
    record_path = f"/entities/{record.pk}/"
    file_path = f"/files/{file.pk}/"
    api = f"/api/v1/entities/{record.pk}"
    # A separate same-origin page can observe the record page even as it leaves.
    # Chromium can discard an outgoing document's console messages during navigation.
    observer: Page = open_as("admin", file_path)
    observer.evaluate(
        """() => {
          const channel = new BroadcastChannel("watch-test");
          channel.onmessage = ({data: {stage, event}}) => {
            const root = document.documentElement;
            const key = "data-" + stage;
            root.setAttribute(key, [root.getAttribute(key), event].filter(Boolean).join(" "));
          };
          window.watchTestChannel = channel;
        }"""
    )
    trace = observer.locator("html")
    page = watch(observer.context.new_page())

    # The route's real loader awaits the record before it asks whether the user watches it.
    # Release the parsed record at pagehide. WebKit can refuse a fetch in the old document
    # before sending a request, so count attempts at native fetch as well as page errors.
    page.add_init_script(
        r"""
        const ref = location.pathname.match(/^\/entities\/([^/]+)\/$/)?.[1];
        const stage = new URLSearchParams(location.search).get("watchTest");
        if (ref && stage) {
          const channel = new BroadcastChannel("watch-test");
          const record = event => channel.postMessage({stage, event});
          const api = `/api/v1/entities/${ref}`;
          const send = window.fetch;
          window.fetch = function(input, init) {
            const url = input instanceof Request ? input.url : new URL(input, location.href).href;
            if (new URL(url).pathname === api + "/watch") {
              record("watch-attempt");
              const signal = init?.signal ?? input.signal;
              signal?.addEventListener("abort", () => {
                record("watch-aborted");
              });
            }
            return send.call(this, input, init);
          };
          // The SDK parses JSON directly or from text, depending on the response headers.
          for (const method of ["json", "text"]) {
            const parse = Response.prototype[method];
            Response.prototype[method] = async function(...args) {
              const result = await parse.apply(this, args);
              if (this.url && new URL(this.url).pathname === api) {
                document.documentElement.dataset.recordReady = "yes";
                await new Promise(resolve => {
                  function release() {
                    window.removeEventListener("pagehide", release);
                    window.removeEventListener("test-release-record", release);
                    record("record-released");
                    resolve();
                  }
                  window.addEventListener("pagehide", release, {once: true});
                  window.addEventListener("test-release-record", release, {once: true});
                });
              }
              return result;
            };
          }
        }
        """
    )
    page.goto(f"{record_path}?watchTest=departure")
    expect(page.locator("html")).to_have_attribute("data-record-ready", "yes")
    page.goto(file_path)
    expect(page.get_by_role("heading", level=1, name=file.name)).to_be_visible()
    expect(trace).to_have_attribute("data-departure", "record-released")
    assert_clean(page)

    # A live GET is allowed to start. Leaving while the server has yet to answer it
    # must abort its signal. Network cancellation alone cannot establish who stopped it.
    page.goto(f"{record_path}?watchTest=inflight")
    expect(page.locator("html")).to_have_attribute("data-record-ready", "yes")
    watch_url = f"**{api}/watch"
    page.route(watch_url, lambda route: None)
    with page.expect_request(watch_url):
        page.evaluate("window.dispatchEvent(new Event('test-release-record'))")
    page.goto(file_path)
    expect(page.get_by_role("heading", level=1, name=file.name)).to_be_visible()
    expect(trace).to_have_attribute("data-inflight", "record-released watch-attempt watch-aborted")
    page.unroute(watch_url)

    # Automation does not guarantee that going Back restores a cached document. Model
    # that lifecycle on this live page and let its original loader finish on restoration.
    page.goto(f"{record_path}?watchTest=restored")
    expect(page.locator("html")).to_have_attribute("data-record-ready", "yes")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
    expect(trace).to_have_attribute("data-restored", "record-released")
    with page.expect_response(lambda response: response.url.endswith(f"{api}/watch")) as recovered:
        page.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))"
        )
    assert recovered.value.status == 200
    expect(page.get_by_role("heading", level=1, name=record.hrid)).to_be_visible()
    expect(trace).to_have_attribute("data-restored", "record-released watch-attempt")

    # The same continuation on a live page must reach the browser. A genuine connect-src
    # refusal is still reported by the browser's CSP listener.
    page.goto(f"{record_path}?watchTest=live")
    expect(page.locator("html")).to_have_attribute("data-record-ready", "yes")
    page.evaluate(
        """path => {
          const watch = new URL(path, location.href).href;
          document.addEventListener("securitypolicyviolation", event => {
            if (event.blockedURI === watch) {
              document.documentElement.dataset.watchViolation = event.effectiveDirective;
            }
          });
          const policy = document.createElement("meta");
          policy.httpEquiv = "Content-Security-Policy";
          policy.content = "connect-src 'none'";
          document.head.append(policy);
          window.dispatchEvent(new Event("test-release-record"));
        }""",
        f"{api}/watch",
    )
    expect(page.locator("html")).to_have_attribute("data-watch-violation", "connect-src")
    expect(trace).to_have_attribute("data-live", "record-released watch-attempt")
    expect(trace).to_have_attribute("data-departure", "record-released")
    assert any(f"{api}/watch" in violation for violation in page.evaluate("window.__cspViolations"))
    page.context.close()


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
def test_finding_files_by_the_instrument_and_operator_they_state(
    open_as: Any, world: World, color_scheme: str
) -> None:
    admin = world.users["admin"]
    a_plate(world)
    tube = add(
        world.project,
        "Tube_003.fcs",
        "application/vnd.isac.fcs",
        size=1_204_224,
        acquired="2026-03-14T10:02:11",
        source="fcs",
        by=admin,
        facts={
            "instrument": {"value": "LSRFortessa X-20", "stated_in": "FCS keyword $CYT"},
            "serial": {"value": "R658222R1012", "stated_in": "FCS keyword $CYTSN"},
            "operator": {"value": "Priya Raman", "stated_in": "FCS keyword $OP"},
        },
    )
    bam = add(
        world.project,
        "NA12878.markdup.bam",
        "application/x-bam",
        size=7_340_032,
        by=admin,
        read_groups=[
            {
                "ID": f"HV7KCDSX5.{lane}",
                "SM": "NA12878",
                "LB": "lib-WGS-0412",
                "PL": "ILLUMINA",
                "PU": f"HV7KCDSX5.{lane}.ACGTACGT",
                "CN": "Genomics core",
                "DT": "2026-03-15T10:00:00",
            }
            for lane in (1, 2, 3, 4)
        ],
    )
    page = open_as("admin", "/projects/kinase-atlas/files/", color_scheme)
    table = page.get_by_role("table", name="Files in Kinase Atlas")
    row = table.get_by_role("row", name=re.compile(r"^Tube_003\.fcs"))
    expect(row).to_contain_text("LSRFortessa X-20 · R658222R1012 · Priya Raman")
    assert_clean(page)

    # By who ran it, in any case; and a read group's sample.
    page.get_by_role("searchbox", name="Search").fill("priya")
    page.wait_for_url(re.compile(r"q=priya"))
    expect(table.locator("tbody tr")).to_have_count(1)
    expect(table.locator("tbody tr")).to_contain_text("Tube_003.fcs")
    page.get_by_role("searchbox", name="Search").fill("lib-wgs")
    page.wait_for_url(re.compile(r"q=lib-wgs"))
    expect(table.locator("tbody tr")).to_have_count(1)
    expect(table.locator("tbody tr")).to_contain_text("NA12878.markdup.bam")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    page.set_viewport_size({"width": 1440, "height": 900})

    # Its page says what it states, and where in the file.
    page.goto(f"/files/{tube.pk}/")
    page.get_by_role("heading", level=1, name="Tube_003.fcs").wait_for()
    facts = page.locator(".file-facts")
    expect(facts).to_contain_text("LSRFortessa X-20 from FCS keyword $CYT")
    expect(facts).to_contain_text("R658222R1012 from FCS keyword $CYTSN")
    expect(facts).to_contain_text("Priya Raman from FCS keyword $OP")
    expect(page.get_by_role("heading", name=re.compile("^Read groups"))).to_have_count(0)
    assert_clean(page)

    # An alignment's read groups, as its header gives them.
    page.goto(f"/files/{bam.pk}/")
    groups = page.get_by_role("table", name="Read groups in NA12878.markdup.bam")
    expect(groups.locator("tbody tr")).to_have_count(4)
    expect(groups.locator("thead")).to_contain_text("Sequencing centre")
    expect(groups.locator("tbody tr").first).to_contain_text("HV7KCDSX5.1.ACGTACGT")
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    assert_clean(page)


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_used_by_links_to_the_records_and_revisions_that_held_a_file(
    open_as: Any, world: World, color_scheme: str, _s3_endpoint: str, _s3_bucket: str
) -> None:
    files = a_plate(world)
    # A trace as an upload leaves it, its bytes in the store: its page reads them to draw it.
    trace = _stored_file(world, _s3_endpoint, _s3_bucket, ABIF / "3730.ab1", "application/x-abi")
    trace.acquired_on = dt.date(2009, 12, 12)
    trace.acquired_at = "2009-12-12T09:56:53"
    trace.acquired_from = "abif"
    trace.save()
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
    page.get_by_role("heading", level=1, name=record.hrid).wait_for()

    # A trace whose format records it says when, and how that's known.
    page.goto(f"/files/{trace.pk}/")
    page.get_by_role("heading", level=1, name="3730.ab1").wait_for()
    expect(page.locator(".file-facts")).to_contain_text(
        "2009-12-12 09:56:53 from the ABIF trace’s run start (RUND and RUNT)"
    )
    page.get_by_text("No record you can read holds it.").wait_for()
    expect(page.get_by_text("Q20 or better")).to_be_visible()
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
def test_uploading_files_to_a_project_saying_when_their_data_was_acquired(
    open_as: Any, world: World, color_scheme: str, tasks: Any
) -> None:
    a_plate(world)
    page = open_as("admin", "/projects/kinase-atlas/files/", color_scheme)
    page.get_by_role("table", name="Files in Kinase Atlas").get_by_role(
        "link", name="1abc.pdb"
    ).wait_for()
    # A plate reader's export says nothing of when it was read: whoever adds it says.
    page.locator("input[type=file][aria-label='Upload files']").set_input_files(
        {"name": "plate-7.csv", "mimeType": "text/csv", "buffer": b""}
    )
    dialog = page.get_by_role("dialog", name="Upload a file to Kinase Atlas")
    dialog.get_by_text("plate-7.csv").wait_for()
    assert_clean(page)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate(NO_SIDEWAYS_SCROLL)
    page.set_viewport_size({"width": 1440, "height": 900})
    dialog.locator("input[type=date]").fill("2026-03-14")
    dialog.get_by_role("button", name="Upload it").click()
    page.get_by_text("Added plate-7.csv.").wait_for()
    expect(dialog).to_be_hidden()
    row = page.get_by_role("table", name="Files in Kinase Atlas").get_by_role(
        "row", name=re.compile(r"^plate-7\.csv")
    )
    expect(row).to_contain_text("2026-03-14")
    stored = File.objects.get(name="plate-7.csv")
    assert (stored.acquired_at, stored.acquired_from) == ("2026-03-14", "given")
    # Added once the task runner had scanned it, even with no bytes to send.
    assert scan_state.status_for(stored.blob).state == "clean"
    assert_clean(page)


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
