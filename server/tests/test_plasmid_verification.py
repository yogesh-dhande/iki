"""A physical prep's archived evidence and signed decision through the installed plugin."""

import runpy
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from first_party_dependencies import ROOT, build_order
from org_world import build_world
from test_lots import LOTS
from test_plugin_migrations import database, iki, migrate, orm, sync
from test_signatures import begin, ready, signed, standing
from test_states import roles

from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.entities.values import shape, to_json
from iki_server.files.models import Blob, File
from iki_server.org.services import Refused

__all__ = ["database"]

TYPE = "iki/lots.PlasmidVerification"


def test_prep_evidence_is_pinned_and_signed_under_its_recorded_flow(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    sequencing = ROOT / "plugins" / "plasmid_sequencing"
    compiled = iki("plugin", "build", *build_order(LOTS, sequencing), "--output", build, cwd=ROOT)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    migrate(build, database)
    sync(build, database)
    orm(
        build,
        database,
        """
        from test_plasmid_verification import exercise_review
        exercise_review(blob)
        result = {}
        """,
    )


def snapshot(entity: Entity) -> dict[str, Any]:
    """The action input's JSON projection at a particular record revision."""
    fields = shape(entity.type).fields()
    values = services.values_at(entity, entity.revision)
    return {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        "type": entity.type,
        "revision": entity.revision,
        "values": {name: to_json(fields[name], value) for name, value in values.items()},
    }


def exercise_review(blob: Blob) -> None:
    world = build_world()
    ready(world)
    analyst_role = "iki/lots.verification_analyst"
    reviewer_role = "iki/lots.verification_reviewer"
    qa_role = "iki/lots.qa"
    roles(
        world,
        editor=(analyst_role, reviewer_role, qa_role),
        admin=(reviewer_role, qa_role),
        viewer=(qa_role,),
    )
    analyst = world.users["editor"]
    reviewer = world.users["admin"]
    archive = runpy.run_path(str(LOTS / "scripts" / "verification.py"))["archive"]

    def create(kind: str, values: dict[str, Any], *, by_run: bool = False) -> Entity:
        entity = services.create(analyst, kind, world.project, values, by_run=by_run).entity
        assert isinstance(entity, Entity)
        return entity

    def edit(entity: Entity, values: dict[str, Any], *, correction: bool = False) -> None:
        entity.refresh_from_db()
        services.update(
            analyst,
            entity,
            values,
            expected_revision=entity.revision,
            allow_correction=correction,
            reason="Corrected interpretation of the evidence" if correction else "",
        )

    # Synthetic short reference strings distinguish two registry entries.
    dna, other_dna = (
        create("iki/dna.DNASequence", {"bases": bases, "topology": "circular"})
        for bases in ("AAGGCCC", "CTTTACGA")
    )
    plasmid, other_plasmid = (
        create("iki/plasmid.Plasmid", {"sequence": str(ref.entry_id)}) for ref in (dna, other_dna)
    )
    # An external source may have no recorded route. Where several routes produced an
    # entry, the project's record can link the specific assembly its material came from.
    source = {"backbone": str(dna.pk), "method": "gibson"}
    routes = [
        create("iki/plasmid.Assembly", source | {"product": str(other_dna.pk)}) for _ in range(2)
    ]
    for route in routes:
        edit(other_plasmid, {"source_assembly": str(route.pk)})
        assert services.current_values_of([other_plasmid])[other_plasmid.pk]["source_assembly"] == (
            route.pk
        )
    for product in ({}, {"product": str(dna.pk)}):
        wrong = create("iki/plasmid.Assembly", source | product)
        with pytest.raises(services.Invalid) as mismatch:
            edit(other_plasmid, {"source_assembly": str(wrong.pk)})
        assert "source_assembly" in mismatch.value.errors

    prep = create("iki/lots.PlasmidPrep", {"plasmid": str(plasmid.entry_id)})
    aligned = File.objects.create(
        blob=blob, name="alignment.fa", media_type="text/plain", project=world.project
    )
    report = File.objects.create(
        blob=blob, name="review.pdf", media_type="application/pdf", project=world.project
    )
    alignment, wrong_alignment = (
        create(
            "iki/core.Alignment",
            {
                "name": "Synthetic alignment",
                "reference": str(ref.entry_id),
                "aligned": str(aligned.pk),
                "verdict": "differs",
            },
        )
        for ref in (dna, other_dna)
    )
    files = {
        str(f.pk): {"name": f.name, "media_type": f.media_type, "size": f.blob.size}
        for f in (aligned, report)
    }
    pinned_prep = snapshot(prep) | {"included": {"plasmid": [snapshot(plasmid)]}}

    def values(flow: str, evidence: Entity | None, reason: str = "") -> dict[str, Any]:
        params = {"outcome": "match", "override_reason": reason}
        inputs = {"prep": [pinned_prep]}
        if evidence is None:
            params["report"] = str(report.pk)
        else:
            inputs["alignment"] = [snapshot(evidence)]
        output = archive(
            {
                "inputs": inputs,
                "params": params,
                "settings": {"verification_flow": flow},
                "files": files,
            }
        )
        [record] = output["creates"]
        assert record["type"] == TYPE
        result: dict[str, Any] = record["values"]
        return result

    research_values = values("research", None)
    override_reason = "Independent review of the second report resolved the discrepancy."
    qc_values = values("regulated_qc", alignment, override_reason)
    assert research_values["report"] == str(report.pk)
    assert research_values["review_flow"] == "research"
    assert qc_values["review_flow"] == "regulated_qc"
    assert qc_values["plasmid_record_id"] == str(plasmid.pk)
    assert qc_values["plasmid_revision"] == plasmid.revision
    assert qc_values["alignment_revision"] == alignment.revision

    # All save paths enforce the relationship, even if a worker sends mismatched evidence.
    for invalid in (
        qc_values | {"alignment": str(wrong_alignment.pk)},
        qc_values | {"plasmid": str(other_plasmid.entry_id)},
        qc_values | {"override_reason": None},
        qc_values | {"override_reason": " \t "},
    ):
        with pytest.raises(services.Invalid):
            create(TYPE, invalid, by_run=True)
    with pytest.raises(services.Invalid) as manual:
        create(TYPE, qc_values)
    assert {"review_flow", "plasmid_record_id", "plasmid_revision"} <= manual.value.errors.keys()
    with pytest.raises(services.Invalid) as unpinned:
        create(TYPE, {"prep": str(prep.pk), "plasmid": str(plasmid.entry_id), "outcome": "match"})
    assert "plasmid_revision" in unpinned.value.errors

    research = create(TYPE, research_values, by_run=True)
    qc = create(TYPE, qc_values, by_run=True)
    edit(research, {"reviewer": str(analyst.pk)})
    edit(qc, {"reviewer": str(reviewer.pk)})
    with pytest.raises(services.Invalid) as missing_reason:
        edit(qc, {"override_reason": None})
    assert "override_reason" in missing_reason.value.errors
    with pytest.raises(services.Invalid):
        edit(qc, {"review_flow": "research", "plasmid_revision": 999})

    def sign(who: str, record: Entity, target: str) -> None:
        signed(world, who, str(record.pk), target, field="review")
        assert states.values_of(record)["review"] == target

    def refused(who: str, record: Entity, target: str) -> None:
        with pytest.raises((PermissionDenied, Refused)):
            begin(world, who, str(record.pk), target, field="review")

    refused("editor", research, "submitted")
    refused("admin", research, "signed_off")
    sign("editor", research, "signed_off")
    [signoff] = standing(world, str(research.pk))
    assert signoff["meaning"] == "verification"
    assert signoff["verification"]["valid"]

    self_review = create(TYPE, qc_values, by_run=True)
    edit(self_review, {"reviewer": str(analyst.pk)})
    sign("editor", self_review, "submitted")
    refused("editor", self_review, "reviewed")

    refused("editor", qc, "signed_off")
    refused("viewer", qc, "released")
    qc.refresh_from_db()
    with pytest.raises(states.NeedsSignatures):
        states.set_state(analyst, qc, "review", "submitted", expected_revision=qc.revision)
    sign("editor", qc, "submitted")
    refused("editor", qc, "reviewed")
    sign("admin", qc, "reviewed")
    refused("editor", qc, "released")
    refused("admin", qc, "released")
    sign("viewer", qc, "released")
    signatures = standing(world, str(qc.pk))
    assert sorted(item["meaning"] for item in signatures) == ["approval", "authorship", "review"]
    assert all(item["verification"]["valid"] for item in signatures)

    edit(qc, {"outcome": "discrepancy", "override_reason": None}, correction=True)
    assert states.values_of(qc)["review"] == "recorded"
    refused("admin", qc, "reviewed")
    refused("viewer", qc, "released")
    edit(plasmid, {"description": "Source record amended"})
    edit(alignment, {"name": "Evidence record amended"})
    plasmid.refresh_from_db()
    alignment.refresh_from_db()
    retained = services.current_values_of([qc])[qc.pk]
    assert retained["outcome"] == "discrepancy"
    assert retained["plasmid_revision"] == qc_values["plasmid_revision"] < plasmid.revision
    assert retained["alignment_revision"] == qc_values["alignment_revision"] < alignment.revision
    original = standing(world, str(qc.pk))
    assert {s["id"]: s["revision"] for s in original} == {
        s["id"]: s["revision"] for s in signatures
    }
    for signature in original:
        assert signature["verification"]["valid"]
        attested = services.values_at(qc, signature["revision"])
        assert attested["outcome"] == "match"
        assert attested["override_reason"] == override_reason

    # The earlier result had an agreement only. Copy it into the new field of
    # every parent revision, including one where only the alignment name changed.
    app = "iki__plasmid_sequencing"
    executor = MigrationExecutor(connection)
    [head] = executor.loader.graph.leaf_nodes(app)
    executor.migrate([(app, "0001_initial")])
    legacy = create(
        "iki/plasmid_sequencing.WholePlasmidAlignment",
        {
            "name": "Earlier alignment",
            "plasmid": str(plasmid.entry_id),
            "reference": str(dna.entry_id),
            "source": "consensus",
            "consensus_given": str(aligned.pk),
            "aligned": str(aligned.pk),
            "agreement": "incomplete",
        },
    )
    edit(legacy, {"name": "Edited earlier alignment"})
    legacy.refresh_from_db()
    before = [services.values_at(legacy, number) for number in range(1, legacy.revision + 1)]
    assert all(row["verdict"] is None for row in before)
    MigrationExecutor(connection).migrate([head])
    assert services.current_values_of([legacy])[legacy.pk]["verdict"] == "incomplete"
    for number, row in enumerate(before, 1):
        assert services.values_at(legacy, number) == row | {"verdict": "incomplete"}
    with pytest.raises(ValueError, match="Give a reason"):
        values("regulated_qc", legacy)
    MigrationExecutor(connection).migrate([(app, "0001_initial")])
    for number, row in enumerate(before, 1):
        assert services.values_at(legacy, number) == row
