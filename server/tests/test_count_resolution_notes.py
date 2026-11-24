"""Count resolution notes distinguish omission, first annotations, and reviewed corrections."""

from typing import Any

import pytest
from api_client import Api
from browser import Response
from org_world import World
from test_count_observation_history import begin, history, review
from test_storage import errors, world

pytestmark = pytest.mark.django_db

__all__ = ["world"]


def missing(api: Api, world: World) -> tuple[str, dict[str, Any], dict[str, Any]]:
    path, _, _ = begin(api, world)
    result = api.post(f"{path}/scans", {"scans": [{"position": "A1"}]})
    assert result.status_code == 200, result.content
    lines = result.json()["lines"]
    return (
        path,
        next(line for line in lines if line["finding"] == "missing"),
        next(line for line in lines if line["finding"] == "empty"),
    )


def write(
    api: Api, path: str, line: dict[str, Any], resolution: str | None, **fields: Any
) -> Response:
    return api.patch(
        f"{path}/lines/{line['id']}",
        {"line_revision": line["line_revision"], "resolution": resolution, **fields},
    )


def updated(response: Response, line: dict[str, Any]) -> dict[str, Any]:
    assert response.status_code == 200, response.content
    lines: list[dict[str, Any]] = response.json()["lines"]
    return next(entry for entry in lines if entry["id"] == line["id"])


@pytest.mark.parametrize("reason", [{}, {"reason": None}], ids=["omitted", "null"])
def test_first_note_can_annotate_the_current_decision_without_a_reason(
    world: World, reason: dict[str, Any]
) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, line, _ = missing(editor, world)
    decision = updated(write(editor, path, line, "removed", note=""), line)
    initial = history(editor, path)
    assert decision["note"] == "" and decision["reviewed_observation"] == line["observation"]

    note = "Confirmed the empty position"
    annotated = updated(write(admin, path, decision, "removed", note=note, **reason), line)
    assert annotated["note"] == note and annotated["line_revision"] > decision["line_revision"]
    assert annotated["reviewed_observation"] == decision["reviewed_observation"]
    assert annotated["resolved_by"] == decision["resolved_by"]
    assert decision["resolved_by"]["id"] == str(world.users["editor"].pk)
    events = history(editor, path)
    assert len(events) == len(initial) + 1 and events[:-1] == initial
    entry = events[-1]
    assert entry["previous"] == initial[-1]["id"] and entry["before"] == initial[-1]["after"]
    assert entry["before"]["note"] == "" and entry["after"]["note"] == note
    assert entry["actor"]["id"] == str(world.users["admin"].pk) and entry["reason"] == ""
    assert (
        entry["after"]["resolved_by"]
        == entry["before"]["resolved_by"]
        == decision["resolved_by"]["id"]
    )
    decided_at = entry["before"]["resolved_at"]
    assert decided_at and entry["after"]["resolved_at"] == decided_at

    current = editor.get(path).json()
    repeat = write(editor, path, annotated, "removed")
    assert repeat.status_code == 200 and repeat.json() == current
    assert "#/reason" in errors(write(editor, path, annotated, "removed", note="Revised"))
    assert editor.get(path).json() == current and history(editor, path) == events
    corrected = updated(
        write(admin, path, annotated, "removed", note="Revised", reason="Corrected the wording"),
        line,
    )
    change = history(editor, path)[-1]
    assert corrected["note"] == change["after"]["note"] == "Revised"
    assert change["before"] == entry["after"] and change["reason"] == "Corrected the wording"
    assert change["actor"]["id"] == str(world.users["admin"].pk)
    assert (
        change["after"]["resolved_at"] == decided_at
        and corrected["resolved_by"] == decision["resolved_by"]
    )

    # The same note can support a different reviewed decision. Omission preserves it
    # and the new choice establishes its own resolver and timestamp.
    changed = updated(
        write(admin, path, corrected, "kept", reason="Keep the recorded placement"), line
    )
    choice = history(editor, path)[-1]
    assert changed["note"] == "Revised" and changed["resolution"] == "kept"
    assert (
        changed["resolved_by"]["id"]
        == choice["after"]["resolved_by"]
        == str(world.users["admin"].pk)
    )
    assert choice["before"] == change["after"] and choice["after"]["resolved_at"] != decided_at


