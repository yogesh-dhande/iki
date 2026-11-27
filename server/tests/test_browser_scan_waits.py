# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A run waiting for a malware scan, in real browsers (ADR 0234). A purity measurement of a
sample whose lot's certificate of analysis predates scanning says it waits for that file,
links to it, and starts once the scan finds it clean. Its attempt then says how long it
waited. The page passes axe in both themes and fits a phone's width."""

import hashlib
import re
from typing import Any
from unittest.mock import patch

import pytest
from org_world import World
from playwright.sync_api import expect
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
from iki_server.files import scan_state
from iki_server.files.models import Blob, File
from iki_server.runs import catalog
from iki_server.runs import services as runs
from iki_server.runs.models import Job
from iki_server.tasks.runner import Runner

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
CERTIFICATE = "CoA lot KA-24-117 (SEC-HPLC).pdf"


def waiting_run(world: World) -> tuple[Any, File]:
    """Measures the purity of a sample of lot KA-24-117, whose certificate of analysis was
    added before the deployment scanned files: its scan hasn't run yet."""
    editor = world.users["editor"]
    data = b"%PDF-1.7 certificate of analysis, lot KA-24-117, SEC-HPLC main peak 98.6%"
    blob = Blob.objects.create(
        sha256=hashlib.sha256(data).hexdigest(), size=len(data), storage_uri="s3://iki/coa-117"
    )
    certificate = File.objects.create(
        blob=blob,
        name=CERTIFICATE,
        media_type="application/pdf",
        project=world.project,
        created_by=editor,
    )
    with patch.object(scan_state.ScanStatus, "require_usable"):
        lot = entities.create(
            editor,
            "acme/protein.Lot",
            world.project,
            {"label": "KA-24-117", "report": certificate.pk},
        ).entity
    sample = entities.create(
        editor, "acme/protein.Sample", world.project, {"label": "KA-24-117-S3", "lot": lot.pk}
    ).entity
    action = catalog.action("acme/protein.measure_purity")
    assert action is not None
    run = runs.launch(
        editor, action, world.project, {"sample": [sample.pk]}, {}, reason="Release testing"
    )
    return run, certificate


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_run_waiting_for_a_scan_says_so_and_starts_once_its_clean(
    open_as: Any, world: World, color_scheme: str
) -> None:
    run, certificate = waiting_run(world)
    assert Job.objects.get(run=run).waiting_for_scan
    page = open_as("editor", f"/runs/{run.hrid}/", color_scheme)
    page.get_by_text("Waiting for a malware scan").wait_for()
    page.get_by_text(
        re.compile("^Waiting for a file it reads to be scanned for malware, since")
    ).wait_for()
    assert_clean(page)
    note = page.get_by_role("status").filter(has_text="Waiting for a malware scan")
    expect(note).to_contain_text("No worker takes the run until it’s found clean")
    expect(note.get_by_role("link", name=CERTIFICATE)).to_have_attribute(
        "href", f"/files/{certificate.pk}/"
    )
    for width, height in [(390, 844), (1440, 900)]:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate(NO_SIDEWAYS_SCROLL)

    scan_state.record_result(
        certificate.blob, state="clean", scanner="ClamAV 1.4.3", signature_version="27453"
    )
    Runner(queues=["default"]).run_ready()
    # The page hears that the run changed: the note goes, and its attempt keeps the wait.
    expect(page.get_by_text("Waiting for a malware scan")).to_have_count(0)
    page.get_by_text(
        re.compile(r"^Waited .+ for a file it reads to be scanned for malware, until")
    ).wait_for()
    assert not Job.objects.get(run=run).waiting_for_scan
    assert_clean(page)
