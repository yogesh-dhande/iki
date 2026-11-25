"""Format recognition, scan decisions and access to the verified bytes of each upload."""

import hashlib
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings
from org_world import World
from test_file_acquired import realistic_ab1
from test_files import PDB, add, api_as, run_tasks, send, start
from test_registrations import lab, put, register
from test_run_artifacts import aligning
from test_runs import world
from test_workers import keep_connection

from iki_server.bundles import imports
from iki_server.bundles.models import ImportState
from iki_server.entities import registry
from iki_server.files import acquired, scan_state, scanner, services, storage
from iki_server.files.models import Blob, BlobScan, File
from iki_server.org.models import OrgRole, OrgRoleAssignment

pytestmark = pytest.mark.django_db
__all__ = ["keep_connection", "lab", "world"]

# The harmless industry-standard antivirus test string, split so its source is ordinary text.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
FLAGGED_TRACE = b"ABIF" + bytes(8192)


def inspect(*, stream: scanner.ScanStream, sha256: str, size: int) -> scanner.ScanResult:
    data = stream.read(size)
    if data == FLAGGED_TRACE:
        return scanner.ScanResult("infected", "test scanner", "42", "Test.Trace")
    assert data == EICAR
    return scanner.ScanResult("infected", "test scanner", "42", "Eicar-Test-Signature")


