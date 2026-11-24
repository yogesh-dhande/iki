"""Run both dose-response starters against their installed plugin tables and worker commands."""

import hashlib
import importlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from api_client import Api
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import ready, signed

from iki_server.audit.models import AuditEvent
from iki_server.entities import access, requests, services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job
from iki_server.setup import dose_response, starters
from iki_server.signatures.services import verify
from iki_server.storage import receipts
from iki_spec.compute import Binding

__all__ = ["database"]
PLUGIN = PLUGINS / "plate_assays"
PREFIX = "iki/plate_assays."


def test_both_flows_deliver_a_signed_fit_from_the_starter(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    (build / "iki_plugin_iki__plate_assays" / "settings.json").write_text(
        '{"review_flow":"research"}'
    )
    migrate(build, database)
    sync(build, database)
    result = django(
        build,
        database,
        "-c",
        "import django; django.setup()\n"
        "from test_dose_response_starter import exercise\nexercise()",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def exercise() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    env = PREFIX + "assay-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    # Store the real bytes under the content hash as the bucket would. The reader
    # command below reads only what the server stored for the File.
    with TemporaryDirectory() as tmp, override_settings(IKI_FILES_BUCKET="test"):
        bucket = Path(tmp)

        def exists(key: str) -> bool:
            return (bucket / Path(key).name).exists()

        def upload(source: Path, key: str, media_type: str) -> None:
            (bucket / Path(key).name).write_bytes(source.read_bytes())

        with (
            patch.object(storage, "exists", side_effect=exists),
            patch.object(storage, "upload_file", side_effect=upload),
        ):
            for flow in ("research", "regulated_qc"):
                world = build_world(flow)
                ready(world)
                _run_example(world, flow, bucket)


def _run_example(world: World, flow: str, bucket: Path) -> None:
    project = world.project
    admin = world.users["admin"]
    asker = Project.objects.create(team=world.team, name="Requesting group")
    # The admin sets up the example. A separate member of the requester can read only
    # what the signed release returns across the project boundary.
    requester = world.users["outsider"]
    ProjectMembership.objects.create(project=asker, user=admin, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=asker, user=requester, role=ProjectRole.VIEWER)
    roles = {
        "iki/example_lims.coordinator": [project.memberships.get(user=admin).pk],
        PREFIX + "analyst": [project.memberships.get(user=world.users["editor"]).pk],
        PREFIX + "reviewer": [project.memberships.get(user=world.users["runner"]).pk],
    }
    if flow == "regulated_qc":
        roles[PREFIX + "qa"] = [project.memberships.get(user=world.users["viewer"]).pk]

    api = Api(world.browser("admin"))
    listed = api.get("/starters", project=str(project.pk))
    assert listed.status_code == 200, listed.content
    [starter] = [s for s in listed.json() if s["id"] == "dose-response"]
    body = {
        "version": starter["version"],
        "project": str(project.pk),
        "requester_project": str(asker.pk),
        "flow": flow,
        "role_assignments": {role: list(map(str, members)) for role, members in roles.items()},
    }
    with patch.object(storage, "exists", side_effect=AssertionError("Preview accessed storage")):
        preview = api.post("/starters/dose-response/preview", body)
    assert preview.status_code == 200, preview.content
    assert preview.json()["changed"] and preview.json()["examples_to_create"]
    with (
        patch.object(storage, "exists", side_effect=OSError("unavailable")),
        pytest.raises(OSError, match="unavailable"),
    ):
        starters.apply(
            project,
            "dose-response",
            starter["version"],
            flow,
            roles,
            requester_project=asker,
            by=admin,
        )
    assert not Entity.objects.filter(owner_project__in=[project, asker]).exists()
    assert not requests.accepts(project, asker, dose_response.REQUEST_TYPE)
    applied = api.post("/starters/dose-response/apply", body)
    assert applied.status_code == 200, applied.content
    assert applied.json()["changed"]
    ids = applied.json()["examples"]
    replay = api.post("/starters/dose-response/apply", body)
    assert replay.status_code == 200, replay.content
    assert not replay.json()["changed"] and replay.json()["examples"] == ids
    source = File.objects.select_related("blob").get(pk=ids["reader_file"], project=project)
    csv = bucket / source.blob.sha256
    assert csv.read_bytes() == (PLUGIN / "tests/data/mars-96-series.csv").read_bytes()
    assert hashlib.sha256(csv.read_bytes()).hexdigest() == source.blob.sha256
    request = Entity.objects.get(pk=ids["request"], owner_project=asker)
    assert requests.accepts(project, asker, request.type)
    other = Project.objects.create(team=world.team, name="Another asking project")
    ProjectMembership.objects.create(project=other, user=admin, role=ProjectRole.EDITOR)
    with pytest.raises(Refused, match="intake"):
        dose_response.check_example(project, other, by=admin)
    assert not requests.accepts(project, other, request.type)
    receipt = receipts.start(admin, request)
    tube = Entity.objects.get(pk=ids["tube"])
    scanned = receipts.scan(admin, receipt, tube.hrid)
    assert scanned.found == "matched"
    receipt = scanned.receipt
    receipt = receipts.describe(
        admin, receipt, handed_over_by="Synthetic shipment", plan_revision=receipt.plan_revision
    )
    assert (
        receipts.complete(admin, receipt, plan_revision=receipt.plan_revision).received_by_id
        == admin.pk
    )

    plate = UUID(ids["plate"])
    sample = UUID(ids["sample"])
    _worker(world, "preview_reader", {"plate": [plate]}, {}, csv)
    _worker(world, "commit_reader", {"plate": [plate]}, {}, csv)
    read = Entity.objects.get(owner_project=project, type=PREFIX + "PlateRead")
    _worker(
        world,
        "fit_curve",
        {"read": [read.pk], "sample": [sample]},
        {"endpoint_kind": "IC50", "exclusions": "A4: Simulated dispensing error"},
        csv,
    )
    name = "RequestedResearchAssayResult" if flow == "research" else "RequestedQCAssayResult"
    result = Entity.objects.get(owner_project=project, type=PREFIX + name)
    values = services.current_values_of([result])[result.pk]
    assert str(values["request"]) == ids["request"]
    assert values["review_flow"] == flow and values["point_count"] == 7
    assert 11 < values["endpoint_value"] < 13 and values["concentration_unit"] == "nM"
    (excluded,) = [p for p in values["points"] if p["exclusion_reason"]]
    assert excluded["well"] == "A4" and excluded["excluded_by"] == str(world.users["editor"].pk)
    assert excluded["exclusion_reason"] == "Simulated dispensing error"
    steps = (
        [("runner", "signed")]
        if flow == "research"
        else [("editor", "submitted"), ("runner", "reviewed"), ("viewer", "released")]
    )
    for signer, to in steps:
        assert not access.visible(requester).filter(pk=result.pk).exists()
        proof = signed(world, signer, str(result.pk), to, field="review").signature
        assert proof is not None and all(check.ok for check in verify(proof))
        assert json.loads(proof.content)["values"]["points"] == values["points"]
    result.refresh_from_db()
    assert access.visible(requester).filter(pk=result.pk).exists()
    released = AuditEvent.objects.get(
        action="request.result_released", project=asker, target_id=str(result.pk)
    )
    assert released.data["released_revision"] == result.revision
    assert released.data["request"] == str(request.pk)


def _worker(
    world: World, name: str, inputs: dict[str, list[UUID]], params: dict[str, Any], csv: Path
) -> None:
    action = catalog.action(PREFIX + name)
    assert action is not None
    run = runs.launch(world.users["editor"], action, world.project, inputs, params)
    job = Job.objects.get(run=run)
    job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
    doc = queue.payload(job, run)
    assays = importlib.import_module("assays")
    if name == "fit_curve":
        output = assays.fit_output(doc)
    else:
        (reader,) = doc["files"].values()
        reader["path"] = str(csv)
        output = assays.reader_output(doc, commit=name == "commit_reader")
    with transaction.atomic():
        results.commit(ActionRun.objects.select_for_update().get(pk=run.pk), output)
