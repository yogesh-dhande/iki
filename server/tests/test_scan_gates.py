"""Scan decisions at the points where people, workers and bundles use stored files."""

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World
from test_api_entities import api_as, create
from test_file_fields import STRUCTURE
from test_run_imports import IMPORT, errors
from test_runs import launch, world
from test_workers import keep_connection, queued, worker

from iki_server.audit.models import AuditEvent
from iki_server.bundles import export
from iki_server.bundles.models import BundleExport, ExportState
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import scan_state
from iki_server.files.models import Blob, File
from iki_server.runs.models import ActionRun, Job

__all__ = ["keep_connection", "world"]

pytestmark = pytest.mark.django_db


def _file(world: World, name: str = "structure.pdb", *, registered: bool = False) -> File:
    """Persist the metadata an older deployment could have written without scan evidence."""
    data = name.encode()
    blob = Blob.objects.create(
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        storage_uri=f"s3://instrument/{name}",
        registered_external=registered,
        object_version="v1" if registered else "",
    )
    return File.objects.create(
        blob=blob,
        project=world.project,
        name=name,
        created_by=world.users["editor"],
        media_type="chemical/seq-aa-fasta" if name.endswith(".fasta") else "chemical/x-pdb",
    )


def _infected(file: File) -> None:
    scan_state.record_result(
        file.blob,
        state="infected",
        scanner="test",
        signature_version="1",
        reason="Eicar-Test-Signature",
    )


def _legacy_lot(world: World, file: File) -> Entity:
    # Older records could already have a reference before their first scan.
    with patch.object(scan_state.ScanStatus, "require_usable"):
        saved = services.create(
            world.users["editor"],
            "acme/protein.Lot",
            world.project,
            {"label": "L1", "report": file.pk},
        )
    return Entity.objects.get(pk=saved.entity.pk)


@pytest.mark.parametrize(
    ("infected", "code"), [(False, "file_scanning"), (True, "file_quarantined")]
)
def test_authorized_readers_see_status_without_getting_a_content_url(
    world: World, infected: bool, code: str
) -> None:
    file = _file(world)
    if infected:
        _infected(file)
    path = f"/files/{file.pk}"
    reader = api_as(world, "viewer")
    detail = reader.get(path)
    assert detail.status_code == 200, detail.content
    scan = detail.json()["scan"]
    assert scan["state"] == ("infected" if infected else "pending")
    assert scan["usable"] is False
    for ending in ("content", "content-url"):
        url = f"{path}/{ending}"
        response = reader.get(url)
        problem(response, 409, code)
        assert "Location" not in response and "url" not in response.json()
        problem(api_as(world, "outsider").get(url), 404, "not_found")


@pytest.mark.parametrize("infected", [False, True])
def test_fields_and_run_parameters_require_a_usable_file(world: World, infected: bool) -> None:
    pdb, fasta = _file(world), _file(world, "proteins.fasta")
    if infected:
        _infected(pdb)
        _infected(fasta)
    editor = api_as(world)
    field = create(editor, world.project, {"structure": str(pdb.pk)}, STRUCTURE)
    param = launch(editor, world.project, {}, IMPORT, params={"file": str(fasta.pk)})
    explanation = "quarantin" if infected else "scan"
    assert explanation in errors(field)["#/values/structure"].lower()
    assert explanation in errors(param)["#/params/file"].lower()
    assert not ActionRun.objects.exists()


@pytest.mark.parametrize("infected", [False, True])
def test_a_lease_waits_for_files_in_the_pinned_included_record(
    world: World, infected: bool
) -> None:
    file = _file(world)
    lot = _legacy_lot(world, file)
    sample = services.create(
        world.users["editor"],
        "acme/protein.Sample",
        world.project,
        {"label": "S1", "lot": lot.pk},
    ).entity
    editor = api_as(world)
    inputs = {"sample": str(sample.pk)}
    action = "acme/protein.measure_purity"
    denied = launch(editor, world.project, inputs, action)
    assert "scann" in problem(denied, 409, "refused")["detail"].lower()
    # The existing queue entry models a job accepted before the scan gate was installed.
    with patch.object(scan_state.ScanStatus, "require_usable"):
        run = launch(editor, world.project, inputs, action)
    assert run.status_code == 201, run.content
    services.update(world.users["editor"], lot, {"report": None}, expected_revision=1)
    pod = worker(world)
    assert pod.lease().status_code == 204
    waiting = Job.objects.get(run_id=run.json()["id"])
    assert waiting.state == "queued" and waiting.queued_at > timezone.now()
    assert "scann" in waiting.progress_message.lower()
    Job.objects.filter(pk=waiting.pk).update(queued_at=timezone.now())
    if infected:
        _infected(file)
        assert pod.lease().status_code == 204
        ended = ActionRun.objects.get(pk=run.json()["id"])
        assert ended.status == "rejected" and ended.error is not None
        assert "quarantin" in ended.error["message"].lower()
    else:
        scan_state.record_result(file.blob, state="clean", scanner="test", signature_version="1")
        leased = pod.lease()
        assert leased.status_code == 200, leased.content
        assert list(leased.json()["files"]) == [str(file.pk)]


def test_an_already_leased_job_cannot_get_a_url_for_held_input(world: World, settings: Any) -> None:
    settings.IKI_FILES_BUCKET = "iki"
    queued(world)
    pod = worker(world)
    job = pod.lease().json()
    file = _file(world)
    Job.objects.filter(pk=job["job"]["id"]).update(input_files=[file.pk])
    problem(pod.report(job, f"files/{file.pk}"), 409, "file_scanning")
    _infected(file)
    problem(pod.report(job, f"files/{file.pk}"), 409, "file_quarantined")


@pytest.mark.parametrize("registered", [False, True])
def test_a_bundle_refuses_a_held_file_in_earlier_history(
    world: World, registered: bool, tmp_path: Path
) -> None:
    file = _file(world, registered=registered)
    lot = _legacy_lot(world, file)
    services.update(world.users["editor"], lot, {"report": None}, expected_revision=1)
    _infected(file)
    path = tmp_path / "lot.tar.gz"
    with pytest.raises(export.ExportError, match=r"(?i)quarantin"):
        export.write(
            world.project, [lot], path, by=world.users["admin"], reason="Review", selection="Lot"
        )
    assert not path.exists()
    assert not AuditEvent.objects.filter(action="bundle.exported").exists()


def test_a_ready_archive_created_before_scanning_requires_a_fresh_export(
    world: World, settings: Any
) -> None:
    settings.IKI_FILES_BUCKET = "iki"
    archive = BundleExport.objects.create(
        project=world.project,
        requested_by=world.users["admin"],
        reason="Review",
        selection="Lot",
        picked=[],
        references=[],
        state=ExportState.READY,
        sha256="1" * 64,
    )
    result = api_as(world, "admin").get(f"/bundles/{archive.pk}/content")
    assert "fresh export" in problem(result, 409, "refused")["detail"].lower()
    assert not AuditEvent.objects.filter(action="bundle.downloaded").exists()
