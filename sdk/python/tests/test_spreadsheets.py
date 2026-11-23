"""Importing spreadsheets through the SDK (ADR 0178): start, map, preview, commit."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fake_iki import FakeIki, config, isolated, problem, store_with

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
        "sheet": "Samples",
        "header_row": 1,
        "preview": None,
        "run_problems": [],
        **extra,
    }


def counts(*, preview_round: int = 1, **found: int) -> dict[str, Any]:
    return {
        "round": preview_round,
        "phase": "done",
        "progress": 1.0,
        "message": "Checked.",
        "counts": found,
    }


def test_a_sheet_is_imported_once_its_read_mapped_and_checked(
    fake: FakeIki, client: Client
) -> None:
    fake.on("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("reading"))
    fake.on("PATCH", BASE, imported("mapping"))
    fake.on("POST", f"{BASE}/preview", imported("previewing", preview=counts(preview_round=4)))
    fake.on("POST", f"{BASE}/commit", imported("committing"))
    done = imported("imported", run={"hrid": "RUN-000009", "status": "succeeded"})
    fake.on(
        "GET",
        BASE,
        imported("mapping"),
        imported("previewed", preview=counts(preview_round=4, create=3)),
        done,
    )

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
    assert fake.body("PATCH", BASE) == {
        "basis": {"sheet": "Samples", "header_row": 1},
        "locale": {"decimal": "comma", "dates": "day_first"},
    }
    assert fake.body("POST", f"{BASE}/commit") == {
        "preview_round": 4,
        "skip_problems": False,
        "reason": "Batch 12",
    }


def test_a_bare_import_id_needs_the_sheet_or_the_view_the_choice_was_made_on(
    fake: FakeIki, client: Client
) -> None:
    fake.on("PATCH", BASE, imported("mapping"))
    with pytest.raises(ValueError, match=r"sheet.*basis"):
        client.spreadsheets.change(IMPORT, skip=[])
    assert not fake.sent("PATCH", BASE)
    assert not fake.sent("GET", BASE)

    client.spreadsheets.change(IMPORT, sheet="Samples", header_row=0)
    assert fake.body("PATCH", BASE) == {"sheet": "Samples", "header_row": 0}
    basis = {"sheet": "Samples", "header_row": 0}
    client.spreadsheets.change(IMPORT, basis=basis, first_row=3)
    assert fake.body("PATCH", BASE) == {"basis": basis, "first_row": 3}
    client.spreadsheets.change(IMPORT)
    assert fake.body("PATCH", BASE) == {}


def test_a_sheet_switch_and_its_settings_compare_the_source_view(
    fake: FakeIki, client: Client
) -> None:
    fake.on("PATCH", BASE, imported("mapping", sheet="Assays", header_row=2))
    source = imported("mapping", sheet="Samples", header_row=1)
    changed = client.spreadsheets.change(source, sheet="Assays", header_row=2)
    assert fake.body("PATCH", BASE) == {
        "basis": {"sheet": "Samples", "header_row": 1},
        "sheet": "Assays",
        "header_row": 2,
    }
    client.spreadsheets.change(changed, last_row=0)
    assert fake.body("PATCH", BASE) == {
        "basis": {"sheet": "Assays", "header_row": 2},
        "last_row": 0,
    }


def test_a_stale_view_is_not_rebound_to_a_fresh_sheet(fake: FakeIki, client: Client) -> None:
    fake.on("PATCH", BASE, problem(409, "The sheet or header row changed.", "conflict"))
    stale = imported("mapping", sheet="Original", header_row=0)
    with pytest.raises(iki.Conflict, match="sheet or header"):
        client.spreadsheets.change(stale, columns=[])
    assert fake.body("PATCH", BASE) == {
        "basis": {"sheet": "Original", "header_row": 0},
        "columns": [],
    }
    assert len(fake.sent("PATCH", BASE)) == 1
    assert not fake.sent("GET", BASE)


def test_one_shot_mapping_uses_the_view_returned_after_reading(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.csv"
    path.touch()
    file = {"id": "f1", "name": path.name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    fake.on("POST", "/api/v1/uploads", {"id": "u1", "state": "ready", "file": file, "parts": []})
    fake.on(
        "POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("reading", sheet="")
    )
    fake.on("GET", BASE, imported("mapping", sheet="Data", header_row=0))
    fake.on("PATCH", BASE, imported("mapping", sheet="Data", header_row=0))
    fake.on(
        "POST", f"{BASE}/preview", imported("previewed", preview=counts(preview_round=7, create=1))
    )
    fake.on("POST", f"{BASE}/commit", imported("imported"))
    assert (
        client.spreadsheets.import_sheet(
            path, type="acme/protein.Sample", project=PROJECT, columns=[]
        )["state"]
        == "imported"
    )
    assert fake.body("PATCH", BASE) == {"basis": {"sheet": "Data", "header_row": 0}, "columns": []}
    assert len(fake.sent("GET", BASE)) == 1
    assert fake.body("POST", f"{BASE}/commit")["preview_round"] == 7


def test_a_one_shot_import_cannot_approve_a_round_started_by_another_reviewer(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.csv"
    path.touch()
    file = {"id": "f1", "name": path.name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    fake.on("POST", "/api/v1/uploads", {"id": "u1", "state": "ready", "file": file, "parts": []})
    fake.on("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("mapping"))
    fake.on("POST", f"{BASE}/preview", imported("previewing", preview=counts(preview_round=8)))
    fake.on("GET", BASE, imported("previewed", preview=counts(preview_round=9, create=1)))
    fake.on("POST", f"{BASE}/commit", imported("imported"))
    with pytest.raises(iki.Conflict, match="preview"):
        client.spreadsheets.import_sheet(
            path, type="acme/protein.Sample", project=PROJECT, timeout=30
        )
    assert len(fake.sent("POST", f"{BASE}/preview")) == 1
    assert len(fake.sent("GET", BASE)) == 1
    assert not fake.sent("POST", f"{BASE}/commit")


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
        client.spreadsheets.commit(IMPORT, preview_round=3, skip_problems=True)
    assert raised.value.imported["state"] == "failed"
    assert fake.body("POST", f"{BASE}/commit") == {
        "preview_round": 3,
        "skip_problems": True,
        "reason": "",
    }


@pytest.mark.parametrize(
    "missing", [IMPORT, imported("mapping"), imported("previewing", preview=counts())]
)
def test_approving_requires_a_reviewed_preview_without_fetching_one(
    fake: FakeIki, client: Client, missing: Any
) -> None:
    with pytest.raises(ValueError, match="preview"):
        client.spreadsheets.commit(missing)
    assert not fake.sent("GET", BASE)
    assert not fake.sent("POST", f"{BASE}/commit")


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "2"])
def test_approving_by_id_requires_a_positive_integer(
    fake: FakeIki, client: Client, invalid: Any
) -> None:
    with pytest.raises(ValueError, match="positive"):
        client.spreadsheets.commit(IMPORT, preview_round=invalid)
    assert not fake.requests


def test_a_conflict_never_approves_another_preview(fake: FakeIki, client: Client) -> None:
    reviewed = imported("previewed", preview=counts(preview_round=2, create=4))
    with pytest.raises(ValueError, match="round"):
        client.spreadsheets.commit(reviewed, preview_round=3)
    assert not fake.requests

    fake.on("POST", f"{BASE}/commit", problem(409, "Review the current preview.", "conflict"))
    with pytest.raises(iki.Conflict, match="Review the current preview"):
        client.spreadsheets.commit(reviewed, wait=False, skip_problems=True)
    assert fake.body("POST", f"{BASE}/commit") == {
        "preview_round": 2,
        "skip_problems": True,
        "reason": "",
    }
    assert len(fake.sent("POST", f"{BASE}/commit")) == 1
    assert not fake.sent("GET", BASE)


def test_a_failed_preview_keeps_the_worker_diagnostic(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/preview", imported("previewing", preview=counts(preview_round=3)))
    fake.on("GET", BASE, imported("mapping", error="The workbook could not be read."))
    with pytest.raises(iki.ImportFailed, match="The workbook could not be read"):
        client.spreadsheets.preview(IMPORT)
    assert not fake.sent("POST", f"{BASE}/commit")


def test_waiting_longer_than_allowed_raises(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/preview", imported("previewing", preview=counts()))
    fake.on("GET", BASE, imported("previewing", preview=counts()))
    with pytest.raises(iki.TimedOut, match="still previewing"):
        client.spreadsheets.preview(IMPORT, timeout=0)
