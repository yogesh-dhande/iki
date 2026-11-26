# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""A plugin's export format in the browser (ADR 0230): a reader exports a transfer as the
liquid_handling example's Tecan worklist, the plugin's format offered beside Iki's own and
saying which plugin writes it, and many transfers as one worklist from the table.

The server runs the writer as a development server does, each export a child process of
its own; the file is checked byte for byte, as EVOware reads it."""

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from org_world import World
from playwright.sync_api import Page
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

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "examples" / "plugins" / "liquid_handling" / "writers" / "tecan-worklist.wasm"
WRITER = "examples/liquid_handling.tecan-worklist"

if not MODULE.is_file():  # pragma: no cover - make test-wasm builds it
    pytest.skip("make test-wasm builds the Tecan writer's module", allow_module_level=True)


def transfers(world: World, *wells: tuple[str, str]) -> list[Any]:
    from iki_server.entities import services as entities

    editor = world.users["editor"]

    def plate(label: str, wells: str, barcode: str) -> Any:
        return entities.create(
            editor,
            "examples/liquid_handling.Plate",
            world.project,
            {"label": label, "wells": wells, "barcode": barcode},
        ).entity

    source = plate("Compound source 1", "plate_384", "CP-0412")
    assay = plate("Assay plate 7", "plate_96", "AP-0007")
    return [
        entities.create(
            editor,
            "examples/liquid_handling.Transfer",
            world.project,
            {
                "source": str(source.pk),
                "source_well": at,
                "destination": str(assay.pk),
                "destination_well": to,
                "volume": 2.5,
            },
        ).entity
        for at, to in wells
    ]


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_reader_downloads_a_transfer_as_the_plugins_worklist(
    open_as: Any, world: World, color_scheme: str, tmp_path: Path
) -> None:
    from iki_server.audit.models import AuditEvent

    (moved,) = transfers(world, ("B3", "A2"))
    page: Page = open_as("viewer", f"/entities/{moved.hrid}/", color_scheme)
    page.get_by_role("button", name=f"More actions for {moved.hrid}").click()
    page.get_by_role("menuitem", name="Export…").click()
    dialog = page.get_by_role("dialog", name=re.compile("^Export "))
    worklist = dialog.get_by_role("radio", name=re.compile("^Tecan worklist"))
    worklist.wait_for()
    # Its only format, so it's chosen; and it says whose it is.
    assert worklist.is_checked()
    dialog.get_by_text("Written by the examples/liquid_handling plugin.", exact=False).wait_for()
    assert_clean(page)
    with page.expect_download() as downloading:
        dialog.get_by_role("button", name="Download").click()
    saved = tmp_path / "worklist.gwl"
    downloading.value.save_as(saved)
    assert downloading.value.suggested_filename.endswith(".gwl")
    assert saved.read_bytes() == (
        b"A;Compound source 1;CP-0412;;34;;2.5;;;;\r\nD;Assay plate 7;AP-0007;;9;;2.5;;;;\r\nW;\r\n"
    )
    page.get_by_text(re.compile(r"^Downloaded .*\.gwl\.$")).wait_for()
    event = AuditEvent.objects.get(action="entity.exported")
    assert event.data["format"] == WRITER
    assert event.data["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    assert event.data["writer"]["module_sha256"] == hashlib.sha256(MODULE.read_bytes()).hexdigest()
    assert_clean(page)


def test_a_reader_exports_the_transfers_picked_as_one_worklist(
    open_as: Any, world: World, tmp_path: Path
) -> None:
    first, second = transfers(world, ("A1", "A1"), ("P24", "H12"))
    page: Page = open_as(
        "viewer", "/projects/kinase-atlas/entities/?type=examples/liquid_handling.Transfer"
    )
    page.get_by_role("checkbox", name=f"Select {first.hrid}").check()
    page.get_by_role("checkbox", name=f"Select {second.hrid}").check()
    page.get_by_role("button", name="Export selected").click()
    dialog = page.get_by_role("dialog", name="Export 2 selected")
    worklist = dialog.get_by_role("radio", name=re.compile("^Tecan worklist"))
    worklist.wait_for()
    assert worklist.is_checked()
    assert_clean(page)
    with page.expect_download() as downloading:
        dialog.get_by_role("button", name="Export").click()
    saved = tmp_path / "worklist.gwl"
    downloading.value.save_as(saved)
    lines = saved.read_bytes().split(b"\r\n")
    # In the table's order, newest first; each A, D and W.
    assert [line.split(b";")[4] for line in lines if line.startswith(b"A;")] == [b"384", b"1"]
    assert len([line for line in lines if line == b"W;"]) == 2
    dialog.get_by_role("status").get_by_text("2 records", exact=False).wait_for()
    assert_clean(page)