@override_settings(IKI_FILES_SCANNER="test_file_scan_ingestion.inspect")
def test_held_aliases_share_one_scan_and_never_reach_acquisition(
    world: World, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_parse(*args: Any) -> None:
        pytest.fail("format parser received held bytes")

    read_range = storage.read_range
    limit = registry.file_formats().signature_bytes

    def read_signature(uri: str, version: str, offset: int, length: int) -> bytes:
        assert offset == 0 and 0 < length <= limit
        return read_range(uri, version, offset, length)

    monkeypatch.setattr(storage, "read_range", read_signature)
    monkeypatch.setattr(acquired, "read_metadata", cannot_parse)
    api = api_as(world)
    file = add(
        api,
        world,
        FLAGGED_TRACE,
        name="download",
        media_type="application/octet-stream",
        acquired_at="2026-03-14",
    )["file"]
    alias = add(api, world, FLAGGED_TRACE, name="renamed", media_type="application/octet-stream")[
        "file"
    ]
    assert alias["id"] != file["id"]
    assert file["media_type"] == alias["media_type"] == "application/x-abi"
    assert file["acquired"]["source"] == "given" and alias["acquired"] is None
    assert BlobScan.objects.count() == 1
    assert file["scan"]["state"] == "infected"
    assert file["scan"]["usable"] is False
    assert alias["scan"] == file["scan"]
    for suffix in ("content", "content-url"):
        response = api.get(f"/files/{file['id']}/{suffix}")
        assert response.status_code == 409
        assert response.json()["code"] == "file_quarantined"


def test_scanner_outage_keeps_verified_bytes_pending_until_retry(
    world: World, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = api_as(world)
    trace = realistic_ab1()
    upload = start(api, world, trace, name="download", media_type="application/octet-stream").json()
    send(upload, trace)
    path = f"/uploads/{upload['id']}"
    assert api.post(f"{path}/complete").status_code == 202

    def offline(**kwargs: Any) -> scanner.ScanResult:
        raise scanner.ScannerError("temporarily unavailable")

    # A worker returns its DB connection to the pool. A failed attempt must drop the
    # session lock too, even though this runner will retry on the very same connection.
    def locks_in_this_session() -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()"
            )
            row = cursor.fetchone()
            assert row is not None
            return int(row[0])

    locks_before = locks_in_this_session()
    with monkeypatch.context() as patch:
        patch.setattr(scanner, "scan_object", offline)
        run_tasks()
    assert locks_in_this_session() == locks_before
    assert api.get(path).json()["state"] == "scanning"
    assert not File.objects.exists()
    assert scan_state.status_for(Blob.objects.get()).state == "pending"
    # The bucket can expire staging during a prolonged outage. The verified object remains.
    staging = storage.staging_key(upload["id"])
    storage.delete(staging)
    assert not storage.exists(staging)
    assert services.check(upload["id"]) == "ready"
    done = api.get(path).json()["file"]
    assert done["scan"]["usable"] and done["scan"]["state"] == "clean"
    assert done["media_type"] == "application/x-abi" and done["acquired"]["source"] == "abif"
    assert api.get(f"/files/{done['id']}/content").status_code == 302


@override_settings(IKI_FILES_SCANNER="test_file_scan_ingestion.inspect")
def test_registered_file_scans_the_pinned_version(world: World, lab: dict[str, Any]) -> None:
    api = api_as(world)
    uri, version = put(lab, "instrument.pdb", EICAR)
    registration = register(api, world, uri).json()
    put(lab, "instrument.pdb", PDB)
    # A later change to where new files may be registered preserves this pinned source.
    with override_settings(IKI_FILES_REGISTER_LOCATIONS=[]):
        run_tasks()
    file = api.get(f"/registrations/{registration['id']}").json()["file"]
    assert file["scan"]["state"] == "infected"
    assert Blob.objects.get().object_version == version
    assert api.get(f"/files/{file['id']}/content").status_code == 409


@pytest.mark.parametrize("policy", ["allow", "refuse"])
def test_oversize_policy_is_visible_on_the_file(
    world: World, s3: Any, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_open(*args: Any) -> None:
        pytest.fail("opened an oversized object for a scan")

    monkeypatch.setattr(storage, "open_object", cannot_open)
    api = api_as(world)
    with override_settings(IKI_FILES_SCAN_MAX_BYTES=1, IKI_FILES_SCAN_OVERSIZE_POLICY=policy):
        file = add(api, world, PDB)["file"]
    assert file["scan"]["state"] == "not_scanned"
    assert file["scan"]["reason"] == "too_large"
    assert file["scan"]["usable"] == (policy == "allow")
    assert api.get(f"/files/{file['id']}/content").status_code == (
        302 if policy == "allow" else 409
    )


@pytest.mark.parametrize("endpoint", ["uploads", "artifacts"])
@override_settings(IKI_FILES_SCANNER="test_file_scan_ingestion.inspect")
def test_workers_receive_a_hold_instead_of_a_usable_result_or_artifact(
    world: World, s3: Any, endpoint: str
) -> None:
    pod, _, job = aligning(world)
    named = {"path": "alignment.txt"} if endpoint == "artifacts" else {"name": "alignment.txt"}
    started = pod.report(
        job,
        endpoint,
        **named,
        media_type="text/plain",
        size=len(EICAR),
        sha256=hashlib.sha256(EICAR).hexdigest(),
    )
    assert started.status_code == 201, started.content
    upload = started.json()
    send(upload, EICAR)
    path = f"uploads/{upload['id']}/complete"
    assert pod.report(job, path).json()["state"] == "checking"
    run_tasks()
    result = pod.report(job, path).json()
    assert result["state"] == "failed" and result["file"] is None
    assert "quarantined" in result["error"]
    decision = BlobScan.objects.get()
    assert decision.state == "infected"
    assert (decision.upload_id is not None) == (endpoint == "artifacts")


@override_settings(IKI_FILES_SCANNER="test_file_scan_ingestion.inspect")
def test_a_held_bundle_is_not_opened_until_its_finding_is_released(world: World, s3: Any) -> None:
    admin = world.users["admin"]
    OrgRoleAssignment.objects.create(user=admin, role=OrgRole.ORG_ADMIN)
    made = imports.start(
        admin,
        world.project,
        name="interlab.tar.gz",
        size=len(EICAR),
        sha256=hashlib.sha256(EICAR).hexdigest(),
    )
    send({"parts": services.parts(made.upload)}, EICAR)
    services.complete(made.upload)
    run_tasks()
    made.refresh_from_db()
    assert made.state == ImportState.UPLOADING
    found = BlobScan.objects.get(upload=made.upload)
    assert found.state == "infected"
    scan_state.release(
        found.pk, actor=world.users["quarantine_admin"], reason="Confirmed test material."
    )
    made.refresh_from_db()
    assert made.state == ImportState.PLANNING


@pytest.mark.parametrize(
    ("backend", "uri", "version"),
    [
        ("s3", "s3://instruments/example", ""),
        ("s3", "s3://instruments/example", "null"),
        ("s3", "s3://instruments/example", "  "),
        ("s3", "s3://instruments/example", " null "),
        ("s3", "https://instruments/example", "pinned"),
        ("s3", "gs://instruments/example", "pinned"),
        ("s3", "s3://instruments/a/../example", "pinned"),
        ("gcs", "gs://instruments/example", "no-generation"),
    ],
)
@override_settings(IKI_FILES_SCAN_MAX_BYTES=1, IKI_FILES_SCAN_OVERSIZE_POLICY="allow")
def test_an_in_place_scan_requires_an_immutable_version(
    backend: str, uri: str, version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = Blob.objects.create(
        registered_external=True,
        storage_uri=uri,
        object_version=version,
        sha256=hashlib.sha256(PDB).hexdigest(),
        size=len(PDB),
    )

    def cannot_scan(**kwargs: Any) -> scanner.ScanResult:
        pytest.fail("attempted to scan a mutable source")

    monkeypatch.setattr(scanner, "scan_object", cannot_scan)
    with override_settings(IKI_FILES_BACKEND=backend):
        result = services.scan_source(blob)
    assert not result.usable and result.state == "not_scanned"
    assert result.reason == "unsupported_source"
