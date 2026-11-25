"""Environment setup builds and the volumes they fill (ADR 0075): a Compute admin asks for
one, another approves it, the provisioner starts a machine with a writable disk for each
volume, its worker runs the setup script, the disks are snapshotted, and machines for the
environment's runs get a read-only copy of the pinned version, which each job records.
Kubernetes is a fake that keeps what it's asked for; the dev cluster's end-to-end checks
use a real one, with the CSI host-path driver's snapshots."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_container_tier import FakeCluster, register, rewrite_compute
from test_run_results import done, output
from test_runs import TEMPLATE, launch, protein
from test_workers import WorkerClient, worker

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.core.ids import uuid7
from iki_server.entities import registry
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.runs import catalog, egress, provisioner, services, setups
from iki_server.runs.models import (
    ActionRun,
    Job,
    Launch,
    LaunchState,
    ManagedVolume,
    RunStatus,
    SetupBuild,
    SetupEgress,
    SetupLog,
    SetupState,
    VolumeState,
)

pytestmark = pytest.mark.django_db

ALPHAFOLD = "acme/prediction.alphafold-gpu"
PREDICT = "acme/prediction.predict_structure"
WORKER_IMAGE = "ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:" + "b" * 64
GI = 1024**3
TI = 1024 * GI


@pytest.fixture
def world(settings: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[World]:
    settings.IKI_RUNS_NAMESPACE = "iki-runs"
    settings.IKI_RUNS_NAMESPACES = []
    settings.IKI_RUNS_WORKER_IMAGE = WORKER_IMAGE
    settings.IKI_RUNS_SERVER_URL = "https://iki.example.com"
    settings.IKI_EGRESS_PROXY_URL = "http://iki-egress.iki.svc:3128"
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["templates"]["gpu-a100"] = {
        **TEMPLATE,
        "name": "gpu-a100",
        "provisioner": "k8s_job",
        "lifecycle": "ephemeral",
        "capacity": {
            "cpu": 8,
            "memory": 128 * GI,
            "gpu_count": 1,
            "gpu_memory": 40 * GI,
            "scratch_disk": 200 * GI,
        },
        "volumes": {
            "af-db": {
                "strategy": "clone_from_snapshot",
                "fast_snapshot_restore": False,
                "storage_class": "ebs-gp3",
                "snapshot_class": "ebs-snapshots",
            }
        },
    }
    compute["bindings"][ALPHAFOLD] = {"environment": ALPHAFOLD, "templates": [["gpu-a100", 0]]}
    file = tmp_path / "with-volumes.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()
    # AlphaFold's environment has both tiers; its action asks for VM machines, whose
    # volumes are test_vm_volumes'. Here it runs on pods.
    predict = catalog.action(PREDICT)
    assert predict is not None
    monkeypatch.setitem(
        registry.plugins()["acme/prediction"].actions,
        "predict_structure",
        dataclasses.replace(predict, tier="container"),
    )
    made = build_world()
    # A second Compute admin, to approve what the first asks for.
    other = User.objects.create_user("approver@example.com", "Ada")
    OrgRoleAssignment.objects.create(user=other, role=OrgRole.COMPUTE_ADMIN)
    made.users["approver"] = other
    yield made
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def ask(world: World, reason: str = "AlphaFold's 2026-10 databases") -> dict[str, Any]:
    made = api_as(world, "compute_admin").post(
        "/compute/setups", {"environment": ALPHAFOLD, "reason": reason}
    )
    assert made.status_code == 201, made.content
    (build,) = made.json()
    result: dict[str, Any] = build
    return result


def approve(world: World, build: dict[str, Any]) -> None:
    done = api_as(world, "approver").post(f"/compute/setups/{build['hrid']}/approve")
    assert done.status_code == 200, done.content


def queue_run(world: World, name: str = "ACDE") -> dict[str, Any]:
    runner = api_as(world, "runner")
    abl = protein(runner, world.project, name=name)
    made = runner.post(
        f"/actions/{PREDICT}/runs",
        {"project": str(world.project.pk), "inputs": {"protein": abl["id"]}},
    )
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def setup_pod(cluster: FakeCluster) -> tuple[str, dict[str, Any]]:
    """The pod of the setup build whose machine is starting."""
    (launch,) = Launch.objects.exclude(setup=None).exclude(state=LaunchState.ENDED)
    return launch.name, cluster.made[(launch.namespace, launch.name)]


def run_setup(world: World, cluster: FakeCluster, *, data_version: str = "2026-10") -> str:
    """Asks for a build, approves it, and runs it through to its snapshot being taken.
    Returns the build's HRID."""
    build = ask(world)
    approve(world, build)
    provisioner.tick(cluster)
    finish_setup_pod(cluster, data_version=data_version)
    hrid: str = build["hrid"]
    assert SetupBuild.objects.get(hrid=hrid).state == SetupState.SUCCEEDED
    return hrid


def finish_setup_pod(cluster: FakeCluster, *, data_version: str = "2026-10") -> None:
    """Finish a running setup and make its snapshot ready."""
    name, _ = setup_pod(cluster)
    pod = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = pod.lease().json()
    lease = {"lease_token": given["job"]["lease_token"]}
    done = pod.post(
        f"/setups/{given['setup']['id']}/finish",
        {**lease, "outcome": "succeeded", "data_version": data_version},
    )
    assert done.json() == {"state": "snapshotting"}, done.content
    cluster.finish(name)
    provisioner.tick(cluster)  # the launch ends, and the snapshot is asked for
    provisioner.tick(cluster)
    for (kind, _, _), item in cluster.objects.items():
        if kind == "snapshot":
            item["status"] = {"readyToUse": True}
    provisioner.tick(cluster)


