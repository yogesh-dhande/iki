"""Legacy content is held during backfill and a quarantined bundle member cannot be applied."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from django.test import override_settings
from org_world import World
from test_bundle_imports import applied, bundle_of, run_tasks, upload, world
from test_bundles import stored
from test_file_scan_ingestion import EICAR

from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.files import scan_recovery, scan_state, scanner, storage
from iki_server.files.models import Blob, File
from iki_server.tasks.models import TaskRecord

pytestmark = pytest.mark.django_db
__all__ = ["world"]


def test_maintenance_backfills_untested_blobs_without_duplicate_tasks(s3: Any) -> None:
    data = b"existing content"
    sha = hashlib.sha256(data).hexdigest()
    key = storage.blob_key(sha)
    storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=data)
    blob = Blob.objects.create(sha256=sha, size=len(data), storage_uri=storage.uri(key))
    assert not scan_state.status_for(blob).usable
    assert scan_recovery.recover() == 1
    assert scan_recovery.recover() == 0
    assert (
        TaskRecord.objects.filter(task_path=scan_recovery.scan_existing_blob.module_path).count()
        == 1
    )
    run_tasks()
    assert scan_state.status_for(blob).state == "clean"
    assert scan_recovery.recover() == 0


def test_backfill_continues_beyond_one_batch_without_waiting_for_maintenance(s3: Any) -> None:
    blobs = []
    for data in (b"first", b"second", b"third"):
        sha = hashlib.sha256(data).hexdigest()
        key = storage.blob_key(sha)
        storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=data)
        blobs.append(Blob.objects.create(sha256=sha, size=len(data), storage_uri=storage.uri(key)))
    assert scan_recovery.recover(limit=1) == 1
    run_tasks()
    assert all(scan_state.status_for(blob).usable for blob in blobs)
    assert scan_recovery.recover(limit=1) == 0


def member_scanner(*, stream: scanner.ScanStream, size: int, sha256: str) -> scanner.ScanResult:
    while stream.read(65536):
        pass
    if sha256 == hashlib.sha256(EICAR).hexdigest():
        return scanner.ScanResult("infected", "fixture", "42", "Eicar-Test-Signature")
    return scanner.ScanResult("clean", "fixture", "42")


@override_settings(IKI_FILES_SCANNER="test_scan_recovery.member_scanner")
def test_an_import_keeps_a_members_finding_when_no_record_can_be_written(
    world: World, tmp_path: Path
) -> None:
    def make() -> list[Any]:
        f = stored(world.project, EICAR, "structure.pdb")
        return [
            services.create(
                world.users["admin"],
                "acme/protein.ProteinStructure",
                world.project,
                {"structure": str(f.pk), "method": "xray"},
            ).entity
        ]

    archive = bundle_of(world, tmp_path, make)
    api = Api(world.browser("admin"))
    made = upload(api, world.project, archive)
    assert made["state"] == "planned"
    result = applied(api, made)
    assert result["state"] == "failed"
    assert "quarantined" in result["error"]
    blob = Blob.objects.get(sha256=hashlib.sha256(EICAR).hexdigest())
    assert scan_state.status_for(blob).state == "infected"
    assert not File.objects.filter(blob=blob).exists()
    assert AuditEvent.objects.filter(action="file.quarantined").exists()
