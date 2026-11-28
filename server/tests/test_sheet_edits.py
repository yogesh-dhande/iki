"""Editing a type's records as a sheet (ADR 0228): a kinase lab's samples typed, pasted and
filled down in the records table, and rows of tubes added with where they're kept and how
much each holds. Each cell is read as an import reads it and checked by making the change
and undoing it; saving makes what was reviewed, every change or none."""

from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token
from test_states import reviewed

from iki_server.amounts.models import Amount, Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.entities.models import Entity, Revision
from iki_server.files import scan_state
from iki_server.files.models import Blob, File
from iki_server.storage.models import Place

pytestmark = pytest.mark.django_db

SAMPLE = "acme/protein.Sample"
PROTEIN = "acme/protein.Protein"
TUBE = "acme/protein.Tube"
BOX = "acme/protein.Box"

# The kinase domains of human ABL1 (UniProt P00519, in part) and EGFR (P00533, in part).
ABL1 = "GPSGVSPNYDKWEMERTDITMKHKLGGGQYGEVYEGVWKKYSLTVAVKTLKEDTMEVEEFLKEAAVMKEIKH"
EGFR = "GSGEAPNQALLRILKETEFKKIKVLGSGAFGTVYKGLWIPEGEKVKIPVAIKELREATSPKANKEILDEAYV"


@pytest.fixture
def world() -> World:
    return build_world()


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def make(world: World, type_name: str, values: dict[str, Any]) -> Entity:
    saved = services.create(world.users["editor"], type_name, world.project, values)
    return Entity.objects.get(pk=saved.entity.pk)


def samples(world: World) -> dict[str, Entity]:
    """Two kinase domains, and three samples of them, as a lab's sheet lists them."""
    abl1 = make(world, PROTEIN, {"name": "ABL1", "sequence": ABL1})
    egfr = make(world, PROTEIN, {"name": "EGFR", "sequence": EGFR})
    return {
        "ABL1": abl1,
        "EGFR": egfr,
        "KD-001": make(world, SAMPLE, {"label": "ABL1-KD-001", "protein": str(abl1.pk)}),
        "KD-002": make(world, SAMPLE, {"label": "ABL1-KD-002", "protein": str(abl1.pk)}),
        "KD-003": make(world, SAMPLE, {"label": "EGFR-KD-001", "protein": str(egfr.pk)}),
    }


def check(world: World, rows: list[dict[str, Any]], principal: str = "editor", **kw: Any) -> Any:
    return api_as(world, principal).post(
        f"/projects/{world.project.pk}/sheet-edits/check",
        {"type": kw.pop("type", SAMPLE), "rows": rows, **kw},
    )