def register_run_pod(cluster: FakeCluster) -> tuple[Launch, WorkerClient]:
    machine = Launch.objects.get(setup=None, state=LaunchState.STARTING)
    made = register(cluster.token(machine.name), name=f"{machine.name}-pod")
    assert made.status_code == 201, made.content
    return machine, WorkerClient(made.json()["token"])


def finish_prediction(cluster: FakeCluster, hrid: str, build: str) -> None:
    """Run the prediction on its provisioned pod and check what that pod actually mounts."""
    provisioner.tick(cluster)
    machine, pod = register_run_pod(cluster)
    manifest = cluster.made[(machine.namespace, machine.name)]["spec"]["template"]["spec"]
    volume = next(v for v in manifest["volumes"] if v["name"] == "volume-0")
    snapshot = volume["ephemeral"]["volumeClaimTemplate"]["spec"]["dataSource"]["name"]
    assert snapshot == ManagedVolume.objects.get(build__hrid=build, name="af-db").handle
    job = pod.lease().json()
    assert job["run"]["hrid"] == hrid
    assert job["volumes"]["af-db"]["hrid"] == build
    assert done(pod, job, output(metrics={"score": 0.9}))["run_status"] == "succeeded"
    cluster.finish(machine.name)


# --- Asking for one ----------------------------------------------------------------------------


def test_a_compute_admin_asks_and_another_approves(world: World) -> None:
    assert (
        api_as(world, "runner").post("/compute/setups", {"environment": ALPHAFOLD}).status_code
        == 403
    )
    build = ask(world)
    assert (build["state"], build["version"], build["place"], build["template"]) == (
        "awaiting_approval",
        1,
        "iki-runs",
        "gpu-a100",
    )
    assert build["hrid"].startswith("SETUP-")
    # Not by whoever asked for it.
    mine = api_as(world, "compute_admin").post(f"/compute/setups/{build['hrid']}/approve")
    problem(mine, 409, "refused")
    assert "Another Compute admin approves it" in mine.json()["detail"]
    # Nor twice at once.
    again = api_as(world, "compute_admin").post("/compute/setups", {"environment": ALPHAFOLD})
    problem(again, 409, "refused")
    approve(world, build)
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['id']}").json()
    assert shown["state"] == "queued"
    assert shown["approved_by"]["name"] == "Ada"
    actions = list(
        AuditEvent.objects.filter(action__startswith="setup.")
        .order_by("occurred_at", "id")
        .values_list("action", flat=True)
    )
    assert actions == ["setup.requested", "setup.approved"]


def test_the_environments_volumes_say_what_runs_wait_for(world: World) -> None:
    (found,) = [
        e
        for e in api_as(world, "compute_admin").get("/compute/volumes").json()
        if e["environment"] == ALPHAFOLD
    ]
    assert found["volumes"] == [
        {
            "name": "af-db",
            "size": 3 * TI,
            "mount": "/data/af",
            "access": "read_only_many",
            "filled_by_setup": True,
        }
    ]
    assert found["setup"]["network"] == ["storage.googleapis.com"]
    assert found["places"] == [
        {
            "place": "iki-runs",
            "build": None,
            "hrid": None,
            "version": None,
            "data_version": None,
            "ready_at": None,
        }
    ]
    assert "no version is ready" in found["waiting"]
    # Runs wait for it, saying so, and no machine starts for them.
    run = queue_run(world)
    cluster = FakeCluster()
    assert provisioner.tick(cluster) == (0, 0)
    shown = api_as(world, "runner").get(f"/runs/{run['hrid']}").json()
    assert shown["waiting"].startswith("Waiting for its reference data (af-db): no version")
    # Once it has a version, retiring it doesn't put this run back on the waiting list.
    build = run_setup(world, cluster)
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{build}/retire")
    assert retired.status_code == 200
    assert api_as(world, "runner").get(f"/runs/{run['hrid']}").json()["waiting"] is None


# --- Running it --------------------------------------------------------------------------------


def test_jobs_waiting_for_the_same_build_check_once_per_round(world: World) -> None:
    queue_run(world)
    queue_run(world, name="ACDEF")
    with patch.object(setups, "pin_for", wraps=setups.pin_for) as lookup:
        assert provisioner.demand(timezone.now()) == {}
        lookup.assert_called_once()


