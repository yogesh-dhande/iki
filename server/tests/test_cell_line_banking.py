"""Cell banking through the installed plugin and the same services as the API."""

from collections.abc import Callable
from pathlib import Path

import pytest
from first_party_dependencies import PLUGINS, build_order
from org_world import build_world
from scan_fixtures import clean_blob
from test_plugin_migrations import database, iki, migrate, orm, sync
from test_signatures import ready, signed

from iki_server.entities import lists, search, services, states
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.files.models import File, FileUse
from iki_server.files.services import EMPTY_SHA256
from iki_server.graph import queries
from iki_server.org.models import LabRoleGrant, ProjectMembership
from iki_server.storage import services as storage

__all__ = ["database"]

PREFIX = "iki/cell_lines."


def test_a_samples_graph_keeps_its_banks_thaw_passage_and_signed_checks(
    tmp_path: Path, database: str
) -> None:
    installed = tmp_path / "build"
    # Each database uses the real first-party migrations and the database's graph triggers.
    built = iki(
        "plugin", "build", *build_order(PLUGINS / "cell_lines"), "--output", installed, cwd=PLUGINS
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(installed, database)
    sync(installed, database)
    orm(
        installed,
        database,
        "from test_cell_line_banking import exercise_history\nresult = exercise_history()",
    )


def change(record: Entity, code: str) -> str:
    """A kind of change, from the lab's own list (ADR 0270): its option's ID, by its code,
    in the record's project's registry."""
    assert record.owner_project is not None
    option = lists.find(record.owner_project.registry_id, PREFIX + "EngineeredChange", code)
    assert option is not None
    return str(option.entry.pk)


def exercise_history() -> None:
    """Runs in the Django process with the first-party plugin installed."""
    world = build_world()
    editor = world.users["editor"]
    membership = ProjectMembership.objects.get(project=world.project, user=editor)
    # The editor records every check, signs research checks off, accepts unsigned ones
    # and submits regulated ones. Others review and release them independently.
    for role in ("research_signer", "recorder", "analyst"):
        LabRoleGrant.objects.create(membership=membership, role=PREFIX + role)
    for who, role in (("viewer", "reviewer"), ("admin", "qa")):
        member = ProjectMembership.objects.get(project=world.project, user=world.users[who])
        LabRoleGrant.objects.create(membership=member, role=PREFIX + role)
    ready(world)

    def make(entity_type: str, **values: object) -> Entity:
        record = services.create(editor, PREFIX + entity_type, world.project, values).entity
        assert isinstance(record, Entity)
        return record

    # The two public catalogue IDs identify WI-38. All local clone, banking,
    # passage, check and file records in this scenario are synthetic.
    # https://www.cellosaurus.org/CVCL_0579 ; https://www.atcc.org/products/ccl-75
    catalogue = {
        "name": "WI-38",
        "cellosaurus_id": "CVCL_0579",
    }
    line = make("ParentalLine", **catalogue)
    assert make("ParentalLine", **catalogue).pk == line.pk
    atcc = {"supplier": "ATCC", "catalogue_id": "CCL-75", "line": str(line.pk)}
    source = make("CellLineSource", **atcc)
    other_source = make(
        "CellLineSource", supplier="Coriell", catalogue_id="AG06814-J", line=str(line.pk)
    )
    assert other_source.pk != source.pk
    assert make("CellLineSource", **atcc).pk == source.pk
    # A conflicting association for a real, different line must fail even on a retry.
    # https://www.cellosaurus.org/CVCL_0030
    different = make("ParentalLine", name="HeLa", cellosaurus_id="CVCL_0030")
    with pytest.raises(Invalid) as caught:
        make("CellLineSource", **{**atcc, "line": str(different.pk)})
    assert "line" in caught.value.errors
    clone = make(
        "EngineeredLine",
        name="Synthetic reporter clone",
        parent=str(line.pk),
        parent_source=str(source.pk),
        changes=[
            {
                "kind": change(line, "stable_transfection"),
                "description": "Synthetic reporter label",
            }
        ],
    )
    master = make(
        "CellBank",
        name="Example master",
        kind="master",
        line=str(clone.pk),
        banked_on="2026-01-01",
        passage_number=3,
    )
    working_values = {
        "kind": "working",
        "line": str(clone.pk),
        "source_bank": str(master.pk),
        "banked_on": "2026-01-05",
        "passage_number": 5,
    }
    working = make("CellBank", name="Example working", **working_values)
    with pytest.raises(Invalid, match="bank of this cell line"):
        make("CellBank", **{**working_values, "name": "Wrong line", "line": str(line.pk)})

    vial = make("CellVial", name="Example vial", contents=str(working.pk))
    # A vial's contents are its bank: anything else, or nothing, is refused.
    with pytest.raises(Invalid) as wrong:
        make("CellVial", name="Wrong contents", contents=str(clone.pk))
    assert "contents" in wrong.value.errors
    with pytest.raises(Invalid, match="vial's bank"):
        make("CellVial", name="No bank")
    box = services.create(
        editor, "iki/inventory.Box", world.project, {"name": "Example box"}
    ).entity
    assert isinstance(box, Entity)
    [placed] = storage.move(editor, [storage.Wanted(vial, box, "B3")], reason="Banked")
    assert placed.to_container_id == box.pk

    def check(bank: Entity, kind: str, result: str) -> Entity:
        return make(
            "CellCheck",
            bank=str(bank.pk),
            kind=kind,
            result=result,
            method="Synthetic report",
            checked_on="2026-01-06",
            due_on="2026-02-01",
        )

    authentication = check(working, "str_authentication", "match")
    mycoplasma = check(working, "mycoplasma", "negative")
    unrelated = check(master, "str_authentication", "inconclusive")
    # A test the plugin doesn't name is one from the lab's list, never a typed name, and
    # its results are those its option gives: endotoxin is judged against a limit.
    endotoxin = lists.find(world.project.registry_id, PREFIX + "CellCheckTest", "endotoxin")
    assert endotoxin is not None
    lab_test = {
        "bank": str(working.pk),
        "kind": "other",
        "lab_test": str(endotoxin.entry.pk),
        "method": "Kinetic chromogenic LAL, synthetic report",
        "checked_on": "2026-01-06",
    }
    with pytest.raises(Invalid, match="conforms or does not conform"):
        make("CellCheck", **lab_test, result="negative")
    with pytest.raises(Invalid, match="the lab's test for a check of kind Other"):
        make("CellCheck", **{**lab_test, "lab_test": None}, result="conforms")
    with pytest.raises(Invalid, match="the lab's test for a check of kind Other"):
        make("CellCheck", **{**lab_test, "kind": "sterility"}, result="negative")
    make("CellCheck", **lab_test, result="conforms")
    thaw_values = {
        "event": "thaw",
        "bank": str(working.pk),
        "vial": str(vial.pk),
        "passage_number": 5,
        "event_on": "2026-01-07",
        "str_check": str(authentication.pk),
        "mycoplasma_check": str(mycoplasma.pk),
    }
    with pytest.raises(Invalid, match="final STR check"):
        make("CellCulture", **thaw_values)
    for test in (authentication, mycoplasma, unrelated):
        signed(world, "editor", str(test.pk), "signed_off", field="review")
    for overrides in [
        {"str_check": str(unrelated.pk)},
        {"str_check": str(mycoplasma.pk)},
        {"event_on": "2026-01-05"},
        {"event_on": "2026-02-02"},
    ]:
        with pytest.raises(Invalid, match="final STR check"):
            make("CellCulture", **{**thaw_values, **overrides})
    thaw = make("CellCulture", **thaw_values)
    with pytest.raises(Invalid, match="thaw record"):
        make("CellCulture", **thaw_values)
    storage.move(editor, [storage.Wanted(vial, None)], reason=f"Thawed for {thaw.hrid}")
    passage_values = {
        "event": "passage",
        "event_on": "2026-01-09",
        "bank": str(working.pk),
        "passage_number": 6,
        "parent_culture": str(thaw.pk),
    }
    with pytest.raises(Invalid, match="increments passage by one"):
        make("CellCulture", **{**passage_values, "passage_number": 5})
    passage = make("CellCulture", **passage_values)
    # An explicitly synthetic empty file tests the attachment relationship.
    data = File.objects.create(
        name="synthetic-observation.csv",
        media_type="text/csv",
        project=world.project,
        blob=clean_blob(sha256=EMPTY_SHA256, size=0, storage_uri="s3://iki/synthetic"),
    )

    # The same bank tested under the two other procedures: an STR result accepted
    # with its report and no signature, without a repeat date, and a mycoplasma
    # result submitted, independently reviewed and released by QA.
    accepted = make(
        "CellCheck",
        bank=str(working.pk),
        kind="str_authentication",
        result="match",
        method="Synthetic outside report",
        checked_on="2026-01-08",
    )
    released = check(working, "mycoplasma", "negative")
    branch_values = {
        **passage_values,
        "event_on": "2026-03-02",
        "str_check": str(accepted.pk),
        "mycoplasma_check": str(released.pk),
    }
    with pytest.raises(Invalid, match="final STR check"):
        make("CellCulture", **branch_values)
    with pytest.raises(states.GuardRefused, match="report"):
        states.set_state(
            editor, accepted, "review", "accepted", expected_revision=accepted.revision
        )
    services.update(editor, accepted, {"report": str(data.pk)}, expected_revision=accepted.revision)
    accepted.refresh_from_db()
    states.set_state(editor, accepted, "review", "accepted", expected_revision=accepted.revision)
    for who, state in (("editor", "submitted"), ("viewer", "reviewed"), ("admin", "released")):
        signed(world, who, str(released.pk), state, field="review")
    with pytest.raises(Invalid, match="final mycoplasma check"):
        make("CellCulture", **branch_values)
    branch = make("CellCulture", **{**branch_values, "event_on": "2026-01-10"})
    assert branch.pk != passage.pk
    sample = make(
        "CellSample",
        name="Example observation",
        culture=str(passage.pk),
        collected_on="2026-01-09",
        data_file=str(data.pk),
    )
    assert FileUse.objects.filter(entity_id=sample.pk, file=data).exists()
    with search.time_limit():
        traced = queries.reach(editor, queries.Query(sample.pk, "upstream"))
        visible = {hrid for _, _, hrid in traced.page(None, 100)}
    assert visible == {
        node.hrid
        for node in (
            passage,
            thaw,
            vial,
            working,
            master,
            authentication,
            mycoplasma,
            clone,
            source,
            line,
        )
    }
    with search.time_limit():
        downstream = queries.reach(
            editor, queries.Query(line.pk, "downstream", types=(PREFIX + "CellSample",))
        )
        assert [hrid for _, _, hrid in downstream.page(None, 100)] == [sample.hrid]
    local_history(make, line, source, master, working, accepted)


def local_history(
    make: Callable[..., Entity],
    line: Entity,
    source: Entity,
    master: Entity,
    working: Entity,
    accepted: Entity,
) -> None:
    """A lab's own line, a one-tier stock and cultures whose history began outside Iki.
    Every local record and identifier here is synthetic."""
    primary = make(
        "ParentalLine", name="Donor 7 fibroblasts", catalogue="local", local_id="PRI-0007"
    )
    assert (
        make("ParentalLine", name="Again", catalogue="local", local_id="PRI-0007").pk == primary.pk
    )
    for wrong in (
        {"catalogue": "local"},
        {"catalogue": "local", "local_id": "PRI-0008", "cellosaurus_id": "CVCL_0579"},
        {"local_id": "PRI-0009"},
    ):
        with pytest.raises(Invalid, match="local line"):
            make("ParentalLine", name="Wrong", **wrong)
    adapted = make(
        "EngineeredLine",
        name="Donor 7 serum-free",
        parent=str(primary.pk),
        changes=[
            {"kind": change(primary, "adaptation"), "description": "Weaned to a serum-free medium"}
        ],
    )

    # A single-tier stock of the catalogued line from its supplier, and one of a local
    # line received with no passage history.
    stock = make(
        "CellBank",
        name="WI-38 lab stock",
        kind="stock",
        line=str(line.pk),
        catalogue_source=str(source.pk),
        banked_on="2026-02-01",
        passage_number=12,
    )
    received = make(
        "CellBank",
        name="Donor 7 serum-free stock",
        kind="stock",
        line=str(adapted.pk),
        banked_on="2026-02-03",
        passage_unknown=True,
    )
    with pytest.raises(Invalid, match="mark it unknown"):
        make("CellBank", name="No passage", kind="stock", line=str(line.pk), banked_on="2026-02-01")
    with pytest.raises(Invalid, match="mark it unknown"):
        make(
            "CellBank",
            name="Both",
            kind="stock",
            line=str(line.pk),
            banked_on="2026-02-01",
            passage_number=4,
            passage_unknown=True,
        )
    # A stock made from another of the line's banks follows it; a two-tier bank keeps its rules.
    restock = {
        "name": "WI-38 restock",
        "kind": "stock",
        "line": str(line.pk),
        "source_bank": str(stock.pk),
        "banked_on": "2026-02-10",
        "passage_number": 14,
    }
    make("CellBank", **restock)
    with pytest.raises(Invalid, match="known passage"):
        make("CellBank", **{**restock, "passage_number": 11})
    with pytest.raises(Invalid, match="bank of this cell line"):
        make("CellBank", **{**restock, "line": str(primary.pk)})
    with pytest.raises(Invalid, match="working bank's source is a master bank"):
        make(
            "CellBank",
            name="Working from stock",
            kind="working",
            line=str(line.pk),
            source_bank=str(stock.pk),
            banked_on="2026-02-10",
            passage_number=14,
        )
    with pytest.raises(Invalid, match="working bank needs its master"):
        make(
            "CellBank",
            name="Orphan",
            kind="working",
            line=str(line.pk),
            banked_on="2026-02-10",
            passage_number=14,
        )
    with pytest.raises(Invalid, match="master bank has no source"):
        make(
            "CellBank",
            name="Sourced master",
            kind="master",
            line=str(line.pk),
            source_bank=str(master.pk),
            banked_on="2026-02-10",
            passage_number=14,
        )
    with pytest.raises(Invalid, match="master bank or stock"):
        make(
            "CellBank",
            name="Catalogue working",
            kind="working",
            line=str(line.pk),
            source_bank=str(master.pk),
            catalogue_source=str(source.pk),
            banked_on="2026-02-10",
            passage_number=14,
        )

    # A vial of the received stock thaws at an unknown passage, and stays unknown.
    vial = make("CellVial", name="Donor 7 vial", contents=str(received.pk))
    thaw = {
        "event": "thaw",
        "bank": str(received.pk),
        "vial": str(vial.pk),
        "event_on": "2026-02-04",
    }
    with pytest.raises(Invalid, match="frozen passage"):
        make("CellCulture", **thaw, passage_number=3)
    thawed = make("CellCulture", **thaw, passage_unknown=True)
    after = {
        "event": "passage",
        "bank": str(received.pk),
        "parent_culture": str(thawed.pk),
        "event_on": "2026-02-06",
    }
    with pytest.raises(Invalid, match="increments passage"):
        make("CellCulture", **after, passage_number=1)
    make("CellCulture", **after, passage_unknown=True)

    # A culture received growing names its line and starts the history without a parent.
    start = {
        "event": "start",
        "line": str(primary.pk),
        "event_on": "2026-02-05",
        "passage_number": 4,
    }
    started = make("CellCulture", **start)
    for wrong in (
        {"vial": str(vial.pk)},
        {"parent_culture": str(thawed.pk)},
    ):
        with pytest.raises(Invalid, match="started outside Iki has neither"):
            make("CellCulture", **start, **wrong)
    with pytest.raises(Invalid, match="names its line instead"):
        make("CellCulture", **{**start, "bank": str(stock.pk)})
    with pytest.raises(Invalid, match="names its line instead"):
        make("CellCulture", event="start", event_on="2026-02-05", passage_number=4)
    # A passage keeps its parent's bank or line: one from a banked thaw can't name a line.
    with pytest.raises(Invalid, match="parent's line"):
        make(
            "CellCulture",
            **{
                **after,
                "bank": None,
                "line": str(primary.pk),
                "parent_culture": str(thawed.pk),
                "passage_number": 5,
            },
        )
    # Its check cites a bank, which it has none of.
    with pytest.raises(Invalid, match="final STR check"):
        make("CellCulture", **start, str_check=str(accepted.pk))
    grown = {
        "event": "passage",
        "line": str(primary.pk),
        "parent_culture": str(started.pk),
        "event_on": "2026-02-07",
        "passage_number": 5,
    }
    make("CellCulture", **grown)
    with pytest.raises(Invalid, match="parent's line"):
        make("CellCulture", **{**grown, "line": str(adapted.pk)})
    with pytest.raises(Invalid, match="parent's line"):
        make("CellCulture", **{**grown, "passage_number": 7})
    # A culture started from a bank before Iki keeps its bank and banking date.
    from_bank = {"event": "start", "bank": str(working.pk), "passage_number": 9}
    make("CellCulture", **from_bank, event_on="2026-01-20")
    with pytest.raises(Invalid, match="on or after its banking date"):
        make("CellCulture", **from_bank, event_on="2026-01-01")
