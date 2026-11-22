"""A record's audit trail (ADR 0176): narrowed by person and field, who changed it, and the
whole trail as CSV for inspection, which tells nobody more than reading the record's
revisions does, and writes no cell a spreadsheet would run."""

import csv
import io
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World
from test_api_entities import api_as, create, patch
from test_registries import PROTEIN, SEQUENCE
from test_signatures import lab, lot, signed, world

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

__all__ = ["lab", "world"]  # fixtures, from test_signatures


def trail(api: Api, ref: str, **params: Any) -> list[dict[str, str]]:
    response = api.get(f"/entities/{ref}/revisions.csv", **params)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    header, *rows = list(csv.reader(io.StringIO(response.getvalue().decode())))
    return [dict(zip(header, row, strict=True)) for row in rows]


def edited(world: World) -> str:
    """A sequence the editor made, then the admin renamed: two people, three fields."""
    ref: str = create(api_as(world), world.project, {"bases": "ACGT", "name": "a"}).json()["id"]
    done = patch(
        api_as(world, "admin"),
        ref,
        {"values": {"name": "b", "topology": "circular"}, "reason": "Rename"},
        '"1"',
    )
    assert done.status_code == 200, done.content
    return ref


def test_the_trail_narrows_to_a_person_or_a_field(world: World) -> None:
    ref = edited(world)
    api = api_as(world, "viewer")
    editor, admin = (str(world.users[p].pk) for p in ("editor", "admin"))

    def numbers(**query: Any) -> list[int]:
        found = api.get(f"/entities/{ref}/revisions", **query)
        assert found.status_code == 200, found.content
        return [r["number"] for r in found.json()["items"]]

    assert numbers() == [2, 1]
    assert numbers(person=editor) == [1]
    assert numbers(person=admin) == [2]
    assert numbers(field="topology") == [2, 1]
    assert numbers(field="topology", person=admin) == [2]
    assert numbers(field="topology", person=editor) == [1]
    # Its bases are its registry entry's (ADR 0115): no revision of the record set them.
    assert numbers(field="bases") == []
    problem(api.get(f"/entities/{ref}/revisions", person="nobody"), 422, "validation_failed")


def test_who_changed_it_is_everyone_who_made_a_revision(world: World) -> None:
    ref = edited(world)
    people = api_as(world, "viewer").get(f"/entities/{ref}/contributors").json()
    assert [p["email"] for p in people] == ["admin@example.com", "editor@example.com"]
    problem(api_as(world, "outsider").get(f"/entities/{ref}/contributors"), 404, "not_found")


def test_the_trail_downloads_a_row_for_each_value_each_revision_set(world: World) -> None:
    ref = edited(world)
    rows = trail(api_as(world, "viewer"), ref)
    assert [(r["Of"], r["Revision"], r["Field"], r["Before"], r["After"]) for r in rows] == [
        ("Record", "1", "Name", "", "a"),
        ("Record", "1", "Topology", "", "linear"),
        ("Record", "2", "Name", "a", "b"),
        ("Record", "2", "Topology", "linear", "circular"),
        # A sequence's bases are its registry entry's (ADR 0115): its revisions follow.
        ("Registry entry", "1", "Bases", "", "ACGT"),
    ]
    renamed = rows[2]
    assert renamed["Who"] == "Admin" and renamed["Email"] == "admin@example.com"
    assert renamed["How"] == "Web app" and renamed["Reason"] == "Rename"
    assert renamed["What"] == "Edited" and renamed["Record"] == rows[4]["Record"]
    assert rows[4]["Who"] == "Editor" and rows[4]["What"] == "Created"
    assert renamed["Project"] == world.project.name
    # In UTC, as the record's page says it.
    assert len(renamed["When (UTC)"]) == len("2026-11-21 09:14:52")
    event = AuditEvent.objects.get(action="entity.trail_exported")
    assert event.actor == world.users["viewer"] and event.target_id == ref
    assert event.project == world.project
    assert event.data["revision"] == 2 and event.data["file"].endswith(" audit trail.csv")


def test_only_someone_who_can_read_it_downloads_its_trail(world: World) -> None:
    ref = edited(world)
    problem(api_as(world, "outsider").get(f"/entities/{ref}/revisions.csv"), 404, "not_found")
    assert not AuditEvent.objects.filter(action="entity.trail_exported").exists()


def test_no_cell_of_the_trail_runs_as_a_formula(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT", "name": "a"}).json()["id"]
    formula = '=HYPERLINK("https://example.com","x")'
    done = patch(api, ref, {"values": {"name": "@SUM(A1)"}, "reason": formula}, '"1"')
    assert done.status_code == 200, done.content
    [renamed] = [r for r in trail(api, ref) if r["Revision"] == "2"]
    assert renamed["Reason"] == "'" + formula
    assert renamed["After"] == "'@SUM(A1)"


def test_a_signed_state_says_who_signed_what_it_means_and_why(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review", reason="Ready")
    [state] = [r for r in trail(api_as(lab, "viewer"), made["id"]) if r["Revision"] == "2"]
    assert state["What"] == "Status set to In review, from Open"
    assert state["Field"] == "Status" and state["After"] == "In review"
    assert state["Signatures"].startswith("Authored by Editor on ")
    assert state["Signatures"].endswith(" UTC. Meaning: Authorship. Reason: “Ready”.")


def test_a_records_trail_has_its_entrys_revisions_as_its_reader_is_told_them(
    world: World,
) -> None:
    rival = Project.objects.create(team=world.team, name="Rival Screens")
    ProjectMembership.objects.create(
        project=rival, user=world.users["outsider"], role=ProjectRole.ADMIN
    )
    editor, outsider = api_as(world, "editor"), api_as(world, "outsider")
    mine = create(editor, world.project, {"sequence": SEQUENCE, "name": "Secret lead"}, PROTEIN)
    assert mine.status_code == 201, mine.content
    theirs = create(outsider, rival, {"sequence": SEQUENCE}, PROTEIN).json()

    # The Atlas's editor registered it: the Atlas is told so, in full.
    ours = [r for r in trail(editor, mine.json()["id"]) if r["Of"] == "Registry entry"]
    assert ours and {r["Email"] for r in ours} == {"editor@example.com"}
    # The rival is told what the entry's first revision set, never who, how or when.
    told = trail(outsider, theirs["id"])
    entry = [r for r in told if r["Of"] == "Registry entry"]
    assert entry and {r["Who"] for r in entry} == {"Made through a project you can’t read"}
    assert {r["When (UTC)"] for r in entry} == {""} and {r["How"] for r in entry} == {""}
    assert any(r["After"] == SEQUENCE for r in entry)
    download = outsider.get(f"/entities/{theirs['id']}/revisions.csv").getvalue().decode()
    assert "editor@example.com" not in download and "Secret lead" not in download
    # Nor does narrowing by person, or listing who changed it, say who made it.
    editor_id = str(world.users["editor"].pk)
    entry_id = theirs["entry"]["id"]
    narrowed = outsider.get(f"/entries/{entry_id}/revisions", person=editor_id).json()
    assert narrowed["items"] == []
    assert editor.get(f"/entries/{entry_id}/revisions", person=editor_id).json()["items"]
    people = outsider.get(f"/entities/{theirs['id']}/contributors").json()
    assert [p["email"] for p in people] == ["outsider@example.com"]