def test_a_volume_setup_and_waiting_run_keep_the_customer_image(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    environments = registry.plugins()["acme/prediction"].environments
    name = "alphafold-gpu"
    monkeypatch.setitem(
        environments,
        name,
        dataclasses.replace(environments[name], image=None, customer_image_required=True),
    )
    image = "registry.example.com/lab/setup@sha256:" + "a" * 64

    def bind(compute: dict[str, Any]) -> None:
        compute["image_bindings"] = [
            {
                "environment": ALPHAFOLD,
                "plugin_version": ">=0",
                "image": image,
                "pull_secret": "volume-builder",
            }
        ]

    rewrite_compute(bind)
    build = ask(world)
    assert build["image"] == image
    approve(world, build)
    run = queue_run(world)
    assert ActionRun.objects.get(pk=run["id"]).volumes is None
    rewrite_compute(lambda compute: compute.pop("image_bindings"))

    cluster = FakeCluster()
    provisioner.tick(cluster)
    _, job = setup_pod(cluster)
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == image
    assert pod["imagePullSecrets"] == [{"name": "volume-builder"}]
    finish_setup_pod(cluster)

    provisioner.tick(cluster)
    launch, runner = register_run_pod(cluster)
    assert launch.image == image
    lease = runner.lease()
    assert lease.status_code == 200, lease.content
    assert lease.json()["run"]["hrid"] == run["hrid"]
    assert lease.json()["volumes"]["af-db"]["hrid"] == build["hrid"]


def test_its_machine_fills_a_fresh_disk_and_its_worker_runs_only_the_setup(world: World) -> None:
    build = ask(world)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    assert not cluster.made  # it waits for approval
    approve(world, build)
    queue_run(world)
    provisioner.tick(cluster)
    name, job = setup_pod(cluster)
    # A disk for the volume, of its size, from the template's storage class.
    ((kind, namespace, claim), disk) = next(iter(cluster.objects.items()))
    assert (kind, namespace) == ("pvc", "iki-runs")
    assert disk["spec"]["storageClassName"] == "ebs-gp3"
    assert disk["spec"]["resources"]["requests"]["storage"] == str(3 * TI)
    pod = job["spec"]["template"]["spec"]
    assert {"name": "fill-0", "persistentVolumeClaim": {"claimName": claim}} in pod["volumes"]
    assert {"name": "fill-0", "mountPath": "/data/af"} in pod["containers"][0]["volumeMounts"]
    # Only the setup's pod: the run waits for its data.
    assert len(cluster.made) == 1
    launch = Launch.objects.get()
    assert launch.setup is not None and launch.setup.hrid == build["hrid"]
    made = register(cluster.token(name), name=f"{name}-x1y2z")
    worker = WorkerClient(made.json()["token"])
    leased = worker.lease()
    assert leased.status_code == 200, leased.content
    given = leased.json()
    assert given["kind"] == "SetupInput"
    assert given["script"] == "setup/download_dbs.sh"
    assert given["volumes"] == {"af-db": {"mount": "/data/af", "size": 3 * TI}}
    assert given["job"]["proxy"].startswith(f"http://{build['id']}:")
    assert given["job"]["timeout"] == 12 * 3600
    # It runs nothing else.
    assert worker.lease().status_code == 204
    assert not Job.objects.exclude(worker=None).exists()
    setup_id, lease = given["setup"]["id"], {"lease_token": given["job"]["lease_token"]}
    beat = worker.post(f"/setups/{setup_id}/heartbeat", lease)
    assert beat.json()["cancel_requested"] is False
    logged = worker.post(f"/setups/{setup_id}/logs", {**lease, "offset": 0, "text": "fetching\n"})
    assert logged.json() == {"next_offset": 9}
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{setup_id}/log").json()
    assert shown == {"offset": 0, "text": "fetching\n", "size": 9, "finished": False}
    assert SetupLog.objects.filter(build_id=setup_id).count() == 1
    worker.post(f"/setups/{setup_id}/finish", {**lease, "outcome": "succeeded"})
    # Its disk is snapshotted once its pod has gone, so nothing's still writing to it.
    provisioner.tick(cluster)
    assert not [k for k in cluster.objects if k[0] == "snapshot"]
    cluster.finish(name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    ((_, snapshot),) = [(k, v) for k, v in cluster.objects.items() if k[0] == "snapshot"]
    assert snapshot["spec"] == {
        "source": {"persistentVolumeClaimName": claim},
        "volumeSnapshotClassName": "ebs-snapshots",
    }
    assert SetupBuild.objects.get(pk=setup_id).state == SetupState.SNAPSHOTTING
    snapshot["status"] = {"readyToUse": True}
    provisioner.tick(cluster)
    done = SetupBuild.objects.get(pk=setup_id)
    assert done.state == SetupState.SUCCEEDED
    assert ManagedVolume.objects.get(build=done).state == VolumeState.READY
    # The disk goes; the snapshot stays.
    assert [k[0] for k in cluster.objects] == ["snapshot"]


def test_runs_read_a_copy_of_the_newest_version_and_record_it(world: World) -> None:
    cluster = FakeCluster()
    first = run_setup(world, cluster, data_version="AlphaFold DBs 2026-10")
    run = queue_run(world)
    provisioner.tick(cluster)
    (name,) = [n for (_, n) in cluster.made if n.startswith("iki-run-")]
    pod = cluster.made[("iki-runs", name)]["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "volume-0")
    claim = volume["ephemeral"]["volumeClaimTemplate"]["spec"]
    assert claim["dataSource"]["kind"] == "VolumeSnapshot"
    assert claim["storageClassName"] == "ebs-gp3"
    assert {"name": "volume-0", "mountPath": "/data/af", "readOnly": True} in pod["containers"][0][
        "volumeMounts"
    ]
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    recorded = given["volumes"]["af-db"]
    assert (recorded["hrid"], recorded["version"], recorded["data_version"]) == (
        first,
        1,
        "AlphaFold DBs 2026-10",
    )
    (attempt,) = api_as(world, "runner").get(f"/runs/{run['hrid']}/attempts").json()
    assert attempt["volumes"]["af-db"]["hrid"] == first
    assert ActionRun.objects.get(pk=run["id"]).status == RunStatus.RUNNING


def test_reuse_requires_the_same_reference_data_build(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: "a" * 40)
    cluster = FakeCluster()
    first = run_setup(world, cluster, data_version="")
    run = queue_run(world)
    finish_prediction(cluster, run["hrid"], first)

    runner = api_as(world, "runner")
    inputs = {"protein": run["inputs"][0]["entity"]}
    same = launch(runner, world.project, inputs, PREDICT).json()
    assert same["reused_from"]["hrid"] == run["hrid"]
    assert same["attempts"] == 0

    # Even with no descriptive label, each build identifies a distinct set of snapshots.
    second = run_setup(world, cluster, data_version="")
    changed = launch(runner, world.project, inputs, PREDICT).json()
    assert changed["reused_from"] is None
    assert changed["status"] == "queued"
    finish_prediction(cluster, changed["hrid"], second)
    same = launch(runner, world.project, inputs, PREDICT).json()
    assert same["reused_from"]["hrid"] == changed["hrid"]


def test_a_queued_run_keeps_its_reference_data_when_a_new_build_finishes(world: World) -> None:
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    run = queue_run(world)
    # Keep the job in the queue while the setup build gets its machine and publishes.
    job = Job.objects.get(run_id=run["id"])
    Job.objects.filter(pk=job.pk).update(queued_at=timezone.now() + timedelta(days=1))
    run_setup(world, cluster)
    assert not Launch.objects.filter(setup=None).exists()
    Job.objects.filter(pk=job.pk).update(queued_at=timezone.now())
    finish_prediction(cluster, run["hrid"], first)
    job.refresh_from_db()
    assert job.volumes["af-db"]["hrid"] == first


def test_a_retired_version_is_kept_for_its_pinned_run_and_machine(world: World) -> None:
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    second = run_setup(world, cluster, data_version="2026-11")
    assert SetupBuild.objects.get(hrid=second).version == 2
    run = queue_run(world)
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{second}/retire")
    assert retired.json()["retired_at"] is not None
    # The queued run already pinned the second build, even though it has no machine yet.
    provisioner.tick(cluster)
    launch = Launch.objects.get(setup=None)
    assert launch.volumes["af-db"]["hrid"] == second
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 2
    # Its snapshot stays until the run and the machine holding a copy have both ended.
    assert api_as(world, "runner").post(f"/runs/{run['id']}/cancel", {}).status_code == 200
    provisioner.tick(cluster)
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 2
    cluster.finish(launch.name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 1
    assert ManagedVolume.objects.get(build__hrid=second).state == VolumeState.DELETED
    # A new run gets the first version again.
    assert setups.current(ALPHAFOLD, "iki-runs").hrid == first  # type: ignore[union-attr]
    queue_run(world, "GHIK")
    provisioner.tick(cluster)
    next_launch = Launch.objects.get(setup=None, state=LaunchState.STARTING)
    assert next_launch.volumes["af-db"]["hrid"] == first


# --- When it doesn't work ------------------------------------------------------------------------


def test_only_a_setups_own_machine_reports_on_it(world: World) -> None:
    """Authorization fuzzing for setups' routes (test_worker_idor does jobs'): another
    worker, given the setup's lease token as if it leaked, is answered as for a setup
    that doesn't exist, and the setup is left as it was."""
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    machine = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    lease = {"lease_token": machine.lease().json()["job"]["lease_token"]}
    other = worker(world, name="pod-elsewhere")
    before = SetupBuild.objects.filter(pk=build["id"]).values().get()
    made_up = str(uuid7())
    for route, body in (
        ("heartbeat", {}),
        ("progress", {"fraction": 0.5}),
        ("logs", {"offset": 0, "text": "x"}),
        ("finish", {"outcome": "succeeded"}),
    ):
        tried = other.post(f"/setups/{build['id']}/{route}", {**lease, **body})
        nothing = other.post(f"/setups/{made_up}/{route}", {**lease, **body})
        assert 400 <= tried.status_code < 500, (route, tried.content)
        said = {**tried.json(), "request_id": None}
        expected = {
            **json.loads(nothing.content.decode().replace(made_up, build["id"])),
            "request_id": None,
        }
        assert (tried.status_code, said) == (nothing.status_code, expected), route
    assert SetupBuild.objects.filter(pk=build["id"]).values().get() == before
    assert not SetupLog.objects.filter(build_id=build["id"]).exists()


def test_a_failed_setup_ends_and_its_disk_is_deleted(world: World) -> None:
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    lease = {"lease_token": given["job"]["lease_token"]}
    ended = worker.post(
        f"/setups/{build['id']}/finish",
        {**lease, "outcome": "failed", "message": "The command exited 1."},
    )
    assert ended.json() == {"state": "failed"}
    provisioner.tick(cluster)
    assert not cluster.objects
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['hrid']}").json()
    assert shown["error"] == {"code": "failed", "message": "The command exited 1."}
    assert setups.current(ALPHAFOLD, "iki-runs") is None


def test_cancelling_a_running_setup_stops_its_script(world: World) -> None:
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    lease = {"lease_token": given["job"]["lease_token"]}
    api_as(world, "compute_admin").post(f"/compute/setups/{build['hrid']}/cancel")
    beat = worker.post(f"/setups/{build['id']}/heartbeat", lease).json()
    assert beat["cancel_requested"] is True
    # Whatever it says, it ends cancelled.
    worker.post(f"/setups/{build['id']}/finish", {**lease, "outcome": "succeeded"})
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.CANCELLED


def test_a_setup_whose_worker_went_away_fails(world: World) -> None:
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    worker.lease()
    SetupBuild.objects.filter(pk=build["id"]).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert setups.recover_expired() == 1
    ended = SetupBuild.objects.get(pk=build["id"])
    assert (ended.state, ended.error["code"]) == (SetupState.FAILED, "worker_lost")  # type: ignore[index]


def test_a_setup_reaches_only_the_hosts_its_environment_lists(world: World) -> None:
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    from urllib.parse import urlsplit

    from test_egress import authorization

    password = urlsplit(given["job"]["proxy"]).password
    assert password and password != given["job"]["lease_token"]
    header = authorization(given)
    allowed = egress.decide(header, "storage.googleapis.com", 443)
    assert allowed.allowed and allowed.setup
    egress.record(allowed.job_id, "storage.googleapis.com", 443, setup=True)  # type: ignore[arg-type]
    refused = egress.decide(header, "example.com", 443)
    assert refused.status == 403
    assert "setup doesn't declare example.com" in refused.refused
    hosts = sorted(SetupEgress.objects.filter(build_id=build["id"]).values_list("host", "refused"))
    assert hosts[0] == ("example.com", refused.refused)
    assert hosts[1] == ("storage.googleapis.com", "")
    shown = api_as(world, "compute_admin").get(f"/compute/setups/{build['hrid']}").json()
    assert [(e["host"], bool(e["refused"])) for e in shown["egress"]] == [
        ("example.com", True),
        ("storage.googleapis.com", False),
    ]


# --- On a shared file system (ADR 0077) ----------------------------------------------------------


def share(path: str | None = "reference") -> None:
    """Gives the template's volume from a shared file system, not snapshots."""
    file = Path(catalog._compute_file())  # type: ignore[arg-type]
    compute = json.loads(file.read_text())
    volume: dict[str, Any] = {"strategy": "shared_filesystem", "claim": "iki-shared"}
    if path is not None:
        volume["path"] = path
    compute["templates"]["gpu-a100"]["volumes"] = {"af-db": volume}
    file.write_text(json.dumps(compute))
    catalog.compute.cache_clear()


def fill_shared(world: World, cluster: FakeCluster, *, outcome: str = "succeeded") -> str:
    """Asks for a build, approves it, and runs its script. Returns the build's HRID."""
    build = ask(world)
    approve(world, build)
    provisioner.tick(cluster)
    name, _ = setup_pod(cluster)
    pod = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = pod.lease().json()
    pod.post(
        f"/setups/{given['setup']['id']}/finish",
        {"lease_token": given["job"]["lease_token"], "outcome": outcome},
    )
    cluster.finish(name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    hrid: str = build["hrid"]
    return hrid


def removals(cluster: FakeCluster) -> dict[str, dict[str, Any]]:
    return {name: job for (kind, _, name), job in cluster.objects.items() if kind == "job"}


def test_on_a_shared_file_system_a_build_fills_its_own_directory(world: World) -> None:
    share()
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, job = setup_pod(cluster)
    # No disk of its own: its version's own directory on the file system, writable, which
    # the worker's image makes first in the directory for Iki.
    assert not cluster.objects
    directory = f"{ALPHAFOLD}/af-db/{build['hrid']}"
    pod = job["spec"]["template"]["spec"]
    assert {"name": "fill-0", "persistentVolumeClaim": {"claimName": "iki-shared"}} in pod[
        "volumes"
    ]
    mounted = {"name": "fill-0", "mountPath": "/data/af", "subPath": f"reference/{directory}"}
    assert mounted in pod["containers"][0]["volumeMounts"]
    install, prepare = pod["initContainers"]
    assert prepare["image"] == install["image"] == WORKER_IMAGE
    assert prepare["securityContext"] == install["securityContext"]
    assert prepare["command"] == ["iki-worker", "prepare", "/iki/shared", directory]
    assert prepare["volumeMounts"] == [
        {"name": "fill-0", "mountPath": "/iki/shared", "subPath": "reference"}
    ]
    assert Launch.objects.get().volumes == {
        "af-db": {"claim": "iki-shared", "directory": directory}
    }
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    assert given["volumes"] == {"af-db": {"mount": "/data/af", "size": 3 * TI}}
    lease = {"lease_token": given["job"]["lease_token"]}
    done = worker.post(f"/setups/{build['id']}/finish", {**lease, "outcome": "succeeded"})
    assert done.json() == {"state": "snapshotting"}
    # It's ready once its pod has gone: nothing to snapshot.
    provisioner.tick(cluster)
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SNAPSHOTTING
    cluster.finish(name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SUCCEEDED
    volume = ManagedVolume.objects.get(build_id=build["id"])
    assert (volume.state, volume.handle) == (VolumeState.READY, directory)
    assert not cluster.objects


def test_runs_mount_their_versions_directory_read_only(world: World) -> None:
    share(path=None)
    cluster = FakeCluster()
    first = fill_shared(world, cluster)
    queue_run(world)
    provisioner.tick(cluster)
    (name,) = [n for (_, n) in cluster.made if n.startswith("iki-run-")]
    pod = cluster.made[("iki-runs", name)]["spec"]["template"]["spec"]
    assert {
        "name": "volume-0",
        "persistentVolumeClaim": {"claimName": "iki-shared", "readOnly": True},
    } in pod["volumes"]
    assert {
        "name": "volume-0",
        "mountPath": "/data/af",
        "subPath": f"iki/{ALPHAFOLD}/af-db/{first}",
        "readOnly": True,
    } in pod["containers"][0]["volumeMounts"]
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    assert worker.lease().json()["volumes"]["af-db"]["hrid"] == first


def test_a_retired_versions_directory_is_removed_once_unused(world: World) -> None:
    share()
    cluster = FakeCluster()
    first = fill_shared(world, cluster)
    second = fill_shared(world, cluster)
    run = queue_run(world)
    provisioner.tick(cluster)
    launch = Launch.objects.get(setup=None)
    assert launch.volumes["af-db"]["hrid"] == second
    api_as(world, "compute_admin").post(f"/compute/setups/{second}/retire")
    # A machine has it mounted, so it stays until that machine's gone.
    assert api_as(world, "runner").post(f"/runs/{run['id']}/cancel", {}).status_code == 200
    provisioner.tick(cluster)
    assert not removals(cluster)
    cluster.finish(launch.name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    ((_, job),) = removals(cluster).items()
    # The worker's image removes it, as runs' user, from the file system's directory for
    # Iki; the Job isn't a launch.
    assert "iki.ikisci.org/launch" not in job["metadata"]["labels"]
    assert job["metadata"]["labels"]["iki.ikisci.org/setup"] == str(
        SetupBuild.objects.get(hrid=second).pk
    )
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == WORKER_IMAGE
    assert container["command"] == [
        "iki-worker",
        "remove",
        "/iki/shared",
        f"{ALPHAFOLD}/af-db/{second}",
    ]
    assert container["volumeMounts"] == [
        {"name": "shared", "mountPath": "/iki/shared", "subPath": "reference"}
    ]
    assert pod["securityContext"]["runAsUser"] == 10001
    assert pod["volumes"] == [
        {"name": "shared", "persistentVolumeClaim": {"claimName": "iki-shared"}}
    ]
    # Not done yet: the version's still there.
    provisioner.tick(cluster)
    assert ManagedVolume.objects.get(build__hrid=second).deleted_at is None
    job["status"] = {"succeeded": 1}
    provisioner.tick(cluster)
    gone = ManagedVolume.objects.get(build__hrid=second)
    assert (gone.state, gone.deleted_at is not None) == (VolumeState.DELETED, True)
    assert not removals(cluster)
    # The first is kept: new machines get it again.
    assert ManagedVolume.objects.get(build__hrid=first).state == VolumeState.READY
    assert setups.current(ALPHAFOLD, "iki-runs").hrid == first  # type: ignore[union-attr]
    provisioner.tick(cluster)
    assert not removals(cluster)


def test_a_failed_builds_directory_is_removed_and_a_failed_removal_tried_again(
    world: World,
) -> None:
    share()
    cluster = FakeCluster()
    hrid = fill_shared(world, cluster, outcome="failed")
    assert SetupBuild.objects.get(hrid=hrid).state == SetupState.FAILED
    ((name, job),) = removals(cluster).items()
    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    provisioner.tick(cluster)
    assert "tries again" in ManagedVolume.objects.get(build__hrid=hrid).message
    # The failed Job stays, for its logs, until Kubernetes deletes it; then it's tried again.
    provisioner.tick(cluster)
    assert removals(cluster) == {name: job}
    cluster.delete("iki-runs", "job", name)
    provisioner.tick(cluster)
    ((again, job),) = removals(cluster).items()
    assert again == name
    job["status"] = {"succeeded": 1}
    provisioner.tick(cluster)
    gone = ManagedVolume.objects.get(build__hrid=hrid)
    assert (gone.state, gone.deleted_at is not None) == (VolumeState.FAILED, True)
    assert not removals(cluster)


def test_a_removal_whose_build_is_gone_is_deleted(world: World) -> None:
    share()
    cluster = FakeCluster()
    cluster.create(
        "iki-runs",
        "job",
        {"metadata": {"name": "iki-remove-x", "labels": {"iki.ikisci.org/setup": "nope"}}},
    )
    provisioner.tick(cluster)
    assert not removals(cluster)


# --- In a bucket (ADR 0078) ------------------------------------------------------------------


def in_a_bucket() -> None:
    """Gives the template's volume from a bucket, mounted with Mountpoint for Amazon S3."""
    file = Path(catalog._compute_file())  # type: ignore[arg-type]
    compute = json.loads(file.read_text())
    compute["templates"]["gpu-a100"]["volumes"] = {
        "af-db": {
            "strategy": "object_mount",
            "claim": "iki-objects",
            "path": "reference",
            "storage_class": "gp3",
        }
    }
    file.write_text(json.dumps(compute))
    catalog.compute.cache_clear()


def publishing(cluster: FakeCluster) -> dict[str, dict[str, Any]]:
    return {n: j for n, j in removals(cluster).items() if n.startswith("iki-publish-")}


def test_in_a_bucket_a_build_fills_a_disk_that_a_job_then_copies_there(world: World) -> None:
    in_a_bucket()
    build = ask(world)
    approve(world, build)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    name, job = setup_pod(cluster)
    # A bucket can't be written in place, so the script fills a disk of its own.
    ((kind, namespace, disk), claim), *rest = cluster.objects.items()
    assert (kind, namespace, rest) == ("pvc", "iki-runs", [])
    assert claim["spec"]["storageClassName"] == "gp3"
    assert claim["spec"]["resources"]["requests"]["storage"] == str(3 * TI)
    pod = job["spec"]["template"]["spec"]
    assert {"name": "fill-0", "persistentVolumeClaim": {"claimName": disk}} in pod["volumes"]
    assert {"name": "fill-0", "mountPath": "/data/af"} in pod["containers"][0]["volumeMounts"]
    assert len(pod["initContainers"]) == 1, "nothing to prepare"
    directory = f"{ALPHAFOLD}/af-db/{build['hrid']}"
    # Recorded before anything's copied, so a failed copy is removed.
    assert ManagedVolume.objects.get(build_id=build["id"]).handle == directory
    worker = WorkerClient(register(cluster.token(name), name=f"{name}-x1y2z").json()["token"])
    given = worker.lease().json()
    lease = {"lease_token": given["job"]["lease_token"]}
    worker.post(f"/setups/{build['id']}/finish", {**lease, "outcome": "succeeded"})
    cluster.finish(name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    # Once its pod has gone, a Job of the worker's image copies the disk into the version's
    # own directory in the bucket, as runs' user, with the disk read-only.
    ((publish, copy),) = publishing(cluster).items()
    assert "iki.ikisci.org/launch" not in copy["metadata"]["labels"]
    assert copy["metadata"]["labels"]["app.kubernetes.io/component"] == "volume-publish"
    spec = copy["spec"]["template"]["spec"]
    (container,) = spec["containers"]
    assert container["image"] == WORKER_IMAGE
    assert container["command"] == [
        "iki-worker",
        "publish",
        "/iki/staging",
        "/iki/shared",
        directory,
    ]
    assert container["volumeMounts"] == [
        {"name": "staging", "mountPath": "/iki/staging", "readOnly": True},
        {"name": "shared", "mountPath": "/iki/shared", "subPath": "reference"},
    ]
    assert spec["volumes"] == [
        {"name": "staging", "persistentVolumeClaim": {"claimName": disk, "readOnly": True}},
        {"name": "shared", "persistentVolumeClaim": {"claimName": "iki-objects"}},
    ]
    assert spec["securityContext"]["runAsUser"] == 10001
    assert copy["spec"]["activeDeadlineSeconds"] == 6 * 3600
    # A long copy is given its own time: the build isn't failed for its snapshots' while.
    SetupBuild.objects.filter(pk=build["id"]).update(started_at=timezone.now() - timedelta(days=2))
    provisioner.tick(cluster)
    waiting = SetupBuild.objects.get(pk=build["id"])
    assert waiting.state == SetupState.SNAPSHOTTING, waiting.error
    assert publishing(cluster) == {publish: copy}
    copy["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}
    provisioner.tick(cluster)
    assert SetupBuild.objects.get(pk=build["id"]).state == SetupState.SUCCEEDED
    volume = ManagedVolume.objects.get(build_id=build["id"])
    assert (volume.state, volume.handle) == (VolumeState.READY, directory)
    provisioner.tick(cluster)
    assert not cluster.objects, "its disk and Job are deleted"
    (listed,) = (
        api_as(world, "compute_admin").get(f"/compute/setups/{build['hrid']}").json()["volumes"]
    )
    assert (listed["kind"], listed["handle"]) == ("bucket", directory)
    # Runs mount the version's directory read-only, as on a shared file system.
    queue_run(world)
    provisioner.tick(cluster)
    (run,) = [n for (_, n) in cluster.made if n.startswith("iki-run-")]
    mounts = cluster.made[("iki-runs", run)]["spec"]["template"]["spec"]["containers"][0]
    assert {
        "name": "volume-0",
        "mountPath": "/data/af",
        "subPath": f"reference/{directory}",
        "readOnly": True,
    } in mounts["volumeMounts"]


def test_a_failed_copy_into_a_bucket_fails_the_build_and_is_removed(world: World) -> None:
    in_a_bucket()
    cluster = FakeCluster()
    hrid = fill_shared(world, cluster)
    ((publish, copy),) = publishing(cluster).items()
    # Removing waits for a copy that's still going.
    SetupBuild.objects.filter(hrid=hrid).update(state=SetupState.FAILED)
    provisioner.tick(cluster)
    assert set(removals(cluster)) == {publish}
    SetupBuild.objects.filter(hrid=hrid).update(state=SetupState.SNAPSHOTTING)
    copy["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    provisioner.tick(cluster)
    build = SetupBuild.objects.get(hrid=hrid)
    assert build.state == SetupState.FAILED
    assert build.error is not None
    assert build.error["code"] == "snapshot_failed"
    assert (
        f"couldn't copy af-db into its bucket. The log of the Job {publish}"
        in (build.error["message"])
    )
    assert ManagedVolume.objects.get(build=build).state == VolumeState.FAILED
    # What it copied is removed; its Job stays a while for its log; its disk goes.
    provisioner.tick(cluster)
    jobs = removals(cluster)
    assert publish in jobs
    (removal,) = [n for n in jobs if n.startswith("iki-remove-")]
    assert jobs[removal]["spec"]["template"]["spec"]["containers"][0]["command"][-1] == (
        f"{ALPHAFOLD}/af-db/{hrid}"
    )
    assert not [k for (k, _, _) in cluster.objects if k == "pvc"]


def test_runs_go_where_a_version_is_ready(world: World, settings: Any) -> None:
    """Bound to templates in two namespaces, a build in each: runs go to the first whose
    version is ready, rather than waiting for the other's."""
    settings.IKI_RUNS_NAMESPACES = ["iki-gpu"]
    file = Path(catalog._compute_file())  # type: ignore[arg-type]
    compute = json.loads(file.read_text())
    compute["templates"]["gpu-shared"] = {
        **compute["templates"]["gpu-a100"],
        "name": "gpu-shared",
        "kubernetes": {"namespace": "iki-gpu"},
        "volumes": {"af-db": {"strategy": "shared_filesystem", "claim": "iki-shared"}},
    }
    compute["bindings"][ALPHAFOLD]["templates"] = [["gpu-a100", 0], ["gpu-shared", 1]]
    file.write_text(json.dumps(compute))
    catalog.compute.cache_clear()
    made = api_as(world, "compute_admin").post(
        "/compute/setups", {"environment": ALPHAFOLD, "reason": "both"}
    )
    shared, snapshots = sorted(made.json(), key=lambda b: str(b["place"]))
    assert (shared["place"], snapshots["place"]) == ("iki-gpu", "iki-runs")
    approve(world, shared)
    approve(world, snapshots)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    for build in (snapshots, shared):
        (launch,) = Launch.objects.filter(setup_id=build["id"])
        pod = WorkerClient(
            register(
                cluster.token(launch.name, launch.namespace), name=f"{launch.name}-x1y2z"
            ).json()["token"]
        )
        given = pod.lease().json()
        pod.post(
            f"/setups/{build['id']}/finish",
            {"lease_token": given["job"]["lease_token"], "outcome": "succeeded"},
        )
        cluster.finish(launch.name, launch.namespace)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    # The shared version is ready; the snapshot isn't yet.
    assert SetupBuild.objects.get(pk=shared["id"]).state == SetupState.SUCCEEDED
    assert SetupBuild.objects.get(pk=snapshots["id"]).state == SetupState.SNAPSHOTTING
    queue_run(world)
    provisioner.tick(cluster)
    (run,) = Launch.objects.filter(setup=None)
    assert (run.namespace, run.template) == ("iki-gpu", "gpu-shared")
    assert run.volumes["af-db"]["hrid"] == shared["hrid"]
    # Once the first's is ready too, runs go there again.
    for (kind, _, _), item in cluster.objects.items():
        if kind == "snapshot":
            item["status"] = {"readyToUse": True}
    provisioner.tick(cluster)
    assert SetupBuild.objects.get(pk=snapshots["id"]).state == SetupState.SUCCEEDED
    queue_run(world, name="FGHI")
    provisioner.tick(cluster)
    assert Launch.objects.filter(setup=None, namespace="iki-runs", template="gpu-a100").exists()


def test_a_worker_can_only_lease_and_pack_runs_on_its_reference_build(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    predict = catalog.action(PREDICT)
    assert predict is not None
    monkeypatch.setitem(
        registry.plugins()[predict.plugin].actions,
        predict.name,
        dataclasses.replace(predict, batchable=2),
    )
    cluster = FakeCluster()
    run_setup(world, cluster)
    older = queue_run(world)
    second = run_setup(world, cluster)
    runner = api_as(world, "runner")
    entity = protein(runner, world.project, name="FGHI")
    batch = runner.post(
        f"/actions/{PREDICT}/runs/batch",
        {"project": str(world.project.pk), "each": "protein", "entities": [entity["id"]]},
    ).json()
    # Two batch members whose reference data became available at different times.
    ActionRun.objects.filter(pk=older["id"]).update(batch_id=batch["batch"]["id"])
    provisioner.tick(cluster)
    machine = Launch.objects.get(setup=None, volumes__contains={"af-db": {"hrid": second}})
    pod = WorkerClient(register(cluster.token(machine.name), name=machine.name).json()["token"])
    job = pod.lease().json()
    assert job["run"]["id"] == batch["runs"][0]["id"]
    assert [e["id"] for e in job["inputs"]["protein"]] == [entity["id"]]
    assert ActionRun.objects.get(pk=older["id"]).status == RunStatus.QUEUED


def test_exact_reproduction_mounts_the_original_build_until_retired(world: World) -> None:
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    run = queue_run(world)
    finish_prediction(cluster, run["hrid"], first)
    run_setup(world, cluster)

    runner = api_as(world, "runner")
    path = f"/runs/{run['hrid']}/reproduce"
    assert runner.get(f"/runs/{run['hrid']}").json()["can"]["reproduce"] is True
    reproduced = runner.post(path, {"reason": "Audit"})
    assert reproduced.status_code == 201, reproduced.content
    again = reproduced.json()
    finish_prediction(cluster, again["hrid"], first)
    assert runner.get(f"/runs/{again['hrid']}").json()["reproduction"]["matches"]

    retired = api_as(world, "compute_admin").post(f"/compute/setups/{first}/retire")
    assert retired.status_code == 200, retired.content
    assert runner.get(f"/runs/{run['hrid']}").json()["can"]["reproduce"] is False
    problem(runner.post(path, {"reason": "Audit"}), 409, "refused")


def test_waiting_for_reference_data_reuses_only_after_a_build_is_pinned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: "a" * 40)
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    run = queue_run(world)
    finish_prediction(cluster, run["hrid"], first)
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{first}/retire")
    assert retired.status_code == 200, retired.content

    runner = api_as(world, "runner")
    inputs = {"protein": run["inputs"][0]["entity"]}
    waiting = launch(runner, world.project, inputs, PREDICT).json()
    assert waiting["status"] == "queued"
    assert waiting["reused_from"] is None
    second = run_setup(world, cluster)
    finish_prediction(cluster, waiting["hrid"], second)
    same = launch(runner, world.project, inputs, PREDICT).json()
    assert same["reused_from"]["hrid"] == waiting["hrid"]
    assert same["attempts"] == 0


@pytest.mark.parametrize("warm_environment", [ALPHAFOLD, "acme/prediction.colabfold"])
def test_a_single_warm_slot_can_run_an_older_reference_build(
    world: World, monkeypatch: pytest.MonkeyPatch, warm_environment: str
) -> None:
    predict = catalog.action(PREDICT)
    assert predict is not None
    monkeypatch.setitem(
        registry.plugins()[predict.plugin].actions,
        predict.name,
        dataclasses.replace(predict, lifecycle=None),
    )
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    run = queue_run(world)
    finish_prediction(cluster, run["hrid"], first)
    second = run_setup(world, cluster)

    templates = catalog.compute().templates
    template = templates["gpu-a100"]
    monkeypatch.setitem(
        templates,
        template.name,
        dataclasses.replace(
            template,
            provisioner="k8s_deployment",
            lifecycle="persistent",
            min_warm=1,
            max_concurrent=1,
        ),
    )
    bindings = catalog.compute().bindings
    binding = bindings[ALPHAFOLD]
    if warm_environment != ALPHAFOLD:
        # The other environment has the slot already when this one joins the pool.
        monkeypatch.delitem(bindings, ALPHAFOLD)
        monkeypatch.setitem(
            bindings, warm_environment, dataclasses.replace(binding, environment=warm_environment)
        )
    provisioner.tick(cluster)
    warm, _ = register_run_pod(cluster)
    assert warm.environment == warm_environment
    if warm_environment == ALPHAFOLD:
        assert warm.volumes["af-db"]["hrid"] == second
    else:
        monkeypatch.setitem(bindings, ALPHAFOLD, binding)

    again = api_as(world, "runner").post(f"/runs/{run['hrid']}/reproduce", {"reason": "Audit"})
    assert again.status_code == 201, again.content
    provisioner.tick(cluster)
    warm.refresh_from_db()
    assert warm.state == LaunchState.ENDED
    replacement, pod = register_run_pod(cluster)
    assert replacement.volumes["af-db"]["hrid"] == first
    leased = pod.lease().json()
    assert leased["run"]["hrid"] == again.json()["hrid"]
    assert leased["volumes"]["af-db"]["hrid"] == first
    assert done(pod, leased, output(metrics={"score": 0.9}))["run_status"] == "succeeded"
