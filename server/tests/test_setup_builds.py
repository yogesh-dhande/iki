"""Environment setup builds and the volumes they fill (ADR 0075): a Compute admin asks for
one, another approves it, the provisioner starts a machine with a writable disk for each
volume, its worker runs the setup script, the disks are snapshotted, and machines for the
environment's runs get a read-only copy of the newest version, which each job records.
Kubernetes is a fake that keeps what it's asked for; the dev cluster's end-to-end checks
use a real one, with the CSI host-path driver's snapshots."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_container_tier import FakeCluster, register
from test_runs import TEMPLATE, protein
from test_workers import WorkerClient

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.runs import catalog, egress, provisioner, setups
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

ALPHAFOLD = "structure_prediction.alphafold-gpu"
PREDICT = "structure_prediction.predict_structure"
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
        registry.plugins()["structure_prediction"].actions,
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
    hrid: str = build["hrid"]
    assert SetupBuild.objects.get(hrid=hrid).state == SetupState.SUCCEEDED
    return hrid


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


# --- Running it --------------------------------------------------------------------------------


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


def test_a_retired_version_is_given_to_no_new_machine_and_deleted_once_unused(
    world: World,
) -> None:
    cluster = FakeCluster()
    first = run_setup(world, cluster)
    second = run_setup(world, cluster, data_version="2026-11")
    assert SetupBuild.objects.get(hrid=second).version == 2
    queue_run(world)
    provisioner.tick(cluster)
    launch = Launch.objects.get(setup=None)
    assert launch.volumes["af-db"]["hrid"] == second
    # Both snapshots are kept: the first isn't retired.
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 2
    retired = api_as(world, "compute_admin").post(f"/compute/setups/{second}/retire")
    assert retired.json()["retired_at"] is not None
    # A machine has a copy of it, so its snapshot stays until that machine's gone.
    provisioner.tick(cluster)
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 2
    cluster.finish(launch.name)
    provisioner.tick(cluster)
    provisioner.tick(cluster)
    assert len([k for k in cluster.objects if k[0] == "snapshot"]) == 1
    assert ManagedVolume.objects.get(build__hrid=second).state == VolumeState.DELETED
    # New machines get the first again.
    assert setups.current(ALPHAFOLD, "iki-runs").hrid == first  # type: ignore[union-attr]


# --- When it doesn't work ------------------------------------------------------------------------


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
    import base64

    credentials = base64.b64encode(f"{build['id']}:{given['job']['lease_token']}".encode())
    header = f"Basic {credentials.decode()}"
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
