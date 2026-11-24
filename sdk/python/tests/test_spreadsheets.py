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
        "revision": 2,
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
    fake.on("PATCH", BASE, imported("mapping", revision=3))
    fake.on("POST", f"{BASE}/preview", imported("previewing", revision=4))
    fake.on("POST", f"{BASE}/commit", imported("committing", revision=4))
    done = imported("imported", revision=4, run={"hrid": "RUN-000009", "status": "succeeded"})
    fake.on(
        "GET",
        BASE,
        imported("mapping"),
        imported("previewed", revision=4, preview=counts(create=3)),
        done,
    )

    found = client.spreadsheets.start(
        file="f1", type="acme/protein.Sample", project=PROJECT, timeout=60
    )
    assert found["state"] == "mapping"
    changed = client.spreadsheets.change(found, locale={"decimal": "comma", "dates": "day_first"})
    checked = client.spreadsheets.preview(changed)
    assert checked["preview"] is not None and checked["preview"]["counts"]["create"] == 3
    ended = client.spreadsheets.commit(checked, reason="Batch 12")
    assert ended["run"] is not None and ended["run"]["hrid"] == "RUN-000009"
    assert fake.body("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports") == {
        "file": "f1",
        "type": "acme/protein.Sample",
    }
    assert fake.body("PATCH", BASE) == {"locale": {"decimal": "comma", "dates": "day_first"}}
    assert fake.body("POST", f"{BASE}/commit") == {"skip_problems": False, "reason": "Batch 12"}
    # Each step is made from the import as the one before it returned it.
    assert fake.sent("PATCH", BASE)[0].headers["If-Match"] == '"2"'
    assert fake.sent("POST", f"{BASE}/preview")[0].headers["If-Match"] == '"3"'
    assert fake.sent("POST", f"{BASE}/commit")[0].headers["If-Match"] == '"4"'


def test_a_step_from_a_revision_it_has_moved_on_from_raises(fake: FakeIki, client: Client) -> None:
    fake.on("PATCH", BASE, problem(412, "It changed since you read it.", "stale"))
    fake.on("POST", f"{BASE}/mappings", {"id": "m1", "name": "Core"})
    with pytest.raises(iki.Conflict):
        client.spreadsheets.change(imported("mapping"), header_row=3)
    # An import named by its ID alone needs the revision it was read at.
    with pytest.raises(iki.IkiError, match="its revision"):
        client.spreadsheets.preview(IMPORT)
    client.spreadsheets.save_mapping(IMPORT, "Core", revision=5)
    assert fake.sent("POST", f"{BASE}/mappings")[0].headers["If-Match"] == '"5"'


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


def test_a_one_shot_import_never_approves_a_newer_preview(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    path = tmp_path / "batch.csv"
    path.touch()
    file = {"id": "f1", "name": path.name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    fake.on("POST", "/api/v1/uploads", {"id": "u1", "state": "ready", "file": file, "parts": []})
    fake.on("POST", f"/api/v1/projects/{PROJECT}/spreadsheet-imports", imported("mapping"))
    fake.on("POST", f"{BASE}/preview", imported("previewing", revision=3))
    fake.on("GET", BASE, imported("previewed", revision=4, preview=counts(create=1)))
    fake.on("POST", f"{BASE}/commit", imported("imported", revision=4))
    with pytest.raises(iki.Conflict):
        client.spreadsheets.import_sheet(path, type="acme/protein.Sample", project=PROJECT)
    assert not fake.sent("POST", f"{BASE}/commit")
    assert len(fake.sent("POST", f"{BASE}/preview")) == len(fake.sent("GET", BASE)) == 1


def test_preview_rows_use_the_reviewed_revision_on_every_page(
    fake: FakeIki, client: Client
) -> None:
    endpoint = f"{BASE}/rows"
    fake.on(
        "GET",
        endpoint,
        {"items": [{"row": 2}], "next_cursor": "page-two"},
        problem(412, "Review the updated preview", "stale"),
        {"items": [], "next_cursor": None},
    )
    rows = iter(client.spreadsheets.rows(imported("previewed", revision=7)))
    assert next(rows)["row"] == 2
    with pytest.raises(iki.Conflict):
        next(rows)
    first, second = fake.sent("GET", endpoint)
    assert second.url.params["cursor"] == "page-two"
    assert first.headers["If-Match"] == second.headers["If-Match"] == '"7"'
    assert list(client.spreadsheets.rows(IMPORT)) == []
    assert "If-Match" not in fake.sent("GET", endpoint)[-1].headers


def test_saving_a_mapping_sends_both_independently_reviewed_versions(
    fake: FakeIki, client: Client
) -> None:
    endpoint = f"{BASE}/mappings"
    target = {"id": "m1", "revision": 4}
    source = imported("mapping", revision=8)
    fake.on(
        "POST",
        endpoint,
        problem(409, "The target changed", "stale_template"),
        {**target, "revision": 6},
    )
    with pytest.raises(iki.Conflict):
        client.spreadsheets.save_mapping(source, "Keep", replace=target)
    (sent,) = fake.sent("POST", endpoint)
    assert sent.headers["If-Match"] == '"8"'
    assert fake.body("POST", endpoint) == {"name": "Keep", "replace": "m1", "target_revision": 4}
    assert not fake.sent("GET", BASE)
    with pytest.raises(iki.IkiError):
        client.spreadsheets.save_mapping(source, "Unchecked", replace="m1")
    assert len(fake.sent("POST", endpoint)) == 1
    result = client.spreadsheets.save_mapping(
        IMPORT, "Fresh", revision=8, replace="m1", target_revision=5
    )
    assert result["revision"] == 6
    assert fake.body("POST", endpoint)["target_revision"] == 5


def test_a_run_that_didnt_write_it_raises_with_its_rows(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/commit", imported("committing"))
    failed = imported(
        "failed",
        run_problems=[{"row": 3, "column": 0, "field": "label", "detail": "“S-2” is taken."}],
    )
    fake.on("GET", BASE, failed)
    with pytest.raises(iki.ImportFailed, match="row 3") as raised:
        client.spreadsheets.commit(IMPORT, revision=2, skip_problems=True)
    assert raised.value.imported["state"] == "failed"
    assert fake.body("POST", f"{BASE}/commit")["skip_problems"] is True


def test_waiting_longer_than_allowed_raises(fake: FakeIki, client: Client) -> None:
    fake.on("POST", f"{BASE}/preview", imported("previewing"))
    fake.on("GET", BASE, imported("previewing"))
    with pytest.raises(iki.TimedOut, match="still previewing"):
        client.spreadsheets.preview(IMPORT, revision=2, timeout=0)
