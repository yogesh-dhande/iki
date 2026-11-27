"""Configure a minimal plate installation and run the explicit Tox21 tutorial in each flow."""

import csv
import hashlib
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4
from wsgiref.simple_server import make_server

import pytest
from api_client import Api
from django.conf import settings
from django.core.wsgi import get_wsgi_application
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from examples.dose_response import public_plate, tutorial
from examples.dose_response.__main__ import SdkApi
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from test_files import run_tasks
from test_oauth import signed_in_token
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import ready, signed

from iki import Client
from iki_server.accounts.models import User
from iki_server.amounts import services as amounts
from iki_server.amounts.models import Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import access, requests, services, states
from iki_server.entities.models import Entity, Intake
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import (
    LabRoleGrant,
    Project,
    ProjectMembership,
    ProjectRole,
    TeamMembership,
)
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job
from iki_server.setup import starters
from iki_server.setup.models import StarterApplication
from iki_server.signatures.services import verify
from iki_server.storage import receipts
from iki_spec.compute import Binding

__all__ = ["database"]
PLUGIN = PLUGINS / "plate_assays"
PREFIX = "iki/plate_assays."


def test_tutorial_plate_preserves_the_deposited_activity_values() -> None:
    # Independent comparison of all 72 cells with the four deposited SID rows.
    assert hashlib.sha256(public_plate.READER_FILE.read_bytes()).hexdigest() == (
        "83ae1ad7d01ea9b9cc1de1dfaf65c4ccba18f3a9477d60421eb90768dfd9115e"
    )
    with public_plate.READER_FILE.open() as source:
        title, columns, *rows = csv.reader(source)
    assert title == ["Normalized activity (%)"]
    assert columns == ["", *map(str, range(1, 13))]
    assert [row[0] for row in rows] == list("ABCDEF")
    assert sum(len(row) - 1 for row in rows) == 72
    # AID 1224835, SID 144208740, replicate 1 at 0.017 and 115.2 µM.
    assert (rows[0][1], rows[0][11]) == ("1.5711", "-81.0799")
    assert (
        public_plate.READER_FILE.read_bytes()
        == (PLUGIN / "tests/data" / public_plate.READER_FILE.name).read_bytes()
    )


def _installed_test(
    tmp_path: Path,
    database: str,
    scenario: str,
    *plugins: Path,
    file_settings: dict[str, str] | None = None,
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN, *plugins), "--output", build, cwd=PLUGINS)
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
        "from django.test import override_settings\n"
        f"from test_dose_response_starter import {scenario}\n"
        f"with override_settings(**{file_settings or {}!r}): {scenario}()",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_configure_the_assay_with_only_its_declared_dependencies(
    tmp_path: Path, database: str
) -> None:
    _installed_test(tmp_path, database, "configure_empty_project")


def configure_empty_project() -> None:
    world = build_world()
    project, admin = world.project, world.users["admin"]
    project.allow_sharing = False
    project.save(update_fields=["allow_sharing"])
    api = Api(world.browser("admin"))
    listed = api.get("/starters", project=str(project.pk))
    assert listed.status_code == 200, listed.content
    [recipe] = [s for s in listed.json() if s["id"] == "dose-response"]
    assert recipe["version"] == "2.0.0"
    roles = {
        PREFIX + "analyst": [project.memberships.get(user=world.users["editor"]).pk],
        PREFIX + "reviewer": [project.memberships.get(user=world.users["runner"]).pk],
    }
    body = {
        "version": recipe["version"],
        "project": str(project.pk),
        "flow": "research",
        "role_assignments": {role: list(map(str, members)) for role, members in roles.items()},
    }

    def empty_assay() -> None:
        assert not Entity.objects.exists()
        assert not File.objects.exists()
        assert not Movement.objects.exists()
        assert not Intake.objects.exists()

    with override_settings(IKI_FILES_BUCKET=""):
        preview = api.post("/starters/dose-response/preview", body)
        assert preview.status_code == 200, preview.content
        assert preview.json()["changed"]
        assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
        empty_assay()
        # A save failure must undo the grants that the administrator just approved.
        with (
            patch.object(StarterApplication, "save", side_effect=RuntimeError("failed to save")),
            pytest.raises(RuntimeError, match="failed to save"),
        ):
            starters.apply(
                project,
                "dose-response",
                recipe["version"],
                "research",
                roles,
                by=admin,
                plan=preview.json()["plan"],
            )
        assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
        empty_assay()

        body["plan"] = preview.json()["plan"]
        applied = api.post("/starters/dose-response/apply", body)
        assert applied.status_code == 200, applied.content
        assert applied.json()["changed"]
        assert starters.effective_flow(project, "dose-response") == "research"
        assert LabRoleGrant.objects.count() == len(roles)
        empty_assay()
        replay = api.post("/starters/dose-response/apply", body)
        assert replay.status_code == 200, replay.content
        assert not replay.json()["changed"]
        assert AuditEvent.objects.filter(action="starter.applied", project=project).count() == 1
        empty_assay()


