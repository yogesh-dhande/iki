"""The installed workflow through scheduling, storage, the draw ledger and signatures."""

import datetime as dt
import importlib
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
import yaml
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.utils import timezone
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from scan_fixtures import clean_blob
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.accounts.models import User
from iki_server.amounts import services as amounts
from iki_server.amounts.models import Amount, DrawPurpose, Movement
from iki_server.entities import registry, services, states
from iki_server.entities.models import Entity
from iki_server.files.models import File
from iki_server.org.models import LabRoleGrant
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job
from iki_server.signatures.models import Signature
from iki_server.signatures.services import verify
from iki_server.storage import services as storage
from iki_spec.compute import Binding

__all__ = ["database"]

PLUGIN = PLUGINS / "stability"
PREFIX = "iki/stability."


@pytest.fixture
def schedule_input() -> dict[str, Any]:
    fixture = next(yaml.safe_load_all((PLUGIN / "tests" / "stability.yaml").read_text()))
    study = fixture["inputs"]["study"][0]
    study.update(id="study", revision=1)
    study["values"]["lot"] = "lot"
    study["values"]["assignments"][0]["tube"] = "tube"
    study["included"]["lot"][0].update(id="lot", roles={"material": "of"})
    study["included"]["assignments.tube"] = [{"id": "tube", "values": {"contents": "lot"}}]
    return {"inputs": {"study": [study]}}


@pytest.mark.parametrize(
    ("type_name", "material_field"),
    [
        ("iki/lots.GenericLot", "of"),
        ("iki/reagents.VolumeStockLot", "item"),
        ("acme/lab.MyProteinLot", "protein"),
    ],
)
def test_schedule_checks_the_material_declared_by_the_lot(
    schedule_input: dict[str, Any], type_name: str, material_field: str
) -> None:
    schedule = runpy.run_path(str(PLUGIN / "scripts" / "stability.py"))["schedule"]
    study = schedule_input["inputs"]["study"][0]
    lot = study["included"]["lot"][0]
    lot.update(type=type_name, roles={"material": material_field}, values={material_field: "other"})
    with pytest.raises(ValueError, match="material"):
        schedule(schedule_input)
    lot["values"][material_field] = study["values"]["material"]
    assert len(schedule(schedule_input)["creates"]) == 1
    del lot["roles"]
    with pytest.raises(ValueError, match="material"):
        schedule(schedule_input)


def test_schedule_uses_the_physical_isolate_as_the_material(schedule_input: dict[str, Any]) -> None:
    schedule = runpy.run_path(str(PLUGIN / "scripts" / "stability.py"))["schedule"]
    study = schedule_input["inputs"]["study"][0]
    lot = study["included"]["lot"][0]
    lot.update(type="iki/lots.StrainIsolate", roles={"material": "id"}, values={"strain": "design"})
    for material in ("design", "other-isolate"):
        study["values"]["material"] = material
        with pytest.raises(ValueError, match="material does not match"):
            schedule(schedule_input)
    study["values"]["material"] = lot["id"]
    [pull] = schedule(schedule_input)["creates"]
    assert pull["values"]["lot"] == lot["id"]


def test_a_tube_can_supply_repeated_pulls_only_under_one_storage_condition(
    schedule_input: dict[str, Any],
) -> None:
    schedule = runpy.run_path(str(PLUGIN / "scripts" / "stability.py"))["schedule"]
    study = schedule_input["inputs"]["study"][0]
    values = study["values"]
    values["time_points"].append({**values["time_points"][0], "code": "M2", "interval": 2})
    values["assignments"].append({**values["assignments"][0], "time_point": "M2"})
    assert len(schedule(schedule_input)["creates"]) == 2
    values["conditions"].append(
        {**values["conditions"][0], "code": "accelerated", "temperature": 40}
    )
    values["assignments"].extend([{**a, "condition": "accelerated"} for a in values["assignments"]])
    with pytest.raises(ValueError, match="condition"):
        schedule(schedule_input)
    for row in values["assignments"]:
        if row["condition"] == "accelerated":
            row["tube"] = "second"
    study["included"]["assignments.tube"].append({"id": "second", "values": {"contents": "lot"}})
    assert len(schedule(schedule_input)["creates"]) == 4


