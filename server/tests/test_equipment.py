"""Equipment review, validity and run provenance."""

import importlib
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.db import transaction
from django.utils import timezone
from first_party_dependencies import FIRST_PARTY, PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import ready, signed
from test_states import roles

from iki_server.amounts import services as amounts
from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.files.models import Blob, File
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, EffectKind, Job, Lineage, RunEffect
from iki_spec import rules
from iki_spec.compute import Binding
from iki_spec.model import EntityType, Plugin
from iki_spec.validate import load_plugin

__all__ = ["database"]

PLUGIN = PLUGINS / "equipment"
PREFIX = "iki/equipment."

SERVICED = {
    "performed_by": "engineer",
    "performed_at": "2026-01-05T09:00:00Z",
    "outcome": "in_tolerance",
    "report": "synthetic-report",
}


@pytest.fixture(scope="module")
def equipment() -> Plugin:
    loaded = load_plugin(PLUGIN, dependencies=FIRST_PARTY[PLUGIN])
    assert loaded.plugin is not None, loaded.problems
    return loaded.plugin


def allows(
    entity_type: EntityType,
    target: str,
    previous: str,
    actor: str,
    values: dict[str, Any],
    *,
    editor: str | None = None,
) -> bool:
    state = entity_type.fields["review"]
    schema = rules.record_schema(entity_type.label, entity_type.fields, None)
    variables = rules.guard_variables(schema, {"review": state}, [], state)
    initial = state.initial
    assert initial is not None
    transitions: list[dict[str, str]] = [
        {"from": "", "to": initial, "by": "scheduler"},
        {"from": initial, "to": "submitted", "by": "engineer"},
        {"from": "submitted", "to": "reviewed", "by": "reviewer"},
    ]
    (outcomes,) = rules.evaluate(
        variables,
        [(guard.check, rules.CONDITION) for guard in state.states[target].guards.values()],
        [
            {
                "record": values,
                "actor": {"person": actor},
                "state": {"field": "review", "from": previous, "to": target},
                "history": {"review": transitions},
                "last": {"review": {change["to"]: change for change in transitions}},
                "edits": {"review": {initial: [{"by": editor}] if editor else []}},
            }
        ],
    )
    assert all(outcome.error is None or outcome.empty for outcome in outcomes), outcomes
    return all(outcome.value is True for outcome in outcomes)


def test_only_the_engineer_who_performed_the_service_can_sign(equipment: Plugin) -> None:
    service = equipment.types[PREFIX + "EquipmentService"]
    research = SERVICED | {"review_flow": "research"}
    qc = SERVICED | {"review_flow": "regulated_qc"}
    assert allows(service, "signed_off", "scheduled", "engineer", research)
    assert not allows(service, "signed_off", "scheduled", "another engineer", research)
    assert not allows(service, "signed_off", "scheduled", "engineer", qc)
    assert allows(service, "submitted", "scheduled", "engineer", qc)
    assert not allows(service, "submitted", "scheduled", "engineer", research)
    assert not allows(service, "submitted", "scheduled", "engineer", qc | {"report": None})
    assert not allows(service, "scheduled", "signed_off", "engineer", research)
    assert not allows(service, "scheduled", "released", "engineer", qc)


def test_cancel_only_unperformed_appointments(equipment: Plugin) -> None:
    service = equipment.types[PREFIX + "EquipmentService"]
    planned = {"performed_at": None, "outcome": None, "report": None}
    assert allows(service, "cancelled", "scheduled", "engineer", planned)
    assert not allows(service, "cancelled", "scheduled", "engineer", SERVICED)
    assert not allows(service, "cancelled", "released", "engineer", SERVICED)


@pytest.mark.parametrize("type_name", ["EquipmentService", "CalibrationImpact"])
def test_regulated_review_and_qa_release_require_independent_signers(
    equipment: Plugin, type_name: str
) -> None:
    type_ = equipment.types[PREFIX + type_name]
    values = {"review_flow": "regulated_qc"}
    assert allows(type_, "reviewed", "submitted", "reviewer", values)
    for person in ("scheduler", "engineer"):
        assert not allows(type_, "reviewed", "submitted", person, values)
        assert not allows(type_, "released", "reviewed", person, values)
    assert not allows(type_, "reviewed", "submitted", "editor", values, editor="editor")
    assert not allows(type_, "released", "reviewed", "reviewer", values)
    assert not allows(type_, "released", "reviewed", "qa", values, editor="qa")
    assert allows(type_, "released", "reviewed", "qa", values)
    assert not allows(type_, "released", "submitted", "qa", values)


def test_research_impact_needs_a_reasoned_disposition(equipment: Plugin) -> None:
    impact = equipment.types[PREFIX + "CalibrationImpact"]
    assessed = {
        "review_flow": "research",
        "disposition": "repeat_required",
        "reasoning": "Synthetic check outside the lab's tolerance.",
    }
    assert allows(impact, "signed_off", "recorded", "qa", assessed)
    assert not allows(impact, "signed_off", "recorded", "qa", assessed | {"reasoning": None})
    assert not allows(
        impact, "signed_off", "recorded", "qa", assessed | {"review_flow": "regulated_qc"}
    )