@pytest.mark.parametrize("clear", [None, "", "  "], ids=["null", "empty", "whitespace"])
def test_explicit_clear_of_an_optional_saved_note_needs_revision_and_reason(
    world: World, clear: str | None
) -> None:
    api = Api(world.browser("editor"))
    path, line, _ = missing(api, world)
    note = "Saw an empty spot"
    decision = updated(write(api, path, line, "removed", note=note), line)
    unchanged, entries = api.get(path).json(), history(api, path)
    assert "#/reason" in errors(write(api, path, decision, "removed", note=clear))
    why = "Removed the note after checking the log"
    stale = write(api, path, line, "removed", note=clear, reason=why)
    assert stale.status_code == 409, stale.content
    assert stale.json()["affected_lines"] == [review(decision)]
    assert api.get(path).json() == unchanged and history(api, path) == entries

    cleared = updated(write(api, path, decision, "removed", note=clear, reason=why), line)
    assert cleared["resolution"] == "removed" and cleared["note"] == ""
    assert cleared["line_revision"] > decision["line_revision"]
    events = history(api, path)
    assert len(events) == len(entries) + 1 and events[:-1] == entries
    last = events[-1]
    assert last["before"] == entries[-1]["after"] and last["before"]["note"] == note
    assert last["after"]["note"] == "" and last["after"]["resolution"] == "removed"
    assert last["reason"] == why and last["previous"] == entries[-1]["id"]


def test_first_note_cannot_replace_the_reason_for_a_different_or_stale_decision(
    world: World,
) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, line, empty = missing(editor, world)
    decision = updated(write(editor, path, line, "removed"), line)
    before, entries = editor.get(path).json(), history(editor, path)
    note = "Rechecked the physical map"
    assert "#/reason" in errors(write(admin, path, decision, "kept", note=note))
    assert "#/reason" in errors(write(admin, path, decision, None))
    # Supplying a note while clearing cannot create a stand-alone note or erase the decision.
    assert write(admin, path, decision, None, note=note).status_code == 422
    assert editor.get(path).json() == before and history(editor, path) == entries

    new_scan = admin.post(
        f"{path}/scans", {"scans": [{"position": "A1"}], "reviewed_lines": [review(empty)]}
    )
    stale = updated(new_scan, line)
    assert stale["resolution_stale"] and stale["observation"] != stale["reviewed_observation"]
    scanned, entries = new_scan.json(), history(admin, path)
    assert "#/reason" in errors(write(editor, path, stale, "removed", note=note))
    assert admin.get(path).json() == scanned and history(admin, path) == entries
    why = "Decision now rests on the second inspection"
    reviewed = updated(write(editor, path, stale, "removed", note=note, reason=why), line)
    assert (
        not reviewed["resolution_stale"]
        and reviewed["reviewed_observation"] == stale["observation"]
    )
    last = history(editor, path)[-1]
    assert last["reason"] == why and last["before"]["note"] == "" and last["after"]["note"] == note
    assert last["before"]["observation"] != last["after"]["observation"]


def test_kept_requires_a_note_and_clearing_the_decision_removes_it_with_history(
    world: World,
) -> None:
    editor, admin = (Api(world.browser(who)) for who in ("editor", "admin"))
    path, line, _ = missing(editor, world)
    assert "#/note" in errors(write(editor, path, line, "kept"))
    note = "Shelf contents verified by hand"
    kept = updated(write(editor, path, line, "kept", note=note), line)
    current, entries = editor.get(path).json(), history(editor, path)
    omitted = write(admin, path, kept, "kept")
    assert omitted.status_code == 200 and omitted.json() == current
    why = "Reopen the finding for retally"
    assert "#/note" in errors(write(admin, path, kept, "kept", note=None, reason=why))
    assert "#/reason" in errors(write(admin, path, kept, None))
    assert editor.get(path).json() == current and history(editor, path) == entries
    cleared = updated(write(admin, path, kept, None, reason=why), line)
    assert cleared["resolution"] is None and cleared["note"] == ""
    events = history(editor, path)
    assert len(events) == len(entries) + 1 and events[:-1] == entries
    last = events[-1]
    assert last["before"] == entries[-1]["after"] and last["before"]["note"] == note
    assert last["after"] is None and last["previous"] == entries[-1]["id"]
    assert last["actor"]["id"] == str(world.users["admin"].pk) and last["reason"] == why