def test_study_draw_and_both_review_paths(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    (build / "iki_plugin_iki__stability" / "settings.json").write_text(
        '{"review_flow":"research"}', encoding="utf-8"
    )
    migrate(build, database)
    sync(build, database)
    exercise = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_stability import exercise_stability; "
        "exercise_stability()",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


def exercise_stability() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    workflow = importlib.import_module("stability")
    world = build_world("stability-")
    ready(world)
    editor = world.users["editor"]
    for who, grants in {
        "editor": (PREFIX + "coordinator", PREFIX + "analyst", PREFIX + "reviewer", "iki/lots.qa"),
        "runner": (PREFIX + "reviewer",),
        "admin": (PREFIX + "qa", "iki/lots.qa"),
    }.items():
        membership = world.project.memberships.get(user=world.users[who])
        for grant in grants:
            LabRoleGrant.objects.create(membership=membership, role=grant)

    def make(type_name: str, **values: Any) -> Entity:
        return Entity.objects.get(
            pk=services.create(editor, type_name, world.project, values).entity.pk
        )

    # The material's public identity is aspirin, PubChem CID 2244. The lot, tests,
    # method, amounts, assay observations and comment below are synthetic fixtures.
    compound = make(
        "iki/small_molecule.Compound",
        name="Aspirin",
        smiles="CC(=O)OC1=CC=CC=C1C(=O)O",
        inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
    )
    lot = make("iki/lots.GenericLot", lot_number="Synthetic lot", of=str(compound.pk))
    container = make("iki/inventory.Box", name="Stability box", rows=1, columns=3)
    tube = make("iki/inventory.Tube", barcode="STUDY-001", contents=str(lot.pk))
    wrong_tube = make("iki/inventory.Tube", barcode="STUDY-002", contents=str(lot.pk))
    for item in [tube, wrong_tube]:
        amounts.adjust(
            editor,
            item,
            amounts.Given.of({"value": 100, "unit": "uL"}),
            reason="Fixture",
            expected_revision=amounts.UNRECORDED_REVISION,
        )

    tool = PREFIX + "stability-tools"
    catalog.compute().bindings[tool] = Binding(environment=tool, templates=(("backend-small", 0),))

    def start(
        name: str, input_name: str, entity: Entity, **params: Any
    ) -> tuple[ActionRun, dict[str, Any]]:
        action = catalog.action(PREFIX + name)
        assert action is not None
        run = runs.launch(
            editor, action, world.project, {input_name: [entity.pk]}, params, reuse_results=False
        )
        job = Job.objects.get(run=run)
        job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
        return run, queue.payload(job, run)

    def commit(run: ActionRun, output: dict[str, Any]) -> None:
        with transaction.atomic():
            results.commit(ActionRun.objects.select_for_update().get(pk=run.pk), output)

    today = dt.datetime.now(dt.UTC).date()

    # ICH Q1A(R2) §2.1.7.1 gives 25 °C ± 2 °C and 60% RH ± 5% RH general-case long term.
    # https://database.ich.org/sites/default/files/Q1A%28R2%29%20Step4.pdf
    def study_on(revision: str, start_date: dt.date) -> Entity:
        study = make(
            PREFIX + "Study",
            name="Illustrative lot study",
            material=str(compound.pk),
            lot=str(lot.pk),
            protocol="Stability protocol",
            protocol_revision=revision,
            starts_on=start_date.isoformat(),
            owner=str(editor.pk),
            conditions=[
                {
                    "code": "long_term",
                    "label": "Long term",
                    "temperature": 25,
                    "temperature_tolerance": 2,
                    "humidity": 60,
                    "humidity_tolerance": 5,
                }
            ],
            time_points=[
                {"code": "T0", "interval": 0, "unit": "month", "days_before": 0, "days_after": 0}
            ],
            assignments=[
                {
                    "condition": "long_term",
                    "time_point": "T0",
                    "replicate": 1,
                    "tube": str(tube.pk),
                    "amount": 12,
                    "amount_unit": "uL",
                }
            ],
            tests=[{"test": "Demo peak area", "method": "Synthetic method v1", "unit": "AU"}],
        )
        run, doc = start("schedule_pulls", "study", study)
        pending, old = start("schedule_pulls", "study", study)
        commit(run, workflow.schedule(doc))
        with pytest.raises(results.Rejected, match="revision"):
            commit(pending, workflow.schedule(old))
        study.refresh_from_db()
        return study

    def pull_for(study: Entity) -> Entity:
        candidates = list(Entity.objects.filter(type=PREFIX + "Pull"))
        values = services.current_values_of(candidates)
        [pull] = [
            candidate for candidate in candidates if values[candidate.pk]["study"] == study.pk
        ]
        return pull

    def draw(
        into: Entity,
        *,
        source: Entity | None = None,
        user: User | None = None,
        volume: int = 12,
        purpose: DrawPurpose = DrawPurpose.QA_SAMPLING,
        confirm: str = "STUDY-001",
    ) -> Movement:
        return amounts.draw(
            user or editor,
            source or tube,
            amounts.Given.of({"value": volume, "unit": "uL"}),
            purpose=purpose,
            used_in=into,
            confirm=confirm,
        )

    def set_status(pull: Entity, state: str, reason: str = "") -> None:
        pull.refresh_from_db()
        states.set_state(
            editor, pull, "status", state, expected_revision=pull.revision, reason=reason
        )

    def observation(pull: Entity) -> Entity:
        prior = set(Entity.objects.filter(type=PREFIX + "Measurement").values_list("pk", flat=True))
        report = File.objects.create(
            name="Synthetic report.csv",
            media_type="text/csv",
            project=world.project,
            blob=clean_blob(
                sha256="e" * 64,
                size=22,
                storage_uri="s3://test/stability-report",
            ),
        )
        run, doc = start("record_measurement", "pull", pull, test="Demo peak area", value=1.25)
        commit(run, workflow.measure(doc))
        found = Entity.objects.filter(type=PREFIX + "Measurement").exclude(pk__in=prior).get()
        services.update(editor, found, {"report": str(report.pk)}, expected_revision=found.revision)
        return found

    research_study = study_on("01", today)
    pull = pull_for(research_study)
    with pytest.raises(states.GuardRefused, match="scan-confirmed draw"):
        set_status(pull, "pulled")
    with pytest.raises(states.GuardRefused, match="last date"):
        set_status(pull, "missed", "Window still open")
    with pytest.raises(amounts.Wrong, match="storage position"):
        draw(pull)
    storage.move(
        editor,
        [storage.Wanted(tube, container, "A1"), storage.Wanted(wrong_tube, container, "A2")],
        reason="Place synthetic study tubes",
    )
    with pytest.raises(amounts.Wrong, match="assigned"):
        draw(pull, source=wrong_tube, confirm="STUDY-002")
    with pytest.raises(amounts.Wrong, match="planned amount"):
        draw(pull, volume=13)
    with pytest.raises(amounts.Wrong, match="Scan"):
        draw(pull, confirm="")
    with pytest.raises(PermissionDenied):
        draw(pull, user=world.users["admin"])
    with pytest.raises(amounts.Wrong, match="quarantined"):
        draw(pull, purpose=DrawPurpose.ROUTINE)

    between = Barrier(2, timeout=10)

    def compete(_: int) -> Movement | None:
        try:
            between.wait()
            return draw(pull)
        except amounts.Wrong as refusal:
            assert "already recorded" in str(refusal)
            return None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        won = [result for result in pool.map(compete, range(2)) if result is not None]
    assert len(won) == 1
    movement = won[0]
    assert (movement.used_in_id, movement.source_id, movement.confirmed) == (
        pull.pk,
        lot.pk,
        "STUDY-001",
    )
    assert movement.change == -12 and Amount.objects.get(entity=tube).value == 88
    with pytest.raises(states.GuardRefused, match="match the UTC date"):
        set_status(pull, "pulled")
    pull.refresh_from_db()
    services.update(editor, pull, {"pulled_on": today.isoformat()}, expected_revision=pull.revision)
    set_status(pull, "pulled")
    with pytest.raises(amounts.Wrong, match="not awaiting a draw"):
        draw(pull)

    # The deployment changes its choice. Older studies still use their pinned flow.
    registry.settings("iki/stability")["review_flow"] = "regulated_qc"
    research = observation(pull)
    with pytest.raises(Refused, match="QC measurement"):
        begin(world, "editor", str(research.pk), "submitted", field="review")
    sign = signed(world, "runner", str(research.pk), "signed", field="review").signature
    assert sign is not None and all(check.ok for check in verify(sign))

    previous_day = today - dt.timedelta(days=1)
    qc = pull_for(study_on("02", previous_day))
    draw(qc)
    services.update(editor, qc, {"pulled_on": today.isoformat()}, expected_revision=qc.revision)
    with pytest.raises(states.GuardRefused, match="recorded draw as a pull"):
        set_status(qc, "missed", "Try to erase late pull")
    with pytest.raises(states.GuardRefused, match="Explain the deviation"):
        set_status(qc, "pulled")
    services.update(
        editor, qc, {"window_note": "Illustrative deviation"}, expected_revision=qc.revision
    )
    set_status(qc, "pulled")

    result = observation(qc)
    with pytest.raises(Refused, match="research measurement"):
        begin(world, "runner", str(result.pk), "signed", field="review")
    signed(world, "editor", str(result.pk), "submitted", field="review")
    with pytest.raises(Refused, match="independent"):
        begin(world, "editor", str(result.pk), "reviewed", field="review")
    signed(world, "runner", str(result.pk), "reviewed", field="review")
    signed(world, "admin", str(result.pk), "released", field="review")
    assert Signature.objects.filter(entity=result).count() == 3

    missed = pull_for(study_on("03", previous_day))
    set_status(missed, "missed", "Illustrative missing sample")
    with pytest.raises(amounts.Wrong, match="not awaiting a draw"):
        draw(missed)

    # A reduced design (ADR 0243): accelerated at the start and after a week, long term
    # only at the start, its accelerated pulls planned by mass from a 2 mg/mL tube, so
    # 50 ug is 25 uL. The ICH Q1A(R2) condition is public; the rest is synthetic.
    solution = make(
        "iki/inventory.Tube", barcode="STUDY-003", contents=str(lot.pk), concentration=2
    )
    amounts.adjust(
        editor,
        solution,
        amounts.Given.of({"value": 100, "unit": "uL"}),
        reason="Fixture",
        expected_revision=amounts.UNRECORDED_REVISION,
    )
    storage.move(editor, [storage.Wanted(solution, container, "A3")], reason="Place it")
    accelerated = {"code": "accelerated", "label": "Accelerated", "temperature": 40}
    point = {"days_before": 0, "days_after": 7}
    reduced = {
        "name": "Illustrative reduced study",
        "material": str(compound.pk),
        "lot": str(lot.pk),
        "protocol": "Stability protocol",
        "protocol_revision": "04",
        "starts_on": today.isoformat(),
        "owner": str(editor.pk),
        "conditions": [{"code": "long_term", "label": "Long term", "temperature": 25}, accelerated],
        "time_points": [
            {"code": "T0", "interval": 0, "unit": "month", **point},
            {"code": "W1", "interval": 1, "unit": "week", **point},
        ],
        "assignments": [
            {
                "condition": "long_term",
                "time_point": "T0",
                "replicate": 1,
                "tube": str(tube.pk),
                "amount": 12,
                "amount_unit": "uL",
            },
            *(
                {
                    "condition": "accelerated",
                    "time_point": code,
                    "replicate": 1,
                    "tube": str(solution.pk),
                    "amount": 50,
                    "amount_unit": "ug",
                }
                for code in ("T0", "W1")
            ),
        ],
        "tests": [{"test": "Demo peak area", "method": "Synthetic method v1", "unit": "AU"}],
    }
    unjustified = make(PREFIX + "Study", **reduced)
    run, doc = start("schedule_pulls", "study", unjustified)
    with pytest.raises(ValueError, match="design justification"):
        workflow.schedule(doc)
    sparse = make(
        PREFIX + "Study",
        **reduced,
        design_justification="Synthetic: matrixed per protocol section 4.2.",
    )
    run, doc = start("schedule_pulls", "study", sparse)
    commit(run, workflow.schedule(doc))
    pulls = list(Entity.objects.filter(type=PREFIX + "Pull"))
    current = services.current_values_of(pulls)
    planned = {
        (current[p.pk]["condition"], current[p.pk]["time_point"]): p
        for p in pulls
        if current[p.pk]["study"] == sparse.pk
    }
    assert sorted(planned) == [("accelerated", "T0"), ("accelerated", "W1"), ("long_term", "T0")]
    week = current[planned[("accelerated", "W1")].pk]
    assert week["elapsed_days"] == 7
    assert str(week["due_on"]) == (today + dt.timedelta(days=7)).isoformat()
    by_mass = planned[("accelerated", "T0")]
    with pytest.raises(amounts.Wrong, match="planned amount of 50 ug"):
        draw(by_mass, source=solution, volume=50, confirm="STUDY-003")
    taken = draw(by_mass, source=solution, volume=25, confirm="STUDY-003")
    assert taken.change == -25 and Amount.objects.get(entity=solution).value == 75