def test_validity_at_measurement_uses_the_selected_instrument_and_final_calibration(
    equipment: Plugin,
) -> None:
    type_ = equipment.types[PREFIX + "InstrumentUse"]
    guard = type_.rules["valid_for_measurement"]
    assert guard.check is not None
    references = {
        "iki/equipment.Instrument": ("Instrument", {}),
        "iki/core.Entity": ("Entity", {}),
        PREFIX + "EquipmentService": (
            "Equipment service",
            equipment.types[PREFIX + "EquipmentService"].fields,
        ),
    }
    schema = rules.record_schema(type_.label, type_.fields, references.get)
    passing = SERVICED | {
        "instrument": "spectrophotometer",
        "kind": "calibration",
        "review": "released",
        "valid_until": "2026-02-05T09:00:00Z",
    }
    values = {
        "instrument": {"id": "spectrophotometer"},
        "calibration": passing,
        "measurement_at": SERVICED["performed_at"],
    }
    cases = [
        values,
        values | {"measurement_at": passing["valid_until"]},
        values | {"calibration": passing | {"instrument": "other"}},
        values | {"calibration": passing | {"review": "reviewed"}},
        values | {"calibration": passing | {"outcome": "out_of_tolerance"}},
    ]
    outcomes = rules.evaluate(
        rules.rule_variables(schema),
        [(guard.check, rules.CONDITION)],
        [{"record": record, "now": "2026-02-07T09:00:00Z"} for record in cases],
    )
    assert all(result.error is None for [result] in outcomes), outcomes
    assert [result.value for [result] in outcomes] == [True, False, False, False, False]


def test_recording_instrument_use_keeps_its_three_inputs(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki(
        "plugin", "build", *build_order(PLUGIN, PLUGINS / "samples"), "--output", build, cwd=ROOT
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_equipment import exercise_instrument_use; "
        "exercise_instrument_use()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_instrument_use() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    worker = importlib.import_module("equipment")
    world = build_world()
    ready(world)
    roles(world, editor=("iki/lab_roles.engineer", "iki/lab_roles.analyst"))
    project, editor = world.project, world.users["editor"]
    environment = PREFIX + "equipment-tools"
    catalog.compute().bindings[environment] = Binding(
        environment=environment, templates=(("backend-small", 0),)
    )

    def make(type_name: str, *, by_run: bool = False, **values: Any) -> Entity:
        saved = services.create(editor, type_name, project, values, by_run=by_run)
        return Entity.objects.get(pk=saved.entity.pk)

    instrument = make(PREFIX + "Instrument", name="Synthetic absorbance reader")
    tube = make("iki/inventory.Tube", name="Absorbance reference", contents=str(instrument.pk))
    amounts.record(editor, tube, amounts.Given.of({"value": 10, "unit": "uL"}))
    measured = make("iki/samples.Sample", source=str(tube.pk), run="RUN-000001")
    amounts.draw(
        editor,
        tube,
        amounts.Given.of({"value": 1, "unit": "uL"}),
        used_in=measured,
        confirm=tube.hrid,
    )
    for status in ("received", "in_testing"):
        measured.refresh_from_db()
        states.set_state(editor, measured, "status", status, expected_revision=measured.revision)
    measured.refresh_from_db()
    services.update(
        editor,
        measured,
        {"measurements": [{"method": "Absorbance", "value": "0.5", "unit": "AU"}]},
        expected_revision=measured.revision,
    )
    measured.refresh_from_db()
    attachment = File.objects.create(
        blob=Blob.objects.create(sha256="a" * 64, size=12, storage_uri="s3://iki/passing"),
        name="Synthetic passing calibration.txt",
        media_type="text/plain",
        project=project,
    )
    calibration = make(
        PREFIX + "EquipmentService",
        by_run=True,
        instrument=str(instrument.pk),
        kind="calibration",
        due_on="2026-01-05",
        procedure="Synthetic optical check",
        review_flow="research",
        performed_by=str(editor.pk),
        performed_at="2026-01-05T09:00:00Z",
        valid_until="2026-02-05T09:00:00Z",
        outcome="in_tolerance",
        report=str(attachment.pk),
    )
    signed(world, "editor", str(calibration.pk), "signed_off", field="review")
    calibration.refresh_from_db()

    action = catalog.action(PREFIX + "record_instrument_use")
    assert action is not None
    evidence = {"instrument": instrument, "result": measured, "calibration": calibration}
    run = runs.launch(
        editor,
        action,
        project,
        {name: [entity.pk] for name, entity in evidence.items()},
        {"measurement_at": "2026-01-05T10:30:00Z"},
        reuse_results=False,
    )
    job = Job.objects.get(run=run)
    job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
    creates = worker.record_use(queue.payload(job, run))
    with transaction.atomic():
        locked = ActionRun.objects.select_for_update().get(pk=run.pk)
        results.commit(locked, {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": creates})

    use = RunEffect.objects.get(run=run, kind=EffectKind.CREATED).entity
    assert use.type == PREFIX + "InstrumentUse"
    inputs = Lineage.objects.filter(entity=use, run=run).values_list(
        "input", "parent_id", "parent_revision"
    )
    assert set(inputs) == {(name, entity.pk, entity.revision) for name, entity in evidence.items()}
