"""A physical prep's archived evidence and signed decision through the installed plugin."""

import runpy
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from first_party_dependencies import FIRST_PARTY, ROOT, build_order, withs
from org_world import build_world
from test_lots import LOTS
from test_plugin_migrations import database, git, iki, migrate, orm, sync, upgrade
from test_signatures import begin, ready, signed, standing
from test_states import roles

from iki_server.audit.models import AuditEvent
from iki_server.entities import access, checking, services, states
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import shape, to_json
from iki_server.files.models import Blob, File
from iki_server.org.services import Refused
from iki_server.runs import catalog, triggers
from iki_server.runs.models import ProjectTrigger, TriggerFiring
from iki_spec import rules as cel

__all__ = ["database"]

TYPE = "iki/lots.PlasmidVerification"


def test_pinned_subtype_fields_stay_with_their_own_evidence() -> None:
    base = {"type": "object", "label": "Alignment", "fields": {"verdict": {"type": "string"}}}
    variables = cel.rule_variables(
        {"type": "object", "label": "Verification", "fields": {"alignment": base}}
    )
    when = '"agreement" in dyn(record.alignment) && dyn(record.alignment).agreement == "differs"'
    contexts = [
        {"record": {"alignment": {"verdict": None, **extra}}}
        for extra in (
            {"agreement": "differs"},
            {},
            {"agreement": "matches"},
            {"agreement": "differs"},
        )
    ]
    evaluated = checking.evaluate_pinned(
        variables, [(when, cel.CONDITION)], contexts, (("alignment", "alignment_revision"),)
    )
    assert [row[0].value for row in evaluated] == [True, False, False, True]
    ordinary = checking.evaluate_pinned(variables, [(when, cel.CONDITION)], contexts, ())
    assert [row[0].value for row in ordinary] == [False] * len(contexts)


def test_marked_plasmid_samples_upgrade_from_the_initial_schema(
    tmp_path: Path, _postgres_url: str
) -> None:
    source = ROOT / "plugins" / "plasmid_sequencing"
    plugin = tmp_path / "plasmid_sequencing"
    shutil.copytree(source, plugin)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "plasmid_sequencing")
    git(tmp_path, "commit", "-qm", "Initial schema")
    checked = upgrade(plugin, _postgres_url, *withs(FIRST_PARTY[source]), "-k", "none")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "Using fixtures marked upgrade in the earlier plugin" in checked.stdout
    assert "rolled back: the samples have their values" in checked.stdout


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


