"""Importing spreadsheets through the SDK (ADR 0178): start, map, preview, commit."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fake_iki import FakeIki, config, isolated, store_with

import iki
from iki.client import Client

__all__ = ["config", "isolated"]  # the fixtures, used here too

PROJECT = "00000000-0000-7000-8000-0000000000a1"
IMPORT = "00000000-0000-7000-8000-0000000000b1"
BASE = f"/api/v1/spreadsheet-imports/{IMPORT}"


@pytest.fixture
def fake() -> FakeIki:
    return FakeIki()


@pytest.fixture
def client(fake: FakeIki, config: Path) -> Client:
    return fake.client(store_with(config))


def imported(state: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": IMPORT,
        "state": state,
        "error": "",
        "file": {"id": "f1", "name": "manifest.xlsx"},
        "preview": None,
        "run_problems": [],
        **extra,
    }


def counts(**found: int) -> dict[str, Any]:
    return {"phase": "done", "progress": 1.0, "message": "Checked.", "counts": found}


def test_a_sheet_is_imported_once_its_read_mapped_and_checked(
    fake: FakeIki, client: Client
) -> None:
    fake.on("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("reading"))
    fake.on("PATCH", BASE, imported("mapping"))
    fake.on("POST", f"{BASE}/preview", imported("previewing"))
    fake.on("POST", f"{BASE}/commit", imported("committing"))
    done = imported("imported", run={"hrid": "RUN-000009", "status": "succeeded"})
    fake.on("GET", BASE, imported("mapping"), imported("previewed", preview=counts(create=3)), done)

    found = client.spreadsheets.start(
        file="f1", type="acme/protein.Sample", project=PROJECT, timeout=60
    )
    assert found["state"] == "mapping"
    client.spreadsheets.change(found, locale={"decimal": "comma", "dates": "day_first"})
    checked = client.spreadsheets.preview(found)
    assert checked["preview"] is not None and checked["preview"]["counts"]["create"] == 3
    ended = client.spreadsheets.commit(checked, reason="Batch 12")
    assert ended["run"] is not None and ended["run"]["hrid"] == "RUN-000009"
    assert fake.body("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports") == {
        "file": "f1",
        "type": "acme/protein.Sample",
    }
    assert fake.body("PATCH", BASE) == {"locale": {"decimal": "comma", "dates": "day_first"}}
    assert fake.body("POST", f"{BASE}/commit") == {"skip_problems": False, "reason": "Batch 12"}


def test_rows_with_problems_stop_an_import_and_say_where(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.csv"
    path.touch()
    record = {"id": "f1", "name": path.name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    fake.on("POST", "/api/v1/uploads", {"id": "u1", "state": "ready", "file": record, "parts": []})
    fake.on("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("mapping"))
    fake.on("POST", f"{BASE}/preview", imported("previewed", preview=counts(create=1, problem=1)))
    problem = {"row": 3, "column": 1, "field": "protein", "detail": "“ABL2” is no protein."}
    fake.on(
        "GET",
        f"{BASE}/rows",
        {"items": [{"row": 3, "outcome": "problem", "problems": [problem]}], "next_cursor": None},
    )
    with pytest.raises(iki.ImportFailed, match="row 3: “ABL2” is no protein") as raised:
        client.spreadsheets.import_sheet(path, type="acme/protein.Sample", project=PROJECT)
    assert raised.value.problems == [problem]
    assert fake.sent("GET", f"{BASE}/rows")[0].url.params["outcome"] == "problem"
    assert not fake.sent("POST", f"{BASE}/commit")


def test_a_run_that_didnt_write_it_raises_with_its_rows(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/commit", imported("committing"))
    failed = imported(
        "failed",
        run_problems=[{"row": 3, "column": 0, "field": "label", "detail": "“S-2” is taken."}],
    )
    fake.on("GET", BASE, failed)
    with pytest.raises(iki.ImportFailed, match="row 3") as raised:
        client.spreadsheets.commit(IMPORT, skip_problems=True)
    assert raised.value.imported["state"] == "failed"
    assert fake.body("POST", f"{BASE}/commit")["skip_problems"] is True


def test_waiting_longer_than_allowed_raises(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/preview", imported("previewing"))
    fake.on("GET", BASE, imported("previewing"))
    with pytest.raises(iki.TimedOut, match="still previewing"):
        client.spreadsheets.preview(IMPORT, timeout=0)
