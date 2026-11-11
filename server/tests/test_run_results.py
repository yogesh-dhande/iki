"""A run's results: checked against what its action declared, then written all or nothing,
with provenance and lineage (ADR 0044)."""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from api_client import problem
from org_world import World, build_world
from test_api_entities import api_as, create
from test_files import run_tasks
from test_runs import TEMPLATE, _capacity, launch, protein
from test_workers import LOCAL, WorkerClient, keep_connection, queued, registration, worker

from iki_server.entities.models import Entity, Revision
from iki_server.files import services as files
from iki_server.files import storage
from iki_server.files.models import UploadState
from iki_server.files.services import EMPTY_SHA256
from iki_server.org.models import ProjectMembership
from iki_server.runs import catalog, queue
from iki_server.runs.models import ActionRun, Job, JobState, Lineage, RunEffect, RunOutput
from iki_spec import schemas

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection"]  # the fixture, used here too

PREDICT = "structure_prediction.predict_structure"
RELAX = "protein.relax_structure"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    """The tests' deployment, with a vm tier too, so predict_structure runs."""
    catalog.compute.cache_clear()
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["templates"]["a100"] = {
        **TEMPLATE,
        "name": "a100",
        "provisioner": "aws_ec2",
        "lifecycle": "ephemeral",
        "capacity": _capacity(96, 1152, gpus=8),
    }
    compute["bindings"]["structure_prediction.alphafold-gpu"] = {
        "environment": "structure_prediction.alphafold-gpu",
        "templates": [["a100", 0]],
    }
    file = tmp_path / "with-vm.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


def output(**parts: Any) -> dict[str, Any]:
    return {"apiVersion": "iki/v1", "kind": "RunOutput", **parts}


def leased(world: World) -> tuple[WorkerClient, dict[str, Any], dict[str, Any]]:
    """A protein.compute_properties run, leased by a backend worker."""
    pod = worker(world)
    run = queued(world)
    job = pod.lease().json()
    return pod, run, job


def weight(run: dict[str, Any], value: Any = 12345.6) -> dict[str, Any]:
    entity = run["inputs"][0]["entity"]
    return {"protein": [{"entity": entity, "values": {"molecular_weight": value}}]}


