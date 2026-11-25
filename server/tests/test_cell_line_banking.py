"""Cell banking through the installed plugin and the same services as the API."""

from pathlib import Path

import pytest
from first_party_dependencies import PLUGINS, build_order
from org_world import build_world
from test_plugin_migrations import database, iki, migrate, orm, sync
from test_signatures import ready, signed

from iki_server.entities import search, services
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.files.models import Blob, File, FileUse
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


def exercise_history() -> None:
    """Runs in the Django process with the first-party plugin installed."""
    world = build_world()
    editor = world.users["editor"]
    membership = ProjectMembership.objects.get(project=world.project, user=editor)
    LabRoleGrant.objects.create(membership=membership, role=PREFIX + "research_signer")
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
        changes=[{"kind": "stable_transfection", "description": "Synthetic reporter label"}],
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
    with pytest.raises(Invalid, match="master bank of this cell line"):
        make("CellBank", **{**working_values, "name": "Wrong line", "line": str(line.pk)})

    vial = make("CellVial", name="Example vial", bank=str(working.pk), contents=str(working.pk))
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
    thaw_values = {
        "event": "thaw",
        "bank": str(working.pk),
        "vial": str(vial.pk),
        "passage_number": 5,
        "event_on": "2026-01-07",
        "str_check": str(authentication.pk),
        "mycoplasma_check": str(mycoplasma.pk),
    }
    with pytest.raises(Invalid, match="signed STR check"):
        make("CellCulture", **thaw_values)
    for test in (authentication, mycoplasma, unrelated):
        signed(world, "editor", str(test.pk), "signed_off", field="review")
    for overrides in [
        {"str_check": str(unrelated.pk)},
        {"str_check": str(mycoplasma.pk)},
        {"event_on": "2026-01-05"},
        {"event_on": "2026-02-02"},
    ]:
        with pytest.raises(Invalid, match="signed STR check"):
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
        blob=Blob.objects.create(sha256=EMPTY_SHA256, size=0, storage_uri="s3://iki/synthetic"),
    )
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