def checked(world: World, rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    answer = check(world, rows, **kw)
    assert answer.status_code == 200, answer.content
    found: list[dict[str, Any]] = answer.json()["rows"]
    return found


def save(world: World, rows: list[dict[str, Any]], principal: str = "editor", **kw: Any) -> Any:
    return api_as(world, principal).post(
        f"/projects/{world.project.pk}/sheet-edits",
        {
            "type": kw.pop("type", SAMPLE),
            "reason": kw.pop("reason", "Batch 12 QC"),
            "rows": rows,
            **kw,
        },
    )


def to_save(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What a check read, as the web app sends it to be saved once it's reviewed."""
    out = []
    for row in rows:
        kept: dict[str, Any] = {"key": row["key"], "values": row["values"]}
        if row["entity"]:
            kept |= {"entity": row["entity"], "revision": row["revision"]}
        if row["location"]:
            kept["location"] = {
                "container": row["location"]["container"],
                "position": row["location"]["position"],
            }
        if row["amount"]:
            kept["amount"] = row["amount"]
        out.append(kept)
    return out


def edit(record: Entity, key: str = "", **cells: str | None) -> dict[str, Any]:
    return {
        "key": key or record.hrid,
        "entity": str(record.pk),
        "revision": record.revision,
        "cells": cells,
    }


def problems_of(row: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in row["problems"]:
        out.setdefault(p["at"], []).append(p["detail"])
    return out


def values_of(record: Entity) -> dict[str, Any]:
    return services.current_values(services.specific(Entity.objects.get(pk=record.pk)))


# --- Reading and checking cells --------------------------------------------------------------


def test_each_cell_is_read_as_its_field_reads_a_spreadsheets_and_nothing_is_written(
    world: World,
) -> None:
    found = samples(world)
    kd1, kd2, kd3 = found["KD-001"], found["KD-002"], found["KD-003"]
    revisions = Revision.objects.count()
    rows = checked(
        world,
        [
            # A paste from a European colleague's sheet, before its locale is said.
            edit(kd1, purity="97,5", concentration="1250 ug/mL", protein="egfr"),
            edit(kd2, due_on="03/04/2027", tags="kinase; crystallography"),
            edit(kd3, purity="120", protein="BRAF", concentration="5 mL"),
        ],
    )
    first, second, third = rows
    # 97,5 reads one way only; 1250 ug/mL is kept as 1.25 mg/mL, the field's unit.
    assert first["kind"] == "edited", first["problems"]
    assert first["shown"] == {
        "purity": "97.5",
        "concentration": "1.25",
        "protein": found["EGFR"].hrid,
    }
    assert first["values"]["protein"] == str(found["EGFR"].pk)
    assert first["values"]["concentration"] == {"value": 1250, "unit": "ug/mL"}
    # 03/04/2027 is the 3rd of April or the 4th of March: said, never guessed.
    assert second["kind"] == "failed"
    [ambiguous] = second["problems"]
    assert (ambiguous["at"], ambiguous["kind"]) == ("values/due_on", "ambiguous")
    assert "3 April 2027 day first, and 4 March 2027 month first" in ambiguous["detail"]
    # Every problem with a row is found, each at its cell.
    assert problems_of(third) == {
        "values/protein": [
            f"“BRAF” is no protein in {world.project.name}, by its ID, HRID or name."
        ],
        "values/concentration": [
            "“5 mL” is in a unit Iki can't convert to mg/mL: `mL` and `mg/mL` measure "
            "different things, so values can't convert."
        ],
        "values/purity": ["Purity must be at most 100."],
    }
    # Nothing was written: no revision, no value changed.
    assert Revision.objects.count() == revisions
    assert values_of(kd1)["purity"] is None

    # Once the sheet says its dates are day first, the cell reads one way.
    [again] = checked(
        world,
        [edit(kd2, due_on="03/04/2027", tags="kinase; crystallography")],
        locale={"dates": "day_first"},
    )
    assert again["kind"] == "edited", again["problems"]
    assert again["shown"] == {"due_on": "2027-04-03", "tags": "kinase; crystallography"}
    # 1,234 reads as a thousand and more, or one and a bit, until the sheet says which.
    [comma] = checked(world, [edit(kd1, purity="1,234")])
    assert comma["problems"][0]["kind"] == "ambiguous"
    [said] = checked(world, [edit(kd1, purity="1,234")], locale={"decimal": "comma"})
    assert said["shown"] == {"purity": "1.234"}


def test_a_check_holds_a_cell_to_every_rule_an_edit_is(world: World) -> None:
    found = samples(world)
    kd1, kd2 = found["KD-001"], found["KD-002"]
    rows = checked(
        world,
        [
            # Another sample's label: a sample's label is unique in its project.
            edit(kd1, label="ABL1-KD-002"),
            # A required value emptied.
            edit(kd2, label=""),
            # A computed field, and one a cell can't hold.
            edit(found["KD-003"], additions="x"),
        ],
    )
    assert problems_of(rows[0]) == {
        "values/label": [f"Label is {kd2.hrid}’s already: no two entities in a project share it."]
    }
    assert problems_of(rows[1]) == {"values/label": ["Label is required."]}
    assert problems_of(rows[2]) == {
        "values/additions": ["Additions: It holds structured data that a cell can't."]
    }


def test_a_locked_record_says_why_at_its_row(world: World) -> None:
    body = reviewed(world)
    record = Entity.objects.get(pk=body["id"])
    [row] = checked(world, [edit(record, notes="Rerun on the new column")])
    assert row["kind"] == "failed"
    [said] = row["problems"]
    assert said["at"] == ""
    assert said["detail"].startswith(f"{record.hrid} is locked by its Status (Reviewed)")
    answer = save(
        world,
        [
            {
                "key": "a",
                "entity": str(record.pk),
                "revision": record.revision,
                "values": {"notes": "x"},
            }
        ],
    )
    body = problem(answer, 422, "sheet_problems")
    assert body["errors"][0]["pointer"] == "#/rows/0"
    assert "is locked by its Status (Reviewed)" in body["errors"][0]["detail"]


def test_references_are_found_by_id_hrid_or_name_and_an_ambiguous_name_says_which(
    world: World,
) -> None:
    found = samples(world)
    # Another construct someone in the project also named ABL1.
    other = make(world, PROTEIN, {"name": "abl1", "sequence": ABL1 + "GS"})
    kd1 = found["KD-001"]
    by_hrid, by_id, by_name = checked(
        world,
        [
            edit(kd1, "a", protein=found["EGFR"].hrid.lower()),
            edit(kd1, "b", protein=str(found["EGFR"].pk)),
            edit(kd1, "c", protein="ABL1"),
        ],
    )
    assert by_hrid["values"]["protein"] == by_id["values"]["protein"] == str(found["EGFR"].pk)
    [said] = problems_of(by_name)["values/protein"]
    assert said.startswith("“ABL1” names 2 records here")
    assert found["ABL1"].hrid in said and other.hrid in said
    # The same record twice in one check is a problem of the second.
    assert problems_of(by_id)[""] == [f"{kd1.hrid} is in the sheet twice."]


# --- Saving ----------------------------------------------------------------------------------


def test_a_sheet_cant_set_a_file_the_scan_holds(world: World) -> None:
    """A cell holds no file (ADR 0228), and a save's values are checked as any edit's are,
    so a quarantined workbook named by its ID reaches no record through the sheet (ADR 0216)."""
    lot = make(world, "acme/protein.Lot", {"label": "LOT-0042"})
    blob = Blob.objects.create(sha256="d" * 64, size=4096, storage_uri="s3://iki/blobs/held")
    scan_state.record_result(
        blob,
        state="infected",
        scanner="fixture",
        signature_version="1",
        reason="Xls.Downloader.Agent-7372345-0",
    )
    held = File.objects.create(
        blob=blob,
        name="EnVision_plate-07.xlsm",
        media_type="application/vnd.ms-excel.sheet.macroenabled.12",
        project=world.project,
        created_by=world.users["editor"],
    )
    rows = checked(world, [edit(lot, report=str(held.pk))], type="acme/protein.Lot")
    assert problems_of(rows[0]) == {
        "values/report": ["Report: It holds a file: add files to records once they're imported."]
    }
    revisions = Revision.objects.count()
    answer = save(
        world,
        [
            {
                "key": lot.hrid,
                "entity": str(lot.pk),
                "revision": lot.revision,
                "values": {"report": str(held.pk)},
            }
        ],
        type="acme/protein.Lot",
    )
    body = problem(answer, 422, "sheet_problems")
    assert [e["pointer"] for e in body["errors"]] == ["#/rows/0/values/report"], body
    assert "quarantine" in " ".join(e["detail"] for e in body["errors"]).lower(), body
    assert Revision.objects.count() == revisions
    assert values_of(lot)["report"] is None


def test_saving_writes_every_change_as_a_revision_with_its_reason(world: World) -> None:
    found = samples(world)
    kd1, kd2 = found["KD-001"], found["KD-002"]
    rows = checked(
        world,
        [
            edit(kd1, purity="98.5", concentration="2.1"),
            edit(kd2, purity="97.2", notes="Aggregates above 2 mg/mL"),
            {"key": "new-1", "cells": {"label": "EGFR-KD-002", "protein": "EGFR", "purity": "95"}},
        ],
    )
    assert [r["kind"] for r in rows] == ["edited", "edited", "created"]
    # A new row's HRID isn't spent by checking it.
    assert rows[2]["hrid"] is None
    answer = save(world, to_save(rows), reason="Purity by SEC, batch 12")
    assert answer.status_code == 200, answer.content
    body = answer.json()
    assert (body["edited"], body["created"]) == (2, 1)
    new = Entity.objects.get(hrid=body["rows"][2]["hrid"])
    # Checked, then saved: the sample after the three made before, no number skipped.
    assert new.hrid == f"SMP-{int(found['KD-003'].hrid.split('-')[1]) + 1:06d}"
    assert values_of(kd1)["purity"] == 98.5
    assert values_of(kd1)["concentration"] == 2.1
    for record in (kd1, kd2):
        revision = Revision.objects.filter(entity=record).latest("number")
        assert (revision.user, revision.reason) == (
            world.users["editor"],
            "Purity by SEC, batch 12",
        )
    created = Revision.objects.get(entity=new, number=1)
    assert created.reason == "Purity by SEC, batch 12"
    event = AuditEvent.objects.get(action="records.sheet_saved")
    assert event.actor == world.users["editor"]
    assert event.data["reason"] == "Purity by SEC, batch 12"
    assert [e["hrid"] for e in event.data["edited"]] == [kd1.hrid, kd2.hrid]
    assert [e["hrid"] for e in event.data["created"]] == [new.hrid]


def test_one_rows_problem_saves_nothing(world: World) -> None:
    found = samples(world)
    kd1, kd2 = found["KD-001"], found["KD-002"]
    before = Revision.objects.count()
    entities = Entity.objects.count()
    answer = save(
        world,
        [
            {
                "key": "a",
                "entity": str(kd1.pk),
                "revision": kd1.revision,
                "values": {"purity": 98.5},
            },
            # Two new rows with one label: the second can't be.
            {"key": "b", "values": {"label": "EGFR-KD-009"}},
            {"key": "c", "values": {"label": "EGFR-KD-009"}},
            {
                "key": "d",
                "entity": str(kd2.pk),
                "revision": kd2.revision,
                "values": {"purity": 140},
            },
        ],
    )
    body = problem(answer, 422, "sheet_problems")
    pointers = {e["pointer"] for e in body["errors"]}
    assert pointers == {"#/rows/2/values/label", "#/rows/3/values/purity"}
    assert Revision.objects.count() == before
    assert Entity.objects.count() == entities
    assert values_of(kd1)["purity"] is None
    assert not AuditEvent.objects.filter(action="records.sheet_saved").exists()


def test_a_record_changed_since_it_was_read_saves_nothing(world: World) -> None:
    found = samples(world)
    kd1, kd2 = found["KD-001"], found["KD-002"]
    [row1, row2] = checked(world, [edit(kd1, purity="98.5"), edit(kd2, purity="97")])
    # Someone else changes the second sample, to a valid value, before the first saves.
    other = api_as(world, "admin").patch(
        f"/entities/{kd2.pk}",
        {"values": {"purity": 91.0}},
        headers={"If-Match": f'"{kd2.revision}"'},
    )
    assert other.status_code == 200, other.content
    answer = save(world, to_save([row1, row2]))
    body = problem(answer, 412, "stale")
    assert body["rows"] == [
        {"key": kd2.hrid, "entity": str(kd2.pk), "hrid": kd2.hrid, "revision": kd2.revision + 1}
    ]
    assert values_of(kd1)["purity"] is None
    assert values_of(kd2)["purity"] == 91.0
    # Checking it again says so, at its row.
    [again] = checked(world, [edit(kd2, purity="97")])
    assert again["problems"][0]["kind"] == "stale"


def test_an_existing_record_must_name_the_revision_that_was_read(world: World) -> None:
    kd1 = samples(world)["KD-001"]
    answer = save(world, [{"key": "a", "entity": str(kd1.pk), "values": {"purity": 98.5}}])
    body = problem(answer, 422, "validation_failed")
    assert body["errors"] == [
        {"pointer": "#/rows/0/revision", "detail": "is required: the revision you read."}
    ]


def test_a_save_needs_a_reason(world: World) -> None:
    kd1 = samples(world)["KD-001"]
    row = {"key": "a", "entity": str(kd1.pk), "revision": kd1.revision, "values": {"purity": 98.5}}
    answer = save(world, [row], reason="")
    assert answer.status_code == 422


def test_an_agents_save_names_the_person_and_the_agent(world: World) -> None:
    kd1 = samples(world)["KD-001"]
    client, tokens = signed_in_token(world, "editor")
    answer = bearer(
        tokens["access_token"],
        "POST",
        f"/projects/{world.project.pk}/sheet-edits",
        {
            "type": SAMPLE,
            "reason": "Purity from the SEC export",
            "rows": [
                {
                    "key": "a",
                    "entity": str(kd1.pk),
                    "revision": kd1.revision,
                    "values": {"purity": 99.1},
                }
            ],
        },
        headers={"Iki-Agent-Model": "claude-opus-5-5"},
    )
    assert answer.status_code == 200, answer.content
    revision = Revision.objects.select_related("agent").filter(entity=kd1).latest("number")
    assert revision.user == world.users["editor"]
    assert revision.agent is not None
    assert (revision.agent.client_id, revision.agent.model) == (client.client_id, "claude-opus-5-5")
    event = AuditEvent.objects.get(action="records.sheet_saved")
    assert event.agent == revision.agent


def test_only_an_editor_checks_or_saves(world: World) -> None:
    kd1 = samples(world)["KD-001"]
    for principal, status in (("viewer", 403), ("outsider", 404)):
        assert check(world, [edit(kd1, purity="9")], principal).status_code == status
        row = {"key": "a", "entity": str(kd1.pk), "revision": kd1.revision, "values": {"purity": 9}}
        assert save(world, [row], principal).status_code == status
    # A record from another project can't be edited through this one's sheet.
    elsewhere = build_world("b-")
    theirs = services.create(
        elsewhere.users["editor"], SAMPLE, elsewhere.project, {"label": "Theirs"}
    ).entity
    record = Entity.objects.get(pk=theirs.pk)
    [row] = checked(world, [edit(record, purity="9")])
    assert problems_of(row) == {"": [f"It isn't a record in {world.project.name}."]}


# --- New tubes: where they're kept, and how much each holds ----------------------------------


def test_new_tubes_are_put_away_and_start_with_their_volume(world: World) -> None:
    found = samples(world)
    box = make(world, BOX, {"name": "Batch 12 retains", "rows": 9, "columns": 9})
    # A1 is taken already.
    first = make(world, TUBE, {"barcode": "TB-0001"})
    from iki_server.storage import services as storage

    storage.move(world.users["editor"], [storage.Wanted(first, box, "A1")], reason="Filed")
    sample = found["KD-001"].hrid
    rows = checked(
        world,
        [
            {
                "key": "t2",
                "cells": {"barcode": "TB-0002", "sample": sample},
                "location": f"{box.hrid} B3",
                "amount": "500",
            },
            # Just the box, by its name: the next free position, in its order.
            {
                "key": "t3",
                "cells": {"barcode": "TB-0003", "sample": sample},
                "location": "Batch 12 retains",
                "amount": "0.25 mL",
            },
            {
                "key": "t4",
                "cells": {"barcode": "TB-0004"},
                "location": f"{box.hrid} A1",
                "amount": "0",
            },
        ],
        type=TUBE,
    )
    t2, t3, t4 = rows
    assert t2["kind"] == "created", t2["problems"]
    assert t2["location"]["shown"] == f"{box.hrid} B3"
    assert t2["amount_shown"] == "500"
    assert t3["location"]["position"] == "A2"
    assert (t3["amount"], t3["amount_shown"]) == ({"value": "0.25", "unit": "mL"}, "250")
    assert problems_of(t4) == {
        "location": [f"{box.hrid} has something at A1 already."],
        "amount": ["“0” must be more than zero."],
    }
    # Nothing was made, nothing put away.
    assert not Entity.objects.filter(type=TUBE).exclude(pk=first.pk).exists()
    assert Place.objects.filter(container=box).count() == 1

    answer = save(world, to_save([t2, t3]), type=TUBE, reason="Aliquoted for stability")
    assert answer.status_code == 200, answer.content
    made = {r["key"]: Entity.objects.get(hrid=r["hrid"]) for r in answer.json()["rows"]}
    for key, position, volume in (("t2", (2, 3), 500), ("t3", (1, 2), 250)):
        place = Place.objects.get(pk=made[key].pk)
        assert (place.container_id, place.row, place.column) == (box.pk, *position)
        assert Amount.objects.get(entity=made[key]).value == volume
        movement = Movement.objects.get(entity=made[key])
        assert movement.reason == "Aliquoted for stability"


def test_a_well_taken_after_the_check_saves_nothing_and_says_where(world: World) -> None:
    """The check found the box's next free well for a tube; another tube went in it, in
    another session, before the save. The save is refused as stale (ADR 0207): the tube
    isn't put in another well than the one reviewed."""
    from iki_server.storage import services as storage

    box = make(world, BOX, {"name": "Batch 12 retains", "rows": 9, "columns": 9})
    first = make(world, TUBE, {"barcode": "TB-0001"})
    storage.move(world.users["editor"], [storage.Wanted(first, box, "A1")], reason="Filed")
    [row] = checked(
        world,
        [{"key": "t2", "cells": {"barcode": "TB-0002"}, "location": "Batch 12 retains"}],
        type=TUBE,
    )
    assert row["location"]["shown"] == f"{box.hrid} A2"
    # In another session, a retain goes in A2.
    other = make(world, TUBE, {"barcode": "TB-0101"})
    storage.move(world.users["admin"], [storage.Wanted(other, box, "A2")], reason="Retains")

    answer = save(world, to_save([row]), type=TUBE, reason="Aliquoted for stability")
    body = problem(answer, 412, "stale")
    assert body["rows"] == []
    assert body["places"] == [
        {"key": "t2", "container": str(box.pk), "hrid": box.hrid, "position": "A2"}
    ]
    assert f"{box.hrid} A2" in body["detail"]
    # Nothing was made, and nothing else moved.
    assert not Entity.objects.filter(type=TUBE).exclude(pk__in=[first.pk, other.pk]).exists()
    assert Place.objects.filter(container=box).count() == 2
    assert not AuditEvent.objects.filter(action="records.sheet_saved").exists()
    # Checked again, it goes in the next free well, to review.
    [again] = checked(
        world,
        [{"key": "t2", "cells": {"barcode": "TB-0002"}, "location": "Batch 12 retains"}],
        type=TUBE,
    )
    assert again["location"]["shown"] == f"{box.hrid} A3"
    saved = save(world, to_save([again]), type=TUBE, reason="Aliquoted")
    assert saved.status_code == 200, saved.content
    made = Entity.objects.get(hrid=saved.json()["rows"][0]["hrid"])
    place = Place.objects.get(pk=made.pk)
    assert (place.container_id, place.row, place.column) == (box.pk, 1, 3)


def test_a_new_records_position_is_the_one_reviewed_never_the_next_free_one(world: World) -> None:
    """A save names each new record's exact position, as the check gave it: only the check
    finds a box's next free one. A container without positions takes none."""
    box = make(world, BOX, {"name": "Batch 12 retains", "rows": 9, "columns": 9})
    without = save(
        world,
        [
            {
                "key": "t2",
                "values": {"barcode": "TB-0002"},
                "location": {"container": str(box.pk), "position": None},
            }
        ],
        type=TUBE,
    )
    body = problem(without, 422, "sheet_problems")
    assert body["errors"] == [
        {
            "pointer": "#/rows/0/location",
            "detail": f"{box.hrid} has positions: save the one the check gave this new tube.",
            "kind": "invalid",
        }
    ]
    assert not Entity.objects.filter(type=TUBE).exists()
    # The key is required: a client says the position, or that there's none.
    unsaid = save(
        world,
        [{"key": "t2", "values": {"barcode": "TB-0002"}, "location": {"container": str(box.pk)}}],
        type=TUBE,
    )
    assert unsaid.status_code == 422
    assert not Entity.objects.filter(type=TUBE).exists()
    # A box goes in a freezer, which has shelves, not positions.
    freezer = make(world, "acme/protein.Freezer", {"name": "Freezer 3", "asset_tag": "AT-0003"})
    [row] = checked(
        world,
        [
            {
                "key": "b1",
                "cells": {"name": "Batch 13 retains", "rows": "9", "columns": "9"},
                "location": "Freezer 3",
            }
        ],
        type=BOX,
    )
    assert row["location"]["position"] is None, row["problems"]
    saved = save(world, to_save([row]), type=BOX, reason="New retains box")
    assert saved.status_code == 200, saved.content
    made = Entity.objects.get(hrid=saved.json()["rows"][0]["hrid"])
    place = Place.objects.get(pk=made.pk)
    assert (place.container_id, place.row, place.column) == (freezer.pk, None, None)


def test_a_location_or_volume_of_an_existing_record_is_changed_elsewhere(world: World) -> None:
    tube = make(world, TUBE, {"barcode": "TB-0009"})
    [row] = checked(
        world,
        [
            {
                "key": "a",
                "entity": str(tube.pk),
                "revision": tube.revision,
                "location": "BOX-1",
                "amount": "5",
            }
        ],
        type=TUBE,
    )
    assert problems_of(row) == {
        "location": ["Move a record from its page or in Storage."],
        "amount": ["changes through its ledger: draw, transfer or adjust it on its page."],
    }


# --- The import template ---------------------------------------------------------------------


def test_a_types_import_template_has_its_importable_fields_with_units(world: World) -> None:
    answer = api_as(world).get(f"/entity-types/{SAMPLE}/import-template.csv")
    assert answer.status_code == 200
    assert answer["Content-Type"] == "text/csv; charset=utf-8"
    assert 'filename="Samples import template.csv"' in answer["Content-Disposition"]
    text = answer.content.decode("utf-8")
    assert text.startswith("﻿")
    headings = text.removeprefix("﻿").strip().split(",")
    # Computed fields, states, files and rows of structured data aren't importable.
    assert headings[:6] == [
        '"Label"',
        '"Protein"',
        '"Lot"',
        '"Material"',
        '"Purity"',
        '"Concentration (mg/mL)"',
    ]
    assert '"Status"' not in headings and '"Additions"' not in headings
    missing = api_as(world).get("/entity-types/acme/protein.Nothing/import-template.csv")
    problem(missing, 404, "not_found")


def test_a_template_heading_never_runs_as_a_formula(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin's label is text a spreadsheet shows, as a search's download writes it
    (ADR 0175), whatever it starts with."""
    from iki_server.entities import search_exports

    monkeypatch.setattr(search_exports, "heading", lambda f: '=HYPERLINK("https://x.test")')
    answer = api_as(world).get(f"/entity-types/{SAMPLE}/import-template.csv")
    first = answer.content.decode().removeprefix("\ufeff").split(",")[0]
    assert first == '"\'=HYPERLINK(""https://x.test"")"'


def test_a_check_takes_a_large_paste_in_parts(world: World) -> None:
    rows = [{"key": f"n{i}", "cells": {"label": f"S-{i}"}} for i in range(201)]
    answer = check(world, rows)
    assert answer.status_code == 422