@pytest.mark.usefixtures("s3")
def test_explicit_tutorial_delivers_both_signed_fits_in_each_flow(
    tmp_path: Path, database: str
) -> None:
    _installed_test(
        tmp_path,
        database,
        "exercise",
        PLUGINS / "samples",
        PLUGINS / "protein",
        file_settings={
            "IKI_FILES_BUCKET": settings.IKI_FILES_BUCKET,
            "IKI_FILES_PREFIX": settings.IKI_FILES_PREFIX,
            "IKI_FILES_ENDPOINT_URL": settings.IKI_FILES_ENDPOINT_URL,
            "IKI_FILES_PUBLIC_ENDPOINT_URL": settings.IKI_FILES_PUBLIC_ENDPOINT_URL,
        },
    )


def exercise() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    env = PREFIX + "assay-tools"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    with TemporaryDirectory() as tmp, _tutorial_server() as url:
        for flow in ("research", "regulated_qc"):
            world = build_world(flow)
            ready(world)
            TeamMembership.objects.create(team=world.team, user=world.users["admin"])
            _, tokens = signed_in_token(world, "admin", scope="read write", projects=None)
            assert set(tokens["scope"].split()) == {"read", "write"}
            with (
                patch.dict(
                    os.environ,
                    {"IKI_URL": url, "IKI_TOKEN": tokens["access_token"], "IKI_CONFIG_DIR": tmp},
                ),
                Client(sleep=_poll_upload) as client,
            ):
                assert client.request("GET", f"/teams/{world.team.pk}")["id"] == str(world.team.pk)
                _run_example(world, flow, Path(tmp), SdkApi(client))


@contextmanager
def _tutorial_server() -> Iterator[str]:
    # The SDK connects to Iki here and PUTs signed multipart bytes to the test object store.
    with (
        override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1"], SECURE_SSL_REDIRECT=False),
        patch.dict(os.environ, {"OAUTHLIB_INSECURE_TRANSPORT": "1"}),
        make_server("127.0.0.1", 0, get_wsgi_application()) as server,
    ):
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def _poll_upload(_seconds: float) -> None:
    # A deployment's task runner verifies uploaded bytes before the SDK's next poll.
    assert run_tasks() > 0


