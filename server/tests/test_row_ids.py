"""A list's rows with stable IDs (`row_id`): any type's, not only an experiment's
observations. The server gives a new row an ID, keeps each kept row's, and refuses an
edit that names a row the record doesn't have, or one twice."""

from __future__ import annotations

from typing import Any

import pytest

from iki_server.entities.services import _row_ids
from iki_server.entities.values import RowProblems
from iki_spec.model import Field

#: A plate reader's readings of a well, a row each, as a lab's own type might keep them.
READINGS = Field.from_manifest(
    "readings",
    {
        "type": "list",
        "items": {
            "type": "row",
            "row_id": "reading_id",
            "fields": {
                "read_at": {"type": "datetime", "required": True},
                "od600": {"type": "float", "required": True},
                "reading_id": {"type": "string", "max_length": 36},
            },
        },
    },
    plugin="acme/plates",
)


def reading(at: str, od: float, row_id: str | None = None) -> dict[str, Any]:
    return {"read_at": at, "od600": od, "reading_id": row_id}


def test_a_new_row_gets_an_id_and_a_kept_row_keeps_its_own() -> None:
    (first,) = _row_ids(READINGS, [reading("2026-11-27T09:00:00Z", 0.12)])
    assert len(first["reading_id"]) == 36
    kept, added = _row_ids(
        READINGS,
        [{**first, "od600": 0.13}, reading("2026-11-27T10:00:00Z", 0.25)],
        [first],
    )
    assert kept["reading_id"] == first["reading_id"]
    assert added["reading_id"] not in (None, first["reading_id"])


def test_an_unchanged_list_sent_without_ids_keeps_them() -> None:
    current = _row_ids(
        READINGS, [reading("2026-11-27T09:00:00Z", 0.12), reading("2026-11-27T10:00:00Z", 0.25)]
    )
    again = _row_ids(
        READINGS,
        [reading("2026-11-27T09:00:00Z", 0.12), reading("2026-11-27T10:00:00Z", 0.25)],
        current,
    )
    assert again == current


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [reading("2026-11-27T09:00:00Z", 0.12, "0193a0c4-0000-7000-8000-000000000001")],
            "isn't one of this record's rows.",
        ),
    ],
)
def test_an_edit_naming_a_row_the_record_hasnt_is_refused(
    rows: list[dict[str, Any]], message: str
) -> None:
    current = _row_ids(READINGS, [reading("2026-11-27T09:00:00Z", 0.12)])
    with pytest.raises(RowProblems) as raised:
        _row_ids(READINGS, rows, current)
    assert raised.value.errors == {"0/reading_id": [message]}


def test_an_edit_naming_a_row_twice_is_refused() -> None:
    (first,) = _row_ids(READINGS, [reading("2026-11-27T09:00:00Z", 0.12)])
    with pytest.raises(RowProblems) as raised:
        _row_ids(READINGS, [first, dict(first)], [first])
    assert raised.value.errors == {"1/reading_id": ["names the same row twice."]}


def test_two_identical_rows_sent_without_ids_cant_be_told_apart() -> None:
    same = reading("2026-11-27T09:00:00Z", 0.12)
    current = _row_ids(READINGS, [dict(same), dict(same)])
    with pytest.raises(RowProblems) as raised:
        _row_ids(READINGS, [dict(same)], current)
    assert raised.value.errors == {"0/reading_id": ["is required to identify this duplicate."]}


def test_rows_without_a_row_id_are_left_as_they_are() -> None:
    plain = Field.from_manifest(
        "parts",
        {"type": "list", "items": {"type": "row", "fields": {"name": {"type": "string"}}}},
        plugin="acme/plates",
    )
    rows = [{"name": "A1"}]
    assert _row_ids(plain, rows) is rows
