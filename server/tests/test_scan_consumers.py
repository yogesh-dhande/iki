"""Malware holds at byte readers that handle workbooks and temporary uploads."""

from collections.abc import Callable
from datetime import timedelta
from unittest.mock import patch

import pytest
from api_client import problem
from django.test import override_settings
from django.utils import timezone
from org_world import World
from test_api_entities import api_as
from test_run_checkpoints import interrupted, leased
from test_runs import world
from test_workers import keep_connection, queued

from iki_server.bundles import applying, imports
from iki_server.bundles.models import BundleImport, ImportState
from iki_server.files import services as files
from iki_server.files.models import Blob, File, Upload, UploadKind, UploadState
from iki_server.files.scan_state import record_result, release
from iki_server.files.scanner import ScanResult
from iki_server.runs import checkpoints
from iki_server.runs.models import Job, RunArtifact, RunCheckpoint
from iki_server.spreadsheets import services as sheets
from iki_server.spreadsheets.models import SpreadsheetImport
from iki_server.tasks.models import TaskRecord

pytestmark = pytest.mark.django_db
__all__ = ["keep_connection", "world"]


def received(world: World, kind: UploadKind) -> Upload:
    return Upload.objects.create(
        project=world.project,
        created_by=world.users["editor"],
        name="input",
        media_type="application/octet-stream",
        sha256="a" * 64,
        size=1,
        part_size=1,
        expires_at=timezone.now() + timedelta(days=1),
        kind=kind,
        state=UploadState.READY,
    )


def quarantine(source: Blob | Upload) -> None:
    record_result(
        source, state="infected", scanner="test", signature_version="1", reason="test finding"
    )


def test_an_artifacts_metadata_explains_why_its_content_is_held(world: World) -> None:
    run = queued(world)
    job = Job.objects.get(run_id=run["id"])
    upload = received(world, UploadKind.ARTIFACT)
    artifact = RunArtifact.objects.create(
        run_id=run["id"], job=job, upload=upload, path="result", expires_at=upload.expires_at
    )
    viewer = api_as(world, "viewer")
    path = f"/runs/{run['hrid']}/artifacts"
    assert viewer.get(path).json()[0]["state"] == "scanning"
    problem(viewer.get(f"{path}/{artifact.pk}/content"), 409, "file_scanning")
    quarantine(upload)
    shown = viewer.get(path).json()[0]
    assert shown["state"] == "quarantined" and not shown["scan"]["usable"]
    problem(viewer.get(f"{path}/{artifact.pk}/content"), 409, "file_quarantined")
    problem(api_as(world, "outsider").get(f"{path}/{artifact.pk}/content"), 404, "not_found")


@override_settings(IKI_FILES_BUCKET="test")
def test_a_held_checkpoint_does_not_replace_the_newest_usable_one(world: World) -> None:
    pod, run, first = leased(world)
    job = Job.objects.get(pk=first["job"]["id"])
    good = received(world, UploadKind.CHECKPOINT)
    record_result(good, state="clean", scanner="test", signature_version="1")
    older = RunCheckpoint.objects.create(job=job, run_id=run["id"], upload=good)
    bad = received(world, UploadKind.CHECKPOINT)
    quarantine(bad)
    newer = RunCheckpoint.objects.create(job=job, run_id=run["id"], upload=bad)
    checkpoints.supersede(job.run)
    assert checkpoints.delete_stale() == 0
    older.refresh_from_db()
    assert older.deleted_at is None
    second = interrupted(pod, first)
    assert second["job"]["checkpoint"]["saved"]["id"] == str(older.pk)
    for action in ("download", "restored"):
        problem(pod.report(second, f"checkpoints/{newer.pk}/{action}"), 409, "file_quarantined")


@override_settings(IKI_FILES_BUCKET="test")
def test_a_workbook_is_checked_before_staging_or_opening_it(world: World) -> None:
    blob = Blob.objects.create(sha256="b" * 64, size=1, storage_uri="s3://test/held")
    file = File.objects.create(
        blob=blob, project=world.project, name="sample.csv", media_type="text/csv"
    )
    wanted = {"file": str(file.pk), "type": "acme/protein.Sample"}
    url = f"/projects/{world.project.pk}/spreadsheet-imports"
    problem(api_as(world).post(url, wanted), 409, "file_scanning")
    quarantine(blob)
    problem(api_as(world).post(url, wanted), 409, "file_quarantined")
    # A workbook that was already queued when scanning was enabled gets the same check.
    pending = SpreadsheetImport.objects.create(
        file=file, project=world.project, requested_by=world.users["editor"], type=wanted["type"]
    )
    assert sheets.read(str(pending.pk)) == "unreadable"
    pending.refresh_from_db()
    assert "quarantined" in pending.error and pending.sheets == []


@pytest.mark.parametrize(
    ("state", "task"),
    [
        (ImportState.PLANNING, imports.plan_import.func),
        (ImportState.APPLYING, applying.apply_import.func),
    ],
)
@pytest.mark.parametrize("infected", [False, True])
@override_settings(IKI_FILES_BUCKET="test")
def test_an_in_flight_archive_waits_until_the_backfill_or_release_resumes_it(
    world: World, state: str, task: Callable[[str], str], infected: bool
) -> None:
    upload = received(world, UploadKind.BUNDLE)
    made = BundleImport.objects.create(
        project=world.project,
        requested_by=world.users["admin"],
        applied_by=world.users["admin"],
        upload=upload,
        state=state,
        size=upload.size,
        sha256=upload.sha256,
        planned_at=timezone.now() if state == ImportState.APPLYING else None,
    )
    args = [str(made.pk)]
    if made.planned_at is not None:
        args.append(made.planned_at.isoformat())
    scheduled = TaskRecord.objects.filter(args=args)
    assert task(str(made.pk)) == "scanning"
    assert not scheduled.exists()
    outcome = (
        ScanResult("infected", "test", "1", "test finding")
        if infected
        else ScanResult("clean", "test", "1")
    )
    with patch("iki_server.files.scanner.scan_object", return_value=outcome):
        decision = files.scan_source(upload)
    if infected:
        assert task(str(made.pk)) == "quarantined"
        assert not scheduled.exists()
        assert decision.decision_id is not None
        release(decision.decision_id, actor=world.users["quarantine_admin"], reason="Reviewed")
    made.refresh_from_db()
    assert made.state == state and made.error == ""
    resumed = scheduled.get()
    expected = imports.plan_import if state == ImportState.PLANNING else applying.apply_import
    assert resumed.task_path == expected.module_path