def _run_example(world: World, flow: str, directory: Path, adapter: SdkApi) -> None:
    admin = world.users["admin"]
    state = directory / f"{flow}.sqlite3"

    def prepare(state: Path = state) -> dict[str, str]:
        return tutorial.prepare(
            adapter,
            state=state,
            team=str(world.team.pk),
            flow=flow,
            analyst=str(world.users["editor"].pk),
            reviewer=str(world.users["runner"].pk),
            qa=str(world.users["viewer"].pk) if flow == "regulated_qc" else None,
        )

    projects_before = set(Project.objects.values_list("pk", flat=True))
    with (
        patch.object(adapter, "upload", side_effect=OSError("bucket unavailable")),
        pytest.raises(OSError, match="bucket unavailable"),
    ):
        prepare()
    training = set(Project.objects.values_list("pk", flat=True)) - projects_before
    # The server can commit the draw even if the client loses its response.
    api_request = adapter.request

    def lose_draw_response(method: str, path: str, **kwargs: Any) -> Any:
        result = api_request(method, path, **kwargs)
        if method == "POST" and path.endswith("/amount/draw"):
            raise OSError("draw response lost")
        return result

    with (
        patch.object(adapter, "request", side_effect=lose_draw_response),
        pytest.raises(OSError, match="draw response lost"),
    ):
        prepare()
    recorded_movements = set(Movement.objects.values_list("pk", flat=True))
    # Resume the interrupted exercise with the command and credentials documented for a lab.
    command = subprocess.run(  # noqa: S603 - this interpreter and the checked-in tutorial
        [
            sys.executable,
            "-m",
            "examples.dose_response",
            "--team",
            str(world.team.pk),
            "--flow",
            flow,
            "--analyst",
            str(world.users["editor"].pk),
            "--reviewer",
            str(world.users["runner"].pk),
            *(["--qa", str(world.users["viewer"].pk)] if flow == "regulated_qc" else []),
            "--state",
            str(state),
        ],
        cwd=PLUGINS.parent,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    ids: dict[str, str] = json.loads(command.stdout)
    assert set(Movement.objects.values_list("pk", flat=True)) == recorded_movements
    project = Project.objects.get(pk=ids["project"])
    asker = Project.objects.get(pk=ids["requester_project"])
    assert {project.pk, asker.pk} == training
    assert project.pk != asker.pk
    assert not Entity.objects.filter(owner_project=world.project).exists()
    world = replace(world, project=project)
    assert starters.effective_flow(project, "dose-response") == flow
    assert AuditEvent.objects.filter(
        project=project, action="starter.applied", actor=admin, agent__isnull=False
    ).exists()
    entity_ids = set(
        Entity.objects.filter(owner_project__in=[project, asker]).values_list("pk", flat=True)
    )
    movements = Movement.objects.count()
    uploaded = File.objects.count()
    with pytest.raises(ValueError, match="belongs to a different run"):
        prepare(directory / f"{flow}-other-run.sqlite3")
    assert prepare() == ids
    assert (
        set(Entity.objects.filter(owner_project__in=[project, asker]).values_list("pk", flat=True))
        == entity_ids
    )
    assert Movement.objects.count() == movements and File.objects.count() == uploaded

    # A requester-only reader sees each returned revision while the service's reagent stays private.
    requester = world.users["outsider"]
    ProjectMembership.objects.create(project=asker, user=requester, role=ProjectRole.VIEWER)
    finisher = world.users["runner" if flow == "research" else "viewer"]
    api = Api(world.browser("admin"))
    source = File.objects.select_related("blob").get(pk=ids["reader_file"], project=project)
    reader_path = directory / source.blob.sha256
    storage.download_file(storage.blob_key(source.blob.sha256), reader_path)
    assert reader_path.read_bytes() == (PLUGIN / "tests/data/aid-1224835-activity.csv").read_bytes()
    assert hashlib.sha256(reader_path.read_bytes()).hexdigest() == source.blob.sha256
    request = Entity.objects.get(pk=ids["request"], owner_project=asker)
    assert request.type == (
        "iki/requests.ResearchRequest" if flow == "research" else "iki/requests.QCRequest"
    )
    assert requests.accepts(project, asker, request.type)
    target_sample = Entity.objects.get(pk=ids["sample"], owner_project=project)
    sample_values = services.current_values_of([target_sample])[target_sample.pk]
    assert (sample_values["source"], sample_values["purpose"]) == (
        UUID(ids["tube"]),
        UUID(ids["plate"]),
    )
    [draw] = sample_values["receipt"]
    assert (draw["record"], draw["amount"], draw["unit"]) == (ids["tube"], "10", "uL")
    other = Project.objects.create(team=world.team, name="Another asking project")
    ProjectMembership.objects.create(project=other, user=admin, role=ProjectRole.EDITOR)
    assert not requests.accepts(project, other, request.type)
    receipt = receipts.start(admin, request)
    tube_ids = [ids[key] for key in public_plate.SERIES]
    assert requests.materials_of(request) == {UUID(tube_id) for tube_id in tube_ids}
    reagent_ids = [ids[key] for key in ("protein", "sample", "tube")]
    assert Entity.objects.filter(pk__in=reagent_ids, owner_project=project).count() == 3
    assert not access.visible(requester).filter(pk__in=reagent_ids).exists()
    assert {str(line.entity_id) for line in receipt.lines.all()} == set(tube_ids)
    lines = []
    for shipped in Entity.objects.filter(pk__in=tube_ids):
        scanned = receipts.scan(admin, receipt, shipped.hrid)
        assert scanned.found == "matched"
        lines.append(scanned.line.pk)
        receipt = scanned.receipt
    receipt = receipts.describe(
        admin, receipt, handed_over_by="Assay requester", plan_revision=receipt.plan_revision
    )
    path = f"/receipts/{receipt.pk}"
    line_id = str(lines[0])
    reviewed = api.get(path).json()
    assert {line["entity"] for line in reviewed["lines"] if line["outcome"] == "matched"} == set(
        tube_ids
    )
    revision = reviewed["plan_revision"]
    repeated = api.post(
        path + "/complete", {"plan_revision": revision, "keep_in_custody": [line_id, line_id]}
    )
    assert repeated.status_code == 422, repeated.content
    assert api.get(path).json() == reviewed
    done = receipts.complete(admin, receipt, plan_revision=revision, keep_in_custody=lines)
    assert done.received_by_id == admin.pk
    received = api.get(path).json()["lines"]
    assert {
        row["id"] for row in received if row["received_unplaced"] and row["put_away"] is None
    } == {str(line) for line in lines}

    waiting = api.get("/storage/awaiting-put-away")
    assert waiting.status_code == 200, waiting.content
    held = {item["id"]: item for item in waiting.json()}
    assert all(
        held[t]["custodian"]["id"] == str(admin.pk) and held[t]["custody_since"] for t in tube_ids
    )
    assert ids["tube"] not in held

    plate = UUID(ids["plate"])
    plate_values = services.current_values_of([Entity.objects.get(pk=plate)])[plate]
    assert str(plate_values["reagent_tube"]) == ids["tube"]
    _worker(world, "preview_reader", {"plate": [plate]}, {}, reader_path)
    _worker(world, "commit_reader", {"plate": [plate]}, {}, reader_path)
    read = Entity.objects.get(owner_project=project, type=PREFIX + "PlateRead")
    measured = list(Entity.objects.filter(owner_project=project, type=PREFIX + "WellMeasurement"))
    assert len(measured) == 72
    rows = services.current_values_of(measured)
    controls = [v for v in rows.values() if v["control"]]
    assert {r["well"] for r in controls} == {"A12", "B12", "C12", "D12", "E12", "F12"}
    assert all(r["signal"] is None for r in rows.values())
    assert {r["readout"] for r in rows.values()} == {"normalized_activity"}
    with public_plate.READER_FILE.open() as archive:
        _, _, *matrix = csv.reader(archive)
    expected_activity = {
        f"{row[0]}{column}": float(value)
        for row in matrix
        for column, value in enumerate(row[1:], start=1)
    }
    assert {r["well"]: r["activity_percent"] for r in rows.values()} == expected_activity
    # Source potencies are per replicate. The publication uses their arithmetic mean.
    for key, reported in (
        ("apigenin", (10.1654, 12.7975, 8.0746)),
        ("daidzein", (24.9383, 19.8092, 14.0238)),
    ):
        sample = UUID(ids[key])
        fit_input = _worker(
            world,
            "fit_curve",
            {"read": [read.pk], "sample": [sample]},
            {"endpoint_kind": "IC50"},
            reader_path,
        )
        [tube] = fit_input["inputs"]["sample"]
        assert (tube["type"], tube["id"], tube["values"]["contents"]) == (
            "iki/inventory.Tube",
            str(sample),
            "unreadable",
        )
        name = "RequestedResearchAssayResult" if flow == "research" else "RequestedQCAssayResult"
        candidates = list(Entity.objects.filter(owner_project=project, type=PREFIX + name))
        evidence = services.current_values_of(candidates)
        [result] = [r for r in candidates if str(evidence[r.pk]["sample"]) == str(sample)]
        values = evidence[result.pk]
        assert str(values["request"]) == ids["request"]
        assert values["review_flow"] == flow and values["point_count"] == 33
        assert values["concentration_unit"] == "uM" and values["response_unit"] == "percent"
        assert values["procedure"] == "PubChem AID 1224835" and values["procedure_revision"] == "1"
        assert {p["replicate"] for p in values["points"]} == {1, 2, 3}
        assert len({p["dose"] for p in values["points"]}) == 11
        assert all(p["exclusion_reason"] is None for p in values["points"])
        reference = sum(reported) / len(reported)
        assert values["endpoint_value"] == pytest.approx(reference, rel=0.05)
        assert values["ci_low"] <= reference <= values["ci_high"]
        _check_release(world, flow, requester, result, values, ids, asker)
        request.refresh_from_db()
        if key == "apigenin":
            with pytest.raises(Refused, match="Daidzein has no returned result yet"):
                states.set_state(
                    finisher, request, "status", "delivered", expected_revision=request.revision
                )
    states.set_state(finisher, request, "status", "delivered", expected_revision=request.revision)
    assert states.values_of(request)["status"] == "delivered"
    assert not access.visible(requester).filter(pk__in=reagent_ids).exists()
    _check_derivative_results(world, flow, asker, other, requester, ids, reader_path)

    existing = set(world.team.projects.values_list("pk", flat=True))
    adapter.request("PATCH", f"/projects/{asker.pk}", json={"name": "Completed training requests"})
    with pytest.raises(ValueError, match="renamed"):
        prepare()
    assert set(world.team.projects.values_list("pk", flat=True)) == existing


def _check_derivative_results(
    world: World,
    flow: str,
    asker: Project,
    other: Project,
    requester: User,
    ids: dict[str, str],
    csv: Path,
) -> None:
    admin, project = world.users["admin"], world.project

    # Two additional wells on this read carry derivatives. Only the first one's
    # physical parent chain ends at a tube listed by this requester.
    def portion(value: int) -> amounts.Given:
        return amounts.Given.of({"value": value, "unit": "uL"})

    stock = services.create(
        admin, "iki/core.Collection", asker, {"name": "Stock for testing"}
    ).entity
    split_source = services.create(
        admin,
        "iki/inventory.Tube",
        asker,
        {"name": "Tube for aliquoting", "contents": str(stock.pk)},
        amount={"value": 10, "unit": "uL"},
    )
    source_tube = Entity.objects.get(pk=split_source.entity.pk)
    created = services.create(
        admin,
        ("iki/requests.ResearchRequest" if flow == "research" else "iki/requests.QCRequest"),
        asker,
        {"title": "Assay the tube derivative", "tubes": [str(source_tube.pk)]},
    )
    request = Entity.objects.get(pk=created.entity.pk)
    requests.submit(admin, request, project, expected_revision=request.revision)
    request.refresh_from_db()
    (first,) = amounts.split(admin, source_tube, 1, portion(5)).aliquots
    (nested,) = amounts.split(admin, first, 1, portion(2)).aliquots
    services.link(admin, nested, project)
    protein = services.create(
        admin, "iki/protein.Protein", other, {"name": "Separate sample protein", "sequence": "MKVL"}
    ).entity
    separate = services.create(
        admin,
        "iki/inventory.Tube",
        other,
        {"name": "Separate stock", "contents": str(protein.pk)},
        amount={"value": 10, "unit": "uL"},
    ).entity
    requests.set_intake(
        project, types=[request.type], from_registry=False, sources=[asker, other], by=admin
    )
    second = services.create(
        admin, request.type, other, {"title": "Second assay request", "tubes": [str(separate.pk)]}
    )
    second_request = Entity.objects.get(pk=second.entity.pk)
    requests.submit(admin, second_request, project, expected_revision=second_request.revision)
    (unrelated,) = amounts.split(admin, Entity.objects.get(pk=separate.pk), 1, portion(2)).aliquots
    services.link(admin, unrelated, project)
    layout = public_plate.assignments({key: ids[key] for key in public_plate.SERIES})
    for assignment, material in zip(
        [r for r in layout if r["well"] in {"A12", "B12"}], (nested, unrelated), strict=True
    ):
        assignment.pop("control")
        assignment["sample"] = str(material.pk)
    clone = services.create(
        admin,
        PREFIX + "AssayPlate",
        project,
        {
            "name": "Derivative review",
            "request": str(request.pk),
            "size": "plate_96",
            "reader_file": ids["reader_file"],
            "reader_format": "iki_plate_matrix",
            "assignments": layout,
            "procedure": "SOP-PLATE",
            "procedure_revision": "2",
        },
    ).entity
    _worker(world, "preview_reader", {"plate": [clone.pk]}, {}, csv)
    _worker(world, "commit_reader", {"plate": [clone.pk]}, {}, csv)
    reads = list(Entity.objects.filter(owner_project=project, type=PREFIX + "PlateRead"))
    values = services.current_values_of(reads)
    [read] = [r for r in reads if values[r.pk]["plate"] == clone.pk]
    name = "RequestedResearchAssayResult" if flow == "research" else "RequestedQCAssayResult"
    assert (
        requests.check_result(
            project, PREFIX + name, {"request": second_request.pk, "sample": unrelated.pk}
        )
        == {}
    )
    assert "sample" in requests.check_result(
        project, PREFIX + name, {"request": request.pk, "sample": unrelated.pk}
    )
    steps = (
        [("runner", "signed")]
        if flow == "research"
        else [("editor", "submitted"), ("runner", "reviewed"), ("viewer", "released")]
    )
    for material, kind in ((nested, name), (unrelated, name.removeprefix("Requested"))):
        _worker(world, "fit_curve", {"read": [read.pk], "sample": [material.pk]}, {}, csv)
        candidates = list(Entity.objects.filter(owner_project=project, type=PREFIX + kind))
        current = services.current_values_of(candidates)
        [made] = [c for c in candidates if current[c.pk]["sample"] == material.pk]
        recorded = current[made.pk]
        assert recorded["sample"] == material.pk
        assert (recorded["procedure"], recorded["procedure_revision"]) == ("SOP-PLATE", "2")
        assert recorded.get("request") == (request.pk if material == nested else None)
        if material == nested:
            assert (
                requests.delivery_refusal(request)
                == "Tube for aliquoting has no returned result yet."
            )
        for signer, to in steps:
            assert signed(world, signer, str(made.pk), to, field="review").signature_id
        assert access.visible(requester).filter(pk=made.pk).exists() == (material == nested)
    # The signed fit on the twice-split aliquot now covers its commissioned source tube.
    finisher = world.users[steps[-1][0]]
    states.set_state(finisher, request, "status", "delivered", expected_revision=request.revision)
    assert states.values_of(request)["status"] == "delivered"


def _check_release(
    world: World,
    flow: str,
    requester: User,
    result: Entity,
    values: dict[str, Any],
    ids: dict[str, str],
    asker: Project,
) -> None:
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
    assert released.data["request"] == ids["request"]


def _worker(
    world: World, name: str, inputs: dict[str, list[UUID]], params: dict[str, Any], csv: Path
) -> dict[str, Any]:
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
    return doc
