"""Reader previews and competing commits against the actual plate plugin's tables."""

import hashlib
import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import connection, transaction
from django.utils import timezone
from first_party_dependencies import PLUGINS, ROOT, build_order
from test_plugin_migrations import database, iki, migrate, orm, sync

from iki_server.accounts.models import User
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job, Lineage, RunEffect
from iki_spec.compute import Binding

__all__ = ["database"]
PLATES = PLUGINS / "plate_assays"


def test_a_plate_commits_only_the_previewed_revision_once(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLATES), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    # The build fixture supplies the site's setting as a deployment build would.
    (build / "iki_plugin_iki__plate_assays" / "settings.json").write_text(
        '{"review_flow":"research"}'
    )
    migrate(build, database)
    sync(build, database)
    orm(
        build,
        database,
        "from test_plate_import import exercise_import\nresult = exercise_import(project)",
    )


def exercise_import(project: Project) -> None:
    """Run inside the deployment that installed the plate plugin."""
    sys.path.insert(0, str(PLATES / "scripts"))
    assay = importlib.import_module("assays")
    source = PLATES / "tests" / "data" / "mars-96-series.csv"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    blob = Blob.objects.create(
        sha256=digest, size=source.stat().st_size, storage_uri=f"s3://test/{digest}"
    )
    reader = File.objects.create(
        name=source.name, media_type="text/csv", project=project, blob=blob
    )
    analyst = User.objects.create_user("analyst@example.com")
    ProjectMembership.objects.create(project=project, user=analyst, role=ProjectRole.EDITOR)
    env = "iki/plate_assays.assay-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    reference = entities.create(
        analyst, "iki/inventory.Tube", project, {"name": "Reference"}
    ).entity
    sample = entities.create(analyst, "iki/inventory.Tube", project, {"name": "Test series"}).entity
    # The synthetic series in mars-96-series.csv. A9 is an undosed sample and
    # A10 a reference control, both retained by the same reader commit.
    layout: list[dict[str, Any]] = [
        {
            "well": f"A{i + 1}",
            "sample": str(sample.pk),
            "replicate": 1,
            "concentration": dose,
            "concentration_unit": "nM",
        }
        for i, dose in enumerate([1, 2, 4, 8, 16, 32, 64, 128])
    ]
    layout += [
        {"well": "A9", "sample": str(reference.pk), "replicate": 2},
        {"well": "A10", "control": "Reference", "replicate": 1},
    ]
    made = entities.create(
        analyst,
        "iki/plate_assays.AssayPlate",
        project,
        {
            "name": "Synthetic reference plate",
            "size": "plate_96",
            "reader_file": str(reader.pk),
            "assignments": layout,
        },
    )
    plate = Entity.objects.get(pk=made.entity.pk)

    def start(name: str, inputs: dict[str, list[UUID]]) -> tuple[ActionRun, dict[str, Any]]:
        action = catalog.action(f"iki/plate_assays.{name}")
        assert action is not None
        run = runs.launch(analyst, action, project, inputs, {}, reuse_results=False)
        job = Job.objects.get(run=run)
        job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
        return run, queue.payload(job, run)

    def reader_run(name: str) -> tuple[ActionRun, dict[str, Any]]:
        run, doc = start(name, {"plate": [plate.pk]})
        # A worker adds the path only after staging the file listed for this run.
        doc["files"][str(reader.pk)]["path"] = str(source)
        return run, assay.reader_output(doc, commit=name == "commit_reader")

    def apply(run: ActionRun, output: dict[str, Any]) -> None:
        with transaction.atomic():
            locked = ActionRun.objects.select_for_update().get(pk=run.pk)
            results.commit(locked, output)

    # The edit lands between the worker reading the plate and its preview arriving.
    preview, old_output = reader_run("preview_reader")
    layout[-1]["control"] = "Corrected reference"
    entities.update(analyst, plate, {"assignments": layout}, expected_revision=plate.revision)
    with pytest.raises(results.Rejected, match="revision") as stale:
        apply(preview, old_output)
    assert stale.value.code == "conflict"
    assert not RunEffect.objects.filter(run=preview).exists()

    apply(*reader_run("preview_reader"))
    old_commit = reader_run("commit_reader")
    plate.refresh_from_db()
    layout[-1]["control"] = "Second corrected reference"
    entities.update(analyst, plate, {"assignments": layout}, expected_revision=plate.revision)
    with pytest.raises(results.Rejected, match="revision"):
        apply(*old_commit)
    with pytest.raises(ValueError, match="current layout"):
        reader_run("commit_reader")
    assert not Entity.objects.filter(type="iki/plate_assays.WellMeasurement").exists()
    apply(*reader_run("preview_reader"))
    one, two = reader_run("commit_reader"), reader_run("commit_reader")
    barrier = Barrier(2, timeout=20)

    def compete(attempt: tuple[ActionRun, dict[str, Any]]) -> str:
        try:
            barrier.wait()
            apply(*attempt)
        except results.Rejected as refused:
            return refused.code
        finally:
            connection.close()
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, [one, two]))
    assert sorted(outcomes) == ["committed", "conflict"]
    lost = (one, two)[outcomes.index("conflict")]
    assert not RunEffect.objects.filter(run=lost[0]).exists()
    with pytest.raises(results.Rejected, match="revision"):
        apply(*lost)
    with pytest.raises(ValueError, match="already been committed"):
        reader_run("commit_reader")

    # Both the retained file and every joined measurement belong to the winning commit.
    read = Entity.objects.get(type="iki/plate_assays.PlateRead")
    measurements = list(Entity.objects.filter(type="iki/plate_assays.WellMeasurement"))
    assert len(measurements) == 10
    values = entities.current_values_of([read])[read.pk]
    assert set(map(str, values["measurements"])) == {str(m.pk) for m in measurements}
    assert str(values["reader_file"]) == str(reader.pk)
    by_well = {v["well"]: v for v in entities.current_values_of(measurements).values()}
    assert str(by_well["A3"]["sample"]) == str(sample.pk)
    assert by_well["A3"]["concentration"] == 4
    assert by_well["A3"]["concentration_unit"] == "nM"
    assert by_well["A9"]["concentration"] is None
    assert (
        Lineage.objects.filter(
            entity__in=measurements, parent=plate, parent_revision=values["plate_revision"]
        ).count()
        == 10
    )
    plate.refresh_from_db()
    layout[-1]["control"] = "Changed after import"
    with pytest.raises(entities.Invalid, match="corrected layout"):
        entities.update(analyst, plate, {"assignments": layout}, expected_revision=plate.revision)

    fit, doc = start("fit_curve", {"read": [read.pk], "sample": [sample.pk]})
    apply(fit, assay.fit_output(doc))
    result = Entity.objects.get(type="iki/plate_assays.ResearchAssayResult")
    stored = entities.current_values_of([result])[result.pk]
    assert stored["fit_status"] == "fitted" and stored["fit_reason"] is None
    assert stored["point_count"] == len(stored["measurements"]) == 8
    assert stored["ci_low"] < stored["endpoint_value"] < stored["ci_high"]