def done(pod: WorkerClient, job: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    answer = pod.report(job, "complete", output=document)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


# --- Updates -----------------------------------------------------------------------------------


def test_a_run_writes_its_results_with_provenance(world: World) -> None:
    pod, run, job = leased(world)
    document = output(
        updates=weight(run),
        metrics={"residues": 412},
        messages=[{"level": "info", "text": "Used average isotopic masses."}],
    )
    assert not list(schemas.check("run-output", document))
    assert done(pod, job, document) == {
        "run_status": "succeeded",
        "code": None,
        "message": None,
        "errors": [],
    }
    editor = api_as(world, "editor")
    body = editor.get(f"/runs/{run['id']}").json()
    entity_id = run["inputs"][0]["entity"]
    assert body["status"] == "succeeded"
    assert body["finished_at"] is not None
    assert body["effects"] == [
        {
            "entity": entity_id,
            "hrid": run["inputs"][0]["hrid"],
            "type": "protein.Protein",
            "kind": "updated",
            "revision": 2,
        }
    ]
    assert body["metrics"] == {"residues": 412}
    assert body["messages"] == [
        {"level": "info", "text": "Used average isotopic masses.", "entity": None}
    ]
    assert Job.objects.get(pk=job["job"]["id"]).state == JobState.SUCCEEDED

    revision = Revision.objects.get(entity_id=entity_id, number=2)
    assert revision.via == "run"
    assert revision.user == world.users["editor"]  # who launched it
    assert revision.changed_fields == ["molecular_weight"]
    provenance = editor.get(f"/entities/{entity_id}/provenance").json()
    assert provenance["molecular_weight"]["via"] == "run"
    assert provenance["molecular_weight"]["run"] == {
        "id": run["id"],
        "hrid": run["hrid"],
        "action": "protein.compute_properties",
    }
    assert provenance["sequence"]["run"] is None
    history = editor.get(f"/entities/{entity_id}/revisions").json()["items"]
    assert [r["run"] and r["run"]["hrid"] for r in history] == [run["hrid"], None]
    assert editor.get(f"/entities/{entity_id}/revisions/2").json()["run"]["hrid"] == run["hrid"]
    # The lease is over.
    problem(pod.report(job, "heartbeat"), 409, "stale_lease")


def test_people_still_cant_set_computed_fields(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    changed = editor.patch(
        f"/entities/{abl['id']}",
        {"values": {"molecular_weight": 1}},
        headers={"If-Match": editor.get(f"/entities/{abl['id']}")["ETag"]},
    )
    assert changed.status_code == 422
    assert "only actions set it" in changed.content.decode()


def _failed(pod: WorkerClient, job: dict[str, Any], document: dict[str, Any]) -> dict[str, str]:
    body = done(pod, job, document)
    assert body["run_status"] == "failed", body
    assert body["code"] == "invalid_output"
    return {e["pointer"]: e["detail"] for e in body["errors"]}


def test_results_must_fit_what_the_action_declares(world: World) -> None:
    pod, run, job = leased(world)
    other = protein(api_as(world, "editor"), world.project, name="other")
    entity = run["inputs"][0]["entity"]
    errors = _failed(
        pod,
        job,
        output(
            updates={
                "protein": [
                    {"entity": entity, "values": {"molecular_weight": 1, "organism": "Mouse"}},
                    {"entity": other["id"], "values": {"molecular_weight": 2}},
                ],
                "dna": [{"entity": entity, "values": {"gc": 1}}],
            },
            creates=[{"type": "protein.ProteinStructure", "values": {}}],
        ),
    )
    assert errors == {
        "#/updates/dna": "The action doesn't declare that it updates `dna` (it updates: protein).",
        "#/updates/protein/0/values/organism": "The action doesn't declare that it sets "
        "`organism`.",
        "#/updates/protein/1/entity": "It wasn't given as `protein`.",
        "#/creates/0/type": "The action doesn't declare that it creates "
        "protein.ProteinStructure (it creates: nothing).",
    }
    # Nothing was written, the output is kept, and it isn't retried.
    assert Revision.objects.filter(entity_id=entity).count() == 1
    assert RunOutput.objects.filter(run_id=run["id"]).exists()
    assert Job.objects.filter(run_id=run["id"]).count() == 1
    body = api_as(world).get(f"/runs/{run['id']}").json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_output"
    assert len(body["error"]["errors"]) == 4
    assert body["effects"] == []


def test_values_must_fit_their_fields(world: World) -> None:
    pod, run, job = leased(world)
    errors = _failed(pod, job, output(updates=weight(run, "heavy")))
    assert list(errors) == ["#/updates/protein/0/values/molecular_weight"]


def test_the_output_must_be_a_run_output(world: World) -> None:
    pod, _, job = leased(world)
    errors = _failed(pod, job, {"apiVersion": "iki/v1", "kind": "Output", "extra": 1})
    assert "#/kind" in errors


def test_a_run_whose_plugin_changed_meanwhile_is_rejected(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    pod, run, job = leased(world)
    monkeypatch.setattr(queue, "_plugin_changed", lambda _run: True)
    body = done(pod, job, output(updates=weight(run, "heavy")))
    assert body["run_status"] == "rejected"
    assert body["code"] == "schema_changed"
    assert "results no longer fit" in body["message"]
    assert RunOutput.objects.filter(run_id=run["id"]).exists()


def test_results_for_an_entity_archived_meanwhile_are_rejected(world: World) -> None:
    pod, run, job = leased(world)
    editor = api_as(world, "editor")
    entity = run["inputs"][0]["entity"]
    editor.patch(
        f"/entities/{entity}",
        {"archived": True},
        headers={"If-Match": editor.get(f"/entities/{entity}")["ETag"]},
    )
    body = done(pod, job, output(updates=weight(run)))
    assert body["run_status"] == "rejected"
    assert body["code"] == "conflict"
    assert "archived" in body["message"]


def test_a_run_doesnt_outlive_the_role_that_started_it(world: World) -> None:
    pod, run, job = leased(world)
    ProjectMembership.objects.filter(project=world.project, user=world.users["editor"]).delete()
    body = done(pod, job, output(updates=weight(run)))
    assert body["run_status"] == "rejected"
    assert body["code"] == "not_allowed"
    assert Revision.objects.filter(entity_id=run["inputs"][0]["entity"]).count() == 1


def test_a_run_cancelled_meanwhile_writes_nothing(world: World) -> None:
    pod, run, job = leased(world)
    api_as(world).post(f"/runs/{run['id']}/cancel", {})
    assert done(pod, job, output(updates=weight(run)))["run_status"] == "cancelled"
    assert Revision.objects.filter(entity_id=run["inputs"][0]["entity"]).count() == 1
    assert RunOutput.objects.filter(run_id=run["id"]).exists()


# --- Creates and lineage -----------------------------------------------------------------------


def _vm(world: World) -> WorkerClient:
    made = WorkerClient(registration(world, tier="vm")).post(
        "/register",
        {
            "name": "gpu-1",
            "version": "0.1.0",
            "capacity": _capacity(96, 1152, gpus=8),
            "plugins": LOCAL,
        },
    )
    assert made.status_code == 201, made.content
    return WorkerClient(made.json()["token"])


def _predicting(world: World) -> tuple[WorkerClient, dict[str, Any], dict[str, Any]]:
    runner = api_as(world, "runner")
    abl = protein(runner, world.project)
    made = runner.post(
        f"/actions/{PREDICT}/runs",
        {"project": str(world.project.pk), "inputs": {"protein": abl["id"]}},
    )
    assert made.status_code == 201, made.content
    gpu = _vm(world)
    job = gpu.lease().json()
    return gpu, made.json(), job


def _upload(gpu: WorkerClient, job: dict[str, Any], data: bytes = b"") -> dict[str, Any]:
    """Uploads a result file as iki-worker does: start, PUT the parts, finish."""
    started = gpu.report(
        job,
        "uploads",
        name="model_1.pdb",
        media_type="chemical/x-pdb",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert started.status_code == 201, started.content
    upload: dict[str, Any] = started.json()
    for part in upload["parts"]:
        requests.put(part["url"], data=data, timeout=30).raise_for_status()
    if upload["parts"]:
        upload = gpu.report(job, f"uploads/{upload['id']}/complete").json()
    return upload


def _structure_file(gpu: WorkerClient, job: dict[str, Any]) -> str:
    upload = _upload(gpu, job)
    assert upload["state"] == "ready"
    return str(upload["file"])


def _structure(
    gpu: WorkerClient, job: dict[str, Any], run: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        "type": "protein.ProteinStructure",
        "values": {
            "structure": _structure_file(gpu, job),
            "method": "predicted",
            "chains": [run["inputs"][0]["entity"]],
        },
        **extra,
    }


def test_new_entities_record_what_they_were_made_from(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    parent = run["inputs"][0]["entity"]
    body = done(
        gpu,
        job,
        output(
            creates=[
                _structure(gpu, job, run),  # derived from the one input, as declared
                _structure(gpu, job, run, derived_from=[parent]),
            ]
        ),
    )
    assert body["run_status"] == "succeeded", body
    runner = api_as(world, "runner")
    effects = runner.get(f"/runs/{run['id']}").json()["effects"]
    assert [(e["type"], e["kind"], e["revision"]) for e in effects] == [
        ("protein.ProteinStructure", "created", 1),
        ("protein.ProteinStructure", "created", 1),
    ]
    made = Entity.objects.get(pk=effects[0]["entity"])
    assert made.owner_project_id == world.project.pk
    assert made.created_by == world.users["runner"]
    lineage = runner.get(f"/entities/{made.pk}/lineage").json()
    assert lineage["made_from"] == [
        {
            "entity": {
                "id": parent,
                "hrid": run["inputs"][0]["hrid"],
                "type": "protein.Protein",
                "archived": False,
            },
            "revision": 1,
            "input": "protein",
            "run": {"id": run["id"], "hrid": run["hrid"], "action": PREDICT},
        }
    ]
    assert lineage["used_to_make"] == []
    used = runner.get(f"/entities/{parent}/lineage").json()
    assert {link["entity"]["id"] for link in used["used_to_make"]} == {e["entity"] for e in effects}
    assert used["more"] is False
    provenance = runner.get(f"/entities/{made.pk}/provenance").json()
    assert provenance["method"]["run"]["hrid"] == run["hrid"]
    assert Lineage.objects.count() == 2


def test_new_entities_must_say_where_they_came_from(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    stranger = protein(api_as(world, "editor"), world.project, name="stranger")
    errors = _failed(
        gpu,
        job,
        output(
            creates=[
                _structure(gpu, job, run, derived_from=[stranger["id"]]),
                {"type": "protein.Protein", "values": {"sequence": "MKVW"}},
            ]
        ),
    )
    # What the action declares is checked first; values only once that fits.
    assert errors == {
        "#/creates/0/derived_from/0": "It isn't one of the inputs protein.ProteinStructure "
        "is derived from (protein).",
        "#/creates/1/type": "The action doesn't declare that it creates protein.Protein "
        "(it creates: protein.ProteinStructure).",
    }
    assert RunEffect.objects.count() == 0
    assert not Entity.objects.filter(type="protein.ProteinStructure").exists()


def test_one_means_at_most_one(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    stored = ActionRun.objects.get(pk=run["id"])
    stored.spec["creates"][0]["cardinality"] = "one"
    ActionRun.objects.filter(pk=stored.pk).update(spec=stored.spec)
    two = [_structure(gpu, job, run), _structure(gpu, job, run)]
    errors = _failed(gpu, job, output(creates=two))
    assert errors == {
        "#/creates/1": "The action creates at most one protein.ProteinStructure per run."
    }


def test_a_failed_write_writes_nothing(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    # The first structure is fine; the second's value isn't. Neither is kept.
    bad = _structure(gpu, job, run)
    bad["values"]["method"] = "guessed"
    errors = _failed(gpu, job, output(creates=[_structure(gpu, job, run), bad]))
    assert list(errors) == ["#/creates/1/values/method"]
    assert not Entity.objects.filter(type="protein.ProteinStructure").exists()
    assert not Lineage.objects.exists()


# --- Result files ------------------------------------------------------------------------------


def test_workers_upload_result_files_for_their_run(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    pdb = b"ATOM      1  N   MET A   1      27.340  24.430   2.614  1.00  9.67           N\n"
    upload = _upload(gpu, job, pdb)
    assert upload["state"] == "checking"  # Iki reads the bytes back, in a task
    run_tasks()
    upload = gpu.report(job, f"uploads/{upload['id']}/complete").json()
    assert upload["state"] == "ready", upload
    structure = {
        "type": "protein.ProteinStructure",
        "values": {
            "structure": upload["file"],
            "method": "predicted",
            "chains": [run["inputs"][0]["entity"]],
        },
    }
    assert done(gpu, job, output(creates=[structure]))["run_status"] == "succeeded"
    made = api_as(world, "runner").get(f"/runs/{run['id']}").json()["effects"][0]
    entity = api_as(world, "runner").get(f"/entities/{made['entity']}").json()
    assert entity["values"]["structure"] == upload["file"]
    # Made for the launcher, in the run's project.
    file = api_as(world, "runner").get(f"/files/{upload['file']}").json()
    assert file["project"]["id"] == str(world.project.pk)


def _alias(url: str) -> str:
    """The same store under another name, as a cluster's internal address would be."""
    parts = urlsplit(url)
    other = {"localhost": "127.0.0.1", "127.0.0.1": "localhost"}.get(parts.hostname or "")
    if other is None:
        pytest.skip("the test store has no second name to reach it by")
    return f"{parts.scheme}://{other}:{parts.port}"


def test_workers_reach_the_store_at_their_own_address(world: World, s3: Any, settings: Any) -> None:
    public = settings.IKI_FILES_PUBLIC_ENDPOINT_URL
    inside = _alias(public)
    settings.IKI_FILES_WORKER_ENDPOINT_URL = inside
    storage.reset()
    gpu, _, job = _predicting(world)
    data = b"ATOM\n"
    started = gpu.report(
        job,
        "uploads",
        name="model_1.pdb",
        media_type="chemical/x-pdb",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    ).json()
    (part,) = started["parts"]
    assert part["url"].startswith(f"{inside}/")
    requests.put(part["url"], data=data, timeout=30).raise_for_status()
    (again,) = gpu.report(job, f"uploads/{started['id']}/parts", numbers=[1]).json()
    assert again["url"].startswith(f"{inside}/")
    assert gpu.report(job, f"uploads/{started['id']}/complete").json()["state"] == "checking"
    # People's browsers still get the public address.
    upload = files.start(
        world.users["editor"],
        world.project,
        name="abl.pdb",
        media_type="chemical/x-pdb",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    (theirs,) = files.parts(upload)
    assert theirs["url"].startswith(f"{public}/")
    uri = f"s3://iki/{settings.IKI_FILES_PREFIX}/x"
    assert storage.download_url(uri, "", "x", "text/plain").startswith(f"{public}/")
    assert storage.download_url(uri, "", "x", "text/plain", workers=True).startswith(f"{inside}/")


def test_results_attach_only_their_own_files(world: World, s3: Any) -> None:
    gpu, run, job = _predicting(world)
    # A file the launcher can read, but this run didn't upload.
    theirs = files.start(
        world.users["runner"],
        world.project,
        name="old.pdb",
        media_type="chemical/x-pdb",
        size=0,
        sha256=EMPTY_SHA256,
    )
    theirs.refresh_from_db()
    assert theirs.state == UploadState.READY
    structure = _structure(gpu, job, run)
    structure["values"]["structure"] = str(theirs.file_id)
    errors = _failed(gpu, job, output(creates=[structure]))
    assert errors == {
        "#/creates/0/values/structure": "Attach only files this run uploaded (once they're "
        "ready) or its inputs' files."
    }


def test_uploads_belong_to_the_job_holding_the_lease(world: World, s3: Any) -> None:
    gpu, _, job = _predicting(world)
    upload = _upload(gpu, job)
    pod, _, other = leased(world)
    problem(pod.report(other, f"uploads/{upload['id']}/complete"), 404, "not_found")
    stale = {**job, "job": {**job["job"], "lease_token": "00000000-0000-0000-0000-000000000000"}}
    problem(gpu.report(stale, f"uploads/{upload['id']}/complete"), 409, "stale_lease")
    bad = gpu.report(job, "uploads", name="x", media_type="text/plain", size=1, sha256="z" * 64)
    assert problem(bad, 422, "validation_failed")["errors"][0]["pointer"] == "#/sha256"


# --- Input files -------------------------------------------------------------------------------


def _stored(world: World, name: str) -> str:
    """A file in the project (empty, so it's ready at once)."""
    upload = files.start(
        world.users["editor"],
        world.project,
        name=name,
        media_type="chemical/x-pdb",
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload.refresh_from_db()
    return str(upload.file_id)


def test_jobs_download_the_files_their_inputs_hold(world: World, s3: Any) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    file_id = _stored(world, "abl.pdb")
    values = {"structure": file_id, "method": "xray", "chains": [abl["id"]]}
    made = create(editor, world.project, values, "protein.ProteinStructure")
    assert made.status_code == 201, made.content
    assert launch(editor, world.project, {"structure": made.json()["id"]}, RELAX).status_code == 201
    pod = worker(world)
    job = pod.lease().json()
    assert not list(schemas.check("run-input", job))
    assert job["files"] == {
        file_id: {
            "name": "abl.pdb",
            "media_type": "chemical/x-pdb",
            "size": 0,
            "sha256": EMPTY_SHA256,
        }
    }
    got = pod.report(job, f"files/{file_id}")
    assert got.status_code == 200, got.content
    body = got.json()
    assert (body["name"], body["size"], body["sha256"]) == ("abl.pdb", 0, EMPTY_SHA256)
    fetched = requests.get(body["url"], timeout=30)
    assert fetched.status_code == 200
    assert fetched.content == b""
    # Only files its inputs hold, and only while it holds the lease.
    elsewhere = _stored(world, "other.pdb")
    problem(pod.report(job, f"files/{elsewhere}"), 404, "not_found")
    stale = {**job, "job": {**job["job"], "lease_token": "00000000-0000-0000-0000-000000000000"}}
    problem(pod.report(stale, f"files/{file_id}"), 409, "stale_lease")


def test_a_job_without_input_files_lists_none(world: World) -> None:
    queued(world)
    assert worker(world).lease().json()["files"] == {}