def test_archiving_a_legacy_discrepant_alignment_requires_a_reason() -> None:
    archive = runpy.run_path(str(LOTS / "scripts" / "verification.py"))["archive"]
    plasmid = {"id": "record", "revision": 1, "values": {"sequence": "reference"}}
    prep = {"id": "prep", "values": {"plasmid": "entry"}, "included": {"plasmid": [plasmid]}}
    alignment = {
        "id": "alignment",
        "revision": 4,
        "values": {
            "agreement": "differs",
            "reference": "reference",
            "aligned": "file",
        },
    }
    run: dict[str, Any] = {
        "inputs": {"prep": [prep], "alignment": [alignment]},
        "params": {"outcome": "match"},
        "files": {"file": {"size": 100}},
        "settings": {"verification_flow": "research"},
    }
    with pytest.raises(ValueError, match="Give a reason"):
        archive(run)
    run["params"]["override_reason"] = "Independent review resolved the discrepancy."
    [verification] = archive(run)["creates"]
    assert verification["values"]["alignment_revision"] == 4


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

    checked = create(
        "iki/plasmid.Assembly",
        source
        | {
            "product": str(dna.pk),
            "matches_product": True,
            "simulated_length": 7,
            "simulated_topology": "circular",
        },
        by_run=True,
    )
    with patch.object(catalog, "availability", return_value=catalog.Availability(True)):
        edit(checked, {"product": str(other_dna.pk)})
    latest = services.current_values_of([checked])[checked.pk]
    assert latest["matches_product"] is None
    assert latest["simulated_length"] is None and latest["simulated_topology"] is None

    recheck = "iki/plasmid.check_stated_product"
    firing = TriggerFiring.objects.get(trigger__trigger=recheck, entity=checked)
    assert firing.editor == analyst
    assert firing.run is not None, firing.error
    assert firing.run.requested_by == analyst and firing.run.via == "trigger"
    assert latest["verification_note"].startswith("Needs re-verification")

    # Resolve the actual pinned references under the editor's permissions.
    visible = access.visible

    def hide_evidence(user: Any) -> Any:
        assert user == analyst
        return visible(user).exclude(pk=dna.pk)

    with (
        patch.object(catalog, "availability", return_value=catalog.Availability(True)),
        patch.object(access, "visible", side_effect=hide_evidence),
    ):
        edit(checked, {"method": "gateway_bp"})
    current = services.current_values_of([checked])[checked.pk]
    assert current["matches_product"] is None
    assert "Needs re-verification" in current["verification_note"]
    assert "backbone" in current["verification_note"]

    # A failed automatic recheck on a reopened record keeps the operator's explanation.
    reopened = routes[0]
    Entity.objects.filter(pk=reopened.pk).update(has_been_locked=True)
    explanation = "Correct the reference on the reopened record"
    with patch.object(catalog, "availability", return_value=catalog.Availability(True)):
        saved = services.update(
            analyst,
            reopened,
            {"product": str(dna.pk)},
            expected_revision=reopened.revision,
            reason=explanation,
            by_run=True,
        )
    assert saved.revision is not None
    diagnostic = Revision.objects.get(entity=reopened, number=saved.revision.number + 1)
    assert (saved.revision.via, saved.revision.reason) == ("run", explanation)
    assert (diagnostic.via, diagnostic.user, diagnostic.reason) == ("trigger", analyst, explanation)
    assert diagnostic.changed_fields == ["verification_note"]
    assert TriggerFiring.objects.filter(entity=reopened, state="failed", run__isnull=True).exists()

    with pytest.raises(PermissionDenied):
        triggers.disable(analyst, world.project, recheck, reason="Manual verification.")
    with pytest.raises(Refused):
        triggers.disable(reviewer, world.project, recheck, reason="  ")
    reason = "Manual review during instrument maintenance."
    triggers.disable(reviewer, world.project, recheck, reason=reason)
    event = AuditEvent.objects.get(action="trigger.disabled", project=world.project)
    assert event.actor == reviewer and event.data["reason"] == reason
    checked.refresh_from_db()
    services.update(
        analyst, checked, {"matches_product": True}, by_run=True, expected_revision=checked.revision
    )
    count = TriggerFiring.objects.filter(entity=checked).count()
    edit(checked, {"method": "gateway_lr"})
    assert services.current_values_of([checked])[checked.pk]["matches_product"] is None
    assert TriggerFiring.objects.filter(entity=checked).count() == count

    # Replaying the upgrade preserves an explicit opt-out and restores the default
    # for projects whose older trigger was switched off without a reason.
    stale = create("iki/plasmid.Assembly", source | {"matches_product": True}, by_run=True)
    with patch.object(triggers, "rechecks", return_value=[]):
        edit(stale, {"method": "gateway_lr"})
    assert services.values_at(stale, 2)["matches_product"] is True
    loader = MigrationExecutor(connection).loader
    key = ("iki__plasmid", "0008_invalidate_edited_plans")
    upgrade = loader.get_migration(*key)
    setting = ProjectTrigger.objects.get(project=world.project, trigger=recheck)
    with connection.schema_editor() as schema:
        upgrade.apply(loader.project_state(key, at_end=False), schema)
        setting.refresh_from_db()
        assert not setting.enabled and setting.disabled_reason == reason
        ProjectTrigger.objects.filter(pk=setting.pk).update(disabled_reason="")
        upgrade.apply(loader.project_state(key, at_end=False), schema)
    revision = stale.revisions.get(kind="migrated")
    assert revision.number == 3 and revision.via == "system" and revision.user_id is None
    assert key[1] in revision.reason
    assert AuditEvent.objects.filter(action="entity.migrated", target_id=str(stale.pk)).count() == 1
    assert services.values_at(stale, 2)["matches_product"] is True
    assert services.values_at(stale, 3)["matches_product"] is None
    restored = ProjectTrigger.objects.get(pk=setting.pk)
    assert restored.enabled and restored.disabled_by_id is None

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
            by_run=True,
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
            params["notes"] = (
                "The reviewer inspected the aligned file against the pinned reference."
            )
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

    imported = {
        "name": "Imported alignment",
        "reference": str(dna.entry_id),
        "aligned": str(aligned.pk),
    }
    with pytest.raises(services.Invalid) as manual_verdict:
        create("iki/core.Alignment", imported | {"verdict": "matches"})
    assert "verdict" in manual_verdict.value.errors
    assert values("research", create("iki/core.Alignment", imported))["outcome"] == "match"

    whole_type = "iki/plasmid_sequencing.WholePlasmidAlignment"
    consensus = File.objects.create(
        blob=blob, name="consensus.fa", media_type="chemical/seq-na-fasta", project=world.project
    )
    whole = imported | {
        "plasmid": str(plasmid.entry_id),
        "source": "consensus",
        "consensus_given": str(aligned.pk),
        "sample": str(prep.pk),
        "consensus_caller": "supplied",
    }
    with pytest.raises(services.Invalid) as manual_caller:
        create(whole_type, whole)
    assert "consensus_caller" in manual_caller.value.errors
    comparison = whole | {
        "plasmid_record_id": str(plasmid.pk),
        "plasmid_revision": plasmid.revision,
        "reference_length": 7,
        "consensus": str(consensus.pk),
        "called_percent": 0.0,
        "verdict": "incomplete",
    }
    for field in (
        "plasmid_record_id",
        "plasmid_revision",
        "reference_length",
        "consensus",
        "called_percent",
        "consensus_caller",
        "verdict",
    ):
        with pytest.raises(services.Invalid):
            create(
                whole_type,
                {name: value for name, value in comparison.items() if name != field},
                by_run=True,
            )
    # A supplied consensus can't claim the plugin's caller, or carry its read evidence.
    with pytest.raises(services.Invalid) as wrong_caller:
        create(whole_type, comparison | {"consensus_caller": "iki_majority_1"}, by_run=True)
    assert "consensus_caller" in wrong_caller.value.errors
    with pytest.raises(services.Invalid):
        create(whole_type, comparison | {"reads_total": 41}, by_run=True)
    run = create("iki/sequencing.SequencingRun", {"name": "Fixture", "instrument_model": "Fixture"})
    library = create(
        "iki/sequencing.SequencingLibrary",
        {"name": "Fixture", "sample": str(prep.pk), "strategy": "fixture"},
    )
    read_set = create(
        "iki/sequencing.ReadSet",
        {"run": str(run.pk), "library": str(library.pk), "read_1": str(aligned.pk)},
    )
    called = {
        "consensus_caller": "iki_majority_1",
        "platform": "nanopore",
        "min_depth": 10,
        "mean_depth": 35.9,
        "least_depth": 0,
        "reads_total": 41,
        "reads_aligned": 37,
        "polishing_rounds": 2,
    }
    reads = comparison | called | {"source": "reads", "read_set": str(read_set.pk)}
    reads["consensus_given"] = None
    with pytest.raises(services.Invalid) as uncalled:
        create(
            whole_type,
            reads | {"read_set_revision": read_set.revision, "polishing_rounds": None},
            by_run=True,
        )
    assert "polishing_rounds" in uncalled.value.errors
    with pytest.raises(services.Invalid) as no_revision:
        create(whole_type, reads, by_run=True)
    assert "read_set_revision" in no_revision.value.errors
    create(whole_type, reads | {"read_set_revision": read_set.revision}, by_run=True)

    whole_alignment = create(whole_type, comparison, by_run=True)
    with pytest.raises(ValueError, match="Give a reason"):
        values("regulated_qc", whole_alignment)
    bound = values("regulated_qc", whole_alignment, override_reason)
    create(TYPE, bound, by_run=True)
    other_prep = create("iki/lots.PlasmidPrep", {"plasmid": str(plasmid.entry_id)})
    with pytest.raises(services.Invalid, match="different physical prep"):
        create(TYPE, bound | {"prep": str(other_prep.pk)}, by_run=True)

    # Sample attribution added after a run must not change the evidence revision it pinned.
    without_sample = create(whole_type, comparison | {"sample": None}, by_run=True)
    before_attribution = values("research", without_sample, override_reason)
    edit(without_sample, {"sample": str(other_prep.pk)})
    create(TYPE, before_attribution, by_run=True)
    without_sample.refresh_from_db()
    with pytest.raises(services.Invalid, match="different physical prep"):
        create(
            TYPE, before_attribution | {"alignment_revision": without_sample.revision}, by_run=True
        )
    with pytest.raises(services.Invalid, match="saved revision"):
        create(
            TYPE,
            before_attribution | {"alignment_revision": without_sample.revision + 1},
            by_run=True,
        )

    # A pending verification saved before the sample rule cannot acquire a new signature.
    legacy_rules = [r for r in checking.checks_of(TYPE) if r.name != "same_physical_prep"]
    with patch.object(checking, "checks_of", return_value=legacy_rules):
        pending = create(TYPE, bound | {"prep": str(other_prep.pk)}, by_run=True)
    edit(pending, {"reviewer": str(reviewer.pk)})
    with pytest.raises(Refused, match="different physical prep"):
        begin(world, "editor", str(pending.pk), "submitted", field="review")

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

    def sign(who: str, record: Entity, target: str, reason: str = "") -> None:
        signed(world, who, str(record.pk), target, field="review", reason=reason)
        assert states.values_of(record)["review"] == target

    def refused(who: str, record: Entity, target: str) -> None:
        with pytest.raises((PermissionDenied, Refused)):
            begin(world, who, str(record.pk), target, field="review")

    # Records saved before the override rule still attest the pinned evidence at signing.
    previous = [
        rule for rule in checking.checks_of(TYPE) if rule.name != "explain_alignment_override"
    ]
    for flow, evidence, target in (
        ("research", alignment, "signed_off"),
        ("regulated_qc", whole_alignment, "submitted"),
    ):
        unsigned = values(flow, evidence, override_reason) | {
            "override_reason": None,
            "reviewer": str(analyst.pk),
        }
        with patch.object(checking, "checks_of", return_value=previous):
            contested = create(TYPE, unsigned, by_run=True)
        with pytest.raises(Refused, match="Give a reason"):
            begin(world, "editor", str(contested.pk), target, field="review")
        with pytest.raises(services.Invalid):
            edit(contested, {"override_reason": "  \t  "})
        edit(contested, {"override_reason": override_reason})
        sign("editor", contested, target)

    imported_review = create(
        TYPE, values("research", create("iki/core.Alignment", imported)), by_run=True
    )
    edit(imported_review, {"reviewer": str(analyst.pk)})
    sign("editor", imported_review, "signed_off")

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

    sign("viewer", qc, "recorded", "Reopen the evidence for correction")
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
    assert {s["id"]: s["revision"] for s in original if s["change"]["to"] != "recorded"} == {
        s["id"]: s["revision"] for s in signatures
    }
    for signature in original:
        assert signature["verification"]["valid"]
        attested = services.values_at(qc, signature["revision"])
        assert attested["outcome"] == "match"
        assert attested["override_reason"] == override_reason

    # The common alignment owns sample from this plugin's first migration.
    # Editing the subtype alone still reads the sample at the parent's last revision.
    late = create(whole_type, comparison | {"sample": None}, by_run=True)
    edit(late, {"sample": str(other_prep.pk)})
    edit(late, {"provider": "External review"})
    assert [services.values_at(late, n)["sample"] for n in (1, 2, 3)] == [
        None,
        other_prep.pk,
        other_prep.pk,
    ]
